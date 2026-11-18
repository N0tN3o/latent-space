# Latent Space — Critical Codebase Audit Report

**Date:** November 18, 2026  
**Branch Audited:** `feat/portable-multi-ai`  
**Status:** All 21 identified findings have been fixed and verified across the 46-test suite (`python -m unittest discover -s tests -v`).  
**Scope:** Full-stack review of `config_manager.py`, `monitoring_server.py`, `start_everything.py`, `qr_utils.py`, `analysis_watcher/*`, `gaze_painter/*`, `print_service/*`, `index.html`, `web_ui/*`, `Dockerfile`, and `tests/*`.

---

## Executive Summary

The codebase has undergone substantial hardening across recent commits—including atomic file replacements, a bounded FIFO analysis worker queue, multi-provider AI failover with secret redaction, decompression-bomb protection, and a 43-test suite (all currently passing).

However, a deep line-by-line audit uncovered **21 remaining bugs, race conditions, mathematical/coordinate errors, and security/reliability edge cases** that were either missed in earlier passes or subtly introduced during recent fixes.

### Key Highlights of What Was Found
1. **Off-Center Cropping Bug in `preprocess_image.py`:** Drawings near any canvas edge are pasted off-center into `image_processed.png`, which directly corrupts the bilateral Rorschach symmetry calculation (`horiz_sym` / `vert_sym`) in `analysis.py`.
2. **Right-Eye Landmark Swap in Desktop `eye_tracking.py` vs `web_ui/draw.html`:** `eye_tracking.py` maps `right_eye_outer` to landmark `362` (medial/inner tear-duct corner) instead of `263` (lateral/outer corner), shifting the bi-ocular head-pose midpoint into the left eye and inflating head-pose gain by ~1.5×.
3. **Unhandled `detectForVideo` Exception Kills Browser Gaze Loop (`web_ui/draw.html`):** `faceLandmarker.detectForVideo(video, now)` is called outside `try/catch` before `requestAnimationFrame(processVideoFrame)`. A single duplicate timestamp or zero-dimension video frame permanently freezes webcam eye tracking.
4. **Windows Mandatory File Lock Crash (`config_manager.py`):** `_try_lock_fd()` executes `os.write(fd, b"\x00")` *outside* its `try/except OSError` block; on Windows (`msvcrt.locking`), writing to byte 0 while another process holds the lock raises an uncaught `PermissionError` instead of waiting for the lock timeout.
5. **Dual-Execution Race When External `run_analysis_watcher.py` Is Running:** `_handled_image_sig` is stored in process memory; `POST /api/drawings` both enqueues the job in `monitoring_server.py` and writes `shared_output/image.png`, causing any external watcher process to run the pipeline a second time in parallel.
6. **ElevenLabs TTS Breaks in Docker & Single-Voice Setups (`tts_el.py`, `run_analysis_watcher.py`, `Dockerfile`):** `Dockerfile` omits `ffmpeg` (required by `pydub`), `tts_el.py` hardcodes `"eleven_v3"`, and clearing `voice_id_2` passes `voice_id=""` to ElevenLabs instead of falling back to single-voice synthesis.

---

## 1. Critical & High-Severity Bugs

### 1.1. `preprocess_image.py` Shifts Edge Drawings Off-Center, Corrupting Rorschach Symmetry (`analysis_watcher/preprocess_image.py:100-125`)
* **Category:** Computer Vision / Mathematical Implementation Bug
* **Location:** `analysis_watcher/preprocess_image.py:100-125` and `analysis_watcher/analysis.py:270-278`
* **Root Cause:**
  ```python
  half = target_side // 2
  src_x1 = max(0, cx - half)
  src_y1 = max(0, cy - half)
  src_x2 = min(w, cx + half)
  src_y2 = min(h, cy + half)
  ...
  dst_y1 = max(0, (target_side - cropped.shape[0]) // 2)
  dst_x1 = max(0, (target_side - cropped.shape[1]) // 2)
  square[dst_y1:dst_y1 + cropped.shape[0], dst_x1:dst_x1 + cropped.shape[1]] = cropped
  ```
  When a visitor's drawing is near any border of the `1280×720` canvas (or spans most of the vertical height so `target_side > 720`), `cx - half < 0` or `cx + half > w` clamps `src_x1` or `src_x2`.
  In `cropped`, the drawing's center `cx` is located at `cx - src_x1` (not at `cropped.shape[1] // 2`).
  By placing `cropped` at `dst_x1 = (target_side - cropped.shape[1]) // 2`, the missing out-of-bounds margin is split equally on both sides of `square`, **pushing the drawing off-center inside `image_processed.png`**.
  * **Concrete Example:** On a `1280×720` canvas, a tall symmetrical drawing (`y_dim = 680`, `x_dim = 120`, `target_side = 748`, `half = 374`) drawn near the left side (`upper_x = 20, lower_x = 140, cx = 80`) gives `src_x1 = 0, src_x2 = 454` (`cropped` width `454`). `dst_x1 = (748 - 454) // 2 = 147`. The drawing's center lands at `x = 147 + 80 = 227` instead of the square's center `374`—an error of **147 pixels**!
  * Because `run_analysis_watcher.py:653` passes `image_processed_{rid}.png` into `analyze_drawing()`, `analyze_stroke_geometry()` (`analysis.py:270-278`) flips `image_processed.png` across `w // 2` (`256`) and computes `_dilated_dice` overlap, which drops to ~`0.0` for a genuinely symmetrical drawing.
