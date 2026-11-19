import base64
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

import cv2
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config_manager
import monitoring_server
import start_everything

TEST_TMP = None


def setUpModule():
    global TEST_TMP
    TEST_TMP = Path(tempfile.mkdtemp(prefix="ls_lockdown_test_"))
    os.environ["LATENT_SPACE_CONFIG_DIR"] = str(TEST_TMP / "config")
    os.environ["LATENT_SPACE_SHARED_DIR"] = str(TEST_TMP / "shared_output")
    os.environ["LATENT_SPACE_LOGS_DIR"] = str(TEST_TMP / "logs")
    os.environ["LATENT_SPACE_ADMIN_PASSWORD"] = "TestLockdownPass!42"
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
        if "r" in mode:
            return self._rfile
        return self._wfile

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


class TestConfigAndValidation(unittest.TestCase):
    def setUp(self):
        config_manager.ensure_directories()
        config_manager.save_settings(config_manager.DEFAULT_SETTINGS)

    def test_admin_password_env_and_generated_pin_file(self):
        pw, src = config_manager.get_or_create_admin_password()
        self.assertEqual(pw, "TestLockdownPass!42")
        self.assertEqual(src, "env:LATENT_SPACE_ADMIN_PASSWORD")

        with mock.patch.dict(os.environ, {"LATENT_SPACE_ADMIN_PASSWORD": "", "ADMIN_PASSWORD": "", "ADMIN_PIN": ""}, clear=False):
            if config_manager.ADMIN_PIN_FILE.exists():
                config_manager.ADMIN_PIN_FILE.unlink()
            gen_pw, gen_src = config_manager.get_or_create_admin_password()
            self.assertEqual(len(gen_pw), 8)
            self.assertEqual(gen_src, str(config_manager.ADMIN_PIN_FILE))
            gen_pw_2, _ = config_manager.get_or_create_admin_password()
            self.assertEqual(gen_pw, gen_pw_2)

    def test_save_settings_preserves_existing_keys_on_partial_update(self):
        config_manager.save_settings({"ai": {"analysis": {"model": "custom-model-x", "api_key": "secret-123"}}})
        updated = config_manager.save_settings({"printing": {"auto_print": True}})
        self.assertTrue(updated["printing"]["auto_print"])
        self.assertEqual(updated["ai"]["analysis"]["model"], "custom-model-x")
        self.assertEqual(updated["ai"]["analysis"]["api_key"], "secret-123")
        masked = config_manager.mask_settings_secrets(updated)
        self.assertEqual(masked["ai"]["analysis"]["api_key"], "***")
        self.assertTrue(masked["ai"]["analysis"]["has_api_key"])

        # Explicit empty string clears saved key, and temperature=0 is accepted
        cleared = config_manager.update_settings_section("ai", {"analysis": {"api_key": "", "temperature": 0}})
        self.assertEqual(cleared["ai"]["analysis"]["api_key"], "")
        self.assertEqual(cleared["ai"]["analysis"]["temperature"], 0.0)

    def test_config_validation_rejects_invalid_ai_tts_server(self):
        with self.assertRaises(ValueError):
            config_manager.update_settings_section("ai", {"analysis": {"temperature": "hot"}})
        with self.assertRaises(ValueError):
            config_manager.update_settings_section("ai", {"analysis": {"provider": "unknown_llm"}})
        with self.assertRaises(ValueError):
            config_manager.update_settings_section("tts", {"provider": "bad_tts"})
        with self.assertRaises(ValueError):
            config_manager.update_settings_section("server", {"port": 99999})

    def test_env_empty_value_does_not_shadow_and_parses_export_comments(self):
        root_env = TEST_TMP / "root.env"
        sub_env = TEST_TMP / "sub.env"
        root_env.write_text("GEMINI_API_KEY=\nexport PORT=8080 # inline comment\n", encoding="utf-8")
        sub_env.write_text("GEMINI_API_KEY='real-key-123'\n", encoding="utf-8")

        with mock.patch.dict(os.environ, {}, clear=True):
            env = {}
            for p in (root_env, sub_env):
                for k, v in config_manager._read_env_file_cached(p).items():
                    if k not in env or (not str(env[k]).strip() and str(v).strip()):
                        env[k] = v
            self.assertEqual(env["GEMINI_API_KEY"], "real-key-123")
            self.assertEqual(env["PORT"], "8080")

    def test_validate_safe_url(self):
        self.assertEqual(config_manager.validate_safe_url("https://kasboek.nl"), "https://kasboek.nl")
        self.assertEqual(config_manager.validate_safe_url("/kasboek-logo.jpg"), "/kasboek-logo.jpg")
        self.assertEqual(config_manager.validate_safe_url(""), "")
        for bad in ("javascript:alert(1)", "data:text/html,<script>", "//evil.com/x", "vbscript:msgbox(1)"):
            with self.assertRaises(ValueError):
                config_manager.validate_safe_url(bad)

    def test_validate_printer_name(self):
        self.assertEqual(config_manager.validate_printer_name("HP LaserJet Pro (Lobby)"), "HP LaserJet Pro (Lobby)")
        for bad in ("HP'; calc.exe; '", 'HP" | whoami', "HP$(calc)", "HP`id`", "HP\nwhoami"):
            with self.assertRaises(ValueError):
                config_manager.validate_printer_name(bad)

    def test_vision_error_secret_redaction(self):
        sys.path.insert(0, str(PROJECT_ROOT / "analysis_watcher"))
        import analysis as vision_analysis

        raw = "Failed connecting to https://admin:supersecret@ollama.local/api/generate?api_key=sk-12345&token=tok999"
        redacted = vision_analysis._redact_error_secrets(raw)
        self.assertNotIn("supersecret", redacted)
        self.assertNotIn("sk-12345", redacted)
        self.assertNotIn("tok999", redacted)
        self.assertIn("https://***@ollama.local", redacted)


