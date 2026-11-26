# Latent Space — Critical Codebase Audit

| | |
| :--- | :--- |
| **Date** | 26 November 2026 |
| **Branch / commit** | `feat/portable-multi-ai` at `c5e9f04`, plus uncommitted edits to `STARTUP_GUIDE.md` |
| **Supersedes** | The report of 18 November 2026. Spot checks confirm that its main fixes are in place (see §6). |
| **Scope** | All Python modules, `index.html`, `web_ui/*.html`, `Dockerfile`, `.dockerignore`, requirements files, docs, and `tests/`. |
| **Method** | Line-by-line read of the backend. Targeted read of every frontend `innerHTML` sink, the auth flows, and all network calls. One full test run, one timed test run, and three reproductions on this machine. |
| **Not reviewed in depth** | Gaze math in `gaze_painter/eye_tracking.py`, `drawing.py`, `eye_visualizer.py`; the translation strings in `web_ui/i18n.js`; CSS and layout. |
| **Test result** | `python -m unittest discover -s tests` → 77 tests, all pass, 81 s. |

## How to read this report

- **High**: an attacker on the venue network, or one ordinary fault, can stop the exhibition, take admin control, or cost money.
- **Medium**: wrong behavior, data exposure, or an operational trap that a real deployment will hit.
- **Low**: hardening, edge cases, and maintainability.

**Reproduced** marks a finding that I confirmed by running code. All other findings come from reading the code. Each finding cites the exact lines.

---

## Summary

| ID | Severity | Area | Finding |
| :--- | :--- | :--- | :--- |
| H1 | High | Abuse / cost | Public `/api/drawings` has no effective abuse limit; one device can fill the queue, burn API credits, and trigger prints |
| H2 | High | Reliability | No per-run deadline; one slow run blocks every visitor; `/draw` shows false timeouts to queued visitors |
| H3 | High | Auth | Admin passcode and session travel over plain HTTP; no idle timeout; raw passcode accepted on every request |
| M1 | Medium | TLS | Local root CA can sign certificates for any domain; CA served over HTTP |
| M2 | Medium | Secrets | `docker build` copies `config/certs/` (private keys) into the image |
| M3 | Medium | Privacy | Default `auto` failover sends visitor data to any cloud provider with a key; stale fallback model IDs |
| M4 | Medium | Privacy / perf | All readings are public, unpaginated, and kept forever; kiosk downloads the full list every 5 s |
| M5 | Medium | Ops | **Reproduced:** launcher crashes on Windows when stdout is redirected |
| M6 | Medium | Printing | Auto-print blocks the pipeline for minutes; false "printed" status; distorted .NET prints |
| M7 | Medium | Printing | Desktop kiosk allows unlimited printing; O(N) refresh every 10 s |
| M8 | Medium | Tests | **Reproduced:** test suite writes fake runs into the production `logs/`; one test loads a real 300 MB model |
| M9 | Medium | Reliability | Local NMT can download a model during a visitor's run while it holds a lock |
| L1 | Low | Security | **Reproduced:** Windows path traversal in the `/api/pdf` fallback branch |
| L2 | Low | Security | Internal error details reach public endpoints and visitors |
| L3 | Low | Security | `/api/ai/translation/preload` loads any model ID or local path |
| L4 | Low | Audio | ElevenLabs harmony voice uses an out-of-range speed; TTS ignores print language |
| L5 | Low | Admin UI | BUas availability checks the ElevenLabs key |
| L6 | Low | Concurrency | Races between the server and an external watcher process |
| L7 | Low | PDF | Localized-PDF lock table and cache defects |
| L8 | Low | Perf | Uncached per-request work on public endpoints |
| L9 | Low | Vision | Vision backends disable themselves permanently after one error |
| L10 | Low | Files | Partial deletes on Windows |
| L11 | Low | Printing | Desktop kiosk overwrites the drawing thumbnail filename |
| L12 | Low | Docker | SIGTERM not handled; HTTPS listener starts inside the container |
| L13 | Low | Security | No CSP, Permissions-Policy, or HSTS headers |
| L14 | Low | UX | QR codes work only on the venue network |
| S1–S7 | Standards | Process | See §4: unmerged 26k-line branch, pasted transcript in docs, no CI or lint, unpinned deps, duplication, silent `except` blocks |