* **Fix:**
  Compute the destination offset directly from the clamped source offset so `(cx, cy)` always maps to `(half, half)`:
  ```python
  raw_x1 = cx - half
  raw_y1 = cy - half
  src_x1 = max(0, raw_x1)
  src_y1 = max(0, raw_y1)
  src_x2 = min(w, raw_x1 + target_side)
  src_y2 = min(h, raw_y1 + target_side)
  ...
  dst_x1 = src_x1 - raw_x1  # > 0 only when raw_x1 < 0
  dst_y1 = src_y1 - raw_y1  # > 0 only when raw_y1 < 0
  ```

---

### 1.2. Right-Eye Outer/Inner Landmarks Are Swapped in `gaze_painter/eye_tracking.py` (`gaze_painter/eye_tracking.py:17-18, 422-423, 442-448`)
* **Category:** Gaze Tracking / Coordinate Math Bug
* **Location:** `gaze_painter/eye_tracking.py:17-18` and `442-448`
* **Root Cause:**
  In MediaPipe FaceMesh:
  * Landmark `33` = Left eye outer corner (lateral canthus)
  * Landmark `133` = Left eye inner corner (medial canthus)
  * Landmark `362` = Right eye **inner** corner (medial canthus, by the nose bridge)
  * Landmark `263` = Right eye **outer** corner (lateral canthus, by the temple)

  In `web_ui/draw.html:1098-1099`, this was properly written as:
  ```javascript
  const lOuter = pt(33);
  const rOuter = pt(263);
  ```
  However, in `gaze_painter/eye_tracking.py:17-18`:
  ```python
  "right_eye_outer": 362,
  "right_eye_inner": 263,
  ```
  And on lines `442-443`:
  ```python
  eye_mid = 0.5 * (l_outer + r_outer)
  outer_dist = max(float(np.linalg.norm(r_outer - l_outer)), 1.0)
  ```
  Averaging landmark `33` (left outer corner) and landmark `362` (right inner corner) places `eye_mid` inside the left eye rather than between the eyes on the nose bridge, and shrinks `outer_dist` to ~65% of the true bi-ocular width. This adds a permanent horizontal bias to `head_offset[0]` and inflates head-pose sensitivity by ~50% in the desktop Gaze Painter.
* **Fix:**
  Swap `"right_eye_outer": 263` and `"right_eye_inner": 362` in `FACEMESH_LANDMARKS` in `gaze_painter/eye_tracking.py`.

---

### 1.3. Uncaught `faceLandmarker.detectForVideo` Exception Permanently Kills Browser Gaze Loop (`web_ui/draw.html:1119-1272`)
* **Category:** Frontend Concurrency / Error Handling Bug
* **Location:** `web_ui/draw.html:1131-1133, 1271`
* **Root Cause:**
  ```javascript
  if (video.readyState >= 2 && faceLandmarker) {
      const res = faceLandmarker.detectForVideo(video, now);
      ...
  }
  requestAnimationFrame(processVideoFrame);
  ```
  MediaPipe's `FaceLandmarker.detectForVideo(video, timestamp)` requires strictly monotonically increasing timestamps (`now > lastDetectTimestamp`) and non-zero video dimensions. In browsers, `requestAnimationFrame` callbacks or manual restarts can occasionally pass a timestamp `<=` the previous call (or `performance.now()` with sub-ms clamping), causing MediaPipe WASM to throw an exception. Because `requestAnimationFrame(processVideoFrame)` is at the bottom of `processVideoFrame` outside any `try/catch`, a single thrown error terminates the loop forever—freezing the webcam feed and gaze tracking until the page is reloaded.
* **Fix:**
  1. Track `lastDetectTs` and enforce `const detectTs = Math.max(now, lastDetectTs + 1); lastDetectTs = detectTs;`.
  2. Wrap the body of `processVideoFrame` in `try { ... } catch (err) { console.warn(err); } finally { if (webcamRunning) requestAnimationFrame(processVideoFrame); }`.

---

