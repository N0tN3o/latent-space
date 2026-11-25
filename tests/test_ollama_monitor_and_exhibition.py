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

        # Admin UI includes live Ollama panel, unload/prewarm wiring, Smart Diagnostics, and Pipeline Stepper
        admin_html = (PROJECT_ROOT / "web_ui" / "admin.html").read_text(encoding="utf-8")
        self.assertIn("ollama_runtime", admin_html)
        self.assertIn("unloadOllamaModel", admin_html)
        self.assertIn("/api/ollama/unload", admin_html)
        self.assertIn("/api/ollama/prewarm", admin_html)
        self.assertIn("smartDiagCard", admin_html)
        self.assertIn("applySmartFix", admin_html)
        self.assertIn("aiPipelineStepper", admin_html)

        # /draw security & cross-device (Desktop/Android/iOS) camera checks
        self.assertIn("drawStaffModal", draw_html)
        self.assertIn("requestStaffControls", draw_html)
        self.assertIn("lockStaffControls", draw_html)
        self.assertIn("/api/auth/login", draw_html)
        self.assertIn("webkit-playsinline", draw_html)
        self.assertIn("pip-box.is-hidden", draw_html)
        self.assertIn("requestCameraStreamCrossDevice", draw_html)
        self.assertIn("draw.err.cam_https", draw_html)
        self.assertIn("exRetryCamera", draw_html)
        # Ensure getUserMedia runs BEFORE MediaPipe FaceLandmarker inside startWebcamGaze
        start_idx = draw_html.index("window.startWebcamGaze = async function")
        gum_idx = draw_html.index("requestCameraStreamCrossDevice()", start_idx)
        mp_idx = draw_html.index("ensureFaceLandmarker('GPU')", start_idx)
        self.assertLess(gum_idx, mp_idx)

        # Gallery minimal exhibition corner & popover
        index_html = (PROJECT_ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn("galleryMenuBtn", index_html)
        self.assertIn("galleryMenuPopover", index_html)
        self.assertIn("singleScreenDrawBtn", index_html)
        # Language switcher must sit to the right of the menu button to prevent EN/HU shift
        self.assertLess(index_html.index('id="galleryMenuBtn"'), index_html.index('id="galleryLangSwitch"'))

    def test_fast_localhost_normalization_stdlib_stats_and_offline_manifests(self):
        # 1. localhost -> 127.0.0.1 normalization eliminates Windows IPv6 stall
        self.assertEqual(
            monitoring_server._fast_local_ollama_url("http://localhost:11434"),
            "http://127.0.0.1:11434",
        )
        self.assertEqual(
            monitoring_server._fast_local_ollama_url("http://192.168.1.20:11434"),
            "http://192.168.1.20:11434",
        )

        # 2. System stats return real disk & non-zero memory (or None for CPU) even without psutil
        with mock.patch.object(monitoring_server, "psutil", None):
            stats = monitoring_server.get_system_stats()
        self.assertIsNone(stats["cpu_percent"])
        self.assertGreater(stats["disk_usage"], 0.0)

        # 3. Offline Ollama manifest layer size fallback when Ollama HTTP server is stopped
        fake_home = self.tmp_root / "fake_home"
        manifest_file = fake_home / ".ollama" / "models" / "manifests" / "registry.ollama.ai" / "library" / "gemma4" / "e4b"
        manifest_file.parent.mkdir(parents=True, exist_ok=True)
        manifest_file.write_text(json.dumps({
            "config": {"size": 1024},
            "layers": [
                {"size": 5 * (1024 ** 3)},
                {"size": 1 * (1024 ** 3)},
            ],
        }), encoding="utf-8")
        with mock.patch.object(monitoring_server.urllib.request, "urlopen", side_effect=OSError("refused")), \
             mock.patch.object(monitoring_server.Path, "home", return_value=fake_home):
            sizes = monitoring_server.get_ollama_model_sizes(force=True)
        self.assertAlmostEqual(sizes.get("gemma4:e4b", 0.0), 6.0, places=2)

    def test_chat_ollama_cold_timeout_no_retry_and_thinking_cap(self):
        import chat
        import requests

        config_manager.update_settings_section("ai", {
            "vision_mode": "stroke_metrics",
            "analysis": {
                "provider": "ollama",
                "model": "gemma4:e4b",
                "base_url": "http://localhost:11434/v1",
                "thinking_mode": "auto",
                "max_tokens": 0,
                "fallback_provider": "none",
            },
        })

        # 1. Cold model uses OLLAMA_COLD_READ_TIMEOUT_SEC (120s) and NEVER retries ReadTimeout
        seen_timeouts = []
        seen_payloads = []

        def fake_post_timeout(url, json=None, headers=None, timeout=None):
            seen_timeouts.append(timeout)
            seen_payloads.append(dict(json or {}))
            raise requests.exceptions.ReadTimeout("Read timed out")

        with mock.patch.object(chat, "_is_ollama_model_loaded", return_value=False), \
             mock.patch.object(chat.requests, "post", side_effect=fake_post_timeout):
            m = chat.LLMModel("analysis")
            with self.assertRaises(chat.AIGenerationError):
                m.chat("Interpret drawing", allow_failover=False)

        # Exactly 1 chat attempt (no destructive retry that kills llama-server mid-load)
        self.assertEqual(len(seen_timeouts), 1)
        self.assertGreaterEqual(seen_timeouts[0][1], 115.0)
        # Auto safety cap prevents unbounded thinking loops on boolean-thinking models
        self.assertEqual(seen_payloads[0].get("reasoning_effort"), "low")
        self.assertEqual(seen_payloads[0].get("max_tokens"), chat.OLLAMA_AUTO_THINKING_MAX_TOKENS["analysis"])

        # 2. Warm model uses OLLAMA_WARM_READ_TIMEOUT_SEC (75s)
        warm_timeouts = []
        ok_resp = mock.MagicMock(status_code=200, ok=True)
        ok_resp.json.return_value = {"choices": [{"message": {"content": "Warm reply."}}]}

        def fake_post_warm(url, json=None, headers=None, timeout=None):
            warm_timeouts.append(timeout)
            return ok_resp

        with mock.patch.object(chat, "_is_ollama_model_loaded", return_value=True), \
             mock.patch.object(chat.requests, "post", side_effect=fake_post_warm):
            reply = m.chat("Interpret drawing", allow_failover=False)
        self.assertEqual(reply, "Warm reply.")
        self.assertAlmostEqual(warm_timeouts[0][1], chat.OLLAMA_WARM_READ_TIMEOUT_SEC, delta=1.0)

    def test_smart_diagnostics_prewarm_and_ollama_env_tuning(self):
        import start_everything

        # 1. Detect multi-model VRAM thrashing + unbounded thinking + cold load + tight VRAM
        cfg = config_manager.load_settings()
        cfg["ai"]["vision_mode"] = "auto"
        cfg["ai"]["analysis"]["provider"] = "ollama"
        cfg["ai"]["analysis"]["model"] = "gemma4:e4b"
        cfg["ai"]["analysis"]["thinking_mode"] = "auto"
        cfg["ai"]["analysis"]["max_tokens"] = 0
        cfg["ai"]["poem"]["provider"] = "ollama"
        cfg["ai"]["poem"]["model"] = "llama3.1:8b"
        cfg["translation"]["enabled"] = True
        cfg["translation"]["mode"] = "native_llm"

        diag = monitoring_server.compute_smart_diagnostics(
            settings=cfg,
            gpu_stats={"available": True, "vram_total_gb": 8.0, "vram_used_gb": 0.0, "vram_percent": 0.0},
            ollama_runtime={"running": True, "loaded_models": []},
            model_sizes={"gemma4:e4b": 6.6, "llama3.1:8b": 4.9},
        )
        self.assertEqual(diag["status"], "critical")
        self.assertEqual(diag["summary"]["swaps_per_drawing"], 3)
        issue_ids = {i["id"]: i for i in diag["issues"]}
        self.assertIn("vram_model_swap", issue_ids)
        self.assertIn("vram_tight_fit", issue_ids)
        self.assertIn("thinking_unbounded", issue_ids)
        self.assertIn("ollama_cold_model", issue_ids)
        swap_actions = {a["id"] for a in issue_ids["vram_model_swap"]["actions"]}
        self.assertIn("sync_poem_to_analysis", swap_actions)

        # 2. Live CPU spill detected from /api/ps
        diag_spill = monitoring_server.compute_smart_diagnostics(
            settings=cfg,
            gpu_stats={"available": True, "vram_total_gb": 8.0},
            ollama_runtime={
                "running": True,
                "loaded_models": [{"name": "gemma4:e4b", "gpu_percent": 70, "cpu_percent": 30, "ram_gb": 2.1}],
            },
            model_sizes={"gemma4:e4b": 6.6, "llama3.1:8b": 4.9},
        )
        self.assertIn("vram_cpu_spill_live", {i["id"] for i in diag_spill["issues"]})

        # 3. Clean single-model warm setup reports status == "ok"
        cfg_ok = json.loads(json.dumps(cfg))
        cfg_ok["ai"]["poem"]["model"] = "gemma4:e4b"
        cfg_ok["ai"]["analysis"]["thinking_mode"] = "off"
        cfg_ok["ai"]["poem"]["thinking_mode"] = "off"
        cfg_ok["translation"]["mode"] = "local_nmt"
        diag_ok = monitoring_server.compute_smart_diagnostics(
            settings=cfg_ok,
            gpu_stats={"available": True, "vram_total_gb": 12.0},
            ollama_runtime={
                "running": True,
                "loaded_models": [{"name": "gemma4:e4b", "gpu_percent": 100, "cpu_percent": 0, "ram_gb": 0.0}],
            },
            model_sizes={"gemma4:e4b": 6.6},
        )
        self.assertEqual(diag_ok["status"], "ok")
        self.assertEqual(diag_ok["summary"]["swaps_per_drawing"], 0)
        self.assertEqual(diag_ok["issues"], [])

        # 4. POST /api/ollama/prewarm auth + validation + zero-token payload
        st_unauth, _ = self._req("POST", "/api/ollama/prewarm", body={"model": "gemma4:e4b"})
        self.assertEqual(st_unauth, 401)
        auth = {"Authorization": "Bearer MonTestPass!99"}
        st_bad, _ = self._req("POST", "/api/ollama/prewarm", body={"model": "bad; cmd"}, headers=auth)
        self.assertEqual(st_bad, 400)

        captured_pw = {}

        class _OkResp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"models": []}'

        def fake_urlopen(req, timeout=None):
            if req.full_url.endswith("/api/generate"):
                captured_pw["url"] = req.full_url
                captured_pw["body"] = json.loads(req.data.decode("utf-8"))
            return _OkResp()

        with mock.patch.object(monitoring_server.urllib.request, "urlopen", side_effect=fake_urlopen):
            st_pw, raw_pw = self._req("POST", "/api/ollama/prewarm", body={"model": "gemma4:e4b"}, headers=auth)
        self.assertEqual(st_pw, 200)
        self.assertEqual(json.loads(raw_pw)["model"], "gemma4:e4b")
        self.assertEqual(captured_pw["body"], {"model": "gemma4:e4b", "prompt": "", "keep_alive": "30m"})

        # 5. start_everything VRAM env defaults honor user overrides
        with mock.patch.dict(os.environ, {"OLLAMA_KEEP_ALIVE": "10m"}, clear=False):
            os.environ.pop("OLLAMA_FLASH_ATTENTION", None)
            os.environ.pop("OLLAMA_KV_CACHE_TYPE", None)
            ol_env = start_everything._default_ollama_serve_env()
            self.assertEqual(ol_env["OLLAMA_FLASH_ATTENTION"], "1")
            self.assertEqual(ol_env["OLLAMA_KV_CACHE_TYPE"], "q8_0")
            self.assertNotIn("OLLAMA_KEEP_ALIVE", ol_env)

    def test_offline_lan_https_cert_generation_renewal_and_tls_server(self):
        import ssl

        # 1. Settings validation for https_enabled / https_port
        updated = config_manager.update_settings_section("server", {"https_enabled": True, "https_port": 8443})
        self.assertTrue(updated["server"]["https_enabled"])
        self.assertEqual(updated["server"]["https_port"], 8443)
        with self.assertRaises(ValueError):
            config_manager.validate_server_data({"https_port": 70000})

        if not config_manager._find_openssl_binary():
            self.skipTest("openssl binary not installed in this environment")

        # 2. Generate local CA + SAN server cert in isolated temp dir
        cert_dir = self.tmp_root / "certs"
        crt_p, key_p, ca_p = config_manager.ensure_lan_tls_cert(cert_dir=cert_dir, extra_ips=["192.168.55.10"])
        self.assertIsNotNone(crt_p)
        self.assertTrue(crt_p.is_file() and key_p.is_file() and ca_p.is_file())
        first_mtime = crt_p.stat().st_mtime_ns

        # Reuses existing cert when IPs are already covered
        crt_p2, _, _ = config_manager.ensure_lan_tls_cert(cert_dir=cert_dir, extra_ips=["192.168.55.10"])
        self.assertEqual(crt_p2.stat().st_mtime_ns, first_mtime)

        # Auto-regenerates leaf cert when a new LAN IP appears while reusing CA
        ca_bytes_before = ca_p.read_bytes()
        crt_p3, _, ca_p3 = config_manager.ensure_lan_tls_cert(cert_dir=cert_dir, extra_ips=["192.168.55.99"])
        self.assertEqual(ca_p3.read_bytes(), ca_bytes_before)
        meta = json.loads((cert_dir / "cert_meta.json").read_text(encoding="utf-8"))
        self.assertIn("192.168.55.99", meta["ips"])
        self.assertIn("127.0.0.1", meta["ips"])

        # 3. Full OpenSSL TLS 1.2+ handshake & IP SAN verification (127.0.0.1 and LAN IP) via MemoryBIO
        server_ctx = monitoring_server._build_https_ssl_context(crt_p3, key_p)
        client_ctx = ssl.create_default_context(cafile=str(ca_p3))

        for target_host in ("127.0.0.1", "192.168.55.99", "localhost"):
            c_in, c_out = ssl.MemoryBIO(), ssl.MemoryBIO()
            s_in, s_out = ssl.MemoryBIO(), ssl.MemoryBIO()
            c_obj = client_ctx.wrap_bio(c_in, c_out, server_side=False, server_hostname=target_host)
            s_obj = server_ctx.wrap_bio(s_in, s_out, server_side=True)
            for _ in range(16):
                c_done = s_done = False
                try:
                    c_obj.do_handshake()
                    c_done = True
                except ssl.SSLWantReadError:
                    pass
                data = c_out.read()
                if data:
                    s_in.write(data)
                try:
                    s_obj.do_handshake()
                    s_done = True
                except ssl.SSLWantReadError:
                    pass
                data = s_out.read()
                if data:
                    c_in.write(data)
                if c_done and s_done:
                    break
            self.assertIn(c_obj.version(), ("TLSv1.2", "TLSv1.3"))
            peer_cert = c_obj.getpeercert()
            self.assertTrue(peer_cert)

        # 4. Aborted/invalid TLS handshake in LockedThreadingHTTPSServer closes socket cleanly
        fake_sock = mock.MagicMock()
        bad_ctx = mock.MagicMock()
        bad_ctx.wrap_socket.side_effect = ssl.SSLError("aborted self-signed probe")
        dummy_https = object.__new__(monitoring_server.LockedThreadingHTTPSServer)
        dummy_https.ssl_context = bad_ctx
        monitoring_server.LockedThreadingHTTPSServer.process_request_thread(dummy_https, fake_sock, ("127.0.0.1", 12345))
        fake_sock.close.assert_called_once()

        # 5. /api/public/info surfaces https_port + https_url, and /api/tls/ca.crt serves ONLY public CA PEM
        prev_ca = monitoring_server._active_ca_cert_path
        prev_hp = monitoring_server._active_https_port
        monitoring_server._active_https_port = 8443
        monitoring_server._active_ca_cert_path = ca_p3
        try:
            st_info, raw_info = self._req("GET", "/api/public/info")
            self.assertEqual(st_info, 200)
            info = json.loads(raw_info)
            self.assertEqual(info["https_port"], 8443)
            self.assertTrue(info["https_url"].startswith("https://") and info["https_url"].endswith(":8443"))

            st_ca, raw_ca = self._req("GET", "/api/tls/ca.crt")
            self.assertEqual(st_ca, 200)
            self.assertIn(b"BEGIN CERTIFICATE", raw_ca)
            self.assertNotIn(b"PRIVATE KEY", raw_ca)
        finally:
            monitoring_server._active_ca_cert_path = prev_ca
            monitoring_server._active_https_port = prev_hp


if __name__ == "__main__":
    unittest.main()
