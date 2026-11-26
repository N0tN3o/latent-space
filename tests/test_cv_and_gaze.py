from collections import deque
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

import cv2
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ANALYSIS_DIR = PROJECT_ROOT / "analysis_watcher"
GAZE_DIR = PROJECT_ROOT / "gaze_painter"
for p in (str(PROJECT_ROOT), str(ANALYSIS_DIR), str(GAZE_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import config_manager
import analysis
import chat
import heatmap
from preprocess_image import preprocess_image
import eye_tracking
from drawing import GazePainter

TEST_TMP = None


def setUpModule():
    global TEST_TMP
    TEST_TMP = Path(tempfile.mkdtemp(prefix="ls_cv_gaze_test_"))
    os.environ["LATENT_SPACE_CONFIG_DIR"] = str(TEST_TMP / "config")
    os.environ["LATENT_SPACE_SHARED_DIR"] = str(TEST_TMP / "shared_output")
    os.environ["LATENT_SPACE_LOGS_DIR"] = str(TEST_TMP / "logs")
    config_manager.reload_paths_from_env()
    config_manager.ensure_directories()


def tearDownModule():
    global TEST_TMP
    if TEST_TMP is not None:
        shutil.rmtree(TEST_TMP, ignore_errors=True)
        TEST_TMP = None


class TestCVAndGeometry(unittest.TestCase):
    def setUp(self):
        config_manager.ensure_directories()
        config_manager.save_settings(config_manager.DEFAULT_SETTINGS)

    def test_uncropped_canvas_and_heatmap_agree_on_corner_drawing(self):
        canvas_path = TEST_TMP / "corner_canvas.png"
        proc_path = TEST_TMP / "corner_proc.png"
        hm_path = TEST_TMP / "corner_hm.png"

        canvas = np.ones((720, 1280), dtype=np.uint8) * 255
        cv2.circle(canvas, (140, 110), 45, 0, 6)
        cv2.line(canvas, (90, 90), (200, 140), 0, 6)
        cv2.imwrite(str(canvas_path), canvas)

        preprocess_image(str(canvas_path), str(proc_path))
        geom = analysis.analyze_stroke_geometry(str(proc_path), canvas_filename=str(canvas_path))
        zones = heatmap.compute_zone_matrix(str(canvas_path))
        heatmap.draw_heatmap(str(canvas_path), str(hm_path))

        self.assertEqual(geom["primary_zone"], "upper-left")
        self.assertEqual(tuple(np.unravel_index(np.argmax(zones), zones.shape)), (0, 0))
        self.assertTrue(hm_path.exists())

    def test_dwell_grid_overrides_zone_metrics_consistently(self):
        proc_path = TEST_TMP / "dwell_proc.png"
        hm_path = TEST_TMP / "dwell_hm.png"
        img = np.ones((512, 512), dtype=np.uint8) * 255
        cv2.line(img, (50, 400), (200, 450), 0, 5)
        cv2.imwrite(str(proc_path), img)

        dwell = [
            [0.0, 0.0, 9.5],
            [0.0, 0.0, 0.0],
            [0.0, 0.0, 0.0],
        ]
        geom = analysis.analyze_stroke_geometry(str(proc_path), dwell_grid=dwell)
        heatmap.draw_heatmap(str(proc_path), str(hm_path), dwell_grid=dwell)

        self.assertEqual(geom["primary_zone"], "upper-right")
        self.assertEqual(geom["spatial_entropy"], 0.0)
        self.assertTrue(hm_path.exists())

    def test_blank_heatmap_is_deterministic_zeros_not_random(self):
        z1 = heatmap.compute_zone_matrix(str(TEST_TMP / "nonexistent.png"))
        z2 = heatmap.compute_zone_matrix(str(TEST_TMP / "nonexistent.png"))
        self.assertTrue(np.array_equal(z1, np.zeros((3, 3), dtype=np.float32)))
        self.assertTrue(np.array_equal(z1, z2))

    def test_sparse_asymmetric_not_labeled_symmetric_rorschach(self):
        asym_path = TEST_TMP / "asym.png"
        img = np.ones((512, 512), dtype=np.uint8) * 255
        cv2.line(img, (40, 40), (160, 120), 0, 3)
        cv2.imwrite(str(asym_path), img)

        geom = analysis.analyze_stroke_geometry(str(asym_path))
        self.assertLess(geom["horiz_symmetry"], 0.2)
        self.assertLess(geom["vert_symmetry"], 0.2)
        self.assertIn("asymmetrical", geom["description"])

        sym_path = TEST_TMP / "sym.png"
        sym_img = np.ones((512, 512), dtype=np.uint8) * 255
        cv2.line(sym_img, (100, 100), (412, 412), 0, 4)
        cv2.line(sym_img, (412, 100), (100, 412), 0, 4)
        cv2.circle(sym_img, (256, 256), 90, 0, 4)
        cv2.imwrite(str(sym_path), sym_img)

        sym_geom = analysis.analyze_stroke_geometry(str(sym_path))
        self.assertGreater(sym_geom["horiz_symmetry"], 0.7)
        self.assertGreater(sym_geom["vert_symmetry"], 0.7)
        self.assertIn("strikingly symmetrical", sym_geom["description"])

    def test_smooth_circle_vs_sharp_zigzag_angularity(self):
        circle_path = TEST_TMP / "circle.png"
        c_img = np.ones((512, 512), dtype=np.uint8) * 255
        cv2.circle(c_img, (256, 256), 170, 0, 5)
        cv2.imwrite(str(circle_path), c_img)
        c_geom = analysis.analyze_stroke_geometry(str(circle_path))
        self.assertIn("fluid, sweeping, and organic", c_geom["description"])

        zig_path = TEST_TMP / "zigzag.png"
        z_img = np.ones((512, 512), dtype=np.uint8) * 255
        pts = np.array([[40, 260], [100, 90], [160, 390], [220, 90], [280, 390], [340, 90], [400, 390], [470, 180]], np.int32)
        cv2.polylines(z_img, [pts], False, 0, 4)
        cv2.imwrite(str(zig_path), z_img)
        z_geom = analysis.analyze_stroke_geometry(str(zig_path))
        self.assertIn("angular, with abrupt changes of direction", z_geom["description"])

        # Elongated falling diagonal with small loops is classified as Descending Sweep (not generic Electric Drift)
        diag_path = TEST_TMP / "diag_sweep.png"
        d_img = np.ones((720, 1280), dtype=np.uint8) * 255
        cv2.line(d_img, (120, 120), (1120, 580), 0, 4)
        for k in range(7):
            cx = 220 + k * 120
            cy = int(120 + (cx - 120) * (460 / 1000.0))
            cv2.circle(d_img, (cx, cy), 18, 0, 3)
        cv2.imwrite(str(diag_path), d_img)
        d_geom = analysis.analyze_stroke_geometry(str(diag_path))
        self.assertEqual(d_geom["summary_label"], "Descending Sweep")
        self.assertEqual(d_geom["direction"], "falling_diagonal")
        self.assertNotIn("dense abstract expressionist webs", d_geom["description"])

    def test_luminance_235_dark_canvas_color_and_nested_bullseye(self):
        # 1. Luminance 235 strokes must agree across preprocess, analysis, and heatmap
        faint_path = TEST_TMP / "faint.png"
        faint_proc = TEST_TMP / "faint_proc.png"
        faint = np.ones((256, 256), dtype=np.uint8) * 255
        cv2.line(faint, (30, 30), (200, 200), 235, 6)
        cv2.imwrite(str(faint_path), faint)
        preprocess_image(str(faint_path), str(faint_proc))
        fg = analysis.analyze_stroke_geometry(str(faint_proc))
        fh = heatmap.compute_zone_matrix(str(faint_path))
        self.assertGreater(fg["ink_ratio"], 0.01)
        self.assertGreater(fg["fractal_dimension"], 1.0)
        self.assertGreater(float(fh.max()), 0.0)

        # 2. Color shading (Ember/Indigo) preserved in image_processed.png
        ember_path = TEST_TMP / "ember.png"
        ember_proc = TEST_TMP / "ember_proc.png"
        ember = np.ones((256, 256, 3), dtype=np.uint8) * 255
        cv2.line(ember, (40, 40), (200, 200), (10, 40, 210), 6, cv2.LINE_AA)
        cv2.imwrite(str(ember_path), ember)
        preprocess_image(str(ember_path), str(ember_proc))
        out_ember = cv2.imread(str(ember_proc), cv2.IMREAD_COLOR)
        self.assertEqual(out_ember.ndim, 3)
        # Red channel > Blue channel in Ember stroke
        self.assertGreater(int(np.max(out_ember[:, :, 2] - out_ember[:, :, 0])), 50)

        # 3. Dark canvas with bright strokes must not invert background into ink
        dark_path = TEST_TMP / "dark.png"
        dark_proc = TEST_TMP / "dark_proc.png"
        dark = np.zeros((300, 400), dtype=np.uint8)
        cv2.circle(dark, (100, 100), 40, 180, 5)
        cv2.imwrite(str(dark_path), dark)
        preprocess_image(str(dark_path), str(dark_proc))
        dg = analysis.analyze_stroke_geometry(str(dark_proc))
        self.assertLess(dg["ink_ratio"], 0.35)
        self.assertGreater(dg["ink_ratio"], 0.02)

        # 4. Open smooth semicircle must be fluid, not angular from ribbon U-turns
        arc_path = TEST_TMP / "arc.png"
        arc = np.ones((512, 512), dtype=np.uint8) * 255
        cv2.ellipse(arc, (256, 256), (160, 160), 0, 0, 180, 0, 5)
        cv2.imwrite(str(arc_path), arc)
        ag = analysis.analyze_stroke_geometry(str(arc_path))
        self.assertIn("fluid, sweeping, and organic", ag["description"])

        # 5. Bullseye (outer ring + solid inner dot at level 2) has 1 enclosed loop, not 2
        eye_path = TEST_TMP / "eye.png"
        eye = np.ones((512, 512), dtype=np.uint8) * 255
        cv2.circle(eye, (256, 256), 140, 0, 6)
        cv2.circle(eye, (256, 256), 30, 0, -1)
        cv2.imwrite(str(eye_path), eye)
        eg = analysis.analyze_stroke_geometry(str(eye_path))
        self.assertEqual(eg["enclosed_loops"], 1)

        # 6. Box-counting on non-power-of-two canvas includes bottom/right edge ink
        edge_mask = np.zeros((135, 135), dtype=np.uint8)
        edge_mask[130:135, 130:135] = 255
        self.assertGreaterEqual(analysis.compute_fractal_dimension(edge_mask), 1.0)

        # 7. Dense central drawing covering >50% of white canvas stays light-polarity via border median
        dense_path = TEST_TMP / "dense.png"
        dense_proc = TEST_TMP / "dense_proc.png"
        dense = np.ones((200, 200), dtype=np.uint8) * 255
        dense[20:180, 20:180] = 0
        cv2.imwrite(str(dense_path), dense)
        preprocess_image(str(dense_path), str(dense_proc))
        dense_mask, is_dark, _ = preprocess_image.__globals__["extract_ink_mask"](dense)
        self.assertFalse(is_dark)
        self.assertEqual(int(dense_mask[10, 10]), 0)
        self.assertEqual(int(dense_mask[100, 100]), 255)

    def test_chat_skips_image_payload_for_text_only_ollama_in_auto_mode(self):
        with chat._OLLAMA_CAPS_LOCK:
            chat._OLLAMA_VISION_CAPS.clear()
        config_manager.update_settings_section("ai", {
            "vision_mode": "auto",
            "analysis": {
                "provider": "ollama",
                "model": "llama3.1:8b",
                "base_url": "http://localhost:11434/v1",
            },
        })
        img_path = TEST_TMP / "tiny.png"
        img_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)

        show_resp = mock.MagicMock(ok=True)
        show_resp.json.return_value = {"capabilities": ["completion"]}
        chat_resp = mock.MagicMock(status_code=200, ok=True)
        chat_resp.json.return_value = {"choices": [{"message": {"content": "Insightful text."}}]}

        with mock.patch("chat.requests.post", side_effect=[show_resp, chat_resp]) as post_mock:
            m = chat.LLMModel("analysis")
            reply = m.chat("Analyze this", image_path=str(img_path), allow_failover=False)

        self.assertEqual(reply, "Insightful text.")
        self.assertEqual(post_mock.call_count, 2)
        chat_call_json = post_mock.call_args_list[1].kwargs["json"]
        self.assertIsInstance(chat_call_json["messages"][-1]["content"], str)

    def test_chat_key_matching_keyless_local_and_null_content(self):
        # 1. OPENAI_API_KEY must NOT be sent to Groq base_url
        config_manager.update_settings_section("ai", {
            "analysis": {
                "provider": "openai_compatible",
                "model": "llama-3.3-70b-versatile",
                "base_url": "https://api.groq.com/openai/v1",
                "api_key": "",
                "fallback_provider": "auto",
            },
            "poem": {
                "provider": "openai_compatible",
                "model": "local-qwen",
                "base_url": "http://localhost:1234/v1",
                "api_key": "",
            },
        })
        env = {"OPENAI_API_KEY": "sk-openai-only"}
        with mock.patch("chat.load_env_vars", return_value=env):
            m = chat.LLMModel("analysis")
            self.assertEqual(m._api_key, "")
            # Keyless local openai_compatible (LM Studio/vLLM) must be included in failover
            candidates = m._build_failover_candidates()
            local_cands = [c for c in candidates if c[2] == "http://localhost:1234/v1"]
            self.assertTrue(local_cands)

        # 2. 401 must NOT retry text-only, and gemma3:1b is NOT vision
        self.assertFalse(chat.LLMModel._ollama_supports_vision("http://localhost:11434/v1", "gemma3:1b"))

        config_manager.update_settings_section("ai", {
            "vision_mode": "vision",
            "analysis": {
                "provider": "openai_compatible",
                "model": "gpt-4o-mini",
                "base_url": "https://api.openai.com/v1",
                "api_key": "bad-key",
            },
        })
        img_path = TEST_TMP / "tiny2.png"
        img_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
        resp_401 = mock.MagicMock(status_code=401, ok=False)
        resp_401.raise_for_status.side_effect = chat.requests.exceptions.HTTPError("401", response=resp_401)
        with mock.patch("chat.requests.post", return_value=resp_401) as post_mock:
            m = chat.LLMModel("analysis")
            with self.assertRaises(chat.AIGenerationError):
                m.chat("Hi", image_path=str(img_path), allow_failover=False)
        self.assertEqual(post_mock.call_count, 1)

        # 3. Null content from OpenAI-compatible reasoning/tool message raises cleanly (not AttributeError)
        resp_null = mock.MagicMock(status_code=200, ok=True)
        resp_null.json.return_value = {"choices": [{"message": {"content": None}}]}
        with mock.patch("chat.requests.post", return_value=resp_null):
            with self.assertRaises(chat.AIGenerationError) as ctx:
                m.chat("Hi", allow_failover=False)
            self.assertNotIn("AttributeError", str(ctx.exception))

    def test_gemini_header_auth_secret_redaction_and_cross_role_key(self):
        # 1. Spoofed host substring must not match cloud vendor
        self.assertEqual(chat._is_cloud_openai_compatible_host("https://evilopenai.com/v1"), "")
        self.assertEqual(chat._is_cloud_openai_compatible_host("https://api.openai.com/v1"), "openai")

        # 2. Gemini sends key in x-goog-api-key header (not URL) and redacts secrets from errors
        secret_key = "AIzaSySuperSecretUnitTestKey999"
        config_manager.update_settings_section("ai", {
            "analysis": {
                "provider": "gemini",
                "model": "gemini-2.0-flash",
                "api_key": secret_key,
                "fallback_provider": "none",
            },
            "poem": {
                "provider": "gemini",
                "model": "gemini-2.0-flash",
                "api_key": "",
                "fallback_provider": "none",
            },
        })
        # Cross-role key sharing for Gemini when poem key is empty
        with mock.patch("chat.load_env_vars", return_value={}):
            poem_m = chat.LLMModel("poem")
            self.assertEqual(poem_m._api_key, secret_key)

        err_resp = mock.MagicMock(status_code=403, ok=False)
        err_resp.raise_for_status.side_effect = chat.requests.exceptions.HTTPError(
            f"403 Forbidden for url: https://example.test/?key={secret_key}",
            response=err_resp,
        )
        with mock.patch("chat.requests.post", return_value=err_resp) as post_mock:
            m = chat.LLMModel("analysis")
            with self.assertRaises(chat.AIGenerationError) as exc_ctx:
                m.chat("Hello", allow_failover=False)
        call_url = post_mock.call_args.args[0]
        call_headers = post_mock.call_args.kwargs["headers"]
        self.assertNotIn(secret_key, call_url)
        self.assertEqual(call_headers.get("x-goog-api-key"), secret_key)
        self.assertNotIn(secret_key, str(exc_ctx.exception))

        # 3. Explicit openai_compatible fallback with gpt-* model picks OpenAI over Groq
        config_manager.update_settings_section("ai", {
            "analysis": {
                "provider": "anthropic",
                "model": "claude-3-5-haiku-latest",
                "fallback_provider": "openai_compatible",
                "fallback_model": "gpt-4o",
            },
            "poem": {"provider": "ollama", "model": "llama3.2"},
        })
        env = {"GROQ_API_KEY": "gsk-123", "OPENAI_API_KEY": "sk-oai-456"}
        with mock.patch("chat.load_env_vars", return_value=env):
            m2 = chat.LLMModel("analysis")
            cands = m2._build_failover_candidates()
        self.assertEqual(cands[0][0], "openai_compatible")
        self.assertEqual(cands[0][1], "gpt-4o")
        self.assertEqual(cands[0][2], "https://api.openai.com/v1")

    def test_empty_model_cross_provider_failover_and_native_vision_detection(self):
        # 1. Failover to Gemini when poem has model="" must NOT send Ollama's llama3.2 to Gemini
        config_manager.update_settings_section("ai", {
            "analysis": {
                "provider": "ollama",
                "model": "llama3.2",
                "base_url": "http://localhost:11434/v1",
                "fallback_provider": "auto",
            },
            "poem": {
                "provider": "gemini",
                "model": "",
                "api_key": "gem-shared-key",
            },
        })
        resp_gemini = mock.MagicMock(status_code=200, ok=True)
        resp_gemini.json.return_value = {"candidates": [{"content": {"parts": [{"text": "Gemini fallback"}]}}]}

        with mock.patch("chat.load_env_vars", return_value={}), \
             mock.patch("chat.requests.post", side_effect=[
                 chat.requests.exceptions.ConnectionError("ollama down"),
                 resp_gemini,
             ]) as post_mock:
            m = chat.LLMModel("analysis")
            reply = m.chat("Hello", allow_failover=True)

        self.assertEqual(reply, "Gemini fallback")
        self.assertIn("models/gemini-2.0-flash:generateContent", post_mock.call_args_list[1].args[0])
        self.assertNotIn("llama3.2", post_mock.call_args_list[1].args[0])
        self.assertEqual(m._last_used_model, "gemini-2.0-flash")

        # 2. Blank base_url must not steal a remote role's OpenAI-compatible API key
        config_manager.update_settings_section("ai", {
            "analysis": {"provider": "openai_compatible", "model": "local", "base_url": "", "api_key": ""},
            "poem": {"provider": "openai_compatible", "model": "gpt-4o", "base_url": "https://api.openai.com/v1", "api_key": "sk-poem"},
        })
        with mock.patch("chat.load_env_vars", return_value={}):
            m_blank = chat.LLMModel("analysis")
            self.assertEqual(m_blank._api_key, "")

        # 3. _analysis_llm_handles_vision_natively detects gpt-4o vs text-only Groq
        config_manager.update_settings_section("ai", {
            "analysis": {"provider": "openai_compatible", "model": "gpt-4o", "base_url": "https://api.openai.com/v1"}
        })
        self.assertTrue(analysis._analysis_llm_handles_vision_natively())
        config_manager.update_settings_section("ai", {
            "analysis": {"provider": "openai_compatible", "model": "llama-3.3-70b-versatile", "base_url": "https://api.groq.com/openai/v1"}
        })
        self.assertFalse(analysis._analysis_llm_handles_vision_natively())

    def test_vision_label_cleaning_and_ollama_vision_options(self):
        # 1. Conversational & numbered VLM replies are filtered into crisp gallery badge titles
        self.assertEqual(analysis.clean_vision_archetype_label("1) The lines resemble a snake."), "Snake")
        self.assertEqual(analysis.clean_vision_archetype_label("Title: Coiled Serpent\nIt winds sharply."), "Coiled Serpent")
        self.assertEqual(analysis.clean_vision_archetype_label("This abstract drawing looks like a bird in flight."), "Bird in Flight")
        self.assertEqual(analysis.clean_vision_archetype_label("A black and white line drawing of a tree."), "Tree")
        self.assertEqual(analysis.clean_vision_archetype_label("An abstract line drawing on a white background.", fallback="Electric Drift"), "Electric Drift")
        # Degenerate VLM tokens ("ike", URLs, underscores, mixed alnum gibberish, unapproved 3-letter noise) fall back
        self.assertEqual(analysis.clean_vision_archetype_label("ike", fallback="Woven Loops"), "Woven Loops")
        self.assertEqual(analysis.clean_vision_archetype_label("xqz", fallback="Woven Loops"), "Woven Loops")
        self.assertEqual(analysis.clean_vision_archetype_label("owl", fallback="Woven Loops"), "Owl")
        self.assertEqual(analysis.clean_vision_archetype_label("visit www.example.com/art_12", fallback="Woven Loops"), "Woven Loops")
        self.assertTrue(analysis.is_degenerate_vision_text("line_1__2__3 https://bad.example.org"))
        self.assertTrue(analysis.is_degenerate_vision_text("ab12cd34 token"))
        self.assertFalse(analysis.is_degenerate_vision_text("A sweeping curve crosses the upper left corner."))

        # 2. run_ollama_vision_analysis honors configured prompts, temperature, max_tokens, and badge_style
        img_path = TEST_TMP / "moondream_sample.png"
        canvas = np.ones((256, 256), dtype=np.uint8) * 255
        cv2.ellipse(canvas, (128, 128), (80, 40), 20, 0, 360, 0, 4)
        cv2.line(canvas, (30, 180), (220, 70), 0, 4)
        cv2.imwrite(str(img_path), canvas)

        config_manager.update_settings_section("ai", {
            "vision_mode": "ollama_vision",
            "ollama_vision_model": "moondream",
            "vision": {
                "ollama_label_prompt": "Name the shape in 2 words.",
                "ollama_description_prompt": "Describe the strokes.",
                "ollama_include_description": True,
                "ollama_temperature": 0.35,
                "ollama_max_tokens": 80,
                "ollama_timeout": 45,
                "clean_label": True,
                "badge_style": "combined",
            },
        })
        r1 = mock.MagicMock(ok=True)
        r1.json.return_value = {"response": "1) The lines resemble a snake."}
        r2 = mock.MagicMock(ok=True)
        r2.json.return_value = {"response": "Sharp sweeping coils dart dynamically across the center."}
        with mock.patch("analysis.requests.post", side_effect=[r1, r2]) as post_mock:
            res = analysis.analyze_drawing(str(img_path), mode="ollama_vision")

        self.assertEqual(post_mock.call_count, 2)
        first_call = post_mock.call_args_list[0].kwargs
        self.assertEqual(first_call["json"]["options"]["temperature"], 0.35)
        self.assertEqual(first_call["timeout"], 45)
        self.assertEqual(res["raw_vision_label"], "1) The lines resemble a snake.")
        self.assertTrue(res["label"].startswith("Snake · "))
        self.assertIn("Sharp sweeping coils", res["prompt_context"])


class TestTimeBasedAffineGazeCalibration(unittest.TestCase):
    def _make_fake_tracker(self):
        with mock.patch.object(eye_tracking.mp.solutions.face_mesh, "FaceMesh", return_value=mock.MagicMock()):
            return eye_tracking.EyeTracker(flip=True)

    def test_glasses_glare_freeze_and_spike_weight_healthy_eye(self):
        l_hist = deque(maxlen=5)
        r_hist = deque(maxlen=5)
        # Seed 4 frames: left eye frozen at reflection (0.0, 0.0), right eye smoothly tracking
        for step in range(4):
            l_pt = np.array([0.0, 0.0], dtype=np.float32)
            r_pt = np.array([0.01 * step, 0.0], dtype=np.float32)
            eye_tracking.fuse_binocular_eyes(l_pt, r_pt, 0.25, 0.25, l_hist, r_hist, 0.135, False)

        # Frame 5: left still frozen at (0,0), right moved to (0.05, 0) -> healthy right eye must dominate
        fused = eye_tracking.fuse_binocular_eyes(
            np.array([0.0, 0.0], dtype=np.float32),
            np.array([0.05, 0.0], dtype=np.float32),
            0.25, 0.25, l_hist, r_hist, 0.135, False,
        )
        self.assertGreater(float(fused[0]), 0.04)

        # Blink frame must NOT append to histories
        hist_len_before = len(l_hist)
        eye_tracking.fuse_binocular_eyes(
            np.array([0.3, 0.3], dtype=np.float32),
            np.array([0.3, 0.3], dtype=np.float32),
            0.05, 0.05, l_hist, r_hist, 0.135, True,
        )
        self.assertEqual(len(l_hist), hist_len_before)

        # Single-eye glare jump spike must be suppressed
        fused_spike = eye_tracking.fuse_binocular_eyes(
            np.array([0.0, 0.0], dtype=np.float32),
            np.array([0.25, 0.0], dtype=np.float32),
            0.25, 0.25, l_hist, r_hist, 0.135, False,
        )
        self.assertLess(float(fused_spike[0]), 0.05)

    def test_cross_visitor_reset_recenter_and_frame0_blink(self):
        tracker = self._make_fake_tracker()
        # Visitor 1 had wide eyes -> raised blink threshold
        tracker.blink_ear_thr = 0.155
        tracker.weights_x = np.array([0.12, 9.0, 0.0], dtype=np.float32)
        tracker.weights_y = np.array([-0.09, 0.0, 13.0], dtype=np.float32)
        tracker.smoothed = np.array([0.02, -0.01], dtype=np.float32)

        # Spacebar recenter_now must zero learned intercept biases
        tracker.recenter_now()
        self.assertEqual(float(tracker.weights_x[0]), 0.0)
        self.assertEqual(float(tracker.weights_y[0]), 0.0)

        # New visitor calibration resets poisoned blink threshold back to default
        with mock.patch("utils.save_calibration"):
            tracker.start_auto_calibration()
        self.assertAlmostEqual(tracker.blink_ear_thr, eye_tracking.BLINK_EAR_THRESHOLD, places=4)
        self.assertIsNone(tracker.smoothed)

        # Frame-0 blink must not set smoothed to closed-eyelid garbage
        fake_lm = []
        for idx in range(477):
            p = mock.MagicMock()
            p.x, p.y = 0.5, 0.5
            fake_lm.append(p)
        # Set eye corners & closed lids (EAR ~ 0.02)
        fake_lm[33].x, fake_lm[33].y = 0.3, 0.4
        fake_lm[133].x, fake_lm[133].y = 0.4, 0.4
        fake_lm[159].x, fake_lm[159].y = 0.35, 0.4
        fake_lm[145].x, fake_lm[145].y = 0.35, 0.402
        fake_lm[362].x, fake_lm[362].y = 0.6, 0.4
        fake_lm[263].x, fake_lm[263].y = 0.7, 0.4
        fake_lm[386].x, fake_lm[386].y = 0.65, 0.4
        fake_lm[374].x, fake_lm[374].y = 0.65, 0.402
        res = mock.MagicMock()
        face = mock.MagicMock()
        face.landmark = fake_lm
        res.multi_face_landmarks = [face]
        tracker.face_mesh.process.return_value = res

        frame = np.zeros((240, 320, 3), dtype=np.uint8)
        _, info = tracker.process_frame(frame)
        self.assertTrue(info["is_blinking"])
        self.assertIsNone(tracker.smoothed)
        self.assertIsNone(info["smoothed_norm"])

    def test_time_based_calibration_handles_skewed_camera_and_low_fps(self):
        tracker = self._make_fake_tracker()
        t0 = 1000.0
        with mock.patch("eye_tracking.time.time", return_value=t0), \
             mock.patch("utils.save_calibration"):
            tracker.start_auto_calibration()

            done = False
            for step in range(116):
                now = t0 + step * 0.04
                pct = tracker.get_calibration_progress(now=now)
                tx, ty = eye_tracking.calibration_target_at_progress(pct)
                rx = 0.015 + 0.075 * (tx - 0.5) + 0.015 * (ty - 0.5)
                ry = -0.008 - 0.010 * (tx - 0.5) + 0.055 * (ty - 0.5)
                gaze_info = {
                    "eye_norm": np.array([rx, ry], dtype=np.float32),
                    "smoothed_norm": np.array([rx, ry], dtype=np.float32),
                    "ear": 0.27,
                    "is_blinking": False,
                }
                done = tracker.add_calibration_sample(gaze_info, now=now)
                if done:
                    break

        self.assertTrue(done)
        self.assertAlmostEqual(tracker.blink_ear_thr, 0.27 * 0.52, places=3)

        for goal_x, goal_y in [(0.2, 0.2), (0.8, 0.75), (0.5, 0.5)]:
            rx = 0.015 + 0.075 * (goal_x - 0.5) + 0.015 * (goal_y - 0.5)
            ry = -0.008 - 0.010 * (goal_x - 0.5) + 0.055 * (goal_y - 0.5)
            g = {"smoothed_norm": np.array([rx, ry], dtype=np.float32), "is_blinking": False}
            out = None
            for _ in range(40):
                out = tracker.get_calibrated_gaze(g, dt=0.033)
            self.assertAlmostEqual(out[0], goal_x, delta=0.08)
            self.assertAlmostEqual(out[1], goal_y, delta=0.08)

    def test_motionless_calibration_clamps_to_safe_bounds(self):
        tracker = self._make_fake_tracker()
        t0 = 2000.0
        with mock.patch("eye_tracking.time.time", return_value=t0), \
             mock.patch("utils.save_calibration"):
            tracker.start_auto_calibration()
            for step in range(120):
                now = t0 + step * 0.04
                gaze_info = {
                    "eye_norm": np.array([0.002, -0.001], dtype=np.float32),
                    "smoothed_norm": np.array([0.002, -0.001], dtype=np.float32),
                    "ear": 0.24,
                    "is_blinking": False,
                }
                if tracker.add_calibration_sample(gaze_info, now=now):
                    break

        self.assertGreaterEqual(float(tracker.weights_x[1]), 4.5)
        self.assertLessEqual(float(tracker.weights_x[1]), 14.0)
        self.assertGreaterEqual(float(tracker.weights_y[2]), 6.0)
        self.assertLessEqual(float(tracker.weights_y[2]), 18.0)

    def test_gaze_painter_records_dwell_and_does_not_overwrite_prior_segments(self):
        painter = GazePainter(width=300, height=300)
        painter.update((0.1, 0.5), 0.03)
        painter.update((0.5, 0.5), 0.03)
        # Snapshot pixel thickness near segment 1 midpoint (x=90, y=150) before dwelling at (0.8, 0.5)
        painter.update((0.8, 0.5), 0.03)
        col_before = np.sum(painter.canvas[135:165, 90] < 250)
        for _ in range(15):
            painter.update((0.8, 0.5), 0.1)
        col_after = np.sum(painter.canvas[135:165, 90] < 250)
        self.assertEqual(int(col_before), int(col_after))
        self.assertGreater(painter.dwell_grid[1, 2], 0.5)
        self.assertEqual(float(painter.dwell_grid[0, 0]), 0.0)

    def test_edge_symmetric_crop_centering_and_right_eye_landmarks_and_vision_cache(self):
        # 1. Tall symmetrical drawing near left edge of 1280x720 canvas stays centered in 512x512 output
        edge_sym_in = TEST_TMP / "edge_sym_in.png"
        edge_sym_out = TEST_TMP / "edge_sym_out.png"
        canvas = np.ones((720, 1280), dtype=np.uint8) * 255
        # Symmetric vertical diamond/axis centered at (cx=80, cy=360), height=640, width=120
        cv2.line(canvas, (80, 40), (80, 680), 0, 5)
        cv2.line(canvas, (20, 360), (140, 360), 0, 5)
        cv2.circle(canvas, (80, 360), 50, 0, 5)
        cv2.imwrite(str(edge_sym_in), canvas)

        preprocess_image(str(edge_sym_in), str(edge_sym_out))
        geom = analysis.analyze_stroke_geometry(str(edge_sym_out), canvas_filename=str(edge_sym_in))
        self.assertGreater(geom["horiz_symmetry"], 0.85)
        self.assertGreater(geom["vert_symmetry"], 0.85)
        self.assertIn("strikingly symmetrical", geom["description"])

        # 2. Right-eye outer landmark is 263 (lateral) and inner is 362 (medial)
        self.assertEqual(eye_tracking.FACEMESH_LANDMARKS["right_eye_outer"], 263)
        self.assertEqual(eye_tracking.FACEMESH_LANDMARKS["right_eye_inner"], 362)

        # 3. _ollama_supports_vision does not permanently cache False on /api/show timeout
        with chat._OLLAMA_CAPS_LOCK:
            chat._OLLAMA_VISION_CAPS.clear()
        with mock.patch("chat.requests.post", side_effect=chat.requests.exceptions.Timeout("timeout")):
            # Transient timeout falls back to name hints without poisoning cache
            self.assertFalse(chat.LLMModel._ollama_supports_vision("http://localhost:11434/v1", "custom-model:latest"))
            self.assertTrue(chat.LLMModel._ollama_supports_vision("http://localhost:11434/v1", "qwen2.5-vl:7b"))
            self.assertTrue(chat.LLMModel._ollama_supports_vision("http://localhost:11434/v1", "gemma3:latest"))
        with chat._OLLAMA_CAPS_LOCK:
            self.assertNotIn(("http://localhost:11434", "custom-model:latest"), chat._OLLAMA_VISION_CAPS)

        # 4. openai_compatible in auto vision_mode skips image for text-only Groq model on first request
        config_manager.update_settings_section("ai", {
            "vision_mode": "auto",
            "analysis": {
                "provider": "openai_compatible",
                "model": "llama-3.3-70b-versatile",
                "base_url": "https://api.groq.com/openai/v1",
                "api_key": "gsk-test",
            },
        })
        groq_ok = mock.MagicMock(status_code=200, ok=True)
        groq_ok.json.return_value = {"choices": [{"message": {"content": "Groq analysis."}}]}
        with mock.patch("chat.requests.post", return_value=groq_ok) as groq_post:
            m_groq = chat.LLMModel("analysis")
            reply = m_groq.chat("Analyze", image_path=str(edge_sym_out), allow_failover=False)
        self.assertEqual(reply, "Groq analysis.")
        self.assertEqual(groq_post.call_count, 1)
        self.assertIsInstance(groq_post.call_args.kwargs["json"]["messages"][-1]["content"], str)

        # 5. EyeTracker falls back to loaded calibration if calibration ends with < 6 samples
        tracker = self._make_fake_tracker()
        tracker.load_calibration({
            "center_norm": [0.05, -0.02],
            "weights_x": [0.0, 11.5, 0.0],
            "weights_y": [0.0, 0.0, 15.0],
            "blink_ear_thr": 0.125,
        })
        tracker.start_auto_calibration()
        tracker.finish_calibration()
        self.assertAlmostEqual(float(tracker.weights_x[1]), 11.5, places=3)
        self.assertAlmostEqual(float(tracker.weights_y[2]), 15.0, places=3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
