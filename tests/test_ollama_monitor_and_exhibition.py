import http.client
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
for p in (str(PROJECT_ROOT), str(PROJECT_ROOT / "analysis_watcher")):
    if p not in sys.path:
        sys.path.insert(0, p)

import config_manager
import monitoring_server


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


class TestOllamaMonitorAndExhibitionMode(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_root = Path(self._tmp.name)
        self._prev_env = {
            k: os.environ.get(k)
            for k in (
                "LATENT_SPACE_SHARED_DIR",
                "LATENT_SPACE_LOGS_DIR",
                "LATENT_SPACE_CONFIG_DIR",
                "LATENT_SPACE_ADMIN_PASSWORD",
            )
        }
        os.environ["LATENT_SPACE_SHARED_DIR"] = str(self.tmp_root / "shared")
        os.environ["LATENT_SPACE_LOGS_DIR"] = str(self.tmp_root / "logs")
        os.environ["LATENT_SPACE_CONFIG_DIR"] = str(self.tmp_root / "config")
        os.environ["LATENT_SPACE_ADMIN_PASSWORD"] = "MonTestPass!99"
        config_manager.reload_paths_from_env()
        config_manager.ensure_directories()
        config_manager.save_settings(config_manager.DEFAULT_SETTINGS)

        with monitoring_server._ollama_runtime_lock:
            monitoring_server._ollama_runtime_cache["data"] = None
            monitoring_server._ollama_runtime_cache["ts"] = 0.0
        with monitoring_server._gpu_stats_lock:
            monitoring_server._gpu_stats_cache["data"] = None
            monitoring_server._gpu_stats_cache["ts"] = 0.0
        with monitoring_server._auth_lock:
            monitoring_server._admin_sessions.clear()
            monitoring_server._login_failures.clear()

    def tearDown(self):
        for k, v in self._prev_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        config_manager.reload_paths_from_env()
        self._tmp.cleanup()

    def _req(self, method, path, body=None, headers=None):
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

    def test_ollama_runtime_ps_parsing_and_split_math(self):
        fake_ps_payload = {
            "models": [
                {
                    "name": "gemma4:e4b",
                    "size": 8 * (1024 ** 3),
                    "size_vram": 6 * (1024 ** 3),
                    "context_length": 8192,
                    "expires_at": "2099-01-01T00:00:00.123456789Z",
                    "details": {"parameter_size": "8.0B", "quantization_level": "Q4_K_M"},
                }
            ]
        }

        class _FakeResp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps(fake_ps_payload).encode("utf-8")

        with mock.patch.object(monitoring_server.urllib.request, "urlopen", return_value=_FakeResp()):
            rt = monitoring_server.get_ollama_runtime_status(force=True)

        self.assertTrue(rt["running"])
        self.assertEqual(len(rt["loaded_models"]), 1)
        m = rt["loaded_models"][0]
        self.assertEqual(m["name"], "gemma4:e4b")
        self.assertEqual(m["size_gb"], 8.0)
        self.assertEqual(m["vram_gb"], 6.0)
        self.assertEqual(m["ram_gb"], 2.0)
        self.assertEqual(m["gpu_percent"], 75)
        self.assertEqual(m["cpu_percent"], 25)
        self.assertEqual(m["context_length"], 8192)
        self.assertEqual(m["quantization"], "Q4_K_M")
        self.assertIsNotNone(m["expires_in_sec"])
        self.assertGreater(m["expires_in_sec"], 0)
        self.assertEqual(rt["total_size_gb"], 8.0)
        self.assertEqual(rt["total_vram_gb"], 6.0)
        self.assertEqual(rt["total_ram_gb"], 2.0)

        # Pinned / far-future year >= 2200 parses to None (Pinned)
        self.assertIsNone(monitoring_server._parse_iso_expires_in_sec("2316-01-01T00:00:00Z"))

    def test_gpu_stats_parsing_and_graceful_fallback(self):
        fake_proc = mock.MagicMock(
            returncode=0,
            stdout="NVIDIA GeForce RTX 4060 Laptop GPU, 42, 6144, 8192, 63\n",
        )
        with mock.patch.object(monitoring_server.shutil, "which", return_value="nvidia-smi"), \
             mock.patch.object(monitoring_server.subprocess, "run", return_value=fake_proc):
            gpu = monitoring_server.get_gpu_stats(force=True)

        self.assertTrue(gpu["available"])
        self.assertIn("RTX 4060", gpu["name"])
        self.assertEqual(gpu["utilization_percent"], 42.0)
        self.assertEqual(gpu["vram_used_mb"], 6144)
        self.assertEqual(gpu["vram_total_mb"], 8192)
        self.assertEqual(gpu["vram_percent"], 75.0)
        self.assertEqual(gpu["temperature_c"], 63.0)

        with mock.patch.object(monitoring_server.shutil, "which", return_value=None):
            gpu_none = monitoring_server.get_gpu_stats(force=True)
        self.assertFalse(gpu_none["available"])

    def test_ollama_unload_endpoint_auth_validation_and_status_fields(self):
        # 1. Unauthenticated unload is blocked with 401
        st_unauth, _ = self._req("POST", "/api/ollama/unload", body={"model": "gemma4:e4b"})
        self.assertEqual(st_unauth, 401)

        auth = {"Authorization": "Bearer MonTestPass!99"}

        # 2. Invalid model name rejected with 400
        st_bad, _ = self._req("POST", "/api/ollama/unload", body={"model": "bad; rm -rf /"}, headers=auth)
        self.assertEqual(st_bad, 400)

        # 3. Valid unload sends keep_alive: 0 to /api/generate
        captured = {}

        class _OkResp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b"{}"

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return _OkResp()

        with mock.patch.object(monitoring_server.urllib.request, "urlopen", side_effect=fake_urlopen):
            st_ok, raw_ok = self._req("POST", "/api/ollama/unload", body={"model": "gemma4:e4b"}, headers=auth)

        self.assertEqual(st_ok, 200)
        self.assertEqual(json.loads(raw_ok)["model"], "gemma4:e4b")
        self.assertTrue(captured["url"].endswith("/api/generate"))
        self.assertEqual(captured["body"], {"model": "gemma4:e4b", "keep_alive": 0})

        # 4. /api/status surfaces ollama_runtime, gpu, and last_timings
        config_manager.write_content_json({
            "status": "ok",
            "runId": "20261125_150000_001",
            "stageTimings": {"vision_sec": 3.2, "analysis_sec": 5.8, "poem_sec": 2.9, "translation_sec": 7.1, "total_sec": 20.5},
        })
        with mock.patch.object(monitoring_server, "get_ollama_runtime_status", return_value={"running": True, "loaded_models": []}), \
             mock.patch.object(monitoring_server, "get_gpu_stats", return_value={"available": True, "vram_percent": 50.0, "temperature_c": 58.0}):
            st_status, raw_status = self._req("GET", "/api/status", headers=auth)
        self.assertEqual(st_status, 200)
        payload = json.loads(raw_status)
        self.assertIn("ollama_runtime", payload)
        self.assertIn("gpu", payload)
        self.assertEqual(payload["last_timings"]["total_sec"], 20.5)

    def test_draw_exhibition_mode_defaults_dual_and_single_screen_markers(self):
        draw_html = (PROJECT_ROOT / "web_ui" / "draw.html").read_text(encoding="utf-8")
        # Default Exhibition Mode ON unless ?exhibition=0 / ?operator=1
        self.assertIn("rawExParam === '0'", draw_html)
        self.assertIn("rawSsParam === '1'", draw_html)
        self.assertIn("exOpenTutorial", draw_html)
        self.assertIn("startExhibitionCountdown", draw_html)
        self.assertIn("toggleDisplayMode", draw_html)
        self.assertIn("draw.ex.done_qr_callout", draw_html)
        self.assertIn("draw.ex.open_gallery_btn", draw_html)

        # Admin UI includes live Ollama panel and unload button wiring
        admin_html = (PROJECT_ROOT / "web_ui" / "admin.html").read_text(encoding="utf-8")
        self.assertIn("ollama_runtime", admin_html)
        self.assertIn("unloadOllamaModel", admin_html)
        self.assertIn("/api/ollama/unload", admin_html)


if __name__ == "__main__":
    unittest.main()