### 1.4. Windows Mandatory File-Lock `PermissionError` in `_try_lock_fd` (`config_manager.py:170-180, 233-257`)
* **Category:** Concurrency / Cross-Process Locking Bug (Windows)
* **Location:** `config_manager.py:170-180`
* **Root Cause:**
  ```python
  def _try_lock_fd(fd: int) -> bool:
      if platform.system() == "Windows":
          import msvcrt
          if os.fstat(fd).st_size == 0:
              os.write(fd, b"\x00")
          try:
              os.lseek(fd, 0, os.SEEK_SET)
              msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
              return True
          except OSError:
              return False
  ```
  On Windows, `msvcrt.locking` acquires a **mandatory** OS byte-range lock on byte `0`, not an advisory lock. Furthermore, `msvcrt.locking` can lock byte range `[0, 1)` even on a 0-byte file without needing `os.write`. If Process A creates the lock file and locks byte `0` before flushing, or if Process B calls `os.write(fd, b"\x00")` at byte `0` while Process A holds the mandatory lock on byte `0`, `os.write` raises `PermissionError` (`OSError`) **outside** the `try/except OSError` block!
  In `_interprocess_file_lock()`, this uncaught `PermissionError` aborts the operation immediately instead of polling until `timeout_sec`.
* **Fix:**
  Move `os.lseek` / `os.write` inside the `try/except OSError:` block (or omit `os.write` entirely since `msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)` locks byte offset `0..1` even when `st_size == 0`).

---

### 1.5. Duplicate Pipeline Execution When External `run_analysis_watcher.py` Is Running (`monitoring_server.py:453, 1326-1334`)
* **Category:** Concurrency / Multi-Process Synchronization Bug
* **Location:** `monitoring_server.py:447-466, 1326-1334` and `analysis_watcher/run_analysis_watcher.py:113-121, 865-900`
* **Root Cause:**
  When a drawing is submitted via `POST /api/drawings`:
  1. `monitoring_server.py` calls `enqueue_pipeline_job(image_bytes=img_bytes, ...)` which queues the job on `monitoring_server.py`'s internal `_worker_thread`.
  2. Immediately afterward (line 1333), `monitoring_server.py` calls `write_live_image_and_mark_handled(target_path, img_bytes)`, which writes `shared_output/image.png` and updates `_handled_image_sig` **in `monitoring_server.py`'s process memory**.
  3. If an external `run_analysis_watcher.py` process is running (which `monitoring_server.py:453` explicitly checks for and allows via `if get_service_status("run_analysis_watcher") == "running": return`), that external process has its **own separate `_handled_image_sig` in memory**. It sees `shared_output/image.png` change on disk and launches a **second parallel pipeline run** on the same drawing, racing on `content.json`, `image_processed.png`, `heatmap.png`, and triggering duplicate auto-prints!
* **Fix:**
  Either:
  * Persist the handled signature in a small lock-protected sidecar file (e.g., `shared_output/.image_handled.sig`) read by `watch_loop()` before triggering, **or**
  * Do not overwrite `shared_output/image.png` from `POST /api/drawings` when the job is already snapshotted to `input_{rid}.png` (or only write `image.png` after persisting `.image_handled.sig` on disk).

---

### 1.6. `save_settings()` Can Overwrite Disk Changes or Corrupt `settings.json` on Invalid Section Types (`config_manager.py:737-774`)
* **Category:** State Persistence / Validation Bug
* **Location:** `config_manager.py:737-774` and `monitoring_server.py:1386-1390`
* **Root Cause:**
  1. **Stale in-memory base:** In `save_settings_unlocked()`:
     ```python
     existing = _memory_settings
     if existing is None and SETTINGS_FILE.exists():
         existing = _read_existing_settings_raw()
     ```
     Unlike `update_settings_section()` (which always re-reads `SETTINGS_FILE` from disk under `.settings.lock` and refuses to overwrite a corrupt file), `save_settings_unlocked()` uses `_memory_settings` whenever it is non-`None`, ignoring any edits written to `settings.json` by another process. Conversely, if `_memory_settings` is `None` and `settings.json` is temporarily unreadable/corrupt, `save_settings()` swallows the exception (`existing = None`) and overwrites `settings.json` with `DEFAULT_SETTINGS`, wiping saved API keys and prompts.
  2. **Non-dict section bypass:** In `_validate_settings_sections(data)`:
     ```python
     if isinstance(cleaned.get("ai"), dict):
         cleaned["ai"] = validate_ai_data(cleaned["ai"])
     ```
     If an admin client posts `{"ai": "invalid"}` or `{"printing": 123}` to `POST /api/settings`, `isinstance(..., dict)` is `False`, so validation is **skipped** and `_deep_merge` replaces `settings["ai"]` with `"invalid"`. Subsequent calls to `settings.get("ai", {}).get(...)` across the server then crash with `AttributeError: 'str' object has no attribute 'get'`.