---

## 1. High severity

### H1. Public upload endpoint has no effective abuse limit

**Where:** [monitoring_server.py:169-170](monitoring_server.py#L169-L170) (15 uploads per IP per 60 s), [monitoring_server.py:113](monitoring_server.py#L113) (queue cap 20), [monitoring_server.py:2224-2315](monitoring_server.py#L2224-L2315) (no credential), [run_analysis_watcher.py:1155](analysis_watcher/run_analysis_watcher.py#L1155) (TTS), [run_analysis_watcher.py:1260-1268](analysis_watcher/run_analysis_watcher.py#L1260-L1268) (auto-print), [chat.py:776-793](analysis_watcher/chat.py#L776-L793) (cloud failover).

**Problem:** `/api/drawings` needs no credential, by design, so that `/draw` works. The only limits are 15 uploads per IP per minute and a global queue of 20 jobs. A real visitor needs at most one upload per ~30 s session. Nothing limits uploads per device, per origin, or globally. Each accepted upload runs the full pipeline: vision, 2–4 LLM calls (cloud calls when a cloud provider or `auto` failover is active), up to two paid ElevenLabs calls when TTS is on, and one physical print when `auto_print` is on.

**Impact:** One laptop on the venue Wi-Fi with a five-line script keeps the queue full, because each job takes 30–90 s and the script adds 15 per minute. Real visitors get HTTP 429 `ERR_QUEUE_FULL` or wait many minutes. With TTS or cloud models on, the script burns paid credits. With auto-print on, it prints up to 15 sheets per minute. The README recommends public cloud hosting (Render, Fly.io, Hugging Face Spaces), which puts this endpoint on the internet.

**Fix:**
1. Require a short-lived upload token on `/api/drawings`. `/draw` can get it from a staff-registered device session, or from a QR code that the gallery screen shows. Keep an explicit "open uploads" switch for single-laptop setups.
2. Lower the per-IP limit to about 3 per minute and add a global limit.
3. Reject an upload when one IP already has a job in the queue.
4. Add a daily cap for cloud LLM and TTS calls.

### H2. One slow run blocks the whole queue, and visitors get false timeouts

**Where:** [monitoring_server.py:313-420](monitoring_server.py#L313-L420) (single FIFO worker), [chat.py:856-869](analysis_watcher/chat.py#L856-L869), [chat.py:1119-1189](analysis_watcher/chat.py#L1119-L1189), [analysis.py:591-631](analysis_watcher/analysis.py#L591-L631), [run_analysis_watcher.py:1005-1155](analysis_watcher/run_analysis_watcher.py#L1005-L1155), [draw.html:1696-1790](web_ui/draw.html#L1696-L1790).

**Problem:**
1. No run has an end-to-end deadline. Only the optional `?sync=1` caller has one (185 s), and `/draw` does not use `sync`.
2. The per-call budgets do not hold. `LLMModel.chat()` computes a 125 s budget for a cold Ollama model. Inside that, `_chat_openai_or_ollama()` can send up to five sequential POSTs (reasoning-effort retry, no-image retry, 404 model-discovery retry, empty-content retry). Each POST gets the full read timeout, and the budget only applies between calls. The vision stage can send up to eight POSTs with a 60 s timeout each.
3. One run calls an LLM 3–7 times: vision (2 passes), analysis, optional condensation, poem, and native-LLM translation (3 or more calls). The worst case can exceed 10 minutes.
4. `/draw` stops polling after 165 × 1.2 s ≈ 198 s, counted from upload, not from run start. It ignores `state == "QUEUED"`.

**Impact:** During a cold start or a provider outage, the worker spends minutes on each drawing. Every visitor behind it sees "timed out" on `/draw` and leaves. Their reading then appears on the gallery screen later, in front of a different visitor.

**Fix:** Give each run a hard deadline (for example 150 s). Pass the deadline into every provider call and compute each request timeout as `min(configured_timeout, deadline - now)`. Count retries against the same deadline. In `/draw`, keep polling while `run.state === "QUEUED"`, show the queue position, and start the client timeout when the run starts.

### H3. Admin passcode and session travel in clear text on the LAN

**Where:** [monitoring_server.py:2204-2207](monitoring_server.py#L2204-L2207) (cookie without `Secure`), [monitoring_server.py:206-217](monitoring_server.py#L206-L217) (sliding expiry), [monitoring_server.py:1667-1689](monitoring_server.py#L1667-L1689) (raw passcode accepted as `X-Admin-Token` or `Bearer` on every request), [kiosk.html:496](web_ui/kiosk.html#L496) and [kiosk.html:778](web_ui/kiosk.html#L778) (auth-checked poll every 5 s), [start_everything.py:337-348](start_everything.py#L337-L348) (advertises `http://<ip>:8000/admin`).

**Problem:**
1. The server offers `/admin` and `/api/auth/login` over plain HTTP on all interfaces (`0.0.0.0`). The login body contains the passcode. The session cookie has no `Secure` flag, so the browser also sends it to `http://…:8000` after an HTTPS login.
2. Each authenticated request extends the server-side session by 12 h. `/kiosk` polls `/api/settings/printing` every 5 s, and that handler checks auth. A staff login on a public tablet therefore never becomes idle. The kiosk's 60 s inactivity reset clears only the search box. The browser cookie still expires 12 h after login.
3. The server accepts the raw passcode as a bearer token on every request. A captured header gives the permanent master secret, not a revocable session.

**Impact:** Anyone who sniffs the venue Wi-Fi gets full admin. They can point `ai.analysis.base_url` at their own server and receive every later visitor's drawing and reading, stop Ollama, delete the archive, or turn on auto-print. A visitor who reaches an unlocked staff tablet has admin for up to 12 h.

**Fix:** Serve `/admin` and `/api/auth/*` only over HTTPS and redirect HTTP, or bind admin to `127.0.0.1` by default. Set `Secure` on the cookie for HTTPS. Add an idle timeout that passive polls do not extend, and log staff out when the kiosk becomes inactive. Accept the passcode only at `/api/auth/login`.

---

## 2. Medium severity

### M1. Local root CA can sign certificates for any domain

**Where:** [config_manager.py:1178-1210](config_manager.py#L1178-L1210), [config_manager.py:1114-1118](config_manager.py#L1114-L1118), [monitoring_server.py:2039-2058](monitoring_server.py#L2039-L2058), [monitoring_server.py:2780](monitoring_server.py#L2780).

**Problem:** `ensure_lan_tls_cert()` creates a 10-year root CA (`CA:TRUE`) with no `nameConstraints`. The CA key is stored unencrypted in `config/certs/ca.key` on the exhibition PC. `os.chmod(0o600)` does not change ACLs on Windows, so other local users can read the key. The startup banner tells staff to download the CA from `http://<ip>:8000/api/tls/ca.crt`, over plain HTTP.

**Impact:** A device that trusts this CA accepts any certificate signed with the kiosk's key, including certificates for unrelated domains. A stolen or compromised exhibition PC becomes a MITM tool for every tablet that installed the CA. An attacker on the LAN can also replace the CA file in transit.

**Fix:** Add `nameConstraints` that permit only private IP ranges, `localhost`, and `.local`. Or drop the CA and use a self-signed leaf certificate. Serve the CA only over HTTPS and show its SHA-256 fingerprint in `/admin` for manual comparison.

### M2. `docker build` copies private keys into the image

**Where:** [.dockerignore](.dockerignore), [Dockerfile:20](Dockerfile#L20) (`COPY . .`).

**Problem:** `.dockerignore` excludes `config/settings.json` and `config/admin_pin.txt`, but not `config/certs/`. This checkout already contains `config/certs/`. The build also copies `.claude/` (including worktrees), the untracked PDF in the project root, and this report.

**Impact:** A locally built image contains `ca.key` and `server.key`. Pushing the image to any registry publishes them.

**Fix:** Exclude all of `config/`, plus `.claude/` and `*.pdf`. Prefer an allow-list `.dockerignore` (`*` first, then `!` for the paths the image needs).

### M3. Visitor data leaves the machine without opt-in, and fallback model IDs are stale

**Where:** [config_manager.py:167](config_manager.py#L167) and [config_manager.py:186](config_manager.py#L186) (`fallback_provider: "auto"`), [chat.py:719-748](analysis_watcher/chat.py#L719-L748), [chat.py:776-793](analysis_watcher/chat.py#L776-L793), [chat.py:1200](analysis_watcher/chat.py#L1200), [chat.py:1283](analysis_watcher/chat.py#L1283).

**Problem:** With the default `auto`, a failed or slow local call fails over to every cloud provider that has a key anywhere in the environment: Gemini, Groq, OpenRouter, OpenAI, Anthropic, and BUas. The request includes the drawing image. The failover model IDs are hard-coded IDs from 2024–2025 (`gemini-2.0-flash`, `claude-3-5-haiku-latest`, `gpt-4o-mini`, `meta-llama/llama-3.2-11b-vision-instruct:free`). Providers retire IDs like these on a schedule. Verify that each one still resolves.

**Impact:** An operator who chooses local Ollama for privacy still sends visitor content to cloud services whenever Ollama is slow. When an ID is retired, failover returns 404 and fails exactly when it is needed.

**Fix:** Default `fallback_provider` to `none`. Show in `/admin` which cloud endpoints are eligible for failover. Move the fallback model IDs into settings and exercise them with the existing "Test" button.

### M4. All visitor readings are public, unpaginated, and kept forever

**Where:** [monitoring_server.py:2095-2096](monitoring_server.py#L2095-L2096), [monitoring_server.py:1497-1550](monitoring_server.py#L1497-L1550), [monitoring_server.py:1990-2005](monitoring_server.py#L1990-L2005), [kiosk.html:777](web_ui/kiosk.html#L777), [index.html:1007](index.html#L1007).

**Problem:** `/api/files/prints` needs no auth and returns every reading with the full analysis, poem, and translations. `/shared_output/` serves every file except dotfiles, including in-flight `input_*.png` uploads and `docs/print_history.json`. Run IDs are timestamps, so URLs are guessable. Nothing prunes old prints automatically. Each open `/kiosk` downloads the complete list every 5 s. The gallery downloads it on every arrow-key press.

**Impact:** Visitor readings are exposed to anyone on the network, or to the internet on the cloud deployments that the README recommends. Response size grows with every print; a multi-day exhibition with about 1,000 prints sends megabytes every 5 s to each kiosk.

**Fix:** Paginate the list and remove the full text from it. Add a retention setting with automatic pruning. Add a random suffix to run IDs that appear in public URLs. Stop serving `input_*` files.

### M5. Launcher crashes on Windows when output is redirected — **Reproduced**

**Where:** [start_everything.py:205-218](start_everything.py#L205-L218), [start_everything.py:330-349](start_everything.py#L330-L349).

**Problem:** The launcher prints `✓`, `✗`, `✅`, and emoji. When stdout is a file or a pipe (Task Scheduler, a service wrapper, `> log.txt`), Python on Windows encodes output as cp1252, and `print("  ✓ …")` raises `UnicodeEncodeError`. In `run_command()` this happens after `Popen` has started the child. The `except` branch then prints `✗` and raises again.

**Reproduction:** `python -c "print('✓')" > out.txt` with the project venv fails with `UnicodeEncodeError: 'charmap' codec can't encode character '✓'`.

**Impact:** An auto-start setup crashes at launch and leaves an orphaned `monitoring_server.py` child that holds port 8000.

**Fix:** Call `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` and the same for `stderr` at startup, or print ASCII only.

### M6. Auto-print blocks the pipeline and reports false success

**Where:** [config_manager.py:1599-1665](config_manager.py#L1599-L1665), [config_manager.py:1562-1596](config_manager.py#L1562-L1596), [run_analysis_watcher.py:1260-1268](analysis_watcher/run_analysis_watcher.py#L1260-L1268).

**Problem:**
1. Auto-print runs on the single pipeline worker. On Windows, the fallback chain has sequential timeouts of 30 s (SumatraPDF), 45 s (.NET), 30 s (`PrintTo`), and 30 s (`Print`). One print can block the next visitor for more than two minutes.
2. `os.startfile(path, "print")` and `Start-Process -Verb PrintTo` return when a viewer process starts, not when a job reaches the spooler. The code then calls `mark_file_printed()` and returns `success`.
3. The .NET path draws each page into `e.MarginBounds`. That applies the default 1-inch margins and ignores the aspect ratio, so A4 pages print shrunk, and distorted on Letter paper.

**Fix:** Run printing on its own worker queue. Confirm the job through the spooler, or report "sent" instead of "printed". Fit the page image into `PageBounds` with the aspect ratio preserved.

### M7. Desktop print kiosk allows unlimited printing and refreshes in O(N)

**Where:** [print_kiosk_ui.py:435-457](print_service/print_kiosk_ui.py#L435-L457), [print_kiosk_ui.py:339-358](print_service/print_kiosk_ui.py#L339-L358), [print_kiosk_ui.py:244](print_service/print_kiosk_ui.py#L244).

**Problem:** Any visitor can print any drawing any number of times. The web kiosk requires staff login for host printing; the desktop kiosk does not. Nothing blocks double taps, so each tap submits a new job. Print jobs share the two-thread thumbnail pool, so they wait behind `pdftoppm` and ImageMagick work. Every 10 s, `refresh_queue()` re-reads every sidecar JSON file, and any change rebuilds every widget and pixmap.

**Fix:** Put host printing behind staff mode or a per-print limit. Disable the button while a job runs. Use a dedicated print executor. Paginate the list.

### M8. Test suite writes into the production logs — **Reproduced**

**Where:** [run_analysis_watcher.py:171-188](analysis_watcher/run_analysis_watcher.py#L171-L188), [test_pipeline_and_failover.py:102](tests/test_pipeline_and_failover.py#L102).

**Problem:** `_setup_logger()` attaches `FileHandler`s to the default `logs/` directory at import time. The tests set `LATENT_SPACE_LOGS_DIR` later, so the handlers keep writing to the real folder. Only `vision.log` is rebound, by `_log_vision_entry()`.

**Evidence:** One `unittest discover` run on 26 November added 103 lines to `logs/installation.log`, 10 lines to `logs/responses.log`, and 5 lines to `logs/predictions.csv`, all with fake run IDs such as `20261120_113000_001`. `/admin` shows these logs to operators.

**Second problem:** `test_content_json_has_print_pdf_before_playwright_render` does not stub translation. Local NMT is enabled by default, so the test loads the real 300 MB Marian model and takes about 15 s. On a machine without the Hugging Face cache, it tries to download the model.

**Fix:** Create the loggers lazily, or rebind all handlers in `reload_paths_from_env()`. Stub `get_translation_config` in the pipeline tests.

### M9. Local NMT can download a model during a visitor's run

**Where:** [translator.py:148-227](analysis_watcher/translator.py#L148-L227), [translator.py:274](analysis_watcher/translator.py#L274), [run_analysis_watcher.py:1046-1111](analysis_watcher/run_analysis_watcher.py#L1046-L1111), [run_analysis_watcher.py:1150](analysis_watcher/run_analysis_watcher.py#L1150).

**Problem:** The pipeline calls `_nmt_translate_batch()` with `allow_download=True`. If the model is not cached, the run downloads about 300 MB while it holds `_NMT_LOCK`. The pipeline waits 45 s, then calls `shutdown(wait=False, cancel_futures=True)`, which does not stop a running thread. The next run blocks on the same lock.

**Fix:** Pass `allow_download=False` inside the pipeline. Download only from `/admin` or `download_offline_assets.py`.

---

## 3. Low severity

### L1. Windows path traversal in the `/api/pdf` fallback branch — **Reproduced**

**Where:** [monitoring_server.py:2021-2033](monitoring_server.py#L2021-L2033).

**Problem:** When the pipeline import fails (`AI_AVAILABLE = False`), the handler builds `DOCS_DIR / f"print_{req_rid}.pdf"` without validating `req_rid`. The path is URL-decoded, so `%5C` becomes `\`, which Windows treats as a separator.

**Reproduction:** In a scratch tree, `docs / "print_..\..\..\..\secret.pdf"` resolved to a file four levels up and read it.

**Impact:** Unauthenticated read of any `.pdf` file on the drive, but only while the pipeline is unavailable.

**Fix:** Check `req_rid` against `_SAFE_RUN_ID_RE` before both branches.

### L2. Internal error details reach public endpoints and visitors

**Where:** [monitoring_server.py:2074-2081](monitoring_server.py#L2074-L2081), [monitoring_server.py:2177-2178](monitoring_server.py#L2177-L2178), [draw.html:1743](web_ui/draw.html#L1743).

**Problem:** `/api/pipeline/status` needs no auth and returns `last_error` and the per-run `error`. These strings contain provider names, model names, internal hosts and ports, and the failover chain. `/draw` shows the text to visitors. `do_POST` returns `str(e)` for every HTTP 500, including on the public upload route.

**Fix:** Return only error codes publicly. Keep the details behind admin auth.

### L3. `/api/ai/translation/preload` loads any model ID or local path

**Where:** [monitoring_server.py:2374-2384](monitoring_server.py#L2374-L2384), [translator.py:230-244](analysis_watcher/translator.py#L230-L244), [config_manager.py:867-873](config_manager.py#L867-L873).

**Problem:** The endpoint passes `local_nmt_model` from the request body straight to `from_pretrained()`. It skips `validate_translation_data()`. An admin session (see H3) can load any Hugging Face repo or local directory. Weights that are not safetensors are pickle files.

**Fix:** Validate the ID with the same regex. Pass `use_safetensors=True`.

### L4. ElevenLabs harmony voice uses an out-of-range speed

**Where:** [run_analysis_watcher.py:792-797](analysis_watcher/run_analysis_watcher.py#L792-L797).

**Problem:** The secondary voice uses `speed=1.3`. The ElevenLabs API documents `voice_settings.speed` as 0.7–1.2, so the call is probably rejected. The `except` branch then falls back to one voice and logs only a generic warning. Check the current API docs. Also, TTS always reads the English poem, even when the print language is Hungarian.

**Fix:** Clamp the speed to the documented range and log the API error text.

### L5. BUas availability checks the ElevenLabs key

**Where:** [analysis.py:891](analysis_watcher/analysis.py#L891), [.env.example:14](.env.example#L14).

**Problem:** `env_keys["buas"]` is true when `API_KEY` is set, but `API_KEY` is the ElevenLabs key. BUas uses `ACCESS_TOKEN` and `USER_KEY`. Rename the ElevenLabs variable to `ELEVENLABS_API_KEY` (the code already accepts it).

### L6. Races between the server and an external watcher process

**Where:** [run_analysis_watcher.py:120-160](analysis_watcher/run_analysis_watcher.py#L120-L160), [run_analysis_watcher.py:1344-1382](analysis_watcher/run_analysis_watcher.py#L1344-L1382), [run_analysis_watcher.py:844-884](analysis_watcher/run_analysis_watcher.py#L844-L884), [monitoring_server.py:2687](monitoring_server.py#L2687).

**Problem:** `.image_handled.sig` stores only the newest digest. If two uploads land within one 0.5 s poll, an external watcher can read upload A while the file holds B's digest, and run A a second time. At startup, `cleanup_orphaned_temp_files()` deletes `input_*`, `.tmp_*`, and `output_*.mp3` files that a still-running watcher process uses.

**Fix:** Allow one pipeline owner only: the server also takes `.watcher.lock` and refuses to run the pipeline if another process holds it. Or make the external watcher submit through HTTP.

### L7. Localized-PDF lock table and cache defects

**Where:** [run_analysis_watcher.py:735-762](analysis_watcher/run_analysis_watcher.py#L735-L762).

**Problem:** When the lock table exceeds 256 entries, the code clears it while other threads may hold locks, so two renders of the same PDF can run at once. Localized PDFs are cached forever and keep old attributions after settings change. Any visitor can trigger one Chromium render per (run, language) through `/api/pdf/…`.

**Fix:** Use an LRU of locks that never evicts a held lock. Invalidate the cache when attributions change. Limit concurrent renders.

### L8. Uncached per-request work on public endpoints

**Where:** [monitoring_server.py:2007-2015](monitoring_server.py#L2007-L2015), [index.html:585](index.html#L585), [index.html:869-881](index.html#L869-L881).

**Problem:** `/api/qr.svg` builds the QR code in pure Python and scores all eight masks on every request, with no cache. Each gallery screen fetches attributions and `content.json` every 4 s, and each attributions call parses `settings.json` under the global lock.

**Fix:** Cache QR SVGs by URL (LRU). Cache settings by file mtime.

### L9. Vision backends disable themselves permanently after one error

**Where:** [analysis.py:34](analysis_watcher/analysis.py#L34), [analysis.py:555-556](analysis_watcher/analysis.py#L555-L556), [analysis.py:746-750](analysis_watcher/analysis.py#L746-L750), [analysis.py:792-796](analysis_watcher/analysis.py#L792-L796).

**Problem:** One transient exception, such as a corrupt image, sets `failed = True` for CLIP, SigLIP, or ResNet until the process restarts. `_LAST_OLLAMA_VISION_ERROR` is a module global that the pipeline thread and `/api/ai/vision/test` share, so one can report the other's error.

### L10. Partial deletes on Windows

**Where:** [monitoring_server.py:2626-2666](monitoring_server.py#L2626-L2666).

**Problem:** `_delete_print_artifacts()` calls `unlink()` without handling `PermissionError`, which Windows raises when a PDF is open in a viewer or in the spooler. In "delete all", the loop swallows the error. Some sidecar files are then gone, but the print stays in `print_history.json` and the gallery sync skips it.

### L11. Desktop kiosk overwrites the drawing thumbnail filename

**Where:** [print_kiosk_ui.py:97-153](print_service/print_kiosk_ui.py#L97-L153).

**Problem:** When `print_{rid}.png` is missing, the kiosk writes a rendered PDF page to that exact name. The gallery archive uses the same file as the drawing image, so it later shows a whole page instead of the drawing.

**Fix:** Write the page render to a separate name, for example `print_{rid}.page.png`.

### L12. Docker runtime details

**Where:** [Dockerfile:34](Dockerfile#L34), [config_manager.py:260-266](config_manager.py#L260-L266).

**Problem:** Python runs as PID 1 and does not handle SIGTERM, so `docker stop` waits 10 s and then kills the process. The HTTPS listener starts on port 8443 inside the container. The image does not expose that port, and the certificate covers container IPs only.

**Fix:** Handle SIGTERM in `run_server()` or run with `--init`. Turn off the HTTPS listener by default in containers.

### L13. No CSP, Permissions-Policy, or HSTS headers

**Where:** [monitoring_server.py:1628-1632](monitoring_server.py#L1628-L1632).

**Problem:** Pages ship without `Content-Security-Policy`, `Permissions-Policy` (camera), or `Strict-Transport-Security` on the HTTPS listener. Output escaping is consistent today (see §6), so this is defense in depth. The inline `onclick` handlers in all pages block a strict CSP.

### L14. QR codes work only on the venue network

**Where:** [index.html:701-704](index.html#L701-L704), [config_manager.py:1320-1327](config_manager.py#L1320-L1327).

**Problem:** Without `remote_server_url`, the QR code encodes `http://<LAN-IP>:8000/…`. A visitor who opens it at home gets nothing. When the gallery is opened over HTTPS, the QR encodes the self-signed HTTPS origin, and phones show a certificate warning.

**Fix:** Show a warning in `/admin` while `remote_server_url` is empty. Always encode the HTTP LAN URL or the remote URL.

---

## 4. Standards and process

**S1. Unreviewable branch.** `feat/portable-multi-ai` holds 50 commits (+26,252 / −4,306 lines across 50 files) that are not on `main`. A change of this size cannot be reviewed as one PR. Split it into topic PRs (TLS, admin redesign, translation, exhibition mode) and merge them in order.

**S2. Pasted transcript in the docs.** The uncommitted change to `STARTUP_GUIDE.md` is a pasted assistant reply ("1. Option 1 Implemented…", "all 64 / 64 tests pass", stale line references). Do not commit it. Rewrite the useful parts (recommended Ollama models for 8 GB VRAM) as normal guide text.

**S3. Commit messages.** 5 of the last 40 commit subjects do not use `<type>(<scope>): <summary>`: `01cde98`, `bed04b4`, `036c2ab`, `e2c3962`, `e6c28bb`.

**S4. No automated gate.** The repo has no CI workflow, no linter or formatter config, no type checking, and no `.pre-commit-config.yaml`. The test suite is the only gate, and it runs only by hand.

**S5. Unpinned dependencies and environment drift.** All requirements use `>=` and there is no lock file. `print_service/requirements.txt` pins nothing. The project venv does not have `psutil`, although `requirements.txt` lists it, so service detection returns `"ready"` in development but works in Docker.

**S6. Duplication with drift.**
- Vision-model hint lists exist in four places with different contents: [chat.py:30](analysis_watcher/chat.py#L30), [analysis.py:856](analysis_watcher/analysis.py#L856), [monitoring_server.py:956](monitoring_server.py#L956), and the `admin.html` JavaScript.
- Thinking-model hints exist in three places.
- The Ollama `localhost` → `127.0.0.1` rewrite exists in six places. It is missing in `prewarm_ollama_model()`, `unload_ollama_model()`, and `stop_ollama_serving()`, so those calls still hit the 2 s IPv6 stall on Windows.
- `escapeHtml` exists four times.

**S7. Size and silent failures.**
- `monitoring_server.py` is 2,809 lines with an if-chain router. `admin.html` (4,146 lines) and `draw.html` (2,977 lines) are single files with inline CSS and JavaScript. The browser gaze math has no JavaScript tests; only its Python port is tested.
- Project Python has 128 `except Exception:` blocks, many followed by `pass`. Failures in cleanup, print history, and status code disappear without a log line. Log at `warning` level at least.

---

## 5. Tests

- All 77 tests pass. The run takes 81 s; one test accounts for 15 s (see M8).
- The suite writes to the real `logs/` directory (M8). Fix this before you trust the logs during an exhibition.
- Add a regression test with each fix where it prevents a concrete failure:
  - H2: a provider stub that returns 400 four times must not exceed the run deadline.
  - H3: the login cookie carries `Secure` on HTTPS, and passive polls do not extend the session.
  - L1: `/api/pdf/..%5C..%5Cx/en` returns 400 when `AI_AVAILABLE` is false.
  - M5: the launcher runs with stdout redirected to a cp1252 file.
  - M8: importing `run_analysis_watcher` under a redirected `LATENT_SPACE_LOGS_DIR` writes nothing to the default `logs/`.

---

## 6. What is in good shape

- Static routes (`/shared_output`, `/assets`, `/vendor`) resolve the path, check `is_relative_to`, and reject dotfiles.
- Upload validation checks magic bytes and image dimensions before decoding.
- Login uses `hmac.compare_digest` and a per-IP rate limit. The cookie is `HttpOnly` and `SameSite=Strict`, which covers CSRF for cookie auth.
- Settings writes are atomic and locked across processes. An unreadable `settings.json` is never overwritten.
- API keys are masked on read, cleared when the endpoint changes, and redacted from error text.
- All four pages now use the same 5-character `escapeHtml`. Every `innerHTML` sink I checked escapes server data.
- Fixes from the previous report, spot-checked:
  - Right-eye landmarks are correct ([eye_tracking.py:17-18](gaze_painter/eye_tracking.py#L17-L18)).
  - `detectForVideo` uses monotonic timestamps ([draw.html:2301-2318](web_ui/draw.html#L2301-L2318)).
  - The edge-drawing crop is centered ([preprocess_image.py:105-128](analysis_watcher/preprocess_image.py#L105-L128)).
  - The Windows lock no longer writes into a locked region ([config_manager.py:279-290](config_manager.py#L279-L290)).
  - `Image.open` uses a context manager ([analysis.py:715-716](analysis_watcher/analysis.py#L715-L716)).

---

## 7. Recommended fix order

1. **H1 + H3:** upload token, HTTPS-only admin, idle timeout, passcode only at login.
2. **H2:** per-run deadline and queue-aware polling in `/draw`.
3. **M2:** fix `.dockerignore` (5 minutes).
4. **M5:** UTF-8 stdout in the launcher (5 minutes).
5. **M8:** isolate test logging, then delete the test entries from `logs/*.log`.
6. **M1:** name-constrain the CA, or drop it.
7. **M3:** default failover to `none`.
8. **M6 + M7:** separate print worker, honest print status, staff gate on the desktop kiosk.
9. **M4 + M9:** pagination and retention; no downloads inside the pipeline.
10. **S1–S4:** discard the transcript in `STARTUP_GUIDE.md`, split the branch into PRs, add CI that runs the test suite.
