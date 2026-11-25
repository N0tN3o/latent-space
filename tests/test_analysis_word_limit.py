import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import cv2
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(PROJECT_ROOT / "analysis_watcher") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "analysis_watcher"))

import config_manager
from analysis_watcher import chat, run_analysis_watcher


LONG_FIVE_PARA_SAMPLE = (
    "As you move your eyes across the canvas, you reveal a hidden landscape of inner turmoil. "
    "Your gaze stirs up the depths of the subconscious, where primal urges and creative tensions unfold. "
    "The Urchin archetype that emerges suggests a deep-seated desire for protection and security, "
    "a fragile shell guarding a vulnerable interior.\n\n"
    "Your strokes, bold and deliberate, like vertical black and red lines, punctuate the canvas with "
    "a sense of urgency and tension. You are drawn to the center of the composition, as if seeking "
    "balance and equilibrium in a world that feels precarious. Your panoramic gaze sweeps across the "
    "space, evincing a restless curiosity, an insatiable hunger for experience.\n\n"
    "Yet, you also avoid the dark, neglected corners of your own psyche, the mid-left region that lies "
    "in shadow. Here, a hidden narrative of fear and uncertainty whispers its presence. Your gaze is "
    "drawn to the mid-right, a place of light and illumination, where creativity and potential reside.\n\n"
    "In this dynamic, asymmetric composition, you are caught in the throes of a creative struggle, "
    "between the stability of established forms and the excitement of uncharted possibilities. Your "
    "movements, darting and electric, like sudden shifts in direction, reflect the tumultuous inner "
    "world of your imagination. The Urchin, with its hard shell and vulnerable interior, serves as a "
    "symbol of your ongoing quest for protection and creative expression.\n\n"
    "Your art is a reflection of the conflicted desires that shape your inner life: the need for "
    "security, the drive for exploration, and the tension between light and darkness. As you navigate "
    "this Latent Space, you are invited to confront the hidden forces that propel you, to surrender "
    "to the creative currents that shape your inner world."
)