* **Fix:**
  In `_validate_settings_sections()`, raise `ValueError` if any known section key (`ai`, `tts`, `attributions`, `printing`, `server`) is present and not a `dict` (or if unknown top-level keys are supplied). In `save_settings_unlocked()`, read from `SETTINGS_FILE` consistently with `update_settings_section()`.

---

## 2. Medium-Severity Bugs (Pipeline, AI Failover, Gaze, Audio, & UI)

### 2.1. ElevenLabs TTS Fails in Docker and When `voice_id_2` Is Blank (`analysis_watcher/run_analysis_watcher.py:482-512`, `analysis_watcher/tts_el.py:7`, `Dockerfile:10-14`)
* **Category:** Audio Pipeline / Environment Bug
* **Location:** `analysis_watcher/run_analysis_watcher.py:495-504`, `analysis_watcher/tts_el.py:7`, `Dockerfile:10-14`
* **Root Cause:**
  1. `run_analysis_watcher.py:496` reads `v2 = tts_cfg.get("voice_id_2", "tJHJUEHzOkMoPmJJ5jo2")`. When an operator clears the "Secondary Harmony Voice ID" input in `/admin` (`voice_id_2: ""`), `v2` is `""` (empty string, not missing!). `run_el_tts` then calls ElevenLabs with `voice_id=""`, which fails with an HTTP error and aborts audio generation completely.
  2. `decrease_volume()` and `overlay_audio()` in `postprocess_audio.py` use `pydub.AudioSegment.from_file()`, which requires the `ffmpeg` (or `avconv`) binary on `PATH`. `Dockerfile` does **not** install `ffmpeg`, and many Windows/macOS machines do not have `ffmpeg` installed. When `pydub` raises `FileNotFoundError` for `ffmpeg`, the `except` block discards `rae_file` (`output_rae_{rid}.mp3`) even though `rae_file` was already synthesized as a valid MP3 by ElevenLabs!
  3. `tts_el.py:7` hardcodes `model_id="eleven_v3"` instead of a universally supported model like `"eleven_multilingual_v2"` or `"eleven_turbo_v2_5"`.
* **Fix:**
  * If `v2` is empty, synthesize only `v1` directly to `output_{rid}.mp3` (atomically) without calling `pydub`.
  * If `v2` is set, wrap `decrease_volume` / `overlay_audio` in a fallback: if `pydub`/`ffmpeg` fails, atomically move `rae_file` to `output_{rid}.mp3` so the primary voice narration still works without `ffmpeg`.
  * Install `ffmpeg` in `Dockerfile` or make `pydub` overlay strictly optional.

---

### 2.2. Thread-Unsafe Mutation of Global `PIL.Image.MAX_IMAGE_PIXELS` (`monitoring_server.py:270-280`)
* **Category:** Concurrency / Race Condition
* **Location:** `monitoring_server.py:270-280`
* **Root Cause:**
  ```python
  orig_limit = Image.MAX_IMAGE_PIXELS
  Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
  try:
      ...
  finally:
      Image.MAX_IMAGE_PIXELS = orig_limit
  ```
  `LockedThreadingHTTPServer` processes requests concurrently across threads. If Thread A and Thread B call `_is_valid_image_bytes()` simultaneously, Thread B captures `orig_limit = MAX_IMAGE_PIXELS` while Thread A restores `Image.MAX_IMAGE_PIXELS = 178956970` right before Thread B calls `Image.open()`, or leaves `Image.MAX_IMAGE_PIXELS` permanently altered.
* **Fix:**
  Set `Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS` once at module initialization instead of mutating and restoring it per request.

---

### 2.3. Unclosed `PIL.Image.open` Handle in `analysis.py` Causes Windows File-Lock Leak (`analysis_watcher/analysis.py:401`)
* **Category:** Resource Leak / Windows File Locking
* **Location:** `analysis_watcher/analysis.py:401`
* **Root Cause:**
  ```python
  pil_img = Image.open(filename).convert("RGB")
  ```
  `Image.open(filename)` returns a lazy `PIL.PngImagePlugin.PngImageFile` whose underlying OS file descriptor stays open until the object is garbage-collected. On Windows, if GC has not run before `run_analysis_watcher.py:844` executes `p.unlink()` in its `finally:` block, `unlink()` raises `PermissionError: [WinError 32]` (which is silently ignored), leaking `image_processed_{rid}.png` files in `shared_output/`.
* **Fix:**
  ```python
  with Image.open(filename) as im:
      pil_img = im.convert("RGB")
  ```

---

