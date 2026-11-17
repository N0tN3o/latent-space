import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import cv2
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ANALYSIS_DIR = PROJECT_ROOT / "analysis_watcher"
GAZE_DIR = PROJECT_ROOT / "gaze_painter"
TEST_TMP = Path(tempfile.mkdtemp(prefix="ls_cv_gaze_test_"))
os.environ["LATENT_SPACE_CONFIG_DIR"] = str(TEST_TMP / "config")
os.environ["LATENT_SPACE_SHARED_DIR"] = str(TEST_TMP / "shared_output")
os.environ["LATENT_SPACE_LOGS_DIR"] = str(TEST_TMP / "logs")

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


class TestCVAndGeometry(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TEST_TMP, ignore_errors=True)

    def setUp(self):
        config_manager.ensure_directories()
        config_manager.save_settings(config_manager.DEFAULT_SETTINGS)

    def test_uncropped_canvas_and_heatmap_agree_on_corner_drawing(self):
        canvas_path = TEST_TMP / "corner_canvas.png"
        proc_path = TEST_TMP / "corner_proc.png"
        hm_path = TEST_TMP / "corner_hm.png"

        canvas = np.ones((720, 1280), dtype=np.uint8) * 255
        # Draw purely in upper-left corner
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
        cv2.line(img, (50, 400), (200, 450), 0, 5)  # lower-left ink
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
        self.assertIn("angular, darting, and electric", z_geom["description"])

    def test_chat_skips_image_payload_for_text_only_ollama_in_auto_mode(self):
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


class TestTimeBasedAffineGazeCalibration(unittest.TestCase):
    def _make_fake_tracker(self):
        with mock.patch.object(eye_tracking.mp.solutions.face_mesh, "FaceMesh", return_value=mock.MagicMock()):
            return eye_tracking.EyeTracker(flip=True)

    def test_time_based_calibration_handles_skewed_camera_and_low_fps(self):
        tracker = self._make_fake_tracker()
        t0 = 1000.0
        with mock.patch("eye_tracking.time.time", return_value=t0), \
             mock.patch("utils.save_calibration"):
            tracker.start_auto_calibration()

            # Simulate 25 FPS over 4.6 seconds with tilted camera & different X/Y gains
            done = False
            for step in range(116):
                now = t0 + step * 0.04
                pct = tracker.get_calibration_progress(now=now)
                tx, ty = eye_tracking.calibration_target_at_progress(pct)
                # Forward model: eye_x = 0.015 + 0.075*(tx-0.5) + 0.015*(ty-0.5)
                #                eye_y = -0.008 - 0.010*(tx-0.5) + 0.055*(ty-0.5)
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

        # Verify target recovery at corners (stepping cursor EMA to convergence)
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

    def test_gaze_painter_records_dwell_grid(self):
        painter = GazePainter(width=300, height=300)
        painter.update((0.9, 0.9), 0.1)
        painter.update((0.9, 0.9), 0.15)
        self.assertGreater(painter.dwell_grid[2, 2], 0.2)
        self.assertEqual(float(painter.dwell_grid[0, 0]), 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