class TestServerLockdownHTTP(unittest.TestCase):
    def setUp(self):
        config_manager.ensure_directories()
        config_manager.save_settings(config_manager.DEFAULT_SETTINGS)
        with monitoring_server._auth_lock:
            monitoring_server._admin_sessions.clear()
            monitoring_server._login_failures.clear()
            monitoring_server._upload_timestamps.clear()

    def _req(self, method, path, body=None, headers=None, client_ip="127.0.0.1"):
        hdr_list = [("Host", "localhost")]
        if headers:
            if isinstance(headers, dict):
                hdr_list.extend(headers.items())
            else:
                hdr_list.extend(headers)
        raw_body = b""
        if body is not None:
            if isinstance(body, (dict, list)):
                raw_body = json.dumps(body).encode("utf-8")
                if not any(k.lower() == "content-type" for k, _ in hdr_list):
                    hdr_list.append(("Content-Type", "application/json"))
            else:
                raw_body = body
            if not any(k.lower() in ("transfer-encoding", "content-length") for k, _ in hdr_list):
                hdr_list.append(("Content-Length", str(len(raw_body))))

        header_lines = [f"{method} {path} HTTP/1.1"] + [f"{k}: {v}" for k, v in hdr_list]
        raw_req = ("\r\n".join(header_lines) + "\r\n\r\n").encode("utf-8") + raw_body
        sock = _FakeSocket(raw_req)
        monitoring_server.LatentSpaceRequestHandler(sock, (client_ip, 54321), _DummyServer())
        raw_resp = sock._wfile.getvalue()

        resp = http.client.HTTPResponse(_TestableHTTPResponseSocket(raw_resp))
        resp.begin()
        data = resp.read()
        resp_headers = list(resp.getheaders())
        resp.close()
        return resp.status, resp_headers, data

    def test_public_pages_and_security_headers(self):
        for route in ("/", "/draw", "/kiosk", "/admin", "/api/auth/status", "/api/public/info", "/api/pipeline/status", "/api/files/prints"):
            status, hdrs, _ = self._req("GET", route)
            self.assertEqual(status, 200, f"Expected 200 for {route}, got {status}")
            lower_hdrs = {k.lower(): v for k, v in hdrs}
            self.assertEqual(lower_hdrs.get("x-content-type-options"), "nosniff")
            self.assertEqual(lower_hdrs.get("x-frame-options"), "SAMEORIGIN")
            self.assertEqual(lower_hdrs.get("referrer-policy"), "same-origin")

        for public_html in ("/", "/draw", "/kiosk"):
            _, _, body = self._req("GET", public_html)
            self.assertNotIn(b'href="/admin"', body, f"{public_html} must not link to /admin")

        # Offline QR SVG endpoint works locally
        qr_st, qr_hdrs, qr_body = self._req("GET", "/api/qr.svg?url=http%3A%2F%2Flocalhost%3A8000%2Fkiosk")
        self.assertEqual(qr_st, 200)
        self.assertIn(b"<svg", qr_body)

        # Local branding assets under assets/ are served when present, and traversal is always blocked
        vv_asset = PROJECT_ROOT / "assets" / "veszprem-vibe" / "vv-skyline-on-paper.svg"
        if vv_asset.exists():
            vv_st, vv_hdrs, vv_body = self._req("GET", "/assets/veszprem-vibe/vv-skyline-on-paper.svg")
            self.assertEqual(vv_st, 200)
            vv_lower = {k.lower(): v for k, v in vv_hdrs}
            self.assertIn("image/svg+xml", vv_lower.get("content-type", ""))
            self.assertIn("default-src 'none'", vv_lower.get("content-security-policy", ""))
            self.assertIn("sandbox", vv_lower.get("content-security-policy", ""))
            self.assertIn(b"<svg", vv_body)

        kas_asset = PROJECT_ROOT / "assets" / "kasboek" / "kasboek-logo.jpg"
        if kas_asset.exists():
            kas_hidden_st, _, _ = self._req("GET", "/assets/kasboek/kasboek-logo.jpg")
            self.assertEqual(kas_hidden_st, 204)
            # Case-variant bypass check for case-insensitive filesystems (Windows/macOS)
            kas_upper_st, _, _ = self._req("GET", "/assets/KASBOEK/kasboek-logo.jpg")
            self.assertEqual(kas_upper_st, 204)
            kas_prev_st, _, kas_prev_body = self._req("GET", "/assets/kasboek/kasboek-logo.jpg?preview=1")
            self.assertEqual(kas_prev_st, 200)
            self.assertTrue(kas_prev_body.startswith(b"\xff\xd8\xff"))

        vv_trav_st, _, _ = self._req("GET", "/assets/../config/settings.json")
        self.assertEqual(vv_trav_st, 403)

    def test_printing_settings_hides_printer_name_when_unauthenticated(self):
        config_manager.update_settings_section("printing", {"auto_print": False, "printer_name": "SecretLobbyPrinter"})
        status, _, data = self._req("GET", "/api/settings/printing")
        self.assertEqual(status, 200)
        parsed = json.loads(data)
        self.assertNotIn("printer_name", parsed)

    def test_admin_endpoints_reject_unauthenticated(self):
        for get_path in ("/api/status", "/api/monitoring", "/api/settings", "/api/settings/tts", "/api/ai/config", "/api/ai/availability", "/api/ai/vision", "/api/logs/installation", "/api/logs/vision"):
            status, _, _ = self._req("GET", get_path)
            self.assertEqual(status, 401, f"GET {get_path} should return 401 when unauthenticated")

        post_cases = [
            ("/api/pipeline/run", {}),
            ("/api/settings", {"printing": {"auto_print": True}}),
            ("/api/settings/attributions", {"enabled": True}),
            ("/api/settings/printing", {"auto_print": True}),
            ("/api/settings/tts", {"enabled": False}),
            ("/api/ai/config", {"model_type": "analysis", "config": {}}),
            ("/api/ai/vision", {"vision_mode": "auto"}),
            ("/api/ai/vision/test", {}),
            ("/api/ai/test", {"model_type": "analysis"}),
            ("/api/ollama/stop", {}),
            ("/api/files/print", {"filename": "print_test.pdf", "mark_only": True}),
            ("/api/files/delete", {"action": "all"}),
        ]
        for post_path, payload in post_cases:
            status, _, _ = self._req("POST", post_path, body=payload)
            self.assertEqual(status, 401, f"POST {post_path} should return 401 when unauthenticated")

    def test_login_cookie_session_and_logout(self):
        status, _, _ = self._req("POST", "/api/auth/login", body={"password": "wrong"})
        self.assertEqual(status, 401)

        status, hdrs, _ = self._req("POST", "/api/auth/login", body={"password": "TestLockdownPass!42"})
        self.assertEqual(status, 200)
        set_cookies = [v for k, v in hdrs if k.lower() == "set-cookie"]
        self.assertTrue(set_cookies)
        set_cookie = set_cookies[0]
        self.assertIn("ls_admin_session=", set_cookie)
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("SameSite=Strict", set_cookie)
        cookie_pair = set_cookie.split(";", 1)[0]

        status, _, status_data = self._req("GET", "/api/status", headers={"Cookie": cookie_pair})
        self.assertEqual(status, 200)
        self.assertIn("services", json.loads(status_data))

        status, _, avail_data = self._req("GET", "/api/ai/availability", headers={"Cookie": cookie_pair})
        self.assertEqual(status, 200)
        avail = json.loads(avail_data)
        self.assertIn("vision_modes", avail)
        self.assertTrue(avail["vision_modes"]["stroke_metrics"]["available"])
        self.assertTrue(avail["vision_modes"]["auto"]["available"])
        self.assertIn("ollama", avail)
        self.assertIn("env_keys", avail)
        self.assertTrue(all(isinstance(v, bool) for v in avail["env_keys"].values()))
        self.assertTrue(all(isinstance(v, bool) for v in avail["saved_keys"].values()))

        # Vision settings GET / POST / test endpoints
        st_v_save, _, v_save_body = self._req(
            "POST",
            "/api/ai/vision",
            body={
                "vision_mode": "stroke_metrics",
                "ollama_vision_model": "moondream",
                "vision": {"ollama_temperature": 0.3, "ollama_max_tokens": 64, "badge_style": "combined"},
            },
            headers={"Cookie": cookie_pair},
        )
        self.assertEqual(st_v_save, 200)
        self.assertEqual(json.loads(v_save_body)["vision"]["badge_style"], "combined")

        for bad_vision_payload in (
            {"vision": {"ollama_temperature": 9.9}},
            {"bogus_top_key": 1},
            {"vision": {"bogus_nested_key": 1}},
            {"ollama_vision_model": "moondream; rm -rf /"},
        ):
            st_v_bad, _, _ = self._req(
                "POST",
                "/api/ai/vision",
                body=bad_vision_payload,
                headers={"Cookie": cookie_pair},
            )
            self.assertEqual(st_v_bad, 400, f"Expected 400 for invalid vision payload {bad_vision_payload}")

        st_v_test, _, v_test_body = self._req("POST", "/api/ai/vision/test", body={}, headers={"Cookie": cookie_pair})
        self.assertEqual(st_v_test, 200)
        self.assertEqual(json.loads(v_test_body)["status"], "success")

        status, _, print_data = self._req("GET", "/api/settings/printing", headers={"Cookie": cookie_pair})
        self.assertEqual(status, 200)
        self.assertIn("printer_name", json.loads(print_data))

        # Invalid URL & printer injection & unknown AI keys rejected cleanly with 400
        status, _, _ = self._req(
            "POST",
            "/api/settings/attributions",
            body={"website_url": "javascript:alert(1)"},
            headers={"Cookie": cookie_pair},
        )
        self.assertEqual(status, 400)

        # Veszprem Vibe attribution toggle + website/linkedin/facebook validation
        status, _, _ = self._req(
            "POST",
            "/api/settings/attributions",
            body={"veszprem_vibe": {"enabled": True, "facebook_url": "javascript:alert(1)"}},
            headers={"Cookie": cookie_pair},
        )
        self.assertEqual(status, 400)

        status, _, _ = self._req(
            "POST",
            "/api/settings/attributions",
            body={"veszprem_vibe": {"bogus_vv_key": "injected"}},
            headers={"Cookie": cookie_pair},
        )
        self.assertEqual(status, 400)

        status, _, vv_save_data = self._req(
            "POST",
            "/api/settings/attributions",
            body={
                "veszprem_vibe": {
                    "enabled": True,
                    "show_logo": True,
                    "logo_path": "/assets/veszprem-vibe/vv-mark.svg",
                    "website_url": "https://veszpremvibe.hu",
                    "linkedin_url": "https://www.linkedin.com/company/veszprem-vibe",
                    "facebook_url": "https://www.facebook.com/veszpremvibe",
                }
            },
            headers={"Cookie": cookie_pair},
        )
        self.assertEqual(status, 200)
        vv_saved = json.loads(vv_save_data)["attributions"]["veszprem_vibe"]
        self.assertTrue(vv_saved["enabled"])
        self.assertEqual(vv_saved["website_url"], "https://veszpremvibe.hu")
        self.assertEqual(vv_saved["linkedin_url"], "https://www.linkedin.com/company/veszprem-vibe")
        self.assertEqual(vv_saved["facebook_url"], "https://www.facebook.com/veszpremvibe")

        status, _, status_after_vv = self._req("GET", "/api/status", headers={"Cookie": cookie_pair})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(status_after_vv)["attributions"]["veszprem_vibe_enabled"])

        status, _, _ = self._req(
            "POST",
            "/api/settings/printing",
            body={"printer_name": "Bad'; calc.exe; '"},
            headers={"Cookie": cookie_pair},
        )
        self.assertEqual(status, 400)

        status, _, _ = self._req(
            "POST",
            "/api/ai/config",
            body={"model_type": "analysis", "config": {"bogus_key": 123}},
            headers={"Cookie": cookie_pair},
        )
        self.assertEqual(status, 400)

        # Logout revokes session
        status, _, _ = self._req("POST", "/api/auth/logout", headers={"Cookie": cookie_pair})
        self.assertEqual(status, 200)
        status, _, _ = self._req("GET", "/api/status", headers={"Cookie": cookie_pair})
        self.assertEqual(status, 401)

    def test_login_rate_limiting_and_proxy_ip(self):
        for _ in range(monitoring_server.LOGIN_MAX_FAILURES):
            status, _, _ = self._req("POST", "/api/auth/login", body={"password": "bad"}, client_ip="10.0.0.99")
            self.assertEqual(status, 401)
        status, hdrs, _ = self._req("POST", "/api/auth/login", body={"password": "TestLockdownPass!42"}, client_ip="10.0.0.99")
        self.assertEqual(status, 429)
        self.assertIn("retry-after", {k.lower(): v for k, v in hdrs})

        # Behind trusted proxy, X-Forwarded-For separates distinct visitors
        with mock.patch.dict(os.environ, {"LATENT_SPACE_TRUST_PROXY": "1"}):
            st_ok, _, _ = self._req(
                "POST", "/api/auth/login",
                body={"password": "TestLockdownPass!42"},
                headers={"X-Forwarded-For": "203.0.113.55"},
                client_ip="10.0.0.99",
            )
            self.assertEqual(st_ok, 200)

    def test_bearer_token_and_traversal_and_upload_guards(self):
        status, _, _ = self._req("GET", "/shared_output/.secret")
        self.assertEqual(status, 403)
        status, _, _ = self._req("GET", "/shared_output/../config/settings.json")
        self.assertEqual(status, 403)

        status, _, _ = self._req("GET", "/api/settings", headers={"Authorization": "Bearer TestLockdownPass!42"})
        self.assertEqual(status, 200)

        # Non-image upload rejected
        bad_b64 = base64.b64encode(b"<html><script>alert(1)</script></html>").decode("ascii")
        status, _, _ = self._req("POST", "/api/drawings", body={"image": bad_b64})
        self.assertEqual(status, 400)

        # Decompression bomb / oversized PNG dimensions rejected before OpenCV decode
        from PIL import Image
        big_png_header = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + (20000).to_bytes(4, "big") + (20000).to_bytes(4, "big") + b"\x08\x02\x00\x00\x00" + b"\x00" * 16
        bomb_b64 = base64.b64encode(big_png_header).decode("ascii")
        status, _, _ = self._req("POST", "/api/drawings", body={"image": bomb_b64})
        self.assertEqual(status, 400)

        # Oversized Content-Length rejected with 413
        status, _, _ = self._req(
            "POST",
            "/api/auth/login",
            body=b"{}",
            headers={"Content-Type": "application/json", "Content-Length": str(monitoring_server.MAX_JSON_BODY_BYTES + 10)},
        )
        self.assertEqual(status, 413)

        # Chunked Transfer-Encoding body parsing works
        chunk_payload = json.dumps({"password": "TestLockdownPass!42"}).encode("utf-8")
        chunked_raw = f"{len(chunk_payload):x}\r\n".encode("ascii") + chunk_payload + b"\r\n0\r\n\r\n"
        status, _, _ = self._req(
            "POST", "/api/auth/login",
            body=chunked_raw,
            headers={"Content-Type": "application/json", "Transfer-Encoding": "chunked"},
            client_ip="10.0.0.50",
        )
        self.assertEqual(status, 200)

    def test_delete_protection_print_listing_and_service_status(self):
        # Cannot delete print_history.json or nonexistent files
        hist_file = config_manager.DOCS_DIR / "print_history.json"
        hist_file.write_text('{"printed": [], "prints": []}', encoding="utf-8")
        auth_hdr = {"Authorization": "Bearer TestLockdownPass!42"}

        st, _, _ = self._req("POST", "/api/files/delete", body={"action": "specific", "filename": "print_history.json"}, headers=auth_hdr)
        self.assertEqual(st, 400)
        self.assertTrue(hist_file.exists())

        st, _, _ = self._req("POST", "/api/files/delete", body={"action": "specific", "filename": "print_99990101_000000_001.pdf"}, headers=auth_hdr)
        self.assertEqual(st, 404)

        # Missing PNG sidecars must NOT fall back to /shared_output/image.png
        pdf_only = config_manager.DOCS_DIR / "print_20260101_120000_001.pdf"
        pdf_only.write_bytes(b"%PDF-1.4\n%%EOF\n")
        st, _, body = self._req("GET", "/api/files/prints")
        self.assertEqual(st, 200)
        items = json.loads(body)
        match = [i for i in items if i["name"] == pdf_only.name][0]
        self.assertEqual(match["thumb_url"], "")
        self.assertEqual(match["heatmap_url"], "")

        # Substring process false positive check (tail -f run_analysis_watcher.log must NOT count)
        fake_procs = [("tail", ["tail", "-f", "run_analysis_watcher.log"])]
        self.assertEqual(monitoring_server.get_service_status("run_analysis_watcher", cmdlines=fake_procs), "stopped")

        # stop_ollama_serving must ignore Groq base_url when provider != ollama
        config_manager.update_settings_section("ai", {
            "analysis": {"provider": "openai_compatible", "base_url": "https://api.groq.com/openai/v1", "model": "llama-3.3-70b-versatile"},
            "poem": {"provider": "openai_compatible", "base_url": "https://api.groq.com/openai/v1", "model": "llama-3.3-70b-versatile"},
        })
        seen_urls = []
        def fake_urlopen(req, timeout=None):
            seen_urls.append(req.full_url)
            raise OSError("no network")
        with mock.patch("start_everything.urllib.request.urlopen", side_effect=fake_urlopen):
            start_everything.stop_ollama_serving(kill_external=False)
        self.assertTrue(all("groq.com" not in u for u in seen_urls))

    def test_upload_rate_limit_and_queue_full_guard(self):
        img = np.ones((32, 32), dtype=np.uint8) * 255
        _, buf = cv2.imencode(".png", img)
        img_b64 = base64.b64encode(buf.tobytes()).decode("ascii")

        # Upload rate limit triggers 429 ERR_RATE_LIMIT after UPLOAD_MAX_PER_WINDOW
        with mock.patch.object(monitoring_server, "enqueue_pipeline_job", return_value=("r1", 1)), \
             mock.patch.object(monitoring_server, "write_live_image_and_mark_handled"):
            for _ in range(monitoring_server.UPLOAD_MAX_PER_WINDOW):
                st, _, _ = self._req("POST", "/api/drawings", body={"image": img_b64}, client_ip="10.20.30.40")
                self.assertEqual(st, 200)
            st_lim, _, body_lim = self._req("POST", "/api/drawings", body={"image": img_b64}, client_ip="10.20.30.40")
            self.assertEqual(st_lim, 429)
            self.assertEqual(json.loads(body_lim)["error_code"], "ERR_RATE_LIMIT")

        # Bounded queue full returns 429 ERR_QUEUE_FULL
        fake_q = mock.MagicMock()
        fake_q.qsize.return_value = monitoring_server.MAX_PIPELINE_QUEUE_SIZE
        with mock.patch.object(monitoring_server, "_pipeline_queue", fake_q):
            st_q, _, body_q = self._req("POST", "/api/drawings", body={"image": img_b64}, client_ip="10.20.30.41")
            self.assertEqual(st_q, 429)
            self.assertEqual(json.loads(body_q)["error_code"], "ERR_QUEUE_FULL")

    def test_bearer_rate_limit_proxy_rightmost_multipart_and_env_port(self):
        # 1. Bearer brute-force attempts are rate limited with 429
        for _ in range(monitoring_server.LOGIN_MAX_FAILURES):
            st, _, _ = self._req(
                "GET", "/api/status",
                headers={"Authorization": "Bearer wrong-guess"},
                client_ip="10.30.40.50",
            )
            self.assertEqual(st, 401)
        st_blk, hdrs_blk, _ = self._req(
            "GET", "/api/status",
            headers={"Authorization": "Bearer TestLockdownPass!42"},
            client_ip="10.30.40.50",
        )
        self.assertEqual(st_blk, 429)
        self.assertIn("retry-after", {k.lower(): v for k, v in hdrs_blk})

        # 2. X-Forwarded-For takes rightmost proxy-appended IP to resist spoofing
        with mock.patch.dict(os.environ, {"LATENT_SPACE_TRUST_PROXY": "1"}):
            for _ in range(monitoring_server.LOGIN_MAX_FAILURES):
                self._req(
                    "POST", "/api/auth/login",
                    body={"password": "bad"},
                    headers={"X-Forwarded-For": "198.51.100.1, 203.0.113.88"},
                    client_ip="10.0.0.2",
                )
            st_spoof, _, _ = self._req(
                "POST", "/api/auth/login",
                body={"password": "TestLockdownPass!42"},
                headers={"X-Forwarded-For": "198.51.100.99, 203.0.113.88"},
                client_ip="10.0.0.2",
            )
            self.assertEqual(st_spoof, 429)

        # 3. .env PORT & remote_server_url in /api/public/info + /api/qr.svg UTF-8 limit + null byte guard
        with mock.patch("config_manager.load_env_vars", return_value={"PORT": "9123"}):
            self.assertEqual(config_manager.get_server_port(), 9123)
        with mock.patch("config_manager.load_env_vars", return_value={"PORT": ""}):
            self.assertEqual(config_manager.get_server_port(), 8000)

        config_manager.update_settings_section("server", {"remote_server_url": "https://art.example.org/"})
        st_info, _, info_body = self._req("GET", "/api/public/info")
        self.assertEqual(st_info, 200)
        self.assertEqual(json.loads(info_body)["lan_url"], "https://art.example.org")

        too_long = "https://art.example.org/" + ("é" * 110)
        from urllib.parse import quote
        st_qr_long, _, _ = self._req("GET", f"/api/qr.svg?url={quote(too_long)}")
        self.assertEqual(st_qr_long, 400)

        st_null, _, _ = self._req("GET", "/api/logs/installation%00.log")
        self.assertEqual(st_null, 400)

        # 4. POST /api/ai/test returns discovered _last_used_model
        fake_m = mock.MagicMock()
        fake_m._provider = "ollama"
        fake_m._llm = "missing-tag"
        fake_m._last_used_model = "qwen2.5:latest"
        fake_m.chat.return_value = "Online."
        with mock.patch.object(monitoring_server, "LLMModel", return_value=fake_m):
            st_test, _, body_test = self._req(
                "POST", "/api/ai/test",
                body={"model_type": "analysis"},
                headers={"Authorization": "Bearer TestLockdownPass!42"},
            )
        self.assertEqual(st_test, 200)
        self.assertEqual(json.loads(body_test)["model"], "qwen2.5:latest")

        # 5. Multipart upload parses PNG file + dwell_grid JSON
        img = np.ones((24, 24), dtype=np.uint8) * 255
        _, buf = cv2.imencode(".png", img)
        boundary = "----TestBoundary123"
        mp_body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="image.png"\r\n'
            f"Content-Type: image/png\r\n\r\n"
        ).encode("utf-8") + buf.tobytes() + (
            f"\r\n--{boundary}\r\n"
            f'Content-Disposition: form-data; name="dwell_grid"; filename="dwell.json"\r\n'
            f"Content-Type: application/json\r\n\r\n"
            f"[[0, 1, 0], [0, 2, 0], [0, 0, 0]]"
            f"\r\n--{boundary}--\r\n"
        ).encode("utf-8")
        with mock.patch.object(monitoring_server, "enqueue_pipeline_job", return_value=("r_mp", 1)) as enq_mock, \
             mock.patch.object(monitoring_server, "write_live_image_and_mark_handled"):
            st_mp, _, _ = self._req(
                "POST", "/api/drawings",
                body=mp_body,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                client_ip="10.30.40.60",
            )
        self.assertEqual(st_mp, 200)
        self.assertEqual(enq_mock.call_args.kwargs["dwell_grid"][1][1], 2)

    def test_duplicate_xff_headers_and_endpoint_change_clears_stale_api_key(self):
        # 1. Duplicate X-Forwarded-For headers must read rightmost proxy-appended header, not client's first header
        with mock.patch.dict(os.environ, {"LATENT_SPACE_TRUST_PROXY": "1"}):
            for idx in range(monitoring_server.LOGIN_MAX_FAILURES):
                self._req(
                    "POST", "/api/auth/login",
                    body={"password": "bad"},
                    headers=[("X-Forwarded-For", f"198.51.100.{idx + 1}"), ("X-Forwarded-For", "203.0.113.77")],
                    client_ip="10.0.0.2",
                )
            st_dup, _, _ = self._req(
                "POST", "/api/auth/login",
                body={"password": "TestLockdownPass!42"},
                headers=[("X-Forwarded-For", "198.51.100.250"), ("X-Forwarded-For", "203.0.113.77")],
                client_ip="10.0.0.2",
            )
            self.assertEqual(st_dup, 429)

        # 2. Changing provider or base_url without supplying a new key automatically clears stale saved api_key
        config_manager.update_settings_section("ai", {
            "analysis": {
                "provider": "openai_compatible",
                "model": "gpt-4o-mini",
                "base_url": "https://api.openai.com/v1",
                "api_key": "sk-openai-secret",
            }
        })
        # Same endpoint update keeps key
        kept = config_manager.update_settings_section("ai", {"analysis": {"model": "gpt-4o"}})
        self.assertEqual(kept["ai"]["analysis"]["api_key"], "sk-openai-secret")

        # Switching base_url without api_key clears old key
        cleared_url = config_manager.update_settings_section("ai", {
            "analysis": {"provider": "openai_compatible", "base_url": "https://api.groq.com/openai/v1"}
        })
        self.assertEqual(cleared_url["ai"]["analysis"]["api_key"], "")

        # Switching provider with explicit new key stores new key
        config_manager.update_settings_section("ai", {
            "analysis": {"provider": "gemini", "model": "gemini-2.0-flash", "api_key": "AIzaNewKey"}
        })
        switched_prov = config_manager.update_settings_section("ai", {
            "analysis": {"provider": "anthropic", "model": "claude-3-5-haiku-latest"}
        })
        self.assertEqual(switched_prov["ai"]["analysis"]["api_key"], "")

        # 3. Null-byte in POST path returns 400 cleanly
        st_post_null, _, _ = self._req("POST", "/api/drawings%00", body={})
        self.assertEqual(st_post_null, 400)

    def test_hash_in_env_secret_relative_url_guard_section_type_and_http_range(self):
        # 1. Unquoted '#' without preceding space is preserved in .env secrets
        self.assertEqual(
            config_manager._parse_env_line("LATENT_SPACE_ADMIN_PASSWORD=Admin#2026!"),
            ("LATENT_SPACE_ADMIN_PASSWORD", "Admin#2026!"),
        )
        self.assertEqual(
            config_manager._parse_env_line("PORT=8080 # inline comment"),
            ("PORT", "8080"),
        )

        # 2. Relative URLs rejected on base_url and remote_server_url; oversized remote_server_url rejected
        with self.assertRaises(ValueError):
            config_manager.update_settings_section("ai", {"analysis": {"base_url": "/v1"}})
        with self.assertRaises(ValueError):
            config_manager.update_settings_section("server", {"remote_server_url": "/relative"})
        with self.assertRaises(ValueError):
            config_manager.update_settings_section("server", {"remote_server_url": "https://example.org/" + ("a" * 160)})

        # 3. Non-dict top-level section in save_settings raises ValueError instead of corrupting settings
        with self.assertRaises(ValueError):
            config_manager.save_settings({"ai": "invalid-string"})

        # 4. HTTP Range request returns 206 Partial Content and 416 on out-of-range
        sample_mp3 = config_manager.SHARED_DIR / "output_range_test.mp3"
        sample_mp3.write_bytes(b"0123456789ABCDEF")
        st_206, hdrs_206, body_206 = self._req(
            "GET", "/shared_output/output_range_test.mp3",
            headers={"Range": "bytes=4-9"},
        )
        self.assertEqual(st_206, 206)
        self.assertEqual(body_206, b"456789")
        lower_206 = {k.lower(): v for k, v in hdrs_206}
        self.assertEqual(lower_206.get("accept-ranges"), "bytes")
        self.assertEqual(lower_206.get("content-range"), "bytes 4-9/16")

        st_416, _, _ = self._req(
            "GET", "/shared_output/output_range_test.mp3",
            headers={"Range": "bytes=100-200"},
        )
        self.assertEqual(st_416, 416)

        # 5. Quote/angle-bracket characters in URLs and unknown top-level settings sections rejected
        for unsafe_u in ('https://example.org/"onclick="alert(1)', "https://example.org/<script>", "https://example.org/ a"):
            with self.assertRaises(ValueError):
                config_manager.validate_safe_url(unsafe_u)

        st_unk, _, _ = self._req(
            "POST", "/api/settings",
            body={"bogus_section": {"foo": "bar"}},
            headers={"Authorization": "Bearer TestLockdownPass!42"},
        )
        self.assertEqual(st_unk, 400)

        # 6. _resolve_print_datetime uses filename date consistently even if st_mtime differs
        from datetime import datetime, timedelta
        yday_dt = datetime.now() - timedelta(days=1)
        yday_pdf = config_manager.DOCS_DIR / f"print_{yday_dt.strftime('%Y%m%d_%H%M%S')}_001.pdf"
        yday_pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
        st_pr, _, pr_body = self._req("GET", "/api/files/prints")
        self.assertEqual(st_pr, 200)
        yday_item = [p for p in json.loads(pr_body) if p["name"] == yday_pdf.name][0]
        self.assertFalse(yday_item["is_today"])
        self.assertTrue(yday_item["is_yesterday"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