### 2.4. `_ollama_supports_vision` Permanently Caches `False` on Transient Timeout & Misses `qwen2.5-vl` / `gemma3:latest` (`analysis_watcher/chat.py:27-30, 545-575`)
* **Category:** AI Failover / Vision Detection Bug
* **Location:** `analysis_watcher/chat.py:27-30, 545-575`
* **Root Cause:**
  1. `_ollama_supports_vision` queries `POST {host_root}/api/show` with `timeout=1.0`. When Ollama is busy loading a model into VRAM or starting up, `/api/show` frequently takes >1.0s and times out.
  2. When `/api/show` fails or times out, `supported` falls back to substring matching against `_VISION_NAME_HINTS` and **stores the fallback boolean in `_OLLAMA_VISION_CAPS[cache_key]` permanently**.
  3. `_VISION_NAME_HINTS` in `chat.py:27-30` is missing `"qwen2.5-vl"` (with hyphen, which is present in `analysis.py:491` but omitted in `chat.py:29`), `"gemma3:latest"`, `"gemma3"` (non-1b), `"mistral-small3.1"`, and `"llama4"`. If `/api/show` times out once, vision is permanently disabled for that model until the process restarts.
* **Fix:**
  Only cache in `_OLLAMA_VISION_CAPS` when `/api/show` succeeds (`supported is not None`), and align `_VISION_NAME_HINTS` between `chat.py` and `analysis.py`.

---

