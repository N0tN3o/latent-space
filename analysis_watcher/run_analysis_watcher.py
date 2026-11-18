#!/usr/bin/env python3
"""
Latent Space Analysis Pipeline & File Watcher
Cross-platform, Google-Drive-free, multi-provider AI pipeline.
"""

from datetime import datetime, timedelta
import hashlib
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import threading
import time
import urllib.request

# Add project root and analysis_watcher to sys.path
CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent
for p in (str(PROJECT_ROOT), str(CURRENT_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import config_manager
from config_manager import (
    atomic_write_bytes,
    atomic_write_text,
    ensure_directories,
    get_public_base_url,
    get_server_port,
    load_env_vars,
    load_settings,
    release_nonblocking_lock,
    send_pdf_to_printer,
    try_acquire_nonblocking_lock,
    write_content_json,
)
from analysis import analyze_drawing
from chat import AIGenerationError, LLMModel
from heatmap import draw_heatmap
from preprocess_image import DrawingValidationError, preprocess_image
from qr_utils import generate_qr_matrix

ensure_directories()

_id_lock = threading.Lock()
_last_run_id = ""
_watcher_sig_lock = threading.Lock()
_handled_image_sig = None

PAPER_SIZES_INCHES = {
    "A4": (8.27, 11.69),
    "A3": (11.69, 16.54),
    "A5": (5.83, 8.27),
    "Letter": (8.50, 11.00),
    "Legal": (8.50, 14.00),
}


class PipelineStageError(RuntimeError):
    """Structured pipeline failure carrying an operator-visible error code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def generate_run_id() -> str:
    """Generate a unique fixed-width per-drawing ID (YYYYMMDD_HHMMSS_mmm), guaranteed monotonic."""
    global _last_run_id
    with _id_lock:
        candidate = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:19]
        if candidate <= _last_run_id:
            time.sleep(0.002)
            candidate = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:19]
            if candidate <= _last_run_id:
                base, suffix = _last_run_id.rsplit("_", 1)
                next_num = int(suffix) + 1
                if next_num >= 1000:
                    dt_sec = datetime.strptime(base, "%Y%m%d_%H%M%S") + timedelta(seconds=1)
                    candidate = f"{dt_sec.strftime('%Y%m%d_%H%M%S')}_000"
                else:
                    candidate = f"{base}_{next_num:03d}"
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
    target = image_path or (config_manager.SHARED_DIR / "image.png")
    with _watcher_sig_lock:
        sig = _compute_image_sig(target)
        if sig is not None:
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
    src = Path(source_path) if source_path else (config_manager.SHARED_DIR / "image.png")
    rid = run_id or generate_run_id()
    snap = config_manager.SHARED_DIR / f"input_{rid}.png"
    atomic_write_bytes(snap, src.read_bytes())
    return rid, snap


def _setup_logger(name, filename, fmt="%(asctime)s;%(name)s;%(levelname)s;%(message)s"):
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        try:
            config_manager.LOGS_DIR.mkdir(parents=True, exist_ok=True)
            fh = logging.FileHandler(config_manager.LOGS_DIR / filename, encoding="utf-8")
            fh.setFormatter(logging.Formatter(fmt))
            logger.addHandler(fh)
        except Exception:
            pass
    return logger


main_logger = _setup_logger("image_analysis_pipeline", "installation.log")
response_logger = _setup_logger("request_responses", "responses.log")
prediction_logger = _setup_logger("predictions", "predictions.csv", fmt="%(asctime)s;%(message)s")

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


def get_gallery_url(print_render: bool = False):
    settings = load_settings()
    port = get_server_port(settings)
    suffix = "?print_render=1" if print_render else ""
    return f"http://127.0.0.1:{port}/{suffix}"


def _wrap_preserving_lines(text: str, width: int) -> str:
    """Wrap paragraphs/lines to a fixed character width while preserving explicit newlines."""
    lines = []
    for raw_line in (text or "").splitlines():
        stripped = raw_line.rstrip()
        if not stripped:
            lines.append("")
        else:
            lines.extend(textwrap.wrap(stripped, width=width, break_long_words=True, break_on_hyphens=False))
    return "\n".join(lines)


def _resolve_local_logo_path(logo_path: str) -> Path:
    """Resolve an attribution logo path safely within PROJECT_DIR or SHARED_DIR."""
    default_logo = PROJECT_ROOT / "kasboek-logo.jpg"
    raw = (logo_path or "").strip()
    if not raw or raw.startswith(("http://", "https://")):
        return default_logo
    rel = raw.lstrip("/")
    for base in (PROJECT_ROOT, config_manager.SHARED_DIR):
        candidate = (base / rel).resolve()
        if candidate.is_relative_to(base.resolve()) and candidate.is_file():
            return candidate
    return default_logo


def _format_pdf_timestamp(ts_raw: str = None) -> str:
    if ts_raw:
        try:
            dt = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
            return dt.strftime("%B %d, %Y %H:%M")
        except Exception:
            pass
    return datetime.now().strftime("%B %d, %Y %H:%M")


def _fit_text_block(text: str, base_width: int, base_fontsize: float, max_height_frac: float,
                    linespacing: float = 1.42, min_fontsize: float = 6.2) -> tuple:
    """Adjust wrap width & font size so text fits inside max_height_frac of the page height."""
    fontsize = base_fontsize
    width = base_width
    wrapped = _wrap_preserving_lines(text, width=width)
    while fontsize > min_fontsize:
        line_count = max(1, wrapped.count("\n") + 1)
        # Approximate line height in A4 figure fraction (~fontsize * linespacing / 842pt)
        est_height = line_count * (fontsize * linespacing / 780.0)
        if est_height <= max_height_frac:
            break
        fontsize = round(fontsize - 0.5, 1)
        scale = base_fontsize / fontsize
        width = max(base_width, int(round(base_width * min(scale, 1.35))))
        wrapped = _wrap_preserving_lines(text, width=width)
    return wrapped, fontsize


def _get_qr_base_url(settings: dict) -> str:
    return get_public_base_url(settings)


def _render_pdf_fallback(pdf_path: Path, website_data: dict, settings: dict) -> str:
    """
    Fallback PDF renderer using thread-safe Matplotlib Figure API if Playwright/Chrome is unavailable.
    Honors paper_format, logo_path, timestamp, custom_footer_note, renders QR code, and avoids overlaps.
    """
    from matplotlib.backends.backend_pdf import FigureCanvasPdf
    from matplotlib.figure import Figure
    import cv2

    printing_cfg = settings.get("printing", {})
    paper_format = printing_cfg.get("paper_format", "A4")
    figsize = PAPER_SIZES_INCHES.get(paper_format, PAPER_SIZES_INCHES["A4"])

    fig = Figure(figsize=figsize, dpi=150)
    FigureCanvasPdf(fig)
    fig.patch.set_facecolor("white")

    # 1. Scribble image at top center (preserve Ember/Indigo RGB shading when present)
    scribble_rel = website_data.get("scribblePath", "image_processed.png")
    scribble_file = config_manager.SHARED_DIR / scribble_rel
    if scribble_file.exists():
        img = cv2.imread(str(scribble_file), cv2.IMREAD_COLOR)
        if img is not None:
            img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            ax_img = fig.add_axes([0.2, 0.58, 0.6, 0.35])
            ax_img.imshow(img_rgb)
            ax_img.axis("off")

    # Title & Archetype/Metrics badge
    fig.text(
        0.5, 0.545,
        "Latent Space, Through the Eyes of the Algorithm",
        ha="center", va="center", fontsize=14, fontweight="bold", color="#2a2a2a",
    )
    badge_parts = []
    if website_data.get("archetype"):
        badge_parts.append(str(website_data["archetype"]).upper())
    if isinstance(website_data.get("fractalDimension"), (int, float)):
        badge_parts.append(f"FRACTAL D={website_data['fractalDimension']:.2f}")
    if isinstance(website_data.get("spatialEntropy"), (int, float)):
        badge_parts.append(f"ENTROPY={round(website_data['spatialEntropy'] * 100)}%")
    if badge_parts:
        fig.text(
            0.5, 0.515,
            "  ·  ".join(badge_parts),
            ha="center", va="center", fontsize=8, color="#555555",
            bbox=dict(boxstyle="round,pad=0.35", facecolor="#fafafa", edgecolor="#d0d0d0", linewidth=0.8),
        )

    # 2. Analysis on bottom-left (bounded vertically above footer at y=0.11)
    fig.text(0.07, 0.48, "ANALYSIS", fontsize=9, fontweight="bold", color="#666666")
    analysis_wrapped, analysis_fs = _fit_text_block(
        website_data.get("psychoanalysis", ""),
        base_width=48, base_fontsize=8.5, max_height_frac=0.35, linespacing=1.42,
    )
    fig.text(
        0.07, 0.46, analysis_wrapped,
        ha="left", va="top", fontsize=analysis_fs, linespacing=1.42, color="#2a2a2a",
    )

    # 3. Poem on bottom-right (bounded above Attention Span / QR row at y=0.255)
    fig.text(0.54, 0.48, "POEM", fontsize=9, fontweight="bold", color="#666666")
    poem_wrapped, poem_fs = _fit_text_block(
        website_data.get("poem", ""),
        base_width=42, base_fontsize=8.5, max_height_frac=0.195, linespacing=1.45,
    )
    fig.text(
        0.54, 0.46, poem_wrapped,
        ha="left", va="top", fontsize=poem_fs, linespacing=1.45, style="italic", color="#2a2a2a",
    )

    # 4. Heatmap & QR Code below poem (y=0.12..0.24)
    heatmap_file = config_manager.SHARED_DIR / website_data.get("heatmapPath", "heatmap.png")
    if heatmap_file.exists():
        hm = cv2.imread(str(heatmap_file), cv2.IMREAD_GRAYSCALE)
        if hm is not None:
            fig.text(0.54, 0.242, "ATTENTION SPAN", fontsize=8, fontweight="bold", color="#666666")
            ax_hm = fig.add_axes([0.54, 0.125, 0.14, 0.105])
            ax_hm.imshow(hm, cmap="gray", vmin=0, vmax=255)
            ax_hm.axis("off")

    pdf_rel = website_data.get("printPDF") or f"docs/{pdf_path.name}"
    qr_target = f"{_get_qr_base_url(settings)}/shared_output/{pdf_rel.lstrip('/')}"
    try:
        qr_mat = generate_qr_matrix(qr_target, quiet=2)
        import numpy as np
        qr_img = np.where(np.array(qr_mat, dtype=bool), 0, 255).astype(np.uint8)
        ax_qr = fig.add_axes([0.75, 0.13, 0.13, 0.098])
        ax_qr.imshow(qr_img, cmap="gray", interpolation="nearest", vmin=0, vmax=255)
        ax_qr.axis("off")
        fig.text(0.815, 0.118, "Scan for digital print", ha="center", va="top", fontsize=6.5, color="#666666")
    except Exception:
        pass

    # 5. Attribution footer if enabled (text shifted right when logo is visible so they never overlap)
    attr = settings.get("attributions", {})
    if attr.get("enabled"):
        logo_shown = False
        if attr.get("show_logo", True):
            logo_file = _resolve_local_logo_path(attr.get("logo_path", "/kasboek-logo.jpg"))
            if logo_file.exists():
                try:
                    logo_bgr = cv2.imread(str(logo_file))
                    if logo_bgr is not None:
                        logo_rgb = cv2.cvtColor(logo_bgr, cv2.COLOR_BGR2RGB)
                        ax_logo = fig.add_axes([0.07, 0.036, 0.10, 0.045])
                        ax_logo.imshow(logo_rgb)
                        ax_logo.axis("off")
                        logo_shown = True
                except Exception:
                    pass

        founders_str = ", ".join(
            f"{f.get('name')} ({f.get('url')})" if f.get("url") else f"{f.get('name')}"
            for f in attr.get("founders", [])
            if f.get("name")
        )
        attr_line = (
            f"{attr.get('intro_text', 'Made possible by Kasboek.')} "
            f"{attr.get('learn_more_text', 'To learn more, visit')} {attr.get('website_url', '')} | "
            f"{attr.get('company_linkedin_url', '')}"
        ).strip()
        if founders_str:
            attr_line += f"\n{attr.get('thanks_text', 'Thank you for letting us use this:')} {founders_str}"
        if attr.get("custom_footer_note") and str(attr.get("custom_footer_note")).strip():
            attr_line += f"\n{str(attr['custom_footer_note']).strip()}"

        text_x = 0.56 if logo_shown else 0.5
        wrap_w = 72 if logo_shown else 92
        attr_wrapped = _wrap_preserving_lines(attr_line, width=wrap_w)
        fig.text(text_x, 0.058, attr_wrapped, ha="center", va="center", fontsize=6.2, color="#444444")

    ts_str = _format_pdf_timestamp(website_data.get("timestamp"))
    fig.text(0.5, 0.018, ts_str, ha="center", va="center", fontsize=7.5, style="italic", color="#666666")

    tmp_pdf = pdf_path.with_name(f".tmp_{os.getpid()}_{threading.get_ident()}_{pdf_path.name}")
    try:
        fig.savefig(str(tmp_pdf), format="pdf")
        os.replace(str(tmp_pdf), str(pdf_path))
    finally:
        if tmp_pdf.exists():
            try:
                tmp_pdf.unlink()
            except Exception:
                pass
    return str(pdf_path)


def _is_gallery_reachable(url: str) -> bool:
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=0.6) as resp:
            return resp.status < 500
    except Exception:
        return False


def save_print_pdf(url=None, out_dir=None, website_data=None, run_id=None):
    """
    Render the served gallery URL with Playwright into an A4/configured-format PDF in out_dir,
    falling back to Matplotlib PDF rendering if Playwright/Chromium or the local server is unavailable.
    """
    if url is None:
        url = get_gallery_url(print_render=True)
    if out_dir is None:
        out_dir = config_manager.DOCS_DIR

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
        if not _is_gallery_reachable(url):
            raise RuntimeError(f"Gallery server unreachable at {url}")
        from playwright.sync_api import sync_playwright
        chrome_path = find_chrome()
        with sync_playwright() as p:
            browser = (
                p.chromium.launch(executable_path=chrome_path, headless=True)
                if chrome_path
                else p.chromium.launch(headless=True)
            )
            try:
                page = browser.new_page()
                page.goto(url, wait_until="networkidle", timeout=15000)
                try:
                    page.wait_for_function(
                        """async () => {
                            const upd = document.getElementById('lastUpdate');
                            if (!upd || upd.textContent === 'Waiting for data...') return false;
                            if (document.fonts && document.fonts.ready) await document.fonts.ready;
                            const imgs = Array.from(document.querySelectorAll('img'))
                                .filter(el => el.getAttribute('src') && el.style.display !== 'none');
                            return imgs.every(img => img.complete && img.naturalWidth > 0);
                        }""",
                        timeout=6000,
                    )
                except Exception:
                    pass
                tmp_pdf = pdf_path.with_name(f".tmp_{os.getpid()}_{threading.get_ident()}_{pdf_path.name}")
                try:
                    page.pdf(
                        path=str(tmp_pdf),
                        format=paper_format,
                        print_background=True,
                        margin={"top": "0cm", "right": "0cm", "bottom": "0cm", "left": "0cm"},
                    )
                    os.replace(str(tmp_pdf), str(pdf_path))
                finally:
                    if tmp_pdf.exists():
                        try:
                            tmp_pdf.unlink()
                        except Exception:
                            pass
            finally:
                browser.close()
        return str(pdf_path)
    except Exception as e:
        main_logger.warning(f"Playwright PDF render unavailable ({e}); using built-in PDF renderer.")
        try:
            content_path = config_manager.SHARED_DIR / "content.json"
            if website_data is None and content_path.exists():
                website_data = json.loads(content_path.read_text(encoding="utf-8"))
            return _render_pdf_fallback(pdf_path, website_data or {}, settings)
        except Exception as e2:
            main_logger.error(f"Fallback PDF renderer failed: {e2}")
            return None


def _run_optional_tts(poem: str, settings: dict, run_id: str = None) -> str:
    """Run optional TTS if enabled in settings. Non-critical: returns '' on failure or when disabled."""
    tts_cfg = settings.get("tts", {})
    if not tts_cfg.get("enabled", False):
        return ""

    provider = tts_cfg.get("provider", "elevenlabs")
    env = load_env_vars()
    rid = run_id or generate_run_id()
    audio_filename = f"output_{rid}.mp3"
    output_mp3 = config_manager.SHARED_DIR / audio_filename

    if provider == "elevenlabs":
        api_key = env.get("API_KEY") or env.get("ELEVENLABS_API_KEY")
        if not api_key:
            main_logger.warning("TTS enabled with ElevenLabs, but API_KEY is not set.")
            return ""
        rae_file = config_manager.SHARED_DIR / f"output_rae_{rid}.mp3"
        ryan_file = config_manager.SHARED_DIR / f"output_ryan_{rid}.mp3"
        try:
            from elevenlabs.client import ElevenLabs
            from postprocess_audio import decrease_volume, overlay_audio
            from tts_el import run_el_tts

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
            raw_v1 = (tts_cfg.get("voice_id_1") or "").strip()
            gc_voice = raw_v1 if ("-" in raw_v1 and len(raw_v1) >= 6) else "en-US-Chirp3-HD-Zephyr"
            client, voice, audio_config = gc_tts_setup(voice_id=gc_voice)
            run_gc_tts(client, voice, audio_config, poem, str(config_manager.SHARED_DIR / f"output_{rid}"))
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
    write_content_json(payload)


def cleanup_orphaned_temp_files():
    """Remove leftover transient per-run temp files in shared_output from previous crashes/timeouts."""
    shared = config_manager.SHARED_DIR
    docs = config_manager.DOCS_DIR
    if not shared.exists():
        return
    patterns = (
        "input_*.png",
        "image_processed_*.png",
        "heatmap_*.png",
        "output_rae_*.mp3",
        "output_ryan_*.mp3",
        "image.tmp.png",
        "dwell_grid.tmp.json",
        ".tmp_*",
        ".*.tmp",
    )
    for pat in patterns:
        for p in shared.glob(pat):
            try:
                if p.is_file():
                    p.unlink()
            except Exception:
                pass
    for mp3 in shared.glob("output_*.mp3"):
        rid = mp3.stem[len("output_"):]
        if not (docs / f"print_{rid}.pdf").exists():
            try:
                mp3.unlink()
            except Exception:
                pass


def run_analysis_pipeline(
    run_id: str = None,
    snapshot_path: str = None,
    raise_on_error: bool = False,
    dwell_grid=None,
    cancel_event: threading.Event = None,
) -> bool:
    """
    Run the complete Latent Space pipeline on an isolated per-drawing snapshot:
      1. Validate & preprocess drawing image (fails fast with ERR_DRAWING_BLANK)
      2. Analyze abstract art composition, stroke geometry, and Rorschach pareidolia
      3. Run Psychoanalysis LLM (retry + cross-provider failover; ERR_ANALYSIS_AI_FAILED on total failure)
      4. Run Poet LLM (retry + cross-provider failover; ERR_POEM_AI_FAILED on total failure)
      5. Optional TTS audio generation (non-critical; '' when disabled or failed)
      6. Generate 3x3 Attention Heatmap
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
        live_img = config_manager.SHARED_DIR / "image.png"
        if not live_img.exists():
            err = PipelineStageError("ERR_DRAWING_BLANK", f"Input drawing image does not exist: {live_img}")
            main_logger.error(f"[{rid}] {err.code}: {err}")
            _write_content_error(rid, err.code, str(err), settings)
            if raise_on_error:
                raise err
            return False
        _, input_img = snapshot_drawing_input(live_img, run_id=rid)
        created_temp_snapshot = True

    processed_run = config_manager.SHARED_DIR / f"image_processed_{rid}.png"
    heatmap_run = config_manager.SHARED_DIR / f"heatmap_{rid}.png"
    poem_audio = ""
    wrote_live_content = False

    def _check_cancelled():
        if cancel_event is not None and cancel_event.is_set():
            raise PipelineStageError("ERR_PIPELINE_TIMEOUT", f"Run {rid} cancelled after caller timeout")

    try:
        if not input_img.exists():
            raise PipelineStageError("ERR_DRAWING_BLANK", f"Input drawing image missing: {input_img}")

        _check_cancelled()

        # 1. Drawing Validation & Preprocessing
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
            analysis_model.initialize()
        except Exception as e:
            main_logger.warning(f"[{rid}] Analysis init note (will still attempt failover on chat): {e}")
        try:
            poem_model.initialize()
        except Exception as e:
            main_logger.warning(f"[{rid}] Poem init note (will still attempt failover on chat): {e}")

        # 3. Abstract Art & Visual Perception Analysis
        main_logger.info(f"[{rid}] Starting visual perception analysis (mode={vision_mode})")
        perception = analyze_drawing(
            str(image_path),
            mode=vision_mode,
            canvas_filename=str(input_img),
            dwell_grid=dwell_grid,
        )
        summary_label = perception["label"]
        prompt_context = perception["prompt_context"]
        safe_csv_label = str(summary_label).replace("\r", " ").replace("\n", " ").replace(";", ",")
        prediction_logger.info(safe_csv_label)
        main_logger.info(f"[{rid}] Visual perception archetype: {summary_label}")

        _check_cancelled()

        # 4. Psychoanalytical Interpretation
        main_logger.info(f"[{rid}] Starting psychoanalysis via {analysis_model._provider} ({analysis_model._llm})")
        analysis_kwargs = {"message": prompt_context, "image_path": str(image_path), "allow_failover": True}
        if cancel_event is not None:
            analysis_kwargs["cancel_event"] = cancel_event
        analysis_text = analysis_model.chat(**analysis_kwargs)
        main_logger.info(f"[{rid}] Psychoanalysis complete via {analysis_model._last_used_provider} ({analysis_model._last_used_model})")
        response_logger.info(
            json.dumps({
                "run_id": rid,
                "role": "analysis",
                "provider": analysis_model._last_used_provider,
                "model": analysis_model._last_used_model,
                "response": analysis_text,
            }, ensure_ascii=False)
        )

        _check_cancelled()

        # 5. Poem Generation
        main_logger.info(f"[{rid}] Starting poem generation via {poem_model._provider} ({poem_model._llm})")
        poem_kwargs = {"message": analysis_text, "allow_failover": True}
        if cancel_event is not None:
            poem_kwargs["cancel_event"] = cancel_event
        poem_text = poem_model.chat(**poem_kwargs)
        main_logger.info(f"[{rid}] Poem complete via {poem_model._last_used_provider} ({poem_model._last_used_model})")
        response_logger.info(
            json.dumps({
                "run_id": rid,
                "role": "poem",
                "provider": poem_model._last_used_provider,
                "model": poem_model._last_used_model,
                "response": poem_text,
            }, ensure_ascii=False)
        )

        _check_cancelled()

        # 6. Optional Text-to-Speech
        poem_audio = _run_optional_tts(poem_text, settings, run_id=rid)

        # 7. Attention Heatmap
        main_logger.info(f"[{rid}] Generating attention heatmap")
        draw_heatmap(
            scribble_filename=str(input_img),
            heatmap_filename=str(heatmap_run),
            dwell_grid=dwell_grid,
        )
        main_logger.info(f"[{rid}] Heatmap complete")

        _check_cancelled()

        # 8. Archive per-drawing image & heatmap under docs/print_{rid}.*
        config_manager.DOCS_DIR.mkdir(parents=True, exist_ok=True)
        archived_thumb = config_manager.DOCS_DIR / f"print_{rid}.png"
        archived_heatmap = config_manager.DOCS_DIR / f"print_{rid}.heatmap.png"
        atomic_write_bytes(archived_thumb, Path(processed_run).read_bytes())
        atomic_write_bytes(archived_heatmap, Path(heatmap_run).read_bytes())

        atomic_write_bytes(config_manager.SHARED_DIR / "image_processed.png", Path(processed_run).read_bytes())
        atomic_write_bytes(config_manager.SHARED_DIR / "heatmap.png", Path(heatmap_run).read_bytes())

        expected_pdf_name = f"print_{rid}.pdf"
        geom = perception.get("geometry", {})
        latest_settings = load_settings()
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
            "printPDF": f"docs/{expected_pdf_name}",
            "pdfRendering": True,
            "analysisProvider": f"{analysis_model._last_used_provider}:{analysis_model._last_used_model}",
            "poemProvider": f"{poem_model._last_used_provider}:{poem_model._last_used_model}",
            "attributions": latest_settings.get("attributions", {}),
            "timestamp": datetime.now().isoformat(),
        }

        write_content_json(website_data)
        wrote_live_content = True
        main_logger.info(f"[{rid}] Website content.json updated")

        # 9. Render Print PDF and save sidecar metadata
        main_logger.info(f"[{rid}] Rendering print PDF")
        pdf_file = save_print_pdf(
            get_gallery_url(print_render=True),
            out_dir=str(config_manager.DOCS_DIR),
            website_data=website_data,
            run_id=rid,
        )
        if cancel_event is not None and cancel_event.is_set():
            for artifact in (archived_thumb, archived_heatmap, Path(pdf_file) if pdf_file else None):
                if artifact and artifact.exists():
                    try:
                        artifact.unlink()
                    except Exception:
                        pass
            raise PipelineStageError("ERR_PIPELINE_TIMEOUT", f"Run {rid} cancelled after caller timeout")

        latest_settings = load_settings()
        website_data["attributions"] = latest_settings.get("attributions", {})
        website_data["pdfRendering"] = False

        if pdf_file:
            main_logger.info(f"[{rid}] Saved print PDF: {pdf_file}")
            pdf_path = Path(pdf_file)
            website_data["printPDF"] = f"docs/{pdf_path.name}"
            try:
                meta_path = pdf_path.with_suffix(".json")
                atomic_write_text(meta_path, json.dumps(website_data, indent=2, ensure_ascii=False), encoding="utf-8")
            except Exception as e:
                main_logger.error(f"[{rid}] Failed to save metadata sidecar: {e}")

            write_content_json(website_data)

            printing_cfg = latest_settings.get("printing", {})
            if printing_cfg.get("auto_print", False) and not (cancel_event and cancel_event.is_set()):
                printer_name = printing_cfg.get("printer_name", "default")
                main_logger.info(f"[{rid}] Auto-print enabled — printing {pdf_path.name} on '{printer_name}'")
                res = send_pdf_to_printer(str(pdf_path), printer_name=printer_name)
                if res.get("status") == "success":
                    main_logger.info(f"[{rid}] Sent PDF to printer ({printer_name}): {pdf_path.name}")
                else:
                    main_logger.error(f"[{rid}] Auto-print failed: {res.get('error')}")
        else:
            website_data["printPDF"] = ""
            write_content_json(website_data)

        main_logger.info(f"[{rid}] Pipeline concluded in {datetime.now() - start_time}")
        return True

    except (PipelineStageError, AIGenerationError) as err:
        main_logger.error(f"[{rid}] Pipeline failed ({err.code}): {err}")
        print(f"[analysis_watcher] Pipeline failed ({err.code}): {err}")
        if poem_audio:
            orphan_mp3 = config_manager.SHARED_DIR / poem_audio
            try:
                if orphan_mp3.exists():
                    orphan_mp3.unlink()
            except Exception:
                pass
        if err.code != "ERR_PIPELINE_TIMEOUT" or wrote_live_content:
            _write_content_error(rid, err.code, str(err), load_settings())
        if raise_on_error:
            raise
        return False

    except Exception as e:
        err = PipelineStageError("ERR_PIPELINE_FAILED", str(e))
        main_logger.error(f"[{rid}] Unexpected pipeline error ({err.code}): {e}")
        print(f"[analysis_watcher] Error occurred ({err.code}): {e}")
        if poem_audio:
            orphan_mp3 = config_manager.SHARED_DIR / poem_audio
            try:
                if orphan_mp3.exists():
                    orphan_mp3.unlink()
            except Exception:
                pass
        _write_content_error(rid, err.code, str(e), load_settings())
        if raise_on_error:
            raise err from e
        return False

    finally:
        cleanup_targets = [processed_run, heatmap_run]
        if created_temp_snapshot or (input_img.parent == config_manager.SHARED_DIR and input_img.name.startswith("input_")):
            cleanup_targets.append(input_img)
        for p in cleanup_targets:
            try:
                if p.exists():
                    p.unlink()
            except Exception:
                pass


def _consume_local_dwell_grid():
    """Read and remove optional sidecar dwell_grid.json written by local Desktop Gaze Painter."""
    dwell_path = config_manager.SHARED_DIR / "dwell_grid.json"
    if not dwell_path.exists():
        return None
    try:
        data = json.loads(dwell_path.read_text(encoding="utf-8"))
    except Exception:
        data = None
    try:
        dwell_path.unlink(missing_ok=True)
    except Exception:
        pass
    return data


def watch_loop(stop_event=None, runner_fn=None):
    """Continuously watch shared_output/image.png for changes and trigger/enqueue the pipeline."""
    global _handled_image_sig
    image_path = config_manager.SHARED_DIR / "image.png"
    with _watcher_sig_lock:
        if _handled_image_sig is None:
            _handled_image_sig = _compute_image_sig(image_path)

    print(f"[analysis_watcher] Watching for changes in {image_path}...")
    while stop_event is None or not stop_event.is_set():
        try:
            should_trigger = False
            with _watcher_sig_lock:
                if image_path.exists():
                    sig = _compute_image_sig(image_path)
                    if sig is not None and sig != _handled_image_sig:
                        _handled_image_sig = sig
                        should_trigger = True

            if should_trigger:
                local_dwell = _consume_local_dwell_grid()
                print("\n[analysis_watcher] Detected new drawing in image.png, queuing analysis pipeline...")
                if runner_fn is not None:
                    if local_dwell is not None:
                        try:
                            runner_fn(image_path, dwell_grid=local_dwell)
                        except TypeError:
                            runner_fn(image_path)
                    else:
                        runner_fn(image_path)
                else:
                    rid, snap = snapshot_drawing_input(image_path)
                    run_analysis_pipeline(run_id=rid, snapshot_path=str(snap), dwell_grid=local_dwell)
        except Exception as e:
            print(f"[analysis_watcher] Watch error: {e}")
        time.sleep(0.5)


if __name__ == "__main__":
    _standalone_lock_fd = try_acquire_nonblocking_lock(config_manager.LOGS_DIR / ".watcher.lock")
    if _standalone_lock_fd is None:
        print("[analysis_watcher] Another watcher instance is already running; exiting.")
        sys.exit(0)
    try:
        watch_loop()
    except KeyboardInterrupt:
        print("\nStopping file watch...")
        try:
            from start_everything import stop_ollama_serving
            stop_ollama_serving()
        except Exception:
            pass
    finally:
        release_nonblocking_lock(_standalone_lock_fd)