class AnalysisWordLimitTests(unittest.TestCase):
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

    def tearDown(self):
        for h in list(run_analysis_watcher.vision_logger.handlers):
            try:
                h.close()
            except Exception:
                pass
            run_analysis_watcher.vision_logger.removeHandler(h)
        for k, v in self._prev_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        config_manager.reload_paths_from_env()
        self._tmp.cleanup()

    def test_bounds_and_passthrough_within_plus_10_percent(self):
        self.assertEqual(chat.compute_word_limit_bounds(110, 0.10), (99, 121))
        self.assertEqual(chat.compute_word_limit_bounds(0, 0.10), (0, 0))

        # 118 words (within 110 + 10% = 121) must pass through untouched with 0 extra LLM calls
        sample_118 = (
            "Your subconscious is ablaze with restlessness, yearning to break free from the constraints "
            "of your conscious mind. Like a charged particle, your gaze drifts erratically, driven by an "
            "insatiable curiosity that refuses to be contained. Your strokes, a whirlwind of angularity "
            "and sudden shifts, betray a tension between order and chaos, between the need for structure "
            "and the thrill of spontaneity.\n\n"
            "Your gaze is anchored in the heart of the composition, yet veers away from the fringes, "
            "as if sensing the uncertainty that lurks beyond the periphery. In the Electric Drift, you "
            "reveal a self in dynamic flux, ever-seeking, yet wary of being grounded. Your artistry is "
            "a symphony of contradictions, a primal expression of your innermost turmoil."
        )
        self.assertEqual(chat.count_words(sample_118), 118)
        fake_llm = mock.MagicMock()
        out, info = chat.enforce_analysis_word_limit(sample_118, max_words=110, tolerance_pct=0.10, llm_model=fake_llm)
        self.assertEqual(out, sample_118)
        self.assertEqual(info["action"], "unchanged")
        self.assertEqual(info["final_words"], 118)
        fake_llm.chat.assert_not_called()

    def test_llm_condensation_strips_meta_preserves_paragraphs_and_enforces_cap(self):
        self.assertGreater(chat.count_words(LONG_FIVE_PARA_SAMPLE), 250)
        condensed_reply = (
            "Here is the condensed version:\n\n"
            "Your gaze stirs a hidden landscape of tension and curiosity. Like an urchin's protective shell "
            "around a tender core, your sharp strokes anchor near the center in search of fragile balance.\n\n"
            "You skirt the shadowed mid-left, drawn instead toward open light and creative possibility. "
            "Caught between stability and wild exploration, your restless lines invite you to face the "
            "currents shaping your inner world.\n"
            "(66 words)"
        )
        fake_llm = mock.MagicMock()
        fake_llm.chat.return_value = condensed_reply

        out, info = chat.enforce_analysis_word_limit(
            LONG_FIVE_PARA_SAMPLE,
            max_words=110,
            tolerance_pct=0.10,
            llm_model=fake_llm,
        )
        fake_llm.chat.assert_called_once()
        call_kwargs = fake_llm.chat.call_args.kwargs
        self.assertIsNone(call_kwargs["image_path"])
        self.assertEqual(call_kwargs["temperature_override"], 0.4)
        self.assertIn("121 words", call_kwargs["system_prompt_override"])
        self.assertEqual(info["action"], "condensed")
        self.assertLessEqual(chat.count_words(out), 121)
        self.assertIn("\n\n", out)  # Multi-paragraph structure preserved
        self.assertNotIn("Here is the condensed version", out)
        self.assertNotIn("(66 words)", out)
        self.assertTrue(out.endswith("."))

    def test_condensation_overshoot_or_failure_falls_back_to_sentence_boundary_trim(self):
        # 1. Condensation still slightly over 121 words -> deterministic sentence-boundary trim
        overshoot_text = (
            "Paragraph one opens with vivid psychological energy, tracing every restless corner of your "
            "inner world as your gaze seeks both shelter and spontaneous motion across the page. "
            "Each sharp bend reveals an unspoken dialogue between caution and wonder, holding tension "
            "without ever surrendering its quiet poetic grace.\n\n"
            "Paragraph two deepens the reflection, showing how your focus gravitates toward luminous "
            "centerpoints while skirting shadowed edges where doubt quietly lingers. "
            "The urchin motif guards a tender interior, balancing protective restraint against bold "
            "creative exploration across uncharted space, while subtle loops whisper of curiosity "
            "returning again and again to familiar emotional anchors.\n\n"
            "Paragraph three pushes past the cap with extra commentary that should be cleanly dropped "
            "on a complete sentence boundary rather than chopped in the middle of a phrase."
        )
        self.assertGreater(chat.count_words(overshoot_text), 121)
        fake_llm = mock.MagicMock()
        fake_llm.chat.return_value = overshoot_text

        out1, info1 = chat.enforce_analysis_word_limit(
            LONG_FIVE_PARA_SAMPLE, max_words=110, tolerance_pct=0.10, llm_model=fake_llm
        )
        self.assertEqual(info1["action"], "condensed_and_trimmed")
        self.assertLessEqual(chat.count_words(out1), 121)
        self.assertTrue(out1.endswith("."))
        self.assertIn("\n\n", out1)
        self.assertNotIn("Paragraph three", out1)

        # 2. Condensation failure -> trims original 282-word sample cleanly on sentence boundaries
        failing_llm = mock.MagicMock()
        failing_llm.chat.side_effect = RuntimeError("timeout")
        out2, info2 = chat.enforce_analysis_word_limit(
            LONG_FIVE_PARA_SAMPLE, max_words=110, tolerance_pct=0.10, llm_model=failing_llm
        )
        self.assertEqual(info2["action"], "trimmed")
        self.assertLessEqual(chat.count_words(out2), 121)
        self.assertGreaterEqual(chat.count_words(out2), 99)
        self.assertIn("\n\n", out2)
        self.assertTrue(out2.endswith("experience."))

    def test_single_runon_sentence_without_periods_still_obeys_hard_cap(self):
        runon = ", ".join(f"your gaze wanders softly through shadow number {i}" for i in range(1, 25))
        self.assertGreater(chat.count_words(runon), 150)
        trimmed = chat.trim_text_to_word_limit(runon, max_words=110, tolerance_pct=0.10)
        self.assertLessEqual(chat.count_words(trimmed), 121)
        self.assertTrue(trimmed.endswith("."))

    def test_config_validation_and_llm_model_max_words_roundtrip(self):
        cfg = config_manager.update_settings_section("ai", {"analysis": {"max_words": 100}})
        self.assertEqual(cfg["ai"]["analysis"]["max_words"], 100)

        with self.assertRaises(ValueError):
            config_manager.validate_ai_data({"analysis": {"max_words": -1}})
        with self.assertRaises(ValueError):
            config_manager.validate_ai_data({"analysis": {"max_words": 501}})
        with self.assertRaises(ValueError):
            config_manager.validate_ai_data({"analysis": {"max_words": True}})

        model = chat.LLMModel("analysis")
        self.assertEqual(model.get_config_dict()["max_words"], 100)
        sys_prompt = model._build_system_prompt()
        self.assertIn("100 words", sys_prompt)
        self.assertIn("110 words", sys_prompt)

        model.update_assistant(max_words=110)
        self.assertEqual(model.get_config_dict()["max_words"], 110)

    def test_pipeline_enforces_analysis_word_limit_before_translation_and_save(self):
        rid = "20261120_113000_001"
        snap = config_manager.SHARED_DIR / f"input_{rid}.png"
        canvas = np.ones((200, 200), dtype=np.uint8) * 255
        cv2.circle(canvas, (100, 100), 60, 0, 4)
        cv2.imwrite(str(snap), canvas)

        config_manager.update_settings_section("ai", {
            "vision_mode": "stroke_metrics",
            "analysis": {"max_words": 110},
        })
        config_manager.update_settings_section("translation", {"enabled": False, "mode": "off"})

        def fake_save_pdf(url=None, out_dir=None, website_data=None, run_id=None):
            out_pdf = Path(out_dir) / f"print_{run_id}.pdf"
            out_pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
            return str(out_pdf)

        condensed_two_para = (
            "Your gaze carves a restless landscape where sharp lines guard a tender interior. "
            "Drawn toward the center, you seek fragile equilibrium amid sudden shifts of energy.\n\n"
            "Avoiding the shadowed left, your focus reaches toward luminous possibility. "
            "In this dynamic tension between shelter and discovery, your eyes reveal a mind "
            "eager to surrender to its own creative currents."
        )

        fake_model = mock.MagicMock()
        fake_model._provider = "ollama"
        fake_model._llm = "llama3.1:8b"
        fake_model._last_used_provider = "ollama"
        fake_model._last_used_model = "llama3.1:8b"
        fake_model.initialize.return_value = {"status": "Success"}
        # Call 1: long 282-word analysis -> Call 2: condensation -> Call 3: haiku poem
        fake_model.chat.side_effect = [
            LONG_FIVE_PARA_SAMPLE,
            condensed_two_para,
            "Shadows wake the line\nCenter holds a quiet spark\nLight opens the frame",
        ]

        with mock.patch.object(run_analysis_watcher, "LLMModel", return_value=fake_model), \
             mock.patch.object(run_analysis_watcher, "save_print_pdf", side_effect=fake_save_pdf):
            ok = run_analysis_watcher.run_analysis_pipeline(run_id=rid, snapshot_path=str(snap))

        self.assertTrue(ok)
        content = json.loads((config_manager.SHARED_DIR / "content.json").read_text(encoding="utf-8"))
        saved_analysis = content["psychoanalysis"]
        self.assertLessEqual(chat.count_words(saved_analysis), 121)
        self.assertIn("\n\n", saved_analysis)
        self.assertEqual(saved_analysis, condensed_two_para)
        self.assertEqual(len(content["poem"].splitlines()), 3)


if __name__ == "__main__":
    unittest.main()