### 2.5. `validate_safe_url` Allows Relative Paths (`/path`) for `ai.{role}.base_url` and `server.remote_server_url` (`config_manager.py:415-429, 539, 595`)
* **Category:** Input Validation Bug
* **Location:** `config_manager.py:415-429, 539, 595`
* **Root Cause:**
  `validate_safe_url()` accepts any string starting with `"/"` (because it was designed to validate `attributions.logo_path = "/kasboek-logo.jpg"`). However, it is also used to validate `ai.{role}.base_url` and `server.remote_server_url`.
  If an admin enters a relative path such as `"/v1"` for `base_url` or `"/gallery"` for `remote_server_url`, validation succeeds, causing `requests.post("/v1/chat/completions")` to crash with `MissingSchema` and breaking QR code URLs (`get_public_base_url` ignores `remote_server_url` when it doesn't start with `http(s)://`, silently reverting after reporting save success).
* **Fix:**
  Add an `allow_relative: bool = True` parameter to `validate_safe_url()`, and pass `allow_relative=False` for `ai.{role}.base_url`, `server.remote_server_url`, `website_url`, `company_linkedin_url`, and `founders[i].url`.

---

### 2.6. `_parse_env_line` Truncates Unquoted Passwords/Keys Containing `#` (`config_manager.py:336-343`)
* **Category:** Configuration Parsing Bug
* **Location:** `config_manager.py:336-343`
* **Root Cause:**
  ```python
  if "#" in val:
      val = val.split("#", 1)[0].strip()
  ```
  In `.env.example`, variables like `LATENT_SPACE_ADMIN_PASSWORD=` are shown unquoted. If a user sets `LATENT_SPACE_ADMIN_PASSWORD=MySecret#2026` without quotes, `_parse_env_line` splits on `#` even when not preceded by whitespace, silently truncating the password to `MySecret`.
* **Fix:**
  Only strip inline `#` comments in unquoted `.env` values when `#` is preceded by whitespace:
  ```python
  val = re.split(r"\s+#", val, maxsplit=1)[0].strip()
  ```

---

### 2.7. Offline / Firewalled Exhibition Networks Hang Playwright & Browser Page Loads on CDN Scripts (`index.html:7`, `web_ui/admin.html:7, 708`, `analysis_watcher/run_analysis_watcher.py:425`)
* **Category:** Offline Reliability / Performance Bug
* **Location:** `index.html:7`, `web_ui/admin.html:7, 708`, `analysis_watcher/run_analysis_watcher.py:425`
* **Root Cause:**
  `index.html` includes `<script src="https://cdn.jsdelivr.net/npm/qrcode-generator@1.4.4/qrcode.min.js"></script>` synchronously in `<head>`, and `admin.html` includes Bootstrap CSS/JS synchronously from `cdn.jsdelivr.net`.
  On an exhibition LAN that is connected to a router without internet (or behind a firewall that drops outbound packets rather than sending immediate TCP RST / ICMP unreachable):
  1. `page.goto(url, wait_until="networkidle", timeout=15000)` in `save_print_pdf()` hangs for the full 15 seconds waiting for `cdn.jsdelivr.net` to time out, then throws `TimeoutError` and falls back to Matplotlib on every drawing.
  2. Opening `/` or `/admin` in a browser stalls initial page rendering while waiting for the synchronous `<head>` script/stylesheet to time out.
* **Fix:**
  Since `qr_utils.py` already provides a 100% self-contained QR SVG generator at `/api/qr.svg`, `index.html` can either bundle `qrcode.min.js` locally in `/vendor/` (or load it asynchronously / use `/api/qr.svg` directly without any external CDN dependency).

---

### 2.8. `gaze_painter/run_gaze_painter.py` Loads `calibration.json` at Startup and Immediately Wipes It (`gaze_painter/run_gaze_painter.py:80-83, 166-178, 228-231`)
* **Category:** State Machine Logic Bug
* **Location:** `gaze_painter/run_gaze_painter.py:80-83, 166-178, 228-231`
* **Root Cause:**
  In `GazeTrackingPainter.__init__()`:
  ```python
  calibration = load_calibration()
  if calibration:
      self.tracker.load_calibration(calibration)
  self.has_calibration = False
  ```
  `self.state` starts in `STATE_SEARCHING`. As soon as a face is seen for 5 frames, line 231 calls `self.transition_to(STATE_CALIBRATING)`, which calls `self.tracker.start_auto_calibration()` -> `self.reset_visitor_state()`, overwriting `center_norm`, `weights_x`, `weights_y`, and `blink_ear_thr` back to hardcoded defaults before collecting new samples. Thus, `load_calibration()` on startup has zero effect.

---

### 2.9. `_render_pdf_fallback` Text Overflow on Long LLM Outputs & `A5` Paper (`analysis_watcher/run_analysis_watcher.py:212-228, 284-305`)
* **Category:** PDF Rendering Layout Bug
* **Location:** `analysis_watcher/run_analysis_watcher.py:212-228, 284-305`
* **Root Cause:**
  1. `_fit_text_block()` reduces `fontsize` down to `min_fontsize = 6.2`, but if the LLM ignores the word-count prompt and outputs a 25+ line analysis or poem, `_fit_text_block()` returns all lines uncapped, causing the text block to overflow downward through the Attention Span heatmap, QR code, and Kasboek attribution footer.
  2. `_fit_text_block()` hardcodes `780.0` points (A4 height) and `base_width=48`/`42` (A4 width). When `paper_format` is set to `"A5"` (`5.83 × 8.27` in = `595 pt` tall), line height as a fraction of the page is 31% larger than estimated, causing vertical collisions.
* **Fix:**
  Scale `page_height_pt = figsize[1] * 72.0` and `width_scale = figsize[0] / 8.27`, and truncate with an ellipsis (`"..."`) if `line_count` still exceeds `max_lines = int(max_height_frac * page_height_pt / (fontsize * linespacing))` at `min_fontsize`.

---

### 2.10. `index.html` History Carousel (`stepHistory`) Race Condition & Duplicate Index `0` Step (`index.html:687-723`)
* **Category:** Frontend State / UX Bug
* **Location:** `index.html:687-723`
* **Root Cause:**
  1. `stepHistory(delta)` is `async` and awaits `fetch('/api/files/prints')` before updating `historyIdx`. Pressing `ArrowLeft` / `ArrowRight` rapidly fires multiple concurrent calls that all read the old `historyIdx` before the first fetch completes.
  2. When stepping backward from Live (`historyIdx = -1`, `delta = 1`), lines 699-701 jump to `nextIdx = 1` (skipping `historyList[0]` because it is the currently displayed live artwork). However, when the user then presses `Next` (`delta = -1`) once from `historyIdx = 1`, `nextIdx` becomes `0` instead of returning to Live (`-1`), displaying the same artwork as Live in muted Archive mode and requiring a second click on `Next` to return to Live.
* **Fix:**
  Add an `isSteppingHistory` lock/guard, and when `delta < 0 && nextIdx === 0 && liveRunId && historyList[0]?.run_id === liveRunId`, call `returnToLive()` directly.

---

### 2.11. `print_service/print_kiosk_ui.py` Blocking Main Thread, Non-Atomic Thumbnail Race, and $O(N^2)$ Disk Reads (`print_service/print_kiosk_ui.py:66-124, 307-320, 393-417`)
* **Category:** Desktop UI Threading & Concurrency Bug
* **Location:** `print_service/print_kiosk_ui.py:66-124, 307-320, 393-417`
* **Root Cause:**
  1. **Non-atomic thumbnail write race:** `PrintRecord.ensure_thumbnail_file()` runs on a background thread (`_thumb_pool`) and writes directly to `print_{rid}.png` via `pix.save(str(png_path))` / `pdftoppm` / `magick`. Meanwhile, the 10-second `sync_timer` on the Qt main thread checks `if png_path.exists(): self.set_thumbnail_file(str(png_path))` and can read a half-written PNG file, resulting in a null `QPixmap`.
  2. **GUI freeze on print:** `print_pdf()` calls `send_pdf_to_printer()` synchronously on the Qt UI thread, freezing the fullscreen kiosk for up to 30–75 seconds while subprocesses execute.
  3. **$O(N^2)$ `print_history.json` reads:** Every 10 seconds, `refresh_queue()` instantiates `PrintRecord` for all $N$ PDFs, and each `PrintRecord.__init__` reads and parses `print_history.json` from disk individually.

---

## 3. Low-Severity & Defense-in-Depth Hardening

### 3.1. `<audio>` Streaming Lacks `Accept-Ranges` / `206 Partial Content` and Logs Unhandled `BrokenPipeError` (`monitoring_server.py:950-994, 1218-1228`)
* **Location:** `monitoring_server.py:950-994, 1218-1228`
* **Details:**
  * Browsers (especially Safari on iOS/macOS) use HTTP `Range: bytes=...` requests when loading `<audio>` elements (`output_{rid}.mp3`). `_send_file()` always returns `200 OK` with the entire file and no `Accept-Ranges` header, which prevents audio scrubbing/replay and breaks playback on iOS Safari.
  * When a browser aborts an in-flight image/audio GET or POST connection, `self.wfile.write()` raises `BrokenPipeError` / `ConnectionResetError` / `ConnectionAbortedError`. In `do_POST`, the generic `except Exception:` block catches this and tries to call `self._send_json(..., 500)` on the dead socket, raising a second traceback.

### 3.2. Orphaned `.png` / `.heatmap.png` in `shared_output/docs/` When PDF Rendering Fails (`analysis_watcher/run_analysis_watcher.py:721-725, 798-800`)
* **Location:** `analysis_watcher/run_analysis_watcher.py:721-725, 798-800` and `monitoring_server.py:1526-1535`
* **Details:**
  * `print_{rid}.png` and `print_{rid}.heatmap.png` are written to `shared_output/docs/` *before* `save_print_pdf()`. If `save_print_pdf()` returns `None` (both Playwright and Matplotlib fail), no `print_{rid}.pdf` exists. Because `api_list_prints()` and `/api/files/delete` only glob `print_*.pdf`, those orphaned `.png` and `.heatmap.png` files are invisible to the admin file manager and can never be pruned via the UI.
  * Additionally, `cleanup_orphaned_temp_files()` (`run_analysis_watcher.py:566-572`) deletes `output_{rid}.mp3` if `print_{rid}.pdf` is missing, even if `content.json` still points to `output_{rid}.mp3`.

### 3.3. `LLMModel.get_config_dict()` Reports `has_api_key: True` Even After Clearing Saved Key When `.env` or Other Role Has a Key (`analysis_watcher/chat.py:202-205, 855-865`)
* **Location:** `analysis_watcher/chat.py:202-205, 855-865`
* **Details:**
  * `_load_local_config()` populates `self._api_key` from `.env` or the other role when `role_cfg.get("api_key")` is empty. Because `get_config_dict()` sets `"has_api_key": bool(self._api_key)`, checking "Clear saved API key" and clicking Save in `/admin` still displays `(Saved — leave blank to keep)` if a fallback key exists in `.env` or on the other role. Reporting both `has_saved_api_key` (in `settings.json`) and `has_env_api_key` avoids operator confusion.

### 3.4. `_chat_buas` Does Not Reset `self.__chat_key` on HTTP Errors (`analysis_watcher/chat.py:752-779`)
* **Location:** `analysis_watcher/chat.py:752-779`
* **Details:**
  * If a cached `self.__chat_key` expires and the BUas server responds with an HTTP 4xx/5xx status code, `resp.raise_for_status()` raises before `self.__chat_key = None` is reached, so the retry attempt reuses the expired `__chat_key`.

### 3.5. `start_everything.py --mode full` Shuts Down Web Server If Desktop Gaze Painter Exits (`start_everything.py:257-268`)
* **Location:** `start_everything.py:257-268`
* **Details:**
  * In `monitor_processes()`, if *any* non-Ollama process exits in `full` mode (for instance, `run_gaze_painter.py` exiting because no webcam is attached to the server host, or the operator pressing `q` in the OpenCV window), `start_everything.py` immediately terminates `monitoring_server.py` as well, taking down `/draw`, `/kiosk`, `/admin`, and the live gallery.

### 3.6. Unbounded String Lengths on AI Prompt Fields & `remote_server_url` QR Length Validation (`config_manager.py:540-550, 594-596`)
* **Location:** `config_manager.py:540-550, 594-596`
* **Details:**
  * `server.remote_server_url` is not length-checked against the 213-byte QR capacity (`qr_utils.py:296`) when combined with `/shared_output/docs/print_YYYYMMDD_HHMMSS_mmm.pdf` (~50 bytes). Capping `remote_server_url` to `<= 155` UTF-8 bytes guarantees QR generation never fails on `/api/qr.svg` or PDF rendering.
  * Prompt fields (`description`, `purpose`, `expertise`, `capabilities`, `restrictions`, `additional`) in `validate_ai_data()` have no per-field character cap (unlike `attributions` fields which are capped at 500 chars).

### 3.7. Admin Preview Logo Returns `204 No Content` When Custom Query String Is Used While Attributions Are Disabled (`web_ui/admin.html:1243-1244`, `monitoring_server.py:1093-1101`)
* **Location:** `web_ui/admin.html:1243-1244`
* **Details:**
  * `admin.html` checks `previewLogo === '/kasboek-logo.jpg' ? '/kasboek-logo.jpg?preview=1' : previewLogo`. If the user enters `/kasboek-logo.jpg?v=2`, `preview=1` is not appended, and when attributions are currently disabled in `settings.json`, `GET /kasboek-logo.jpg?v=2` returns `204 No Content`, hiding the logo in the Admin live preview box.

---

## 4. Summary Priority Matrix

| ID | Severity | Component | Issue Summary |
| :--- | :--- | :--- | :--- |
| **1.1** | **High** | `analysis_watcher/preprocess_image.py` | Edge drawings shifted off-center in `image_processed.png`, breaking Rorschach symmetry scores |
| **1.2** | **High** | `gaze_painter/eye_tracking.py` | `right_eye_outer` (`362`) and `right_eye_inner` (`263`) swapped, biasing head-pose offset |
| **1.3** | **High** | `web_ui/draw.html` | Uncaught `detectForVideo` exception permanently kills `requestAnimationFrame` gaze loop |
| **1.4** | **High** | `config_manager.py` | Windows `os.write` outside `try/except` in `_try_lock_fd` raises `PermissionError` on locked file |
| **1.5** | **High** | `monitoring_server.py` / `run_analysis_watcher.py` | External watcher process runs pipeline a second time in parallel on `POST /api/drawings` |
| **1.6** | **High** | `config_manager.py` | `save_settings()` skips section validation on non-dict values and uses stale `_memory_settings` |
| **2.1** | **Medium** | `tts_el.py` / `run_analysis_watcher.py` / `Dockerfile` | ElevenLabs TTS fails when `voice_id_2` is empty or `ffmpeg` is missing (`Dockerfile` lacks `ffmpeg`) |
| **2.2** | **Medium** | `monitoring_server.py` | Thread-unsafe per-request mutation of global `PIL.Image.MAX_IMAGE_PIXELS` |
| **2.3** | **Medium** | `analysis_watcher/analysis.py` | Unclosed `Image.open()` handle leaks `image_processed_{rid}.png` on Windows |
| **2.4** | **Medium** | `analysis_watcher/chat.py` | `_ollama_supports_vision` caches `False` permanently on 1s timeout; misses `qwen2.5-vl` |
| **2.5** | **Medium** | `config_manager.py` | `validate_safe_url` allows relative `/path` for `base_url` and `remote_server_url` |
| **2.6** | **Medium** | `config_manager.py` | `_parse_env_line` truncates unquoted `.env` passwords/keys containing `#` |
| **2.7** | **Medium** | `index.html` / `admin.html` | Synchronous `<head>` CDN scripts stall Playwright PDF render (15s timeout) on blackholed LANs |
| **2.8** | **Medium** | `gaze_painter/run_gaze_painter.py` | `calibration.json` loaded at startup is immediately wiped on transition to `STATE_CALIBRATING` |
| **2.9** | **Medium** | `analysis_watcher/run_analysis_watcher.py` | `_render_pdf_fallback` text overflows on long LLM outputs and `A5` paper format |
| **2.10** | **Medium** | `index.html` | `stepHistory()` lacks async re-entrancy guard and steps onto duplicate Live item at index `0` |
| **2.11** | **Medium** | `print_service/print_kiosk_ui.py` | Non-atomic thumbnail write race, main-thread print freeze, and $O(N^2)$ `print_history.json` reads |
| **3.1** | **Low** | `monitoring_server.py` | No HTTP `Range` support for `<audio>` (iOS Safari) and unhandled `BrokenPipeError` |
| **3.2** | **Low** | `run_analysis_watcher.py` / `monitoring_server.py` | Orphaned `docs/print_{rid}.png` cannot be deleted via UI if PDF rendering fails |
| **3.3** | **Low** | `analysis_watcher/chat.py` | `has_api_key` stays `True` after clearing saved key if `.env` or other role has a key |
| **3.4–3.7** | **Low** | Multiple files | BUas `__chat_key` reset on HTTP error, `--mode full` exit coupling, prompt/QR URL length caps |
