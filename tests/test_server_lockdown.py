import base64
import http.client
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEST_TMP = Path(tempfile.mkdtemp(prefix="ls_lockdown_test_"))
os.environ["LATENT_SPACE_CONFIG_DIR"] = str(TEST_TMP / "config")
os.environ["LATENT_SPACE_SHARED_DIR"] = str(TEST_TMP / "shared_output")
os.environ["LATENT_SPACE_LOGS_DIR"] = str(TEST_TMP / "logs")
os.environ["LATENT_SPACE_ADMIN_PASSWORD"] = "TestLockdownPass!42"

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config_manager
import monitoring_server


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
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TEST_TMP, ignore_errors=True)

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
        with monitoring_server._auth_lock:
            monitoring_server._admin_sessions.clear()
            monitoring_server._login_failures.clear()

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

        # Invalid URL & printer injection rejected with 400 even when authenticated
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

        # Logout revokes session
        status, _, _ = self._req("POST", "/api/auth/logout", headers={"Cookie": cookie_pair})
        self.assertEqual(status, 200)
        status, _, _ = self._req("GET", "/api/status", headers={"Cookie": cookie_pair})
        self.assertEqual(status, 401)

    def test_login_rate_limiting(self):
        for _ in range(monitoring_server.LOGIN_MAX_FAILURES):
            status, _, _ = self._req("POST", "/api/auth/login", body={"password": "bad"}, client_ip="10.0.0.99")
            self.assertEqual(status, 401)
        status, hdrs, _ = self._req("POST", "/api/auth/login", body={"password": "TestLockdownPass!42"}, client_ip="10.0.0.99")
        self.assertEqual(status, 429)
        self.assertIn("retry-after", {k.lower(): v for k, v in hdrs})

    def test_bearer_token_and_traversal_and_upload_guards(self):
        # Traversal / dotfile blocked
        status, _, _ = self._req("GET", "/shared_output/.secret")
        self.assertEqual(status, 403)
        status, _, _ = self._req("GET", "/shared_output/../config/settings.json")
        self.assertEqual(status, 403)

        # Bearer auth works
        status, _, _ = self._req("GET", "/api/settings", headers={"Authorization": "Bearer TestLockdownPass!42"})
        self.assertEqual(status, 200)

        # Non-image upload rejected
        bad_b64 = base64.b64encode(b"<html><script>alert(1)</script></html>").decode("ascii")
        status, _, _ = self._req("POST", "/api/drawings", body={"image": bad_b64})
        self.assertEqual(status, 400)

        # Oversized Content-Length rejected with 413
        status, _, _ = self._req(
            "POST",
            "/api/auth/login",
            body=b"{}",
            headers={"Content-Type": "application/json", "Content-Length": str(monitoring_server.MAX_JSON_BODY_BYTES + 10)},
        )
        self.assertEqual(status, 413)


if __name__ == "__main__":
    unittest.main(verbosity=2)
