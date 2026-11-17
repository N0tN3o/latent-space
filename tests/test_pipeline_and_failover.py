import http.client
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock

import cv2
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ANALYSIS_DIR = PROJECT_ROOT / "analysis_watcher"
for p in (str(PROJECT_ROOT), str(ANALYSIS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import config_manager
import monitoring_server
import qr_utils
import run_analysis_watcher
from chat import AIGenerationError, LLMModel

TEST_TMP = None


def setUpModule():
    global TEST_TMP
    TEST_TMP = Path(tempfile.mkdtemp(prefix="ls_pipeline_test_"))
    os.environ["LATENT_SPACE_CONFIG_DIR"] = str(TEST_TMP / "config")
    os.environ["LATENT_SPACE_SHARED_DIR"] = str(TEST_TMP / "shared_output")
    os.environ["LATENT_SPACE_LOGS_DIR"] = str(TEST_TMP / "logs")
    os.environ["LATENT_SPACE_ADMIN_PASSWORD"] = "TestPipelinePass!42"
    config_manager.reload_paths_from_env()
    config_manager.ensure_directories()


def tearDownModule():
    global TEST_TMP
    if TEST_TMP is not None:
        shutil.rmtree(TEST_TMP, ignore_errors=True)
        TEST_TMP = None


class _FakeSocket:
    def __init__(self, raw_request: bytes):
        self._rfile = io.BytesIO(raw_request)
        self._wfile = io.BytesIO()

    def makefile(self, mode, *args, **kwargs):
        return self._rfile if "r" in mode else self._wfile

    def sendall(self, data):
        self._wfile.write(data)

    def settimeout(self, _timeout):
        pass

    def close(self):
        pass


class _DummyServer:
    pass


class _TestableHTTPResponseSocket:
    def __init__(self, raw_response: bytes):
        self._buf = io.BytesIO(raw_response)

    def makefile(self, mode, *args, **kwargs):
        return self._buf


class TestPipelinePDFFailoverAndCleanup(unittest.TestCase):
    def setUp(self):
        config_manager.ensure_directories()
        config_manager.save_settings(config_manager.DEFAULT_SETTINGS)

    def _http(self, method, path, body=None, headers=None):
        hdrs = {"Host": "localhost"}
        if headers:
            hdrs.update(headers)
        raw_body = b""
        if body is not None:
            raw_body = json.dumps(body).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
            hdrs.setdefault("Content-Length", str(len(raw_body)))
        lines = [f"{method} {path} HTTP/1.1"] + [f"{k}: {v}" for k, v in hdrs.items()]
        sock = _FakeSocket(("\r\n".join(lines) + "\r\n\r\n").encode("utf-8") + raw_body)
        monitoring_server.LatentSpaceRequestHandler(sock, ("127.0.0.1", 54321), _DummyServer())
        resp = http.client.HTTPResponse(_TestableHTTPResponseSocket(sock._wfile.getvalue()))
        resp.begin()
        data = resp.read()
        resp.close()
        return resp.status, data

    def test_content_json_has_print_pdf_before_playwright_render(self):
        rid = "20261117_120000_001"
        snap = config_manager.SHARED_DIR / f"input_{rid}.png"
        canvas = np.ones((200, 200), dtype=np.uint8) * 255
        cv2.line(canvas, (20, 20), (180, 180), 0, 4)
        cv2.imwrite(str(snap), canvas)

        observed_during_pdf_render = {}

        def fake_save_pdf(url=None, out_dir=None, website_data=None, run_id=None):
            content_on_disk = json.loads((config_manager.SHARED_DIR / "content.json").read_text(encoding="utf-8"))
            observed_during_pdf_render["printPDF"] = content_on_disk.get("printPDF")
            observed_during_pdf_render["pdfRendering"] = content_on_disk.get("pdfRendering")
            out_pdf = Path(out_dir) / f"print_{run_id}.pdf"
            out_pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
            return str(out_pdf)

        fake_model = mock.MagicMock()
        fake_model._provider = "ollama"
        fake_model._llm = "llama3.2"
        fake_model._last_used_provider = "ollama"
        fake_model._last_used_model = "llama3.2"
        fake_model.initialize.return_value = {"status": "Success"}
        fake_model.chat.side_effect = ["A thoughtful psychoanalytical reading.", "Silent eyes trace\nPaths in white space."]

        with mock.patch.object(run_analysis_watcher, "LLMModel", return_value=fake_model), \
             mock.patch.object(run_analysis_watcher, "save_print_pdf", side_effect=fake_save_pdf):
            ok = run_analysis_watcher.run_analysis_pipeline(run_id=rid, snapshot_path=str(snap), raise_on_error=True)

        self.assertTrue(ok)
        self.assertEqual(observed_during_pdf_render.get("printPDF"), f"docs/print_{rid}.pdf")
        self.assertTrue(observed_during_pdf_render.get("pdfRendering"))
        final_content = json.loads((config_manager.SHARED_DIR / "content.json").read_text(encoding="utf-8"))
        self.assertFalse(final_content.get("pdfRendering"))

    def test_fallback_pdf_renderer_wraps_and_renders_valid_pdf(self):
        rid = "20261117_120500_001"
        thumb = config_manager.DOCS_DIR / f"print_{rid}.png"
        hm = config_manager.DOCS_DIR / f"print_{rid}.heatmap.png"
        img = np.ones((128, 128, 3), dtype=np.uint8) * 255
        cv2.circle(img, (64, 64), 32, (20, 40, 200), 3)
        cv2.imwrite(str(thumb), img)
        cv2.imwrite(str(hm), img)

        pdf_out = config_manager.DOCS_DIR / f"print_{rid}.pdf"
        website_data = {
            "scribblePath": f"docs/{thumb.name}",
            "heatmapPath": f"docs/{hm.name}",
            "archetype": "Intertwined Orbits",
            "fractalDimension": 1.37,
            "spatialEntropy": 0.64,
            "psychoanalysis": ("Your gaze traces continuous loops around an unspoken center of gravity. " * 10).strip(),
            "poem": "\n".join([f"Line {i} of a long contemplative poem." for i in range(1, 20)]),
            "timestamp": "2026-10-01T14:30:00",
        }
        settings = config_manager.load_settings()
        settings["printing"]["paper_format"] = "Letter"
        settings["attributions"]["enabled"] = True
        settings["attributions"]["custom_footer_note"] = "Exhibition Archive Note"

        res_path = run_analysis_watcher._render_pdf_fallback(pdf_out, website_data, settings)
        self.assertEqual(res_path, str(pdf_out))
        self.assertTrue(pdf_out.exists())
        self.assertTrue(pdf_out.read_bytes().startswith(b"%PDF-"))

        wrapped = run_analysis_watcher._wrap_preserving_lines(website_data["psychoanalysis"], width=48)
        self.assertTrue(all(len(line) <= 48 for line in wrapped.splitlines()))

    def test_qr_utils_decodes_with_opencv(self):
        det = cv2.QRCodeDetector()
        url = "http://192.168.1.42:8000/shared_output/docs/print_20261117_220500_001.pdf"
        mat = qr_utils.generate_qr_matrix(url, quiet=4)
        img = np.array([[0 if c else 255 for c in row] for row in mat], dtype=np.uint8).repeat(4, 0).repeat(4, 1)
        decoded, _, _ = det.detectAndDecode(img)
        self.assertEqual(decoded, url)
        self.assertIn("<svg", qr_utils.qr_svg(url))

    def test_failover_does_not_leak_fallback_model_across_providers(self):
        config_manager.update_settings_section("ai", {
            "analysis": {
                "provider": "anthropic",
                "model": "claude-3-5-haiku-latest",
                "api_key": "",
                "fallback_provider": "openai_compatible",
                "fallback_model": "gpt-4o",
            },
            "poem": {
                "provider": "ollama",
                "model": "qwen2.5:7b",
            },
        })
        env = {
            "GEMINI_API_KEY": "gem-test-key",
            "OPENAI_API_KEY": "oai-test-key",
        }
        with mock.patch("chat.load_env_vars", return_value=env):
            m = LLMModel("analysis")
            candidates = m._build_failover_candidates()

        gemini_entries = [c for c in candidates if c[0] == "gemini"]
        ollama_entries = [c for c in candidates if c[0] == "ollama"]
        self.assertTrue(gemini_entries)
        self.assertEqual(gemini_entries[0][1], "gemini-2.0-flash")
        self.assertTrue(ollama_entries)
        self.assertEqual(ollama_entries[0][1], "qwen2.5:7b")

    def test_ollama_404_discovery_reports_actual_model_without_mutating_primary(self):
        config_manager.update_settings_section("ai", {
            "analysis": {
                "provider": "gemini",
                "model": "gemini-2.0-flash",
                "api_key": "",
                "fallback_provider": "ollama",
                "fallback_model": "missing-tag",
            }
        })
        resp_404 = mock.MagicMock(status_code=404, ok=False)
        resp_tags = mock.MagicMock(ok=True)
        resp_tags.json.return_value = {"models": [{"name": "nomic-embed-text"}, {"name": "qwen2.5:latest"}]}
        resp_ok = mock.MagicMock(status_code=200, ok=True)
        resp_ok.json.return_value = {"choices": [{"message": {"content": "Discovered reply."}}]}

        with mock.patch("chat.load_env_vars", return_value={}), \
             mock.patch("chat.requests.post", side_effect=[resp_404, resp_ok]), \
             mock.patch("chat.requests.get", return_value=resp_tags):
            m = LLMModel("analysis")
            reply = m.chat("Hello", allow_failover=True)

        self.assertEqual(reply, "Discovered reply.")
        self.assertEqual(m._last_used_provider, "ollama")
        self.assertEqual(m._last_used_model, "qwen2.5:latest")
        self.assertEqual(m._llm, "gemini-2.0-flash")

    def test_generate_run_id_stays_fixed_width_past_999(self):
        with run_analysis_watcher._id_lock:
            run_analysis_watcher._last_run_id = "20991231_235959_999"
        next_id = run_analysis_watcher.generate_run_id()
        self.assertEqual(next_id, "21000101_000000_000")
        self.assertEqual(len(next_id), 19)
        self.assertGreater(next_id, "20991231_235959_999")
        with run_analysis_watcher._id_lock:
            run_analysis_watcher._last_run_id = ""

    def test_per_run_status_isolation_and_mp3_cleanup(self):
        img = np.ones((64, 64), dtype=np.uint8) * 255
        ok, buf = cv2.imencode(".png", img)
        self.assertTrue(ok)
        png_bytes = buf.tobytes()

        call_log = []

        def fake_pipeline(run_id=None, snapshot_path=None, raise_on_error=False, dwell_grid=None):
            call_log.append(run_id)
            if run_id == "run_one":
                return True
            if run_id == "run_false":
                return False
            raise run_analysis_watcher.PipelineStageError("ERR_POEM_AI_FAILED", "Poet down")

        with mock.patch.object(monitoring_server, "run_analysis_pipeline", side_effect=fake_pipeline):
            ev1 = threading.Event()
            ev2 = threading.Event()
            ev3 = threading.Event()
            r1, _ = monitoring_server.enqueue_pipeline_job(image_bytes=png_bytes, run_id="run_one", done_event=ev1)
            r2, _ = monitoring_server.enqueue_pipeline_job(image_bytes=png_bytes, run_id="run_two", done_event=ev2)
            r3, _ = monitoring_server.enqueue_pipeline_job(image_bytes=png_bytes, run_id="run_false", done_event=ev3)
            self.assertTrue(ev1.wait(timeout=5))
            self.assertTrue(ev2.wait(timeout=5))
            self.assertTrue(ev3.wait(timeout=5))

        st1, d1 = self._http("GET", f"/api/pipeline/status?run_id={r1}")
        st2, d2 = self._http("GET", f"/api/pipeline/status?run_id={r2}")
        st3, d3 = self._http("GET", f"/api/pipeline/status?run_id={r3}")
        self.assertEqual(st1, 200)
        self.assertEqual(st2, 200)
        self.assertEqual(st3, 200)
        self.assertEqual(json.loads(d1)["run"]["state"], "COMPLETED")
        self.assertEqual(json.loads(d2)["run"]["state"], "ERROR")
        self.assertEqual(json.loads(d2)["run"]["error_code"], "ERR_POEM_AI_FAILED")
        # Returning False without raising must also leave pipeline state in ERROR, not IDLE
        p3 = json.loads(d3)
        self.assertEqual(p3["run"]["state"], "ERROR")
        self.assertEqual(p3["state"], "ERROR")

        rid = "20250101_090000_001"
        for ext in (".pdf", ".png", ".json", ".heatmap.png"):
            (config_manager.DOCS_DIR / f"print_{rid}{ext}").write_bytes(b"test")
        audio_file = config_manager.SHARED_DIR / f"output_{rid}.mp3"
        audio_file.write_bytes(b"ID3test")

        with mock.patch.dict(os.environ, {"LATENT_SPACE_ADMIN_PASSWORD": "TestPipelinePass!42"}):
            status, resp = self._http(
                "POST",
                "/api/files/delete",
                body={"action": "specific", "filename": f"print_{rid}.pdf"},
                headers={"Authorization": "Bearer TestPipelinePass!42"},
            )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(resp)["deleted"], 1)
        self.assertFalse(audio_file.exists())
        self.assertFalse((config_manager.DOCS_DIR / f"print_{rid}.pdf").exists())

    def test_sync_timeout_cancel_and_cleanup_orphaned_temp_files(self):
        img = np.ones((64, 64), dtype=np.uint8) * 255
        _, buf = cv2.imencode(".png", img)
        snap = config_manager.SHARED_DIR / "input_cancel_test.png"
        snap.write_bytes(buf.tobytes())

        cancel_ev = threading.Event()
        cancel_ev.set()
        content_file = config_manager.SHARED_DIR / "content.json"
        if content_file.exists():
            content_file.unlink()
        ok = run_analysis_watcher.run_analysis_pipeline(
            run_id="cancel_test", snapshot_path=str(snap), raise_on_error=False, cancel_event=cancel_ev,
        )
        self.assertFalse(ok)
        self.assertFalse(content_file.exists())

        # chat() aborts immediately without HTTP calls when cancel_event is already set
        m = LLMModel("analysis")
        with mock.patch("chat.requests.post") as post_mock:
            with self.assertRaises(AIGenerationError):
                m.chat("Hi", allow_failover=True, cancel_event=cancel_ev)
            self.assertEqual(post_mock.call_count, 0)

        # Orphaned temp files cleanup
        orphan_in = config_manager.SHARED_DIR / "input_20250101_000000_001.png"
        orphan_mp3 = config_manager.SHARED_DIR / "output_20250101_000000_001.mp3"
        valid_mp3 = config_manager.SHARED_DIR / "output_20250101_000000_002.mp3"
        valid_pdf = config_manager.DOCS_DIR / "print_20250101_000000_002.pdf"
        for p in (orphan_in, orphan_mp3, valid_mp3, valid_pdf):
            p.write_bytes(b"data")
        run_analysis_watcher.cleanup_orphaned_temp_files()
        self.assertFalse(orphan_in.exists())
        self.assertFalse(orphan_mp3.exists())
        self.assertTrue(valid_mp3.exists())

    def test_shared_print_history_mark_and_prune(self):
        fname = "print_20261117_150000_001.pdf"
        config_manager.mark_file_printed(fname, extra_meta={"source": "kiosk"})
        hist_file = config_manager.DOCS_DIR / "print_history.json"
        self.assertTrue(hist_file.exists())
        hist = json.loads(hist_file.read_text(encoding="utf-8"))
        self.assertIn(fname, hist["printed"])
        self.assertEqual(hist["prints"][-1]["source"], "kiosk")

        config_manager.remove_from_print_history([fname])
        hist_after = json.loads(hist_file.read_text(encoding="utf-8"))
        self.assertNotIn(fname, hist_after["printed"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
