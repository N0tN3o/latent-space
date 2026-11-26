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
             mock.patch.object(run_analysis_watcher, "save_print_pdf", side_effect=fake_save_pdf), \
             mock.patch.object(run_analysis_watcher, "get_translation_config", return_value={"enabled": False, "mode": "off", "target_languages": [], "default_print_language": "en"}):
            ok = run_analysis_watcher.run_analysis_pipeline(run_id=rid, snapshot_path=str(snap), raise_on_error=True)

        self.assertTrue(ok)
        self.assertEqual(observed_during_pdf_render.get("printPDF"), f"docs/print_{rid}.pdf")
        self.assertTrue(observed_during_pdf_render.get("pdfRendering"))
        final_content = json.loads((config_manager.SHARED_DIR / "content.json").read_text(encoding="utf-8"))
        self.assertFalse(final_content.get("pdfRendering"))
        vision_log_file = config_manager.LOGS_DIR / "vision.log"
        self.assertTrue(vision_log_file.exists())
        self.assertIn(rid, vision_log_file.read_text(encoding="utf-8"))

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
        settings["attributions"]["veszprem_vibe"]["enabled"] = True
        settings["attributions"]["veszprem_vibe"]["linkedin_url"] = "https://www.linkedin.com/company/veszprem-vibe"
        settings["attributions"]["veszprem_vibe"]["facebook_url"] = "https://www.facebook.com/veszpremvibe"

        res_path = run_analysis_watcher._render_pdf_fallback(pdf_out, website_data, settings)
        self.assertEqual(res_path, str(pdf_out))
        self.assertTrue(pdf_out.exists())
        self.assertTrue(pdf_out.read_bytes().startswith(b"%PDF-"))
        self.assertEqual(run_analysis_watcher._count_pdf_pages(pdf_out), 1)
        self.assertNotIn(b"Scan for digital print", pdf_out.read_bytes())

        wrapped = run_analysis_watcher._wrap_preserving_lines(website_data["psychoanalysis"], width=48)
        self.assertTrue(all(len(line) <= 48 for line in wrapped.splitlines()))

    def test_print_layout_omits_qr_and_keeps_two_columns_for_single_page_pdf(self):
        html = (PROJECT_ROOT / "index.html").read_text(encoding="utf-8")
        # Responsive collapse must be screen-only so A4 printing (794px wide) keeps the 2-column layout
        self.assertIn("@media screen and (max-width: 1024px)", html)
        self.assertNotIn("@media (max-width: 1024px)", html)
        # QR code must be hidden in print CSS and skipped during Playwright print_render=1
        self.assertIn("audio, .floating-nav, .qr-card { display: none !important; }", html)
        self.assertIn("if (isPrintRender || !targetPath)", html)

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

    def test_mid_pdf_timeout_cancellation_updates_content_json_without_dangling_links(self):
        rid = "20261118_100000_001"
        snap = config_manager.SHARED_DIR / f"input_{rid}.png"
        canvas = np.ones((200, 200), dtype=np.uint8) * 255
        cv2.line(canvas, (20, 20), (180, 180), 0, 4)
        cv2.imwrite(str(snap), canvas)

        cancel_ev = threading.Event()

        def fake_pdf_cancel(url=None, out_dir=None, website_data=None, run_id=None):
            out_pdf = Path(out_dir) / f"print_{run_id}.pdf"
            out_pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
            cancel_ev.set()
            return str(out_pdf)

        fake_model = mock.MagicMock()
        fake_model._provider = "ollama"
        fake_model._llm = "llama3.2"
        fake_model._last_used_provider = "ollama"
        fake_model._last_used_model = "llama3.2"
        fake_model.initialize.return_value = {"status": "Success"}
        fake_model.chat.side_effect = ["Analysis text.", "Poem text."]

        with mock.patch.object(run_analysis_watcher, "LLMModel", return_value=fake_model), \
             mock.patch.object(run_analysis_watcher, "save_print_pdf", side_effect=fake_pdf_cancel), \
             mock.patch.object(run_analysis_watcher, "get_translation_config", return_value={"enabled": False, "mode": "off", "target_languages": [], "default_print_language": "en"}):
            ok = run_analysis_watcher.run_analysis_pipeline(
                run_id=rid, snapshot_path=str(snap), raise_on_error=False, cancel_event=cancel_ev,
            )

        self.assertFalse(ok)
        self.assertFalse((config_manager.DOCS_DIR / f"print_{rid}.png").exists())
        self.assertFalse((config_manager.DOCS_DIR / f"print_{rid}.heatmap.png").exists())
        self.assertFalse((config_manager.DOCS_DIR / f"print_{rid}.pdf").exists())
        content_after = json.loads((config_manager.SHARED_DIR / "content.json").read_text(encoding="utf-8"))
        self.assertEqual(content_after.get("status"), "error")
        self.assertEqual(content_after.get("errorCode"), "ERR_PIPELINE_TIMEOUT")
        self.assertNotIn("scribblePath", content_after)

    def test_qr_utils_versions_1_to_10_and_local_dwell_and_winprint_fallback(self):
        det = cv2.QRCodeDetector()
        for url in [
            "http://10.0.0.1:8000/p.pdf",
            "https://gallery.example.org/shared_output/docs/print_20261118_100000_001.pdf",
            "https://gallery.example.org/shared_output/docs/" + ("x9y8z7" * 28)[:166],  # 213 bytes max V10
        ]:
            mat = qr_utils.generate_qr_matrix(url, quiet=4)
            img = np.array([[0 if c else 255 for c in row] for row in mat], dtype=np.uint8).repeat(4, 0).repeat(4, 1)
            decoded, _, _ = det.detectAndDecode(img)
            self.assertEqual(decoded, url)

        with self.assertRaises(ValueError):
            qr_utils.generate_qr_matrix("https://example.org/" + "é" * 100)  # >213 UTF-8 bytes

        # Local dwell_grid.json sidecar consumed atomically once
        dwell_file = config_manager.SHARED_DIR / "dwell_grid.json"
        dwell_file.write_text(json.dumps([[0, 0, 1.5], [0, 0, 0], [0, 0, 0]]), encoding="utf-8")
        loaded = run_analysis_watcher._consume_local_dwell_grid()
        self.assertEqual(loaded[0][2], 1.5)
        self.assertFalse(dwell_file.exists())

        # _print_windows_via_dotnet returns False instead of raising when PowerShell fails
        dummy_pdf = TEST_TMP / "dummy.pdf"
        dummy_pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
        with mock.patch.object(config_manager, "_render_pdf_pages_to_pngs", return_value=[TEST_TMP / "p0.png"]), \
             mock.patch("config_manager.subprocess.run", side_effect=OSError("powershell error")):
            self.assertFalse(config_manager._print_windows_via_dotnet(dummy_pdf, "default", False))

    def test_gc_tts_language_inference_watcher_lock_and_delete_syncs_gallery(self):
        # 1. Google Cloud TTS derives language_code dynamically and omits hardcoded gender
        import types
        fake_tts_mod = types.ModuleType("google.cloud.texttospeech")
        fake_tts_mod.TextToSpeechClient = mock.MagicMock(return_value=mock.MagicMock())
        fake_tts_mod.VoiceSelectionParams = mock.MagicMock(side_effect=lambda **kw: kw)
        fake_tts_mod.AudioConfig = mock.MagicMock(side_effect=lambda **kw: kw)
        fake_tts_mod.AudioEncoding = types.SimpleNamespace(MP3="MP3")
        fake_cloud = types.ModuleType("google.cloud")
        fake_cloud.texttospeech = fake_tts_mod
        fake_google = types.ModuleType("google")
        fake_google.cloud = fake_cloud
        with mock.patch.dict(sys.modules, {"google": fake_google, "google.cloud": fake_cloud, "google.cloud.texttospeech": fake_tts_mod}):
            sys.modules.pop("tts_gc", None)
            import tts_gc
            _, voice, _ = tts_gc.gc_tts_setup(voice_id="nl-NL-Wavenet-B")
            self.assertEqual(voice["language_code"], "nl-NL")
            self.assertEqual(voice["name"], "nl-NL-Wavenet-B")
            self.assertNotIn("ssml_gender", voice)

        # 2. Non-blocking interprocess lock prevents duplicate watcher acquire until released
        lock_file = TEST_TMP / "logs" / ".test_watcher.lock"
        fd1 = config_manager.try_acquire_nonblocking_lock(lock_file)
        self.assertIsNotNone(fd1)
        fd2 = config_manager.try_acquire_nonblocking_lock(lock_file)
        self.assertIsNone(fd2)
        config_manager.release_nonblocking_lock(fd1)
        fd3 = config_manager.try_acquire_nonblocking_lock(lock_file)
        self.assertIsNotNone(fd3)
        config_manager.release_nonblocking_lock(fd3)

        # 3. Deleting active print rolls content.json back to previous archived print, then to empty state
        for existing in config_manager.DOCS_DIR.glob("print_*"):
            existing.unlink(missing_ok=True)
        rid_old = "20261118_110000_001"
        rid_new = "20261118_110500_001"
        for rid, poem in ((rid_old, "Older poem"), (rid_new, "Newer poem")):
            (config_manager.DOCS_DIR / f"print_{rid}.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
            (config_manager.DOCS_DIR / f"print_{rid}.png").write_bytes(b"png")
            (config_manager.DOCS_DIR / f"print_{rid}.json").write_text(
                json.dumps({
                    "status": "ok",
                    "runId": rid,
                    "scribblePath": f"docs/print_{rid}.png",
                    "heatmapPath": "",
                    "psychoanalysis": f"Analysis for {rid}",
                    "poem": poem,
                    "poemAudio": "",
                    "printPDF": f"docs/print_{rid}.pdf",
                    "pdfRendering": False,
                }),
                encoding="utf-8",
            )
        os.utime(config_manager.DOCS_DIR / f"print_{rid_old}.pdf", (1000.0, 1000.0))
        os.utime(config_manager.DOCS_DIR / f"print_{rid_new}.pdf", (2000.0, 2000.0))

        config_manager.write_content_json({
            "status": "ok",
            "runId": rid_new,
            "scribblePath": f"docs/print_{rid_new}.png",
            "printPDF": f"docs/print_{rid_new}.pdf",
            "poem": "Newer poem",
        })

        with mock.patch.dict(os.environ, {"LATENT_SPACE_ADMIN_PASSWORD": "TestPipelinePass!42"}):
            st1, _ = self._http(
                "POST", "/api/files/delete",
                body={"action": "specific", "filename": f"print_{rid_new}.pdf"},
                headers={"Authorization": "Bearer TestPipelinePass!42"},
            )
            self.assertEqual(st1, 200)
            content_mid = json.loads((config_manager.SHARED_DIR / "content.json").read_text(encoding="utf-8"))
            self.assertEqual(content_mid["runId"], rid_old)
            self.assertEqual(content_mid["poem"], "Older poem")

            st2, _ = self._http(
                "POST", "/api/files/delete",
                body={"action": "specific", "filename": f"print_{rid_old}.pdf"},
                headers={"Authorization": "Bearer TestPipelinePass!42"},
            )
            self.assertEqual(st2, 200)
            content_empty = json.loads((config_manager.SHARED_DIR / "content.json").read_text(encoding="utf-8"))
            self.assertEqual(content_empty["status"], "empty")
            self.assertEqual(content_empty["scribblePath"], "")
            self.assertEqual(content_empty["printPDF"], "")

    def test_cross_process_watcher_sig_and_elevenlabs_single_voice_and_pdf_fit(self):
        # 1. Persisted .image_handled.sig prevents external watcher double-trigger
        target_img = config_manager.SHARED_DIR / "image.png"
        run_analysis_watcher.write_live_image_and_mark_handled(target_img, b"\x89PNG\r\n\x1a\n" + b"test1234")
        persisted = run_analysis_watcher._read_persisted_handled_digest()
        sig = run_analysis_watcher._compute_image_sig(target_img)
        self.assertEqual(persisted, sig[2])

        # 2. ElevenLabs TTS works when voice_id_2 is blank or when pydub/ffmpeg raises FileNotFoundError
        import types
        fake_el_client_mod = types.ModuleType("elevenlabs.client")
        fake_el_instance = mock.MagicMock()
        fake_el_instance.text_to_speech.convert.return_value = [b"ID3", b"-fake-mp3-bytes"]
        fake_el_client_mod.ElevenLabs = mock.MagicMock(return_value=fake_el_instance)
        fake_el_pkg = types.ModuleType("elevenlabs")
        fake_el_pkg.client = fake_el_client_mod
        fake_pydub = types.ModuleType("pydub")
        fake_pydub.AudioSegment = mock.MagicMock()
        fake_pydub.AudioSegment.from_file.side_effect = FileNotFoundError("ffmpeg not found")

        settings_single = config_manager.load_settings()
        settings_single["tts"] = {
            "enabled": True,
            "provider": "elevenlabs",
            "voice_id_1": "voice-primary",
            "voice_id_2": "",
        }
        with mock.patch.dict(sys.modules, {"elevenlabs": fake_el_pkg, "elevenlabs.client": fake_el_client_mod, "pydub": fake_pydub}), \
             mock.patch("run_analysis_watcher.load_env_vars", return_value={"API_KEY": "el-test-key"}):
            sys.modules.pop("postprocess_audio", None)
            audio_single = run_analysis_watcher._run_optional_tts("Short poem", settings_single, run_id="tts_single")
            self.assertEqual(audio_single, "output_tts_single.mp3")
            self.assertTrue((config_manager.SHARED_DIR / audio_single).exists())

            # With voice_id_2 set but ffmpeg missing, falls back to primary voice MP3
            settings_dual = dict(settings_single)
            settings_dual["tts"] = dict(settings_single["tts"], voice_id_2="voice-secondary")
            audio_fallback = run_analysis_watcher._run_optional_tts("Short poem", settings_dual, run_id="tts_fallback")
            self.assertEqual(audio_fallback, "output_tts_fallback.mp3")
            self.assertTrue((config_manager.SHARED_DIR / audio_fallback).exists())

        # 3. _fit_text_block truncates extreme multi-line overflow cleanly with ellipsis
        huge_poem = "\n".join([f"Verse line {i} wandering through the latent space" for i in range(60)])
        fitted, fs = run_analysis_watcher._fit_text_block(
            huge_poem, base_width=30, base_fontsize=8.5, max_height_frac=0.195, page_height_pt=550.0,
        )
        self.assertEqual(fs, 6.2)
        self.assertTrue(fitted.endswith("..."))
        self.assertLessEqual(len(fitted.splitlines()), 15)

    def test_audit_h2_provider_400_retries_obey_single_call_deadline(self):
        import time
        import requests

        config_manager.update_settings_section("ai", {
            "vision_mode": "vision",
            "analysis": {
                "provider": "openai_compatible",
                "model": "gpt-4o",
                "base_url": "http://localhost:1234/v1",
                "thinking_mode": "low",
                "fallback_provider": "none",
            },
        })
        img_file = TEST_TMP / "h2_dummy.png"
        img = np.ones((32, 32), dtype=np.uint8) * 255
        _, buf = cv2.imencode(".png", img)
        img_file.write_bytes(buf.tobytes())

        observed_timeouts = []

        def slow_400_post(*args, **kwargs):
            to = kwargs.get("timeout")
            read_to = to[1] if isinstance(to, (tuple, list)) else float(to)
            observed_timeouts.append(read_to)
            time.sleep(min(0.18, read_to))
            resp = mock.MagicMock(status_code=400, ok=False)
            resp.raise_for_status.side_effect = requests.exceptions.HTTPError("400 Bad Request", response=resp)
            return resp

        m = LLMModel("analysis")
        t0 = time.monotonic()
        single_call_budget = 0.35
        with mock.patch("chat.requests.post", side_effect=slow_400_post):
            with self.assertRaises(AIGenerationError):
                m.chat(
                    "Test H2 deadline",
                    image_path=str(img_file),
                    allow_failover=False,
                    deadline=t0 + single_call_budget,
                )
        elapsed = time.monotonic() - t0
        self.assertLessEqual(elapsed, single_call_budget + 0.15)
        self.assertTrue(all(t <= single_call_budget + 0.01 for t in observed_timeouts))
        # Second attempt timeout must be strictly smaller than first attempt timeout
        if len(observed_timeouts) >= 2:
            self.assertLess(observed_timeouts[1], observed_timeouts[0])

    def test_audit_m5_start_everything_cp1252_stdout_and_m8_zero_default_log_leak(self):
        import start_everything

        # M5: start_everything.py output redirected to a strict cp1252 file/stream never raises UnicodeEncodeError
        cp1252_file = TEST_TMP / "cp1252_stdout.txt"
        raw_buf = io.BytesIO()
        cp1252_wrapper = io.TextIOWrapper(raw_buf, encoding="cp1252", errors="strict")
        old_stdout = sys.stdout
        try:
            sys.stdout = cp1252_wrapper
            starter = start_everything.LatentSpaceStarter(mode="server", port=8000, https_port=8443)
            with mock.patch.object(starter, "start_server_and_pipeline"), \
                 mock.patch.object(starter, "monitor_processes"), \
                 mock.patch.object(starter, "stop_all"), \
                 mock.patch.object(starter, "get_ip_address", return_value="192.168.1.50"):
                starter.run()
            cp1252_wrapper.flush()
        finally:
            sys.stdout = old_stdout
        decoded_out = raw_buf.getvalue().decode("cp1252", errors="replace")
        self.assertIn("LATENT SPACE WEB SUITE RUNNING", decoded_out)

        # Also test subprocess execution with stdout redirected to a cp1252 file
        import subprocess
        with open(cp1252_file, "wb") as f_out:
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "cp1252:strict"
            proc = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "import start_everything; "
                        "print('  ✓ Unloaded Ollama model: test'); "
                        "print('  ⚡ Applying Ollama VRAM tuning'); "
                        "print('✅ LATENT SPACE WEB SUITE RUNNING! 🖼️ 👁️ 🖨️ 🎛️ 📱')"
                    ),
                ],
                stdout=f_out,
                stderr=subprocess.PIPE,
                env=env,
                timeout=10,
            )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr.decode("utf-8", errors="replace"))

        # M8: Running a pipeline under redirected LATENT_SPACE_LOGS_DIR writes zero bytes to the repo's logs/ directory
        repo_logs_dir = PROJECT_ROOT / "logs"
        before_snapshots = {}
        if repo_logs_dir.exists():
            for lf in repo_logs_dir.iterdir():
                if lf.is_file():
                    before_snapshots[lf.name] = lf.read_bytes()

        rid = "20261126_120000_999"
        snap = config_manager.SHARED_DIR / f"input_{rid}.png"
        canvas = np.ones((120, 120), dtype=np.uint8) * 255
        cv2.line(canvas, (10, 10), (110, 110), 0, 4)
        cv2.imwrite(str(snap), canvas)

        fake_model = mock.MagicMock()
        fake_model._provider = "ollama"
        fake_model._llm = "llama3.2"
        fake_model._last_used_provider = "ollama"
        fake_model._last_used_model = "llama3.2"
        fake_model.initialize.return_value = {"status": "Success"}
        fake_model.chat.side_effect = ["Redirected log test reading.", "Line one\nLine two\nLine three"]

        with mock.patch.object(run_analysis_watcher, "LLMModel", return_value=fake_model), \
             mock.patch.object(run_analysis_watcher, "save_print_pdf", return_value=None), \
             mock.patch.object(run_analysis_watcher, "get_translation_config", return_value={"enabled": False, "mode": "off", "target_languages": [], "default_print_language": "en"}):
            self.assertTrue(run_analysis_watcher.run_analysis_pipeline(run_id=rid, snapshot_path=str(snap)))

        if repo_logs_dir.exists():
            for name, before_bytes in before_snapshots.items():
                after_bytes = (repo_logs_dir / name).read_bytes()
                self.assertEqual(
                    after_bytes,
                    before_bytes,
                    f"Production log {name} was modified during redirected test run!",
                )
        self.assertIn(rid, (config_manager.LOGS_DIR / "installation.log").read_text(encoding="utf-8"))
        self.assertIn(rid, (config_manager.LOGS_DIR / "responses.log").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
