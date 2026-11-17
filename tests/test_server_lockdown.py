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


class TestServerLockdownHTTP(unittest.TestCase):
    def setUp(self):
        config_manager.ensure_directories()
        config_manager.save_settings(config_manager.DEFAULT_SETTINGS)
        with monitoring_server._auth_lock:
            monitoring_server._admin_sessions.clear()
            monitoring_server._login_failures.clear()
            monitoring_server._upload_timestamps.clear()

    def _req(self, method, path, body=None, headers=None, client_ip="127.0.0.1"):
        hdrs = {"Host": "localhost"}
        if headers:
            hdrs.update(headers)
        raw_body = b""
        if body is not None:
            if isinstance(body, (dict, list)):
                raw_body = json.dumps(body).encode("utf-8")
                hdrs.setdefault("Content-Type", "application/json")
            else:
                raw_body = body
            if "Transfer-Encoding" not in hdrs:
                hdrs.setdefault("Content-Length", str(len(raw_body)))

        header_lines = [f"{method} {path} HTTP/1.1"] + [f"{k}: {v}" for k, v in hdrs.items()]
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

    def test_printing_settings_hides_printer_name_when_unauthenticated(self):
        config_manager.update_settings_section("printing", {"auto_print": False, "printer_name": "SecretLobbyPrinter"})
        status, _, data = self._req("GET", "/api/settings/printing")
        self.assertEqual(status, 200)
        parsed = json.loads(data)
        self.assertNotIn("printer_name", parsed)

    def test_admin_endpoints_reject_unauthenticated(self):
        for get_path in ("/api/status", "/api/monitoring", "/api/settings", "/api/settings/tts", "/api/ai/config", "/api/logs/installation"):
            status, _, _ = self._req("GET", get_path)
            self.assertEqual(status, 401, f"GET {get_path} should return 401 when unauthenticated")

        post_cases = [
            ("/api/pipeline/run", {}),
            ("/api/settings", {"printing": {"auto_print": True}}),
            ("/api/settings/attributions", {"enabled": True}),
            ("/api/settings/printing", {"auto_print": True}),
            ("/api/settings/tts", {"enabled": False}),
            ("/api/ai/config", {"model_type": "analysis", "config": {}}),
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
