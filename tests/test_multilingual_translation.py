import http.client
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
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
import run_analysis_watcher
import translator

TEST_TMP = None


def setUpModule():
    global TEST_TMP
    TEST_TMP = Path(tempfile.mkdtemp(prefix="ls_i18n_test_"))
    os.environ["LATENT_SPACE_CONFIG_DIR"] = str(TEST_TMP / "config")
    os.environ["LATENT_SPACE_SHARED_DIR"] = str(TEST_TMP / "shared_output")
    os.environ["LATENT_SPACE_LOGS_DIR"] = str(TEST_TMP / "logs")
    os.environ["LATENT_SPACE_ADMIN_PASSWORD"] = "TestI18nPass!42"
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


class TestMultilingualTranslationAndI18n(unittest.TestCase):
    def setUp(self):
        config_manager.ensure_directories()
        config_manager.save_settings(config_manager.DEFAULT_SETTINGS)
        translator.reset_local_nmt_cache()
        with monitoring_server._auth_lock:
            monitoring_server._admin_sessions.clear()
            monitoring_server._login_failures.clear()
            monitoring_server._upload_timestamps.clear()

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

    def test_translation_config_validation(self):
        updated = config_manager.update_settings_section("translation", {
            "enabled": True,
            "mode": "ollama",
            "target_languages": ["hu"],
            "local_nmt_model": "Helsinki-NLP/opus-mt-tc-big-en-hu",
            "local_nmt_threads": 4,
            "ollama_model": "translategemma:4b",
            "ollama_temperature": 0.15,
            "ollama_timeout": 30,
            "default_print_language": "hu",
        })
        tr = updated["translation"]
        self.assertEqual(tr["mode"], "ollama")
        self.assertEqual(tr["local_nmt_threads"], 4)
        self.assertEqual(tr["default_print_language"], "hu")

        for bad_payload in (
            {"mode": "invalid_engine"},
            {"target_languages": ["en"]},
            {"target_languages": ["xx"]},
            {"local_nmt_model": "../etc/passwd"},
            {"local_nmt_threads": 0},
            {"local_nmt_threads": True},
            {"default_print_language": "de"},
            {"ollama_base_url": "/relative/url"},
            {"unknown_field": 123},
        ):
            with self.assertRaises(ValueError, msg=f"Expected ValueError for {bad_payload}"):
                config_manager.update_settings_section("translation", bad_payload)

    def test_curated_archetype_and_line_preserving_local_nmt(self):
        calls = []

        def fake_batch(texts, model_id, num_threads=6, allow_download=True):
            calls.append(list(texts))
            return [f"HU({t})" for t in texts]

        with mock.patch.object(translator, "_nmt_translate_batch", side_effect=fake_batch):
            res = translator.translate_reading(
                archetype="Urchin · Woven Loops",
                psychoanalysis="First sentence here. Second sentence follows!\n\nNew paragraph arrives.",
                poem="Line one of stanza one,\nLine two of stanza one.\n\nLine one of stanza two.",
                target_lang="hu",
            )

        self.assertTrue(res["ok"])
        self.assertEqual(res["mode"], "local_nmt")
        # 'Woven Loops' hits curated Hungarian table; only 'Urchin' calls NMT
        self.assertEqual(res["archetype"], "HU(Urchin) · Szőtt hurkok")
        self.assertEqual(
            res["psychoanalysis"],
            "HU(First sentence here.) HU(Second sentence follows!)\n\nHU(New paragraph arrives.)",
        )
        # Blank stanza separator preserved verbatim
        self.assertEqual(
            res["poem"],
            "HU(Line one of stanza one,)\nHU(Line two of stanza one.)\n\nHU(Line one of stanza two.)",
        )
        self.assertEqual(calls[0], ["Urchin"])
        self.assertEqual(len(calls[1]), 3)
        self.assertEqual(len(calls[2]), 3)

        # Haiku cleanup strips accidental syllable markers & code fences
        raw_haiku = "```\nDark ink drifts (5)\nLoops bend to the light (7)\nStillness holds the edge (5)\n```"
        self.assertEqual(
            translator._clean_translated_output(raw_haiku, is_poem=True),
            "Dark ink drifts\nLoops bend to the light\nStillness holds the edge",
        )

    def test_ollama_translategemma_and_generic_prompts(self):
        captured_payloads = []

        def fake_post(url, json=None, headers=None, timeout=None):
            captured_payloads.append((url, json))
            r = mock.MagicMock()
            r.raise_for_status.return_value = None
            r.json.return_value = {"choices": [{"message": {"content": "Magyar szöveg"}}]}
            return r

        cfg_tg = config_manager.update_settings_section("translation", {
            "enabled": True,
            "mode": "ollama",
            "ollama_model": "translategemma:4b",
        })
        with mock.patch.object(translator.requests, "post", side_effect=fake_post):
            res_tg = translator.translate_reading("Labyrinth", "Analysis text.", "Line A\nLine B", target_lang="hu", settings=cfg_tg)
        self.assertTrue(res_tg["ok"])
        self.assertEqual(res_tg["archetype"], "Labirintus")
        # TranslateGemma uses single user message with official (en) -> (hu) template
        self.assertEqual(len(captured_payloads), 2)
        self.assertEqual(len(captured_payloads[0][1]["messages"]), 1)
        self.assertIn("English (en) to Hungarian (hu)", captured_payloads[0][1]["messages"][0]["content"])

        captured_payloads.clear()
        cfg_gen = config_manager.update_settings_section("translation", {
            "enabled": True,
            "mode": "ollama",
            "ollama_model": "gemma3:4b",
        })
        with mock.patch.object(translator.requests, "post", side_effect=fake_post):
            res_gen = translator.translate_reading("Phoenix", "Analysis text.", "Line A\nLine B", target_lang="hu", settings=cfg_gen)
        self.assertTrue(res_gen["ok"])
        # Generic Ollama model uses system + user role messages
        self.assertEqual(captured_payloads[0][1]["messages"][0]["role"], "system")
        self.assertEqual(captured_payloads[0][1]["messages"][1]["role"], "user")

    def test_native_llm_mode_authors_hungarian_directly(self):
        cfg = config_manager.update_settings_section("translation", {
            "enabled": True,
            "mode": "native_llm",
        })
        fake_analysis_model = mock.MagicMock()
        fake_analysis_model._llm = "gemma3:4b"
        fake_analysis_model._last_used_model = "gemma3:4b"
        fake_analysis_model.chat.return_value = "A tekinteted csendes belső utat rajzol."

        fake_poem_model = mock.MagicMock()
        fake_poem_model._llm = "gemma3:4b"
        fake_poem_model._last_used_model = "gemma3:4b"
        fake_poem_model.chat.side_effect = [
            "Csendes vonalak,\nBelső horizont nyílik.",
            "Tengerisün",
        ]

        res = translator.translate_reading(
            archetype="Urchin · Woven Loops",
            psychoanalysis="English analysis.",
            poem="English poem.",
            target_lang="hu",
            settings=cfg,
            prompt_context="Visual profile",
            analysis_model=fake_analysis_model,
            poem_model=fake_poem_model,
        )
        self.assertTrue(res["ok"])
        self.assertEqual(res["archetype"], "Tengerisün · Szőtt hurkok")
        self.assertEqual(res["psychoanalysis"], "A tekinteted csendes belső utat rajzol.")
        self.assertEqual(res["poem"], "Csendes vonalak,\nBelső horizont nyílik.")
        # Verify low temperature & dedicated Hungarian translator/poet system prompts (no temp=1.0 inheritance)
        a_kwargs = fake_analysis_model.chat.call_args.kwargs
        self.assertLessEqual(a_kwargs["temperature_override"], 0.4)
        self.assertIn("tegezés", a_kwargs["system_prompt_override"])
        self.assertIn("Látens Tér", a_kwargs["system_prompt_override"])
        p_kwargs = fake_poem_model.chat.call_args_list[0].kwargs
        self.assertLessEqual(p_kwargs["temperature_override"], 0.55)
        self.assertIn("haiku", p_kwargs["system_prompt_override"].lower())
        arch_kwargs = fake_poem_model.chat.call_args_list[1].kwargs
        self.assertEqual(arch_kwargs["temperature_override"], 0.1)
        self.assertIn("archetype title", arch_kwargs["system_prompt_override"].lower())

        # Deterministic Hungarian surface polisher & suspect-output detector
        raw_glitched = "A egyenes vonalok között keresed magadot, és skanálod a látóhatárt, mint egy kis rák."
        polished = translator.polish_hungarian_text(raw_glitched)
        self.assertEqual(
            polished,
            "Az egyenes vonalak között keresed magadat, és fürkészed a látóhatárt, mint egy tengerisün.",
        )
        self.assertEqual(translator.hungarian_quality_issues(polished), [])
        self.assertTrue(translator.hungarian_quality_issues("A gaze lines in the void és nézedés."))

        # Poem prompt builder anchors the poet in the drawing's physical gesture & spatial focus
        poem_msg = run_analysis_watcher._build_poem_message(
            "You linger near the centre.",
            {
                "label": "River Bend · Descending Sweep",
                "raw_vision_label": "River Bend",
                "geometry": {
                    "summary_label": "Descending Sweep",
                    "gesture_desc": "a long diagonal sweep from the upper left toward the lower right",
                    "primary_zone": "center",
                    "least_zone": "lower-left",
                    "line_quality": "angular",
                },
            },
        )
        self.assertIn("a long diagonal sweep from the upper left toward the lower right", poem_msg)
        self.assertIn("The eyes lingered most in the center; the lower left stayed empty.", poem_msg)
        self.assertIn("River Bend", poem_msg)

        # Structured persona fields and selectable poem styles (lyrical_4_6, two_stanza, quatrain, haiku)
        from chat import LLMModel
        a_llm = LLMModel("analysis")
        p_llm = LLMModel("poem")
        self.assertTrue(a_llm._purpose and a_llm._expertise and a_llm._capabilities and a_llm._restrictions and a_llm._additional)
        self.assertTrue(p_llm._purpose and p_llm._expertise and p_llm._capabilities and p_llm._restrictions and p_llm._additional)
        self.assertEqual(p_llm._poem_style, "lyrical_4_6")
        self.assertIn("4 to 6 unrhymed lines", p_llm._build_system_prompt())
        self.assertIn("Purpose:", a_llm._build_system_prompt())
        self.assertIn("Restrictions:", a_llm._build_system_prompt())

        p_llm.update_assistant(poem_style="two_stanza")
        self.assertEqual(p_llm.get_config_dict()["poem_style"], "two_stanza")
        self.assertIn("two-stanza", p_llm.get_config_dict()["description"].lower())
        self.assertIn("two-stanza lyrical free-verse poem of 6 to 8 unrhymed lines", p_llm._build_system_prompt())

        p_llm.update_assistant(poem_style="haiku")
        self.assertEqual(p_llm.get_config_dict()["poem_style"], "haiku")
        self.assertIn("5-7-5", p_llm.get_config_dict()["description"])
        self.assertIn("5-7-5", p_llm.get_config_dict()["purpose"])
        self.assertIn("strict 5-7-5 syllable rhythm", p_llm._build_system_prompt())
        p_llm.update_assistant(poem_style="lyrical_4_6")

        # Single-line multi-sentence poem output from small LLMs is split into verse lines
        single_line_poem = (
            "Branches grasp and release in the upper boughs, while a lone trunk stands steady. "
            "Leaves rustle and settle into the quiet grass below. "
            "Morning light rests on the open field."
        )
        cleaned_verse = translator._clean_translated_output(single_line_poem, is_poem=True, poem_style="lyrical_4_6")
        self.assertGreaterEqual(len(cleaned_verse.splitlines()), 3)

        # Print Kiosk (/kiosk) no longer renders the attribution footer; Admin uses custom .combo instead of <datalist>
        kiosk_html = (PROJECT_ROOT / "web_ui" / "kiosk.html").read_text(encoding="utf-8")
        self.assertNotIn("kioskAttribution", kiosk_html)
        self.assertNotIn("renderKioskAttribution", kiosk_html)
        admin_html = (PROJECT_ROOT / "web_ui" / "admin.html").read_text(encoding="utf-8")
        self.assertNotIn('list="ollamaModelList"', admin_html)
        self.assertIn('data-combo="vision"', admin_html)
        self.assertIn('id="poem_style"', admin_html)

        # Live pipeline stage progress callback updates monitoring_server._pipeline_state
        with monitoring_server._pipeline_lock:
            monitoring_server._pipeline_state["running"] = True
            monitoring_server._pipeline_state["active_run_id"] = "stage_test_run"
            monitoring_server._pipeline_state["stages"] = {}
        try:
            monitoring_server._on_pipeline_stage_progress("stage_test_run", "vision", "done", elapsed_sec=1.25)
            monitoring_server._on_pipeline_stage_progress("stage_test_run", "analysis", "running")
            with monitoring_server._pipeline_lock:
                snap = monitoring_server._snapshot_pipeline_state_unlocked()
            self.assertEqual(snap["current_stage"], "analysis")
            self.assertEqual(snap["stages"]["vision"]["status"], "done")
            self.assertEqual(snap["stages"]["vision"]["elapsed_sec"], 1.25)
            self.assertEqual(snap["stages"]["analysis"]["status"], "running")
        finally:
            with monitoring_server._pipeline_lock:
                monitoring_server._pipeline_state["running"] = False
                monitoring_server._pipeline_state["active_run_id"] = None
                monitoring_server._pipeline_state["current_stage"] = None
                monitoring_server._pipeline_state["stages"] = {}

    def test_overlapped_pipeline_bilingual_storage_and_fault_tolerance(self):
        rid = "20261120_100000_001"
        snap = config_manager.SHARED_DIR / f"input_{rid}.png"
        canvas = np.ones((200, 200), dtype=np.uint8) * 255
        cv2.circle(canvas, (100, 100), 60, 0, 4)
        cv2.imwrite(str(snap), canvas)

        def fake_save_pdf(url=None, out_dir=None, website_data=None, run_id=None):
            out_pdf = Path(out_dir) / f"print_{run_id}.pdf"
            out_pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
            return str(out_pdf)

        fake_model = mock.MagicMock()
        fake_model._provider = "ollama"
        fake_model._llm = "llama3.1:8b"
        fake_model._last_used_provider = "ollama"
        fake_model._last_used_model = "llama3.1:8b"
        fake_model.initialize.return_value = {"status": "Success"}
        fake_model.chat.side_effect = [
            "Your gaze moves in serene concentric loops.",
            "Quiet circles turn,\nEchoes of a steady mind.",
        ]

        with mock.patch.object(run_analysis_watcher, "LLMModel", return_value=fake_model), \
             mock.patch.object(run_analysis_watcher, "save_print_pdf", side_effect=fake_save_pdf), \
             mock.patch.object(
                 run_analysis_watcher,
                 "translate_analysis_stage_local_nmt",
                 return_value={
                     "archetype": "Szőtt hurkok",
                     "psychoanalysis": "A tekinteted nyugodt körökben halad.",
                     "provider": "local_nmt:Helsinki-NLP/opus-mt-tc-big-en-hu",
                 },
             ), \
             mock.patch.object(
                 run_analysis_watcher,
                 "translate_poem_stage_local_nmt",
                 return_value="Csendes körök forognak,\nNyugodt elme visszhangjai.",
             ):
            ok = run_analysis_watcher.run_analysis_pipeline(run_id=rid, snapshot_path=str(snap), raise_on_error=True)

        self.assertTrue(ok)
        content = json.loads((config_manager.SHARED_DIR / "content.json").read_text(encoding="utf-8"))
        self.assertEqual(content["languages"], ["en", "hu"])
        self.assertEqual(content["psychoanalysis"], "Your gaze moves in serene concentric loops.")
        self.assertEqual(content["translations"]["hu"]["psychoanalysis"], "A tekinteted nyugodt körökben halad.")
        self.assertEqual(content["translations"]["hu"]["poem"], "Csendes körök forognak,\nNyugodt elme visszhangjai.")
        self.assertEqual(content["translations"]["hu"]["archetype"], "Szőtt hurkok")

        # Fault-tolerance check: failing translator must NEVER fail the visitor's reading
        rid2 = "20261120_100001_002"
        snap2 = config_manager.SHARED_DIR / f"input_{rid2}.png"
        cv2.imwrite(str(snap2), canvas)
        fake_model.chat.side_effect = ["English reading survives.", "English poem survives."]
        with mock.patch.object(run_analysis_watcher, "LLMModel", return_value=fake_model), \
             mock.patch.object(run_analysis_watcher, "save_print_pdf", side_effect=fake_save_pdf), \
             mock.patch.object(
                 run_analysis_watcher,
                 "translate_analysis_stage_local_nmt",
                 side_effect=RuntimeError("Offline model missing"),
             ):
            ok2 = run_analysis_watcher.run_analysis_pipeline(run_id=rid2, snapshot_path=str(snap2), raise_on_error=True)

        self.assertTrue(ok2)
        content2 = json.loads((config_manager.SHARED_DIR / "content.json").read_text(encoding="utf-8"))
        self.assertEqual(content2["languages"], ["en"])
        self.assertIn("Offline model missing", content2["translationError"])

    def test_localized_pdf_endpoint_listing_non_collision_and_cleanup(self):
        rid = "20261120_110000_001"
        thumb = config_manager.DOCS_DIR / f"print_{rid}.png"
        hm = config_manager.DOCS_DIR / f"print_{rid}.heatmap.png"
        pdf_en = config_manager.DOCS_DIR / f"print_{rid}.pdf"
        meta_file = config_manager.DOCS_DIR / f"print_{rid}.json"

        img = np.ones((128, 128, 3), dtype=np.uint8) * 255
        cv2.circle(img, (64, 64), 30, (20, 20, 20), 3)
        cv2.imwrite(str(thumb), img)
        cv2.imwrite(str(hm), img)

        meta = {
            "status": "ok",
            "runId": rid,
            "scribblePath": f"docs/{thumb.name}",
            "heatmapPath": f"docs/{hm.name}",
            "archetype": "Woven Loops",
            "fractalDimension": 1.32,
            "spatialEntropy": 0.61,
            "psychoanalysis": "English psychoanalysis.",
            "poem": "English line one.\nEnglish line two.",
            "translations": {
                "en": {
                    "archetype": "Woven Loops",
                    "psychoanalysis": "English psychoanalysis.",
                    "poem": "English line one.\nEnglish line two.",
                },
                "hu": {
                    "archetype": "Szőtt hurkok",
                    "psychoanalysis": "Magyar pszichoanalízis őűáéíóöúü.",
                    "poem": "Első magyar sor,\nMásodik magyar sor.",
                },
            },
            "languages": ["en", "hu"],
            "printLanguage": "en",
            "printPDF": f"docs/{pdf_en.name}",
            "timestamp": "2026-11-20T11:00:00",
        }
        meta_file.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        run_analysis_watcher._render_pdf_fallback(pdf_en, meta, config_manager.load_settings(), lang="en")
        self.assertTrue(pdf_en.exists())

        # Request Hungarian PDF via /api/pdf/{rid}/hu (forces Matplotlib fallback in test env)
        with mock.patch.object(run_analysis_watcher, "_is_gallery_reachable", return_value=False):
            st_hu, body_hu = self._req("GET", f"/api/pdf/{rid}/hu")
        self.assertEqual(st_hu, 200)
        self.assertTrue(body_hu.startswith(b"%PDF-"))

        cached_hu = config_manager.DOCS_DIR / "lang" / "hu" / f"print_{rid}.pdf"
        self.assertTrue(cached_hu.exists())
        self.assertEqual(run_analysis_watcher._count_pdf_pages(cached_hu), 1)

        # Unsupported language and traversal rejected
        st_bad_lang, _ = self._req("GET", f"/api/pdf/{rid}/de")
        self.assertEqual(st_bad_lang, 400)
        st_bad_id, _ = self._req("GET", "/api/pdf/..%2Fsecret/hu")
        self.assertEqual(st_bad_id, 400)
        st_bad_id2, _ = self._req("GET", "/api/pdf/..badid/hu")
        self.assertEqual(st_bad_id2, 404)

        # Listing prints must NOT double-count cached docs/lang/hu/print_*.pdf
        st_list, list_raw = self._req("GET", "/api/files/prints")
        self.assertEqual(st_list, 200)
        listed = [p for p in json.loads(list_raw) if p["run_id"] == rid]
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["languages"], ["en", "hu"])
        self.assertEqual(listed[0]["pdf_urls"]["hu"], f"/api/pdf/{rid}/hu")

        # Host print with lang="hu" passes the localized Hungarian PDF to send_pdf_to_printer
        auth = {"Authorization": "Bearer TestI18nPass!42"}
        with mock.patch.object(monitoring_server, "send_pdf_to_printer", return_value={"status": "success"}) as mock_print:
            st_p, _ = self._req("POST", "/api/files/print", body={"filename": pdf_en.name, "lang": "hu"}, headers=auth)
        self.assertEqual(st_p, 200)
        self.assertEqual(Path(mock_print.call_args[0][0]).resolve(), cached_hu.resolve())

        # Delete cleans up both primary and localized Hungarian PDF
        st_del, _ = self._req("POST", "/api/files/delete", body={"action": "specific", "filename": pdf_en.name}, headers=auth)
        self.assertEqual(st_del, 200)
        self.assertFalse(pdf_en.exists())
        self.assertFalse(cached_hu.exists())

    def test_translation_admin_endpoints_auth_locked(self):
        for method, route, payload in (
            ("GET", "/api/settings/translation", None),
            ("GET", "/api/ai/translation/status", None),
            ("POST", "/api/settings/translation", {"mode": "local_nmt"}),
            ("POST", "/api/ai/translation/preload", {}),
            ("POST", "/api/ai/translation/test", {"target_lang": "hu"}),
        ):
            st, _ = self._req(method, route, body=payload)
            self.assertEqual(st, 401, f"Expected 401 unauthenticated for {method} {route}")

        auth = {"Authorization": "Bearer TestI18nPass!42"}
        st_get, raw_get = self._req("GET", "/api/settings/translation", headers=auth)
        self.assertEqual(st_get, 200)
        self.assertEqual(json.loads(raw_get)["mode"], "local_nmt")

        st_status, raw_status = self._req("GET", "/api/ai/translation/status", headers=auth)
        self.assertEqual(st_status, 200)
        self.assertIn("local_nmt", json.loads(raw_status))

        with mock.patch.object(
            monitoring_server,
            "translate_reading",
            return_value={
                "ok": True,
                "mode": "local_nmt",
                "provider": "local_nmt:mock",
                "archetype": "Szőtt hurkok",
                "psychoanalysis": "Teszt elemzés.",
                "poem": "Teszt vers.",
                "elapsed_sec": 0.42,
            },
        ):
            st_test, raw_test = self._req("POST", "/api/ai/translation/test", body={"target_lang": "hu"}, headers=auth)
        self.assertEqual(st_test, 200)
        self.assertEqual(json.loads(raw_test)["translated"]["poem"], "Teszt vers.")

    def test_independent_per_page_i18n_isolation(self):
        # /i18n.js is served publicly
        st, js_bytes = self._req("GET", "/i18n.js")
        self.assertEqual(st, 200)
        js_text = js_bytes.decode("utf-8")
        self.assertIn("ls_lang_", js_text)
        self.assertNotIn("addEventListener('storage'", js_text)

        # Each of the 4 pages registers its own unique pageId
        expected_pages = {
            PROJECT_ROOT / "index.html": "createPageController('gallery'",
            PROJECT_ROOT / "web_ui" / "draw.html": "createPageController('draw'",
            PROJECT_ROOT / "web_ui" / "kiosk.html": "createPageController('kiosk'",
            PROJECT_ROOT / "web_ui" / "admin.html": "createPageController('admin'",
        }
        for path, marker in expected_pages.items():
            html = path.read_text(encoding="utf-8")
            self.assertIn(marker, html, f"{path.name} missing independent i18n controller '{marker}'")

        # Execute i18n.js in Node (if installed) to verify strict runtime isolation across all 4 pages
        node_bin = shutil.which("node")
        if node_bin:
            verify_js = (
                "const fs = require('fs');\n"
                "const vm = require('vm');\n"
                "const store = new Map();\n"
                "const win = {\n"
                "  URLSearchParams: URLSearchParams,\n"
                "  location: { search: '' },\n"
                "  localStorage: {\n"
                "    getItem: k => store.has(k) ? store.get(k) : null,\n"
                "    setItem: (k, v) => store.set(k, String(v)),\n"
                "  },\n"
                "  document: { documentElement: { lang: 'en' }, title: '', querySelectorAll: () => [] },\n"
                "};\n"
                "win.window = win;\n"
                "vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), win);\n"
                "const g = win.LS_I18N.createPageController('gallery');\n"
                "const d = win.LS_I18N.createPageController('draw');\n"
                "const k = win.LS_I18N.createPageController('kiosk');\n"
                "const a = win.LS_I18N.createPageController('admin');\n"
                "g.setLang('hu');\n"
                "d.setLang('hu');\n"
                "if (g.getLang() !== 'hu' || d.getLang() !== 'hu') throw new Error('Expected hu');\n"
                "if (a.getLang() !== 'en' || k.getLang() !== 'en') throw new Error('Cross-page leak!');\n"
                "if (store.get('ls_lang_gallery') !== 'hu' || store.get('ls_lang_admin') !== undefined) throw new Error('Storage leak!');\n"
                "a.setLang('en');\n"
                "if (g.getLang() !== 'hu') throw new Error('Admin affected gallery!');\n"
                "const picked = g.pickContent({ archetype: 'EN', translations: { en: { archetype: 'EN', poem: 'P_EN' }, hu: { archetype: 'HU', poem: 'P_HU' } } });\n"
                "if (picked.poem !== 'P_HU') throw new Error('pickContent failed');\n"
                "const enKeys = Object.keys(win.LS_I18N.DICTIONARIES.en);\n"
                "const huDict = win.LS_I18N.DICTIONARIES.hu;\n"
                "const missingHu = enKeys.filter(k => !huDict[k] || !String(huDict[k]).trim());\n"
                "if (missingHu.length) throw new Error('Missing HU keys: ' + missingHu.join(', '));\n"
            )
            proc = subprocess.run(
                [node_bin, "-e", verify_js, str(PROJECT_ROOT / "web_ui" / "i18n.js")],
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, f"i18n runtime isolation check failed:\n{proc.stderr}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
