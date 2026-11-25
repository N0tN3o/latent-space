import base64
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import cv2
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
for p in (str(PROJECT_ROOT), str(PROJECT_ROOT / "analysis_watcher")):
    if p not in sys.path:
        sys.path.insert(0, p)

import config_manager
from analysis_watcher import analysis, chat, translator


class TestGemma4AndThinkingSupport(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_root = Path(self._tmp.name)
        self._prev_env = {
            k: os.environ.get(k)
            for k in ("LATENT_SPACE_SHARED_DIR", "LATENT_SPACE_LOGS_DIR", "LATENT_SPACE_CONFIG_DIR")
        }
        os.environ["LATENT_SPACE_SHARED_DIR"] = str(self.tmp_root / "shared")
        os.environ["LATENT_SPACE_LOGS_DIR"] = str(self.tmp_root / "logs")
        os.environ["LATENT_SPACE_CONFIG_DIR"] = str(self.tmp_root / "config")
        config_manager.reload_paths_from_env()
        config_manager.ensure_directories()
        config_manager.save_settings(config_manager.DEFAULT_SETTINGS)
        with chat._OLLAMA_CAPS_LOCK:
            chat._OLLAMA_VISION_CAPS.clear()
            chat._OLLAMA_THINKING_CAPS.clear()

    def tearDown(self):
        for k, v in self._prev_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        config_manager.reload_paths_from_env()
        self._tmp.cleanup()

    def _make_canvas_image(self, width=1280, height=720, name="gaze.png") -> Path:
        p = self.tmp_root / name
        img = np.ones((height, width, 3), dtype=np.uint8) * 255
        cv2.circle(img, (width // 2, height // 2), min(width, height) // 4, (20, 20, 20), 6)
        cv2.imwrite(str(p), img)
        return p

    def test_gemma4_and_modern_vlm_vision_detection_aligned(self):
        for model in ("gemma4:e4b", "gemma4:e2b", "gemma4:26b", "gemma4:31b", "qwen3-vl:8b", "qwen3.5:9b"):
            self.assertTrue(analysis._is_vision_model_name(model), f"analysis missing vision hint for {model}")
            self.assertTrue(chat.LLMModel._openai_compatible_supports_vision(model), f"chat missing vision hint for {model}")
            with mock.patch("chat.requests.post", side_effect=OSError("offline")):
                self.assertTrue(
                    chat.LLMModel._ollama_supports_vision("http://localhost:11434/v1", model),
                    f"offline ollama vision hint missing for {model}",
                )

        self.assertFalse(analysis._is_vision_model_name("gemma3:1b"))
        self.assertFalse(analysis._is_vision_model_name("llama3.1:8b"))

        # Verify Admin UI counterpart includes gemma4
        admin_html = (PROJECT_ROOT / "web_ui" / "admin.html").read_text(encoding="utf-8")
        self.assertIn("'gemma4'", admin_html)

    def test_config_validation_for_thinking_and_vision_token_budgets(self):
        for budget in (70, 140, 280, 560, 1120):
            cfg = config_manager.update_settings_section("ai", {
                "vision": {
                    "vision_token_budget": budget,
                    "ollama_max_tokens": 1120,
                    "ollama_thinking": "low",
                },
                "analysis": {"thinking_mode": "low", "max_tokens": 768},
                "poem": {"thinking_mode": "off", "max_tokens": 0},
            })
            self.assertEqual(cfg["ai"]["vision"]["vision_token_budget"], budget)
            self.assertEqual(cfg["ai"]["vision"]["ollama_max_tokens"], 1120)
            self.assertEqual(cfg["ai"]["vision"]["ollama_thinking"], "low")
            self.assertEqual(cfg["ai"]["analysis"]["thinking_mode"], "low")
            self.assertEqual(cfg["ai"]["analysis"]["max_tokens"], 768)

        for bad in (
            {"vision": {"vision_token_budget": 100}},
            {"vision": {"vision_token_budget": True}},
            {"vision": {"ollama_max_tokens": 2049}},
            {"vision": {"ollama_thinking": "turbo"}},
            {"analysis": {"thinking_mode": "max"}},
            {"analysis": {"max_tokens": 32}},
            {"analysis": {"max_tokens": 5000}},
            {"poem": {"max_tokens": True}},
        ):
            with self.assertRaises(ValueError, msg=f"Expected ValueError for {bad}"):
                config_manager.validate_ai_data(bad)

        # Round-trip via LLMModel.update_assistant & get_config_dict
        m = chat.LLMModel("analysis")
        m.update_assistant(thinking_mode="medium", max_tokens=1120)
        d = m.get_config_dict()
        self.assertEqual(d["thinking_mode"], "medium")
        self.assertEqual(d["max_tokens"], 1120)

    def test_vision_token_budget_downscaling(self):
        img_path = self._make_canvas_image(1280, 720)

        def decode_shape(b64_str):
            raw = base64.b64decode(b64_str)
            arr = np.frombuffer(raw, dtype=np.uint8)
            im = cv2.imdecode(arr, cv2.IMREAD_UNCHANGED)
            return im.shape[1], im.shape[0]  # (w, h)

        w70, h70 = decode_shape(analysis._encode_image_for_vision(str(img_path), token_budget=70))
        self.assertLessEqual(w70 * h70, 70 * 48 * 48 * 1.05)
        self.assertAlmostEqual(w70 / h70, 1280 / 720, delta=0.05)

        w280, h280 = decode_shape(analysis._encode_image_for_vision(str(img_path), token_budget=280))
        self.assertLessEqual(w280 * h280, 280 * 48 * 48 * 1.05)
        self.assertGreater(w280, w70)

        # 560 tokens (~1.29M px) preserves 1280x720 (921.6K px) without upscaling
        w560, h560 = decode_shape(analysis._encode_image_for_vision(str(img_path), token_budget=560))
        self.assertEqual((w560, h560), (1280, 720))

    def test_ollama_vision_gemma4_thinking_controls_and_fallbacks(self):
        img_path = self._make_canvas_image(640, 480)

        # 1. Auto mode on gemma4:e4b sends think=False for crisp fast vision
        config_manager.update_settings_section("ai", {
            "vision_mode": "ollama_vision",
            "ollama_vision_model": "gemma4:e4b",
            "vision": {
                "ollama_thinking": "auto",
                "ollama_max_tokens": 280,
                "ollama_include_description": False,
            },
        })
        ok_resp = mock.MagicMock(status_code=200, ok=True)
        ok_resp.json.return_value = {"response": "Coiled Serpent"}
        with mock.patch("analysis.requests.post", return_value=ok_resp) as post_mock:
            res = analysis.run_ollama_vision_analysis(str(img_path))
        self.assertEqual(res["summary_label"], "Coiled Serpent")
        sent_payload = post_mock.call_args.kwargs["json"]
        self.assertIs(sent_payload["think"], False)
        self.assertEqual(sent_payload["options"]["num_predict"], 64)

        # 2. Low thinking mode lifts Pass 1 token clamp (>=280) and recovers if thinking exhausts budget
        config_manager.update_settings_section("ai", {
            "vision_mode": "ollama_vision",
            "ollama_vision_model": "gemma4:e4b",
            "vision": {
                "ollama_thinking": "low",
                "ollama_max_tokens": 140,
                "ollama_include_description": False,
            },
        })
        exhausted_resp = mock.MagicMock(status_code=200, ok=True)
        exhausted_resp.json.return_value = {"thinking": "Analyzing abstract curves...", "response": ""}
        direct_retry_resp = mock.MagicMock(status_code=200, ok=True)
        direct_retry_resp.json.return_value = {"response": "Phoenix"}
        with mock.patch("analysis.requests.post", side_effect=[exhausted_resp, direct_retry_resp]) as post_mock:
            res2 = analysis.run_ollama_vision_analysis(str(img_path))
        self.assertEqual(res2["summary_label"], "Phoenix")
        self.assertEqual(post_mock.call_count, 2)
        first_p = post_mock.call_args_list[0].kwargs["json"]
        second_p = post_mock.call_args_list[1].kwargs["json"]
        # Both share the mutated payload dict reference on retry, so check options & final think=False
        self.assertEqual(first_p["options"]["num_predict"], 280)
        self.assertIs(second_p["think"], False)

        # 3. 400 fallback when a model rejects string think level -> retries True -> strips think
        resp_400 = mock.MagicMock(status_code=400, ok=False)
        resp_ok_bool = mock.MagicMock(status_code=200, ok=True)
        resp_ok_bool.json.return_value = {"response": "Owl"}
        with mock.patch("analysis.requests.post", side_effect=[resp_400, resp_ok_bool]) as post_mock:
            res3 = analysis.run_ollama_vision_analysis(str(img_path))
        self.assertEqual(res3["summary_label"], "Owl")
        self.assertEqual(post_mock.call_count, 2)

        # 4. Non-thinking model (moondream) NEVER receives 'think' in auto mode
        config_manager.update_settings_section("ai", {
            "vision_mode": "ollama_vision",
            "ollama_vision_model": "moondream",
            "vision": {"ollama_thinking": "auto", "ollama_include_description": False},
        })
        with mock.patch("analysis.requests.post", return_value=ok_resp) as post_mock:
            analysis.run_ollama_vision_analysis(str(img_path))
        self.assertNotIn("think", post_mock.call_args.kwargs["json"])

    def test_chat_gemma4_thinking_reasoning_effort_fallbacks_and_translator(self):
        img_path = self._make_canvas_image(640, 480)
        config_manager.update_settings_section("ai", {
            "vision_mode": "auto",
            "analysis": {
                "provider": "ollama",
                "model": "gemma4:e4b",
                "base_url": "http://localhost:11434/v1",
                "thinking_mode": "auto",
                "max_tokens": 560,
                "fallback_provider": "none",
            },
        })

        # /api/show returns both vision and thinking capabilities
        show_resp = mock.MagicMock(status_code=200, ok=True)
        show_resp.json.return_value = {"capabilities": ["completion", "vision", "thinking"]}

        # Chat returns empty content with reasoning on call 1 -> automatically retries with reasoning_effort="none"
        empty_thinking_resp = mock.MagicMock(status_code=200, ok=True)
        empty_thinking_resp.json.return_value = {
            "choices": [{"message": {"content": "", "reasoning": "Thinking too long..."}}]
        }
        good_resp = mock.MagicMock(status_code=200, ok=True)
        good_resp.json.return_value = {
            "choices": [{"message": {"content": "<think>brief</think>Your gaze opens like quiet water."}}]
        }

        captured_payloads = []

        def fake_post(url, json=None, headers=None, timeout=None):
            captured_payloads.append((url, dict(json or {})))
            if url.endswith("/api/show"):
                return show_resp
            if len(captured_payloads) == 2:
                return empty_thinking_resp
            return good_resp

        with mock.patch("chat.requests.post", side_effect=fake_post):
            m = chat.LLMModel("analysis")
            reply = m.chat("Interpret this drawing", image_path=str(img_path), allow_failover=False)

        self.assertEqual(reply, "Your gaze opens like quiet water.")
        # First chat attempt sent image + reasoning_effort="low" + max_tokens=560
        self.assertEqual(captured_payloads[1][1]["reasoning_effort"], "low")
        self.assertEqual(captured_payloads[1][1]["max_tokens"], 560)
        self.assertIsInstance(captured_payloads[1][1]["messages"][-1]["content"], list)
        # Retry switched to reasoning_effort="none"
        self.assertEqual(captured_payloads[2][1]["reasoning_effort"], "none")

        # 400 on reasoning_effort strips reasoning_effort while PRESERVING image payload
        captured_payloads.clear()
        resp_400 = mock.MagicMock(status_code=400, ok=False)

        def fake_post_400(url, json=None, headers=None, timeout=None):
            captured_payloads.append((url, dict(json or {})))
            if len(captured_payloads) == 1:
                return resp_400
            return good_resp

        with mock.patch("chat.requests.post", side_effect=fake_post_400):
            reply2 = m.chat("Interpret this drawing", image_path=str(img_path), allow_failover=False)
        self.assertEqual(reply2, "Your gaze opens like quiet water.")
        self.assertNotIn("reasoning_effort", captured_payloads[1][1])
        self.assertIsInstance(captured_payloads[1][1]["messages"][-1]["content"], list)

        # Translator disables thinking for Ollama gemma4:e4b, omits for translategemma:4b
        tr_payloads = []

        def fake_tr_post(url, json=None, headers=None, timeout=None):
            tr_payloads.append(dict(json or {}))
            r = mock.MagicMock(status_code=200, ok=True)
            r.json.return_value = {"choices": [{"message": {"content": "Magyar szöveg."}}]}
            return r

        cfg_g4 = config_manager.update_settings_section("translation", {
            "enabled": True, "mode": "ollama", "ollama_model": "gemma4:e4b",
        })
        with mock.patch.object(translator.requests, "post", side_effect=fake_tr_post):
            translator.translate_reading("Labyrinth", "Text.", "Line 1\nLine 2", target_lang="hu", settings=cfg_g4)
        self.assertEqual(tr_payloads[0].get("reasoning_effort"), "none")

        tr_payloads.clear()
        cfg_tg = config_manager.update_settings_section("translation", {
            "enabled": True, "mode": "ollama", "ollama_model": "translategemma:4b",
        })
        with mock.patch.object(translator.requests, "post", side_effect=fake_tr_post):
            translator.translate_reading("Labyrinth", "Text.", "Line 1\nLine 2", target_lang="hu", settings=cfg_tg)
        self.assertNotIn("reasoning_effort", tr_payloads[0])


if __name__ == "__main__":
    unittest.main()
