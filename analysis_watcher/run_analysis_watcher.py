#!/usr/bin/env python3
"""
Latent Space Analysis Pipeline & File Watcher
Cross-platform, Google-Drive-free, multi-provider AI pipeline.
"""

import hashlib
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

# Add project root and analysis_watcher to sys.path
CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent
for p in (str(PROJECT_ROOT), str(CURRENT_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from config_manager import (
    SHARED_DIR,
    DOCS_DIR,
    LOGS_DIR,
    atomic_write_bytes,
    atomic_write_text,
    load_settings,
    load_env_vars,
    ensure_directories,
)
from chat import LLMModel, AIGenerationError
from analysis import analyze_drawing
from heatmap import draw_heatmap
from preprocess_image import preprocess_image, DrawingValidationError

ensure_directories()

_id_lock = threading.Lock()
_last_run_id = ""
_history_lock = threading.Lock()
_watcher_sig_lock = threading.Lock()
_handled_image_sig = None


class PipelineStageError(RuntimeError):
    """Structured pipeline failure carrying an operator-visible error code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def generate_run_id() -> str:
    """Generate a unique per-drawing ID (YYYYMMDD_HHMMSS_mmm), guaranteed monotonic."""
    global _last_run_id
    with _id_lock:
        candidate = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:19]
        if candidate <= _last_run_id:
            time.sleep(0.002)
            candidate = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:19]
            if candidate <= _last_run_id:
                base, suffix = _last_run_id.rsplit("_", 1)
                candidate = f"{base}_{int(suffix) + 1:03d}"
        _last_run_id = candidate
        return candidate


def _compute_image_sig(image_path: Path):
    if not image_path.exists():
        return None
    try:
        st = image_path.stat()
        digest = hashlib.sha256(image_path.read_bytes()).hexdigest()
        return (st.st_mtime_ns, st.st_size, digest)
    except Exception:
        return None


def mark_image_handled(image_path: Path = None):
    """Record current image.png signature so the watcher skips an already-queued upload."""
    global _handled_image_sig
    target = image_path or (SHARED_DIR / "image.png")
    sig = _compute_image_sig(target)
    if sig is not None:
        with _watcher_sig_lock:
            _handled_image_sig = sig


def write_live_image_and_mark_handled(target_path: Path, data: bytes):
    """Atomically write live image.png and mark its signature handled under the watcher lock."""
    global _handled_image_sig
    with _watcher_sig_lock:
        atomic_write_bytes(target_path, data)
        sig = _compute_image_sig(target_path)
        if sig is not None:
            _handled_image_sig = sig


def snapshot_drawing_input(source_path: Path = None, run_id: str = None) -> tuple:
    """Copy a drawing image to an isolated per-run snapshot file input_{run_id}.png."""
    src = Path(source_path) if source_path else (SHARED_DIR / "image.png")
    rid = run_id or generate_run_id()
    snap = SHARED_DIR / f"input_{rid}.png"
    atomic_write_bytes(snap, src.read_bytes())
    return rid, snap


def _setup_logger(name, filename, fmt="%(asctime)s;%(name)s;%(levelname)s;%(message)s"):
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        for directory in (LOGS_DIR, CURRENT_DIR):
            try:
                directory.mkdir(parents=True, exist_ok=True)
                fh = logging.FileHandler(directory / filename, encoding="utf-8")
                fh.setFormatter(logging.Formatter(fmt))
                logger.addHandler(fh)
                break
            except Exception:
                pass
    return logger


main_logger = _setup_logger("image_analysis_pipeline", "installation.log")
response_logger = _setup_logger("request_responses", "responses.log")
prediction_logger = _setup_logger("predictions", "predictions.csv", fmt="%(asctime)s;%(message)s")
credits_logger = _setup_logger("credits", "credits.csv")

CHROME_CANDIDATES = [
    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
]


def find_chrome():
    for p in CHROME_CANDIDATES:
        expanded = os.path.expandvars(p)
        if expanded and Path(expanded).exists():
            return str(expanded)
    return None


def get_gallery_url():
    settings = load_settings()
    port = os.environ.get("PORT") or settings.get("server", {}).get("port", 8000)
    return f"http://127.0.0.1:{port}/"


def _render_pdf_fallback(pdf_path: Path, website_data: dict, settings: dict) -> str:
    """
    Fallback PDF renderer using thread-safe Matplotlib Figure API if Playwright/Chrome is unavailable.
    Renders the artwork, heatmap, analysis, poem, and optional Kasboek attribution onto an A4 PDF.
    """
    from matplotlib.backends.backend_pdf import FigureCanvasPdf
    from matplotlib.figure import Figure
    import cv2

    fig = Figure(figsize=(8.27, 11.69), dpi=150)  # A4 portrait
    FigureCanvasPdf(fig)
    fig.patch.set_facecolor("white")

    # 1. Scribble image at top center
    scribble_file = SHARED_DIR / website_data.get("scribblePath", "image_processed.png")
    if scribble_file.exists():
        img = cv2.imread(str(scribble_file), cv2.IMREAD_GRAYSCALE)
        if img is not None:
            ax_img = fig.add_axes([0.2, 0.56, 0.6, 0.36])
            ax_img.imshow(img, cmap="gray", vmin=0, vmax=255)
            ax_img.axis("off")

    # Title
    fig.text(
        0.5, 0.52,
        "Latent Space, Through the Eyes of the Algorithm",
        ha="center", va="center", fontsize=14, fontweight="bold", color="#2a2a2a"
    )

    # 2. Analysis on bottom-left
    fig.text(0.08, 0.47, "ANALYSIS", fontsize=9, fontweight="bold", color="#666666")
    analysis_text = website_data.get("psychoanalysis", "")
    fig.text(
        0.08, 0.45, analysis_text,
        ha="left", va="top", fontsize=8.5, color="#2a2a2a",
        wrap=True, bbox=dict(boxstyle="square,pad=0", facecolor="none", edgecolor="none"),
    )

    # 3. Poem on bottom-right
    fig.text(0.55, 0.47, "POEM", fontsize=9, fontweight="bold", color="#666666")
    poem_text = website_data.get("poem", "")
    fig.text(
        0.55, 0.45, poem_text,
        ha="left", va="top", fontsize=8.5, style="italic", color="#2a2a2a", wrap=True
    )

    # 4. Heatmap below poem
    heatmap_file = SHARED_DIR / website_data.get("heatmapPath", "heatmap.png")
    if heatmap_file.exists():
        hm = cv2.imread(str(heatmap_file), cv2.IMREAD_GRAYSCALE)
        if hm is not None:
            fig.text(0.55, 0.25, "ATTENTION SPAN", fontsize=8, fontweight="bold", color="#666666")
            ax_hm = fig.add_axes([0.55, 0.12, 0.15, 0.12])
            ax_hm.imshow(hm, cmap="gray", vmin=0, vmax=255)
            ax_hm.axis("off")

    # 5. Attribution footer if enabled
    attr = settings.get("attributions", {})
    if attr.get("enabled"):
        founders_str = ", ".join(
            f"{f.get('name')} ({f.get('url')})"
            for f in attr.get("founders", [])
            if f.get("name")
        )
        attr_line = (
            f"{attr.get('intro_text', 'Made possible by Kasboek.')} "
            f"{attr.get('learn_more_text', 'To learn more, visit')} {attr.get('website_url', '')} | "
            f"{attr.get('company_linkedin_url', '')}"
        )
        if founders_str:
            attr_line += f"\n{attr.get('thanks_text', 'Thank you for letting us use this:')} {founders_str}"
        fig.text(0.5, 0.05, attr_line, ha="center", va="center", fontsize=6.5, color="#444444")

    ts_str = datetime.now().strftime("%B %d, %Y %H:%M")
    fig.text(0.5, 0.02, ts_str, ha="center", va="center", fontsize=7.5, style="italic", color="#666666")

    tmp_pdf = pdf_path.with_name(f".tmp_{os.getpid()}_{pdf_path.name}")
    fig.savefig(str(tmp_pdf), format="pdf")
    os.replace(str(tmp_pdf), str(pdf_path))
    return str(pdf_path)


def save_print_pdf(url=None, out_dir=None, website_data=None, run_id=None):
    """
    Render the served gallery URL with Playwright into an A4 PDF in out_dir,
    falling back to Matplotlib PDF rendering if Playwright/Chromium is unavailable.
    """
    if url is None:
        url = get_gallery_url()
    if out_dir is None:
        out_dir = DOCS_DIR

    out_dir_path = Path(out_dir)
    out_dir_path.mkdir(parents=True, exist_ok=True)
    rid = run_id or generate_run_id()
    pdf_path = out_dir_path / f"print_{rid}.pdf"
    settings = load_settings()
    paper_format = settings.get("printing", {}).get("paper_format", "A4")

    try:
        import importlib.util
        if importlib.util.find_spec("playwright") is None:
            raise ModuleNotFoundError("No module named 'playwright'")
        from playwright.sync_api import sync_playwright
        chrome_path = find_chrome()
        with sync_playwright() as p:
            browser = (
                p.chromium.launch(executable_path=chrome_path, headless=True)
                if chrome_path
                else p.chromium.launch(headless=True)
            )
            page = browser.new_page()
            page.goto(url, wait_until="networkidle", timeout=15000)
            page.pdf(
                path=str(pdf_path),
                format=paper_format,
                print_background=True,
                margin={"top": "0cm", "right": "0cm", "bottom": "0cm", "left": "0cm"},
            )
            browser.close()
        return str(pdf_path)
    except Exception as e:
        main_logger.warning(f"Playwright PDF render unavailable ({e}); using built-in PDF renderer.")
        try:
            if website_data is None and (SHARED_DIR / "content.json").exists():
                website_data = json.loads((SHARED_DIR / "content.json").read_text(encoding="utf-8"))
            return _render_pdf_fallback(pdf_path, website_data or {}, settings)
        except Exception as e2:
            main_logger.error(f"Fallback PDF renderer failed: {e2}")
            return None


def mark_file_printed(filename: str):
    """Record a printed PDF atomically in shared_output/docs/print_history.json."""
    print_log = DOCS_DIR / "print_history.json"
    with _history_lock:
        history = {"printed": [], "prints": []}
        if print_log.exists():
            try:
                history = json.loads(print_log.read_text(encoding="utf-8"))
            except Exception:
                pass
        if filename not in history.get("printed", []):
            history.setdefault("printed", []).append(filename)
        history.setdefault("prints", []).append({
            "filename": filename,
            "timestamp": datetime.now().isoformat(),
        })
        atomic_write_text(print_log, json.dumps(history, indent=2), encoding="utf-8")


def remove_from_print_history(filenames):
    """Prune deleted PDF filenames from print_history.json atomically."""
    if not filenames:
        return
    remove_set = set(filenames)
    print_log = DOCS_DIR / "print_history.json"
    with _history_lock:
        if not print_log.exists():
            return
        try:
            history = json.loads(print_log.read_text(encoding="utf-8"))
        except Exception:
            return
        history["printed"] = [f for f in history.get("printed", []) if f not in remove_set]
        history["prints"] = [p for p in history.get("prints", []) if p.get("filename") not in remove_set]
        atomic_write_text(print_log, json.dumps(history, indent=2), encoding="utf-8")


def send_pdf_to_printer(pdf_path: str, printer_name: str = "default") -> dict:
    """
    Cross-platform print helper for Windows, macOS, and Linux.
    Honors configured printer_name when non-default.
    """
    path = Path(pdf_path)
    if not path.exists():
        return {"status": "error", "error": f"File not found: {pdf_path}"}

    target_printer = (printer_name or "").strip()
    is_custom_printer = bool(target_printer and target_printer.lower() != "default")

    sys_name = platform.system()
    try:
        if sys_name == "Windows":
            if is_custom_printer:
                try:
                    safe_printer = target_printer.replace('"', '`"')
                    subprocess.run(
                        [
                            "powershell",
                            "-NoProfile",
                            "-Command",
                            f'Start-Process -FilePath "{path}" -Verb PrintTo -ArgumentList \'"{safe_printer}"\'',
                        ],
                        check=True,
                        capture_output=True,
                        text=True,
                    )
                except Exception:
                    try:
                        os.startfile(str(path), "print")
                    except Exception:
                        subprocess.run(
                            ["powershell", "-NoProfile", "-Command", f'Start-Process -FilePath "{path}" -Verb Print'],
                            check=True,
                            capture_output=True,
                            text=True,
                        )
            else:
                try:
                    os.startfile(str(path), "print")
                except Exception:
                    subprocess.run(
                        ["powershell", "-NoProfile", "-Command", f'Start-Process -FilePath "{path}" -Verb Print'],
                        check=True,
                        capture_output=True,
                        text=True,
                    )
        else:
            cmd = ["lp"]
            if is_custom_printer:
                cmd.extend(["-d", target_printer])
            cmd.append(str(path))
            subprocess.run(cmd, check=True, capture_output=True, text=True)

        mark_file_printed(path.name)
        main_logger.info(f"Sent PDF to printer ({target_printer or 'default'}): {path.name}")
        return {"status": "success", "filename": path.name, "printer": target_printer or "default"}
    except Exception as e:
        main_logger.error(f"Printing failed for {path.name}: {e}")
        return {"status": "error", "error": str(e)}


def _run_optional_tts(poem: str, settings: dict, run_id: str = None) -> str:
    """Run optional TTS if enabled in settings. Non-critical: returns '' on failure or when disabled."""
    tts_cfg = settings.get("tts", {})
    if not tts_cfg.get("enabled", False):
        return ""

    provider = tts_cfg.get("provider", "elevenlabs")
    env = load_env_vars()
    rid = run_id or generate_run_id()
    audio_filename = f"output_{rid}.mp3"
    output_mp3 = SHARED_DIR / audio_filename

    if provider == "elevenlabs":
        api_key = env.get("API_KEY") or env.get("ELEVENLABS_API_KEY")
        if not api_key:
            main_logger.warning("TTS enabled with ElevenLabs, but API_KEY is not set.")
            return ""
        rae_file = SHARED_DIR / f"output_rae_{rid}.mp3"
        ryan_file = SHARED_DIR / f"output_ryan_{rid}.mp3"
        try:
            from elevenlabs.client import ElevenLabs
            from tts_el import run_el_tts
            from postprocess_audio import decrease_volume, overlay_audio

            client = ElevenLabs(api_key=api_key)
            v1 = tts_cfg.get("voice_id_1", "45QiOxkuEAY6ckHqLMe8")
            v2 = tts_cfg.get("voice_id_2", "tJHJUEHzOkMoPmJJ5jo2")
            run_el_tts(client=client, voice_id=v1, output=str(rae_file), text=poem, speed=1.2)
            run_el_tts(client=client, voice_id=v2, output=str(ryan_file), text=poem, speed=1.3)
            decrease_volume(str(ryan_file), dB=-10)
            overlay_audio(str(ryan_file), str(rae_file), output_name=str(output_mp3))
            return audio_filename
        except Exception as e:
            main_logger.warning(f"ElevenLabs TTS failed (non-critical, skipping audio): {e}")
            return ""
        finally:
            for tmp in (rae_file, ryan_file):
                try:
                    if tmp.exists():
                        tmp.unlink()
                except Exception:
                    pass
    elif provider == "google_cloud":
        try:
            from tts_gc import gc_tts_setup, run_gc_tts
            client, voice, audio_config = gc_tts_setup()
            run_gc_tts(client, voice, audio_config, poem, str(SHARED_DIR / f"output_{rid}"))
            return audio_filename
        except Exception as e:
            main_logger.warning(f"Google Cloud TTS failed (non-critical, skipping audio): {e}")
            return ""
    return ""


def _write_content_error(run_id: str, code: str, message: str, settings: dict = None):
    """Persist explicit pipeline error state atomically to content.json so UI/kiosk/admin surface it."""
    cfg = settings or load_settings()
    payload = {
        "status": "error",
        "runId": run_id,
        "errorCode": code,
        "errorMessage": message,
        "userMessage": f"We could not complete your reading. Please try again, or ask staff to check error code {code}.",
        "poemAudio": "",
        "attributions": cfg.get("attributions", {}),
        "timestamp": datetime.now().isoformat(),
    }
    atomic_write_text(SHARED_DIR / "content.json", json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def run_analysis_pipeline(run_id: str = None, snapshot_path: str = None, raise_on_error: bool = False) -> bool:
    """
    Run the complete Latent Space pipeline on an isolated per-drawing snapshot:
      1. Validate & preprocess drawing image (fails fast with ERR_DRAWING_BLANK)
      2. Analyze abstract art composition, stroke geometry, and Rorschach pareidolia
      3. Run Psychoanalysis LLM (retry + cross-provider failover; ERR_ANALYSIS_AI_FAILED on total failure)
      4. Run Poet LLM (retry + cross-provider failover; ERR_POEM_AI_FAILED on total failure)
      5. Optional TTS audio generation (non-critical; '' when disabled or failed)
      6. Generate 3x3 Attention Heatmap (non-critical; randomized fallback on failure)
      7. Archive artifacts under docs/print_{run_id}.* and update shared_output/content.json atomically
      8. Render Print PDF (and auto-print if enabled)
    """
    start_time = datetime.now()
    rid = run_id or generate_run_id()
    main_logger.info(f"[{rid}] Started analysis pipeline at {start_time.isoformat()}")
    settings = load_settings()
    vision_mode = settings.get("ai", {}).get("vision_mode", "auto")

    created_temp_snapshot = False
    if snapshot_path:
        input_img = Path(snapshot_path)
    else:
        live_img = SHARED_DIR / "image.png"
        if not live_img.exists():
            err = PipelineStageError("ERR_DRAWING_BLANK", f"Input drawing image does not exist: {live_img}")
            main_logger.error(f"[{rid}] {err.code}: {err}")
            _write_content_error(rid, err.code, str(err), settings)
            if raise_on_error:
                raise err
            return False
        _, input_img = snapshot_drawing_input(live_img, run_id=rid)
        created_temp_snapshot = True

    processed_run = SHARED_DIR / f"image_processed_{rid}.png"
    heatmap_run = SHARED_DIR / f"heatmap_{rid}.png"
    content_file = SHARED_DIR / "content.json"

    try:
        if not input_img.exists():
            raise PipelineStageError("ERR_DRAWING_BLANK", f"Input drawing image missing: {input_img}")

        # 1. Drawing Validation & Preprocessing (Crucial)
        try:
            image_path = preprocess_image(
                input_filename=str(input_img),
                output_filename=str(processed_run),
            )
        except DrawingValidationError as dve:
            raise PipelineStageError("ERR_DRAWING_BLANK", str(dve)) from dve

        # 2. Model setup
        main_logger.info(f"[{rid}] Setting up AI models")
        analysis_model = LLMModel("analysis")
        poem_model = LLMModel("poem")
        try:
            init_response_a = analysis_model.initialize()
            response_logger.info(init_response_a)
        except Exception as e:
            main_logger.warning(f"[{rid}] Analysis init note (will still attempt failover on chat): {e}")
        try:
            init_response_p = poem_model.initialize()
            response_logger.info(init_response_p)
        except Exception as e:
            main_logger.warning(f"[{rid}] Poem init note (will still attempt failover on chat): {e}")

        # 3. Abstract Art & Visual Perception Analysis
        main_logger.info(f"[{rid}] Starting visual perception analysis (mode={vision_mode})")
        perception = analyze_drawing(str(image_path), mode=vision_mode)
        summary_label = perception["label"]
        prompt_context = perception["prompt_context"]
        prediction_logger.info(summary_label)
        main_logger.info(f"[{rid}] Visual perception archetype: {summary_label}")

        # 4. Psychoanalytical Interpretation (Crucial: retries + cross-provider failover)
        main_logger.info(f"[{rid}] Starting psychoanalysis via {analysis_model._provider} ({analysis_model._llm})")
        analysis_text = analysis_model.chat(message=prompt_context, image_path=str(image_path), allow_failover=True)
        main_logger.info(f"[{rid}] Psychoanalysis complete via {analysis_model._last_used_provider} ({analysis_model._last_used_model})")

        # 5. Poem Generation (Crucial: retries + cross-provider failover)
        main_logger.info(f"[{rid}] Starting poem generation via {poem_model._provider} ({poem_model._llm})")
        poem_text = poem_model.chat(message=analysis_text, allow_failover=True)
        main_logger.info(f"[{rid}] Poem complete via {poem_model._last_used_provider} ({poem_model._last_used_model})")

        # 6. Optional Text-to-Speech (Non-critical: never reuse stale audio)
        poem_audio = _run_optional_tts(poem_text, settings, run_id=rid)

        # 7. Attention Heatmap (Non-critical: draw_heatmap falls back to randomized 3x3 matrix on failure)
        main_logger.info(f"[{rid}] Generating attention heatmap")
        draw_heatmap(
            scribble_filename=str(input_img),
            heatmap_filename=str(heatmap_run),
        )
        main_logger.info(f"[{rid}] Heatmap complete")

        # 8. Archive per-drawing image & heatmap under docs/print_{rid}.*
        DOCS_DIR.mkdir(parents=True, exist_ok=True)
        archived_thumb = DOCS_DIR / f"print_{rid}.png"
        archived_heatmap = DOCS_DIR / f"print_{rid}.heatmap.png"
        atomic_write_bytes(archived_thumb, Path(processed_run).read_bytes())
        atomic_write_bytes(archived_heatmap, Path(heatmap_run).read_bytes())

        # Also update compatibility files atomically
        atomic_write_bytes(SHARED_DIR / "image_processed.png", Path(processed_run).read_bytes())
        atomic_write_bytes(SHARED_DIR / "heatmap.png", Path(heatmap_run).read_bytes())

        geom = perception.get("geometry", {})
        website_data = {
            "status": "ok",
            "runId": rid,
            "scribblePath": f"docs/{archived_thumb.name}",
            "heatmapPath": f"docs/{archived_heatmap.name}",
            "archetype": summary_label,
            "fractalDimension": geom.get("fractal_dimension"),
            "spatialEntropy": geom.get("spatial_entropy"),
            "primaryZone": geom.get("primary_zone"),
            "psychoanalysis": analysis_text,
            "poem": poem_text,
            "poemAudio": poem_audio,
            "analysisProvider": f"{analysis_model._last_used_provider}:{analysis_model._last_used_model}",
            "poemProvider": f"{poem_model._last_used_provider}:{poem_model._last_used_model}",
            "attributions": settings.get("attributions", {}),
            "timestamp": datetime.now().isoformat(),
        }

        atomic_write_text(content_file, json.dumps(website_data, indent=2, ensure_ascii=False), encoding="utf-8")
        main_logger.info(f"[{rid}] Website content.json updated")

        # 9. Render Print PDF and save sidecar metadata
        main_logger.info(f"[{rid}] Rendering print PDF")
        pdf_file = save_print_pdf(get_gallery_url(), out_dir=str(DOCS_DIR), website_data=website_data, run_id=rid)
        if pdf_file:
            main_logger.info(f"[{rid}] Saved print PDF: {pdf_file}")
            pdf_path = Path(pdf_file)
            website_data["printPDF"] = f"docs/{pdf_path.name}"
            try:
                meta_path = pdf_path.with_suffix(".json")
                atomic_write_text(meta_path, json.dumps(website_data, indent=2, ensure_ascii=False), encoding="utf-8")
            except Exception as e:
                main_logger.error(f"[{rid}] Failed to save metadata sidecar: {e}")

            atomic_write_text(content_file, json.dumps(website_data, indent=2, ensure_ascii=False), encoding="utf-8")

            printing_cfg = settings.get("printing", {})
            if printing_cfg.get("auto_print", False):
                printer_name = printing_cfg.get("printer_name", "default")
                main_logger.info(f"[{rid}] Auto-print enabled — printing {pdf_path.name} on '{printer_name}'")
                send_pdf_to_printer(str(pdf_path), printer_name=printer_name)

        main_logger.info(f"[{rid}] Pipeline concluded in {datetime.now() - start_time}")
        return True

    except (PipelineStageError, AIGenerationError) as err:
        main_logger.error(f"[{rid}] Pipeline failed ({err.code}): {err}")
        print(f"[analysis_watcher] Pipeline failed ({err.code}): {err}")
        _write_content_error(rid, err.code, str(err), settings)
        if raise_on_error:
            raise
        return False

    except Exception as e:
        err = PipelineStageError("ERR_PIPELINE_FAILED", str(e))
        main_logger.error(f"[{rid}] Unexpected pipeline error ({err.code}): {e}")
        print(f"[analysis_watcher] Error occurred ({err.code}): {e}")
        _write_content_error(rid, err.code, str(e), settings)
        if raise_on_error:
            raise err from e
        return False

    finally:
        cleanup_targets = [processed_run, heatmap_run]
        if created_temp_snapshot or (input_img.parent == SHARED_DIR and input_img.name.startswith("input_")):
            cleanup_targets.append(input_img)
        for p in cleanup_targets:
            try:
                if p.exists():
                    p.unlink()
            except Exception:
                pass


def watch_loop(stop_event=None, runner_fn=None):
    """Continuously watch shared_output/image.png for changes and trigger/enqueue the pipeline."""
    global _handled_image_sig
    image_path = SHARED_DIR / "image.png"
    with _watcher_sig_lock:
        if _handled_image_sig is None:
            _handled_image_sig = _compute_image_sig(image_path)

    print(f"[analysis_watcher] Watching for changes in {image_path}...")
    while stop_event is None or not stop_event.is_set():
        try:
            if image_path.exists():
                sig = _compute_image_sig(image_path)
                should_trigger = False
                with _watcher_sig_lock:
                    if sig is not None and sig != _handled_image_sig:
                        _handled_image_sig = sig
                        should_trigger = True

                if should_trigger:
                    print("\n[analysis_watcher] Detected new drawing in image.png, queuing analysis pipeline...")
                    if runner_fn is not None:
                        runner_fn(image_path)
                    else:
                        rid, snap = snapshot_drawing_input(image_path)
                        run_analysis_pipeline(run_id=rid, snapshot_path=str(snap))
        except Exception as e:
            print(f"[analysis_watcher] Watch error: {e}")
        time.sleep(0.5)


if __name__ == "__main__":
    try:
        watch_loop()
    except KeyboardInterrupt:
        print("\nStopping file watch...")
        try:
            from start_everything import stop_ollama_serving
            stop_ollama_serving()
        except Exception:
            pass
