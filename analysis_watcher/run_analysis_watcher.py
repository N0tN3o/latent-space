#!/usr/bin/env python3
"""
Latent Space Analysis Pipeline & File Watcher
Cross-platform, Google-Drive-free, multi-provider AI pipeline.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import textwrap
import threading
import time
from urllib.parse import parse_qs, urlencode, urlparse
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
    get_server_port,
    load_env_vars,
    load_settings,
    release_nonblocking_lock,
    send_pdf_to_printer,
    try_acquire_nonblocking_lock,
    write_content_json,
)
from analysis import analyze_drawing
from chat import AIGenerationError, LLMModel, enforce_analysis_word_limit
from heatmap import draw_heatmap
from preprocess_image import DrawingValidationError, preprocess_image
from translator import (
    _clean_translated_output,
    get_translation_config,
    translate_analysis_stage_local_nmt,
    translate_poem_stage_local_nmt,
    translate_reading,
)

ensure_directories()

_id_lock = threading.Lock()
_last_run_id = ""
_watcher_sig_lock = threading.Lock()
_handled_image_sig = None
_localized_pdf_locks_guard = threading.Lock()
_localized_pdf_locks = {}
_SAFE_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_]{4,64}$")

HU_MONTHS = (
    "",
    "január", "február", "március", "április", "május", "június",
    "július", "augusztus", "szeptember", "október", "november", "december",
)

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


def _persist_handled_digest(digest: str):
    if not digest:
        return
    try:
        atomic_write_text(config_manager.SHARED_DIR / ".image_handled.sig", f"{digest}\n", encoding="utf-8")
    except Exception:
        pass


def _read_persisted_handled_digest() -> str:
    sig_file = config_manager.SHARED_DIR / ".image_handled.sig"
    if not sig_file.exists():
        return ""
    try:
        return sig_file.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def mark_image_handled(image_path: Path = None):
    """Record current image.png signature (in memory and on disk) so watchers skip an already-queued upload."""
    global _handled_image_sig
    target = image_path or (config_manager.SHARED_DIR / "image.png")
    with _watcher_sig_lock:
        sig = _compute_image_sig(target)
        if sig is not None:
            _handled_image_sig = sig
            _persist_handled_digest(sig[2])


def write_live_image_and_mark_handled(target_path: Path, data: bytes):
    """Atomically write live image.png and mark its signature handled across threads and external processes."""
    global _handled_image_sig
    digest = hashlib.sha256(data).hexdigest()
    with _watcher_sig_lock:
        _persist_handled_digest(digest)
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
vision_logger = _setup_logger("vision_perception", "vision.log")


def _log_vision_entry(payload: dict):
    """Append a structured vision perception entry to the active config_manager.LOGS_DIR/vision.log."""
    try:
        config_manager.LOGS_DIR.mkdir(parents=True, exist_ok=True)
        target = (config_manager.LOGS_DIR / "vision.log").resolve()
        needs_rebind = not vision_logger.handlers or not any(
            isinstance(h, logging.FileHandler) and Path(getattr(h, "baseFilename", "")).resolve() == target
            for h in vision_logger.handlers
        )
        if needs_rebind:
            for h in list(vision_logger.handlers):
                try:
                    h.close()
                except Exception:
                    pass
                vision_logger.removeHandler(h)
            fh = logging.FileHandler(target, encoding="utf-8")
            fh.setFormatter(logging.Formatter("%(asctime)s;%(name)s;%(levelname)s;%(message)s"))
            vision_logger.addHandler(fh)
        vision_logger.info(json.dumps(payload, ensure_ascii=False))
    except Exception:
        pass

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


def get_gallery_url(print_render: bool = False, lang: str = None, run_id: str = None):
    settings = load_settings()
    port = get_server_port(settings)
    params = []
    if print_render:
        params.append(("print_render", "1"))
    if lang and str(lang).strip().lower() in config_manager.ALLOWED_LANGUAGES:
        params.append(("lang", str(lang).strip().lower()))
    if run_id and _SAFE_RUN_ID_RE.match(str(run_id).strip()):
        params.append(("run_id", str(run_id).strip()))
    suffix = f"?{urlencode(params)}" if params else ""
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
    default_logo = PROJECT_ROOT / "assets" / "kasboek" / "kasboek-logo.jpg"
    raw = (logo_path or "").strip()
    if not raw or raw.startswith(("http://", "https://")):
        return default_logo
    rel = raw.lstrip("/")
    if rel == "kasboek-logo.jpg" and default_logo.is_file():
        return default_logo
    for base in (PROJECT_ROOT, config_manager.SHARED_DIR):
        candidate = (base / rel).resolve()
        if candidate.is_relative_to(base.resolve()) and candidate.is_file():
            return candidate
    return default_logo


def _format_pdf_timestamp(ts_raw: str = None, lang: str = "en") -> str:
    dt = None
    if ts_raw:
        try:
            dt = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
        except Exception:
            dt = None
    if dt is None:
        dt = datetime.now()
    if (lang or "en").lower() == "hu":
        return f"{dt.year}. {HU_MONTHS[dt.month]} {dt.day}. {dt.strftime('%H:%M')}"
    return dt.strftime("%B %d, %Y %H:%M")


def _fit_text_block(
    text: str,
    base_width: int,
    base_fontsize: float,
    max_height_frac: float,
    linespacing: float = 1.42,
    min_fontsize: float = 6.2,
    page_height_pt: float = 780.0,
) -> tuple:
    """Adjust wrap width & font size so text fits inside max_height_frac of the page height."""
    fontsize = base_fontsize
    width = max(24, base_width)
    wrapped = _wrap_preserving_lines(text, width=width)
    eff_height_pt = max(400.0, float(page_height_pt))
    while fontsize > min_fontsize:
        line_count = max(1, wrapped.count("\n") + 1)
        est_height = line_count * (fontsize * linespacing / eff_height_pt)
        if est_height <= max_height_frac:
            break
        fontsize = max(min_fontsize, round(fontsize - 0.5, 1))
        scale = base_fontsize / fontsize
        width = max(base_width, int(round(base_width * min(scale, 1.35))))
        wrapped = _wrap_preserving_lines(text, width=width)

    max_lines = max(2, int((max_height_frac * eff_height_pt) / (fontsize * linespacing)))
    lines = wrapped.splitlines()
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(". ") + "..."
        wrapped = "\n".join(lines)
    return wrapped, fontsize


def _count_pdf_pages(pdf_file: Path) -> int:
    """Return the number of pages in a generated PDF (using PyMuPDF if available)."""
    try:
        try:
            import pymupdf as fitz_mod
        except ImportError:
            import fitz as fitz_mod
        if hasattr(fitz_mod, "open"):
            doc = fitz_mod.open(str(pdf_file))
            try:
                return len(doc)
            finally:
                doc.close()
    except Exception:
        pass
    try:
        import re
        raw = pdf_file.read_bytes()
        # Count individual page dictionaries (/Type /Page, excluding /Type /Pages)
        return len(re.findall(rb"/Type\s*/Page\b(?!s)", raw)) or 1
    except Exception:
        return 1


def _pick_localized_fields(website_data: dict, lang: str = "en") -> dict:
    """Resolve archetype, psychoanalysis, and poem for the requested language with English fallback."""
    eff_lang = (lang or "en").strip().lower()
    translations = website_data.get("translations") if isinstance(website_data.get("translations"), dict) else {}
    entry = translations.get(eff_lang) if isinstance(translations.get(eff_lang), dict) else {}
    en_entry = translations.get("en") if isinstance(translations.get("en"), dict) else {}
    return {
        "archetype": entry.get("archetype") or en_entry.get("archetype") or website_data.get("archetype", ""),
        "psychoanalysis": entry.get("psychoanalysis") or en_entry.get("psychoanalysis") or website_data.get("psychoanalysis", ""),
        "poem": entry.get("poem") or en_entry.get("poem") or website_data.get("poem", ""),
    }


def _render_pdf_fallback(pdf_path: Path, website_data: dict, settings: dict, lang: str = None) -> str:
    """
    Fallback PDF renderer using thread-safe Matplotlib Figure API if Playwright/Chrome is unavailable.
    Honors paper_format, logo_path, timestamp, custom_footer_note, language, strictly single-page, no QR code.
    """
    from matplotlib.backends.backend_pdf import FigureCanvasPdf
    from matplotlib.figure import Figure
    import cv2

    eff_lang = (lang or website_data.get("printLanguage") or "en").strip().lower()
    if eff_lang not in config_manager.ALLOWED_LANGUAGES:
        eff_lang = "en"
    loc = _pick_localized_fields(website_data, eff_lang)
    is_hu = eff_lang == "hu"

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
        "Latent Space — Az algoritmus szemével" if is_hu else "Latent Space, Through the Eyes of the Algorithm",
        ha="center", va="center", fontsize=14, fontweight="bold", color="#2a2a2a",
    )
    badge_parts = []
    if loc.get("archetype"):
        badge_parts.append(str(loc["archetype"]).upper())
    if isinstance(website_data.get("fractalDimension"), (int, float)):
        lbl_f = "FRAKTÁL" if is_hu else "FRACTAL"
        badge_parts.append(f"{lbl_f} D={website_data['fractalDimension']:.2f}")
    if isinstance(website_data.get("spatialEntropy"), (int, float)):
        lbl_e = "ENTRÓPIA" if is_hu else "ENTROPY"
        badge_parts.append(f"{lbl_e}={round(website_data['spatialEntropy'] * 100)}%")
    if badge_parts:
        fig.text(
            0.5, 0.515,
            "  ·  ".join(badge_parts),
            ha="center", va="center", fontsize=8, color="#555555",
            bbox=dict(boxstyle="round,pad=0.35", facecolor="#fafafa", edgecolor="#d0d0d0", linewidth=0.8),
        )

    # 2. Analysis on bottom-left (bounded vertically above footer at y=0.11)
    page_height_pt = figsize[1] * 66.7
    width_scale = figsize[0] / PAPER_SIZES_INCHES["A4"][0]
    analysis_base_w = max(32, int(round(48 * width_scale)))
    poem_base_w = max(28, int(round(42 * width_scale)))

    fig.text(0.07, 0.48, "ELEMZÉS" if is_hu else "ANALYSIS", fontsize=9, fontweight="bold", color="#666666")
    analysis_wrapped, analysis_fs = _fit_text_block(
        loc.get("psychoanalysis", ""),
        base_width=analysis_base_w,
        base_fontsize=8.5,
        max_height_frac=0.35,
        linespacing=1.42,
        page_height_pt=page_height_pt,
    )
    fig.text(
        0.07, 0.46, analysis_wrapped,
        ha="left", va="top", fontsize=analysis_fs, linespacing=1.42, color="#2a2a2a",
    )

    # 3. Poem on bottom-right (bounded above Attention Span / QR row at y=0.255)
    fig.text(0.54, 0.48, "VERS" if is_hu else "POEM", fontsize=9, fontweight="bold", color="#666666")
    poem_wrapped, poem_fs = _fit_text_block(
        loc.get("poem", ""),
        base_width=poem_base_w,
        base_fontsize=8.5,
        max_height_frac=0.195,
        linespacing=1.45,
        page_height_pt=page_height_pt,
    )
    fig.text(
        0.54, 0.46, poem_wrapped,
        ha="left", va="top", fontsize=poem_fs, linespacing=1.45, style="italic", color="#2a2a2a",
    )

    # 4. Heatmap below poem (y=0.12..0.24); QR code is intentionally omitted from printed PDFs
    heatmap_file = config_manager.SHARED_DIR / website_data.get("heatmapPath", "heatmap.png")
    if heatmap_file.exists():
        hm = cv2.imread(str(heatmap_file), cv2.IMREAD_GRAYSCALE)
        if hm is not None:
            fig.text(0.54, 0.242, "FIGYELEMTÉRKÉP" if is_hu else "ATTENTION SPAN", fontsize=8, fontweight="bold", color="#666666")
            ax_hm = fig.add_axes([0.54, 0.125, 0.14, 0.105])
            ax_hm.imshow(hm, cmap="gray", vmin=0, vmax=255)
            ax_hm.axis("off")

    # 5. Attribution footer if enabled (supports Veszprém Vibe and/or Kasboek without overlaps)
    attr = settings.get("attributions", {})
    vv_attr = attr.get("veszprem_vibe") if isinstance(attr.get("veszprem_vibe"), dict) else {}
    kasboek_on = bool(attr.get("enabled"))
    vv_on = bool(vv_attr.get("enabled"))

    if kasboek_on or vv_on:
        logo_shown_left = False
        logo_shown_right = False
        footer_parts = []

        if vv_on:
            if vv_attr.get("show_logo", True):
                vv_logo_raw = str(vv_attr.get("logo_path") or "/assets/veszprem-vibe/vv-skyline-on-paper.svg").strip()
                vv_logo_file = _resolve_local_logo_path(vv_logo_raw) if not vv_logo_raw.lower().endswith(".svg") else None
                drawn_vv_raster = False
                if vv_logo_file and vv_logo_file.exists() and vv_logo_file.suffix.lower() != ".svg":
                    try:
                        vv_bgr = cv2.imread(str(vv_logo_file))
                        if vv_bgr is not None:
                            ax_vv = fig.add_axes([0.07, 0.036, 0.09, 0.045])
                            ax_vv.imshow(cv2.cvtColor(vv_bgr, cv2.COLOR_BGR2RGB))
                            ax_vv.axis("off")
                            logo_shown_left = True
                            drawn_vv_raster = True
                    except Exception:
                        pass
                if not drawn_vv_raster:
                    # Crisp vector VV monogram mark matching vv-mark.svg for Matplotlib PDF fallback
                    ax_vv = fig.add_axes([0.07, 0.038, 0.055, 0.042])
                    ax_vv.plot([14, 50, 86], [22, 80, 22], color="#1A2638", linewidth=2.6, solid_joinstyle="miter")
                    ax_vv.plot([32, 50, 68], [22, 50, 22], color="#E6995A", linewidth=2.0, solid_joinstyle="miter")
                    ax_vv.set_xlim(0, 100)
                    ax_vv.set_ylim(100, 0)
                    ax_vv.set_aspect("equal")
                    ax_vv.axis("off")
                    logo_shown_left = True

            vv_intro = (vv_attr.get("intro_text") or "Created for the Veszprém Vibe community.").strip()
            vv_urls = [
                u.strip()
                for u in (
                    vv_attr.get("website_url") or "",
                    vv_attr.get("linkedin_url") or "",
                    vv_attr.get("facebook_url") or "",
                )
                if u and u.strip()
            ]
            if vv_urls:
                vv_prefix = (vv_attr.get("learn_more_text") or "Connect with us:").strip()
                vv_line = f"{vv_intro} {vv_prefix} {' | '.join(vv_urls)}".strip()
            else:
                vv_line = vv_intro
            if vv_attr.get("custom_note") and str(vv_attr["custom_note"]).strip():
                vv_line += f" — {str(vv_attr['custom_note']).strip()}"
            footer_parts.append(vv_line)

        if kasboek_on:
            if attr.get("show_logo", True):
                logo_file = _resolve_local_logo_path(attr.get("logo_path", "/assets/kasboek/kasboek-logo.jpg"))
                if logo_file.exists():
                    try:
                        logo_bgr = cv2.imread(str(logo_file))
                        if logo_bgr is not None:
                            logo_rgb = cv2.cvtColor(logo_bgr, cv2.COLOR_BGR2RGB)
                            if logo_shown_left:
                                ax_logo = fig.add_axes([0.84, 0.036, 0.09, 0.045])
                                logo_shown_right = True
                            else:
                                ax_logo = fig.add_axes([0.07, 0.036, 0.10, 0.045])
                                logo_shown_left = True
                            ax_logo.imshow(logo_rgb)
                            ax_logo.axis("off")
                    except Exception:
                        pass

            founders_str = ", ".join(
                f"{f.get('name')} ({f.get('url')})" if f.get("url") else f"{f.get('name')}"
                for f in attr.get("founders", [])
                if f.get("name")
            )
            intro_str = (attr.get("intro_text") or "Made possible by Kasboek.").strip()
            url_parts = [u.strip() for u in (attr.get("website_url") or "", attr.get("company_linkedin_url") or "") if u and u.strip()]
            if url_parts:
                learn_more = (attr.get("learn_more_text") or "To learn more, visit").strip()
                attr_line = f"{intro_str} {learn_more} {' | '.join(url_parts)}".strip()
            else:
                attr_line = intro_str
            if founders_str:
                attr_line += f"\n{attr.get('thanks_text', 'Thank you for letting us use this:')} {founders_str}"
            if attr.get("custom_footer_note") and str(attr.get("custom_footer_note")).strip():
                attr_line += f"\n{str(attr['custom_footer_note']).strip()}"
            footer_parts.append(attr_line)

        combined_footer = "\n".join(footer_parts)
        if logo_shown_left and logo_shown_right:
            text_x, wrap_w = 0.50, 68
        elif logo_shown_left:
            text_x, wrap_w = 0.55, 74
        else:
            text_x, wrap_w = 0.50, 92
        fs_footer = 5.6 if (kasboek_on and vv_on) else 6.2
        attr_wrapped = _wrap_preserving_lines(combined_footer, width=wrap_w)
        fig.text(text_x, 0.058, attr_wrapped, ha="center", va="center", fontsize=fs_footer, color="#444444")

    ts_str = _format_pdf_timestamp(website_data.get("timestamp"), lang=eff_lang)
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


def save_print_pdf(url=None, out_dir=None, website_data=None, run_id=None, lang=None):
    """
    Render the served gallery URL with Playwright into an A4/configured-format PDF in out_dir,
    falling back to Matplotlib PDF rendering if Playwright/Chromium or the local server is unavailable.
    """
    if url is None:
        url = get_gallery_url(print_render=True, lang=lang)
    if out_dir is None:
        out_dir = config_manager.DOCS_DIR

    eff_lang = (lang or "").strip().lower()
    if not eff_lang and url:
        try:
            q_lang = (parse_qs(urlparse(url).query).get("lang") or [""])[0].strip().lower()
            if q_lang in config_manager.ALLOWED_LANGUAGES:
                eff_lang = q_lang
        except Exception:
            pass
    if not eff_lang and isinstance(website_data, dict):
        eff_lang = str(website_data.get("printLanguage") or "en").strip().lower()
    if eff_lang not in config_manager.ALLOWED_LANGUAGES:
        eff_lang = "en"

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
                paper_w_in, paper_h_in = PAPER_SIZES_INCHES.get(paper_format, PAPER_SIZES_INCHES["A4"])
                printable_w_px = max(360, int(round((paper_w_in - (24.0 / 25.4)) * 96.0)))
                printable_h_px = max(480, int(round((paper_h_in - (20.0 / 25.4)) * 96.0)))
                page = browser.new_page(viewport={"width": printable_w_px, "height": printable_h_px})
                page.goto(url, wait_until="networkidle", timeout=15000)
                try:
                    page.wait_for_function(
                        """async () => {
                            const upd = document.getElementById('lastUpdate');
                            if (!upd) return false;
                            const txt = (upd.textContent || '').trim();
                            if (!txt || txt === 'Waiting for data...' || txt.startsWith('Várakozás')) return false;
                            if (document.fonts && document.fonts.ready) await document.fonts.ready;
                            const imgs = Array.from(document.querySelectorAll('img'))
                                .filter(el => el.getAttribute('src') && el.style.display !== 'none');
                            return imgs.every(img => img.complete && img.naturalWidth > 0);
                        }""",
                        timeout=6000,
                    )
                except Exception:
                    pass
                page.emulate_media(media="print")
                try:
                    content_h = page.evaluate(
                        """() => {
                            const el = document.querySelector('.container') || document.body;
                            return Math.ceil(el.getBoundingClientRect().height);
                        }"""
                    )
                except Exception:
                    content_h = 0

                scale = 1.0
                safe_h = printable_h_px - 16
                if content_h and content_h > safe_h:
                    scale = max(0.5, min(1.0, round(safe_h / float(content_h), 3)))

                tmp_pdf = pdf_path.with_name(f".tmp_{os.getpid()}_{threading.get_ident()}_{pdf_path.name}")
                try:
                    for _attempt in range(4):
                        page.pdf(
                            path=str(tmp_pdf),
                            format=paper_format,
                            scale=scale,
                            print_background=True,
                            margin={"top": "0cm", "right": "0cm", "bottom": "0cm", "left": "0cm"},
                        )
                        if _count_pdf_pages(tmp_pdf) <= 1 or scale <= 0.5:
                            break
                        scale = max(0.5, round(scale * 0.92, 3))
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
            return _render_pdf_fallback(pdf_path, website_data or {}, settings, lang=eff_lang)
        except Exception as e2:
            main_logger.error(f"Fallback PDF renderer failed: {e2}")
            return None


def ensure_localized_print_pdf(run_id: str, lang: str = "en"):
    """
    Return the Path to a print PDF for (run_id, lang), rendering and caching it under
    shared_output/docs/lang/<lang>/print_<run_id>.pdf on first request if needed.
    """
    rid = str(run_id or "").strip()
    eff_lang = str(lang or "en").strip().lower()
    if not _SAFE_RUN_ID_RE.match(rid) or eff_lang not in config_manager.ALLOWED_LANGUAGES:
        return None

    docs_dir = config_manager.DOCS_DIR
    primary_pdf = docs_dir / f"print_{rid}.pdf"
    meta_path = docs_dir / f"print_{rid}.json"
    if not primary_pdf.exists() and not meta_path.exists():
        return None

    meta = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            meta = {}

    primary_lang = str(meta.get("printLanguage") or "en").strip().lower()
    if eff_lang == primary_lang and primary_pdf.exists():
        return primary_pdf

    localized_dir = docs_dir / "lang" / eff_lang
    localized_pdf = localized_dir / f"print_{rid}.pdf"
    if localized_pdf.exists() and localized_pdf.stat().st_size > 100:
        return localized_pdf

    lock_key = (rid, eff_lang)
    with _localized_pdf_locks_guard:
        lock = _localized_pdf_locks.setdefault(lock_key, threading.Lock())
        if len(_localized_pdf_locks) > 256:
            _localized_pdf_locks.clear()
            _localized_pdf_locks[lock_key] = lock

    with lock:
        if localized_pdf.exists() and localized_pdf.stat().st_size > 100:
            return localized_pdf
        if not meta:
            return primary_pdf if primary_pdf.exists() else None
        localized_dir.mkdir(parents=True, exist_ok=True)
        rendered = save_print_pdf(
            url=get_gallery_url(print_render=True, lang=eff_lang, run_id=rid),
            out_dir=str(localized_dir),
            website_data=meta,
            run_id=rid,
            lang=eff_lang,
        )
        if rendered and Path(rendered).exists():
            return Path(rendered)
        return primary_pdf if primary_pdf.exists() else None


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
            v1 = (tts_cfg.get("voice_id_1") or "").strip() or "45QiOxkuEAY6ckHqLMe8"
            v2 = (tts_cfg.get("voice_id_2") or "").strip()
            run_el_tts(client=client, voice_id=v1, output=str(rae_file), text=poem, speed=1.2)
            if v2:
                try:
                    run_el_tts(client=client, voice_id=v2, output=str(ryan_file), text=poem, speed=1.3)
                    decrease_volume(str(ryan_file), dB=-10)
                    overlay_audio(str(ryan_file), str(rae_file), output_name=str(output_mp3))
                except Exception as overlay_err:
                    main_logger.warning(f"Secondary voice / pydub overlay unavailable ({overlay_err}); using primary voice.")
                    os.replace(str(rae_file), str(output_mp3))
            else:
                os.replace(str(rae_file), str(output_mp3))
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
    active_audio = ""
    content_file = shared / "content.json"
    if content_file.exists():
        try:
            active_audio = Path(json.loads(content_file.read_text(encoding="utf-8")).get("poemAudio") or "").name
        except Exception:
            active_audio = ""

    for mp3 in shared.glob("output_*.mp3"):
        if mp3.name == active_audio:
            continue
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
    pipeline_t0 = time.monotonic()

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
        effective_vision = perception.get("effective_mode", vision_mode)
        fallback_reason = perception.get("fallback_reason", "")
        geom_metrics = {k: v for k, v in (perception.get("geometry") or {}).items() if k != "description"}
        safe_csv_label = str(summary_label).replace("\r", " ").replace("\n", " ").replace(";", ",")
        prediction_logger.info(safe_csv_label)
        if fallback_reason:
            main_logger.warning(f"[{rid}] Vision fallback: {fallback_reason}")
        main_logger.info(f"[{rid}] Visual perception archetype: {summary_label} (effective={effective_vision})")
        _log_vision_entry({
            "run_id": rid,
            "requested_mode": perception.get("requested_mode", vision_mode),
            "effective_mode": effective_vision,
            "fallback_reason": fallback_reason,
            "archetype": summary_label,
            "raw_vision_label": perception.get("raw_vision_label", ""),
            "badge_style": perception.get("badge_style", "vision_only"),
            "elapsed_sec": perception.get("elapsed_sec"),
            "vision_options": perception.get("vision_options", {}),
            "metrics": geom_metrics,
            "vision_output": perception.get("vision_output", ""),
            "prompt_context": prompt_context,
        })

        _check_cancelled()

        # 4. Psychoanalytical Interpretation
        analysis_t0 = time.monotonic()
        main_logger.info(f"[{rid}] Starting psychoanalysis via {analysis_model._provider} ({analysis_model._llm})")
        analysis_kwargs = {"message": prompt_context, "image_path": str(image_path), "allow_failover": True}
        if cancel_event is not None:
            analysis_kwargs["cancel_event"] = cancel_event
        analysis_text = analysis_model.chat(**analysis_kwargs)
        analysis_cfg = (settings.get("ai") or {}).get("analysis") or {}
        try:
            analysis_max_words = int(analysis_cfg.get("max_words", 110))
        except (TypeError, ValueError):
            analysis_max_words = 110
        if analysis_max_words > 0:
            analysis_text, wl_info = enforce_analysis_word_limit(
                analysis_text,
                max_words=analysis_max_words,
                tolerance_pct=0.10,
                llm_model=analysis_model,
                cancel_event=cancel_event,
                language_name="English",
            )
            if wl_info.get("action") not in ("unchanged", "disabled"):
                main_logger.info(
                    f"[{rid}] Analysis word-limit enforcer ({wl_info['action']}): "
                    f"{wl_info['original_words']} -> {wl_info['final_words']} words "
                    f"(max {wl_info['max_words']} ±10%, hard cap {wl_info['hard_max_words']})"
                )
        analysis_elapsed = round(time.monotonic() - analysis_t0, 2)
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

        # 4b. Kick off zero-VRAM CPU translation of archetype + psychoanalysis while GPU writes the poem!
        tcfg = get_translation_config(settings)
        target_langs = tcfg["target_languages"] if (tcfg["enabled"] and tcfg["mode"] != "off") else []
        tr_executor = None
        tr_analysis_futures = {}
        tr_start = time.monotonic()
        if target_langs and tcfg["mode"] == "local_nmt":
            tr_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ls_nmt")
            for tlang in target_langs:
                tr_analysis_futures[tlang] = tr_executor.submit(
                    translate_analysis_stage_local_nmt,
                    summary_label,
                    analysis_text,
                    tlang,
                    settings,
                )

        try:
            # 5. Poem Generation (runs on GPU while CPU NMT translates analysis in parallel)
            poem_t0 = time.monotonic()
            main_logger.info(f"[{rid}] Starting poem generation via {poem_model._provider} ({poem_model._llm})")
            poem_kwargs = {"message": analysis_text, "allow_failover": True}
            if cancel_event is not None:
                poem_kwargs["cancel_event"] = cancel_event
            poem_text = _clean_translated_output(poem_model.chat(**poem_kwargs), is_poem=True)
            poem_elapsed = round(time.monotonic() - poem_t0, 2)
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

            # 5b. Complete multilingual translations (non-critical: never fails a reading)
            translations = {
                "en": {
                    "archetype": summary_label,
                    "psychoanalysis": analysis_text,
                    "poem": poem_text,
                }
            }
            translation_provider = "off"
            translation_error = ""
            translation_elapsed = 0.0

            if target_langs:
                tr_errors = []
                if tcfg["mode"] == "local_nmt":
                    for tlang in target_langs:
                        try:
                            part_res = tr_analysis_futures[tlang].result(timeout=45)
                            tr_poem = translate_poem_stage_local_nmt(poem_text, target_lang=tlang, settings=settings)
                            if part_res.get("psychoanalysis") or tr_poem:
                                translations[tlang] = {
                                    "archetype": part_res.get("archetype") or summary_label,
                                    "psychoanalysis": part_res.get("psychoanalysis") or analysis_text,
                                    "poem": tr_poem or poem_text,
                                }
                                translation_provider = part_res.get("provider", f"local_nmt:{tcfg['local_nmt_model']}")
                                main_logger.info(
                                    f"[{rid}] Translated reading -> {tlang} via {translation_provider} "
                                    f"(overlapped total {time.monotonic() - tr_start:.2f}s)"
                                )
                        except Exception as tr_e:
                            tr_errors.append(f"{tlang}: {tr_e}")
                            main_logger.warning(f"[{rid}] Local NMT translation ({tlang}) skipped: {tr_e}")
                    translation_elapsed = round(time.monotonic() - tr_start, 2)
                else:
                    seq_t0 = time.monotonic()
                    for tlang in target_langs:
                        if cancel_event is not None and cancel_event.is_set():
                            break
                        try:
                            main_logger.info(f"[{rid}] Starting translation -> {tlang} via mode={tcfg['mode']}")
                            tr_res = translate_reading(
                                archetype=summary_label,
                                psychoanalysis=analysis_text,
                                poem=poem_text,
                                target_lang=tlang,
                                settings=settings,
                                prompt_context=prompt_context,
                                analysis_model=analysis_model,
                                poem_model=poem_model,
                                cancel_event=cancel_event,
                            )
                            if tr_res.get("ok"):
                                translations[tlang] = {
                                    "archetype": tr_res.get("archetype") or summary_label,
                                    "psychoanalysis": tr_res.get("psychoanalysis") or analysis_text,
                                    "poem": tr_res.get("poem") or poem_text,
                                }
                                translation_provider = tr_res.get("provider", tcfg["mode"])
                                main_logger.info(
                                    f"[{rid}] Translated reading -> {tlang} via {translation_provider} in {tr_res.get('elapsed_sec')}s"
                                )
                        except Exception as tr_e:
                            tr_errors.append(f"{tlang}: {tr_e}")
                            main_logger.warning(f"[{rid}] Translation ({tcfg['mode']} -> {tlang}) skipped: {tr_e}")
                    translation_elapsed = round(time.monotonic() - seq_t0, 2)

                if tr_errors:
                    translation_error = "; ".join(tr_errors)
        finally:
            if tr_executor is not None:
                tr_executor.shutdown(wait=False, cancel_futures=True)

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
        req_print_lang = get_translation_config(latest_settings)["default_print_language"]
        print_lang = req_print_lang if req_print_lang in translations else "en"
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
            "translations": translations,
            "languages": list(translations.keys()),
            "printLanguage": print_lang,
            "translationProvider": translation_provider,
            "translationElapsedSec": translation_elapsed,
            "translationError": translation_error,
            "poemAudio": poem_audio,
            "printPDF": f"docs/{expected_pdf_name}",
            "pdfRendering": True,
            "analysisProvider": f"{analysis_model._last_used_provider}:{analysis_model._last_used_model}",
            "poemProvider": f"{poem_model._last_used_provider}:{poem_model._last_used_model}",
            "stageTimings": {
                "vision_sec": round(float(perception.get("elapsed_sec") or 0.0), 2),
                "analysis_sec": analysis_elapsed,
                "poem_sec": poem_elapsed,
                "translation_sec": translation_elapsed,
                "total_sec": round(time.monotonic() - pipeline_t0, 2),
            },
            "attributions": latest_settings.get("attributions", {}),
            "timestamp": datetime.now().isoformat(),
        }

        write_content_json(website_data)
        wrote_live_content = True
        main_logger.info(f"[{rid}] Website content.json updated")

        # 9. Render Print PDF and save sidecar metadata
        main_logger.info(f"[{rid}] Rendering print PDF (lang={print_lang})")
        pdf_render_url = (
            get_gallery_url(print_render=True)
            if print_lang == "en"
            else get_gallery_url(print_render=True, lang=print_lang)
        )
        pdf_file = save_print_pdf(
            pdf_render_url,
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
        if isinstance(website_data.get("stageTimings"), dict):
            website_data["stageTimings"]["total_sec"] = round(time.monotonic() - pipeline_t0, 2)

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
            for orphan_doc in (archived_thumb, archived_heatmap):
                try:
                    if orphan_doc.exists():
                        orphan_doc.unlink()
                except Exception:
                    pass
            website_data["scribblePath"] = "image_processed.png"
            website_data["heatmapPath"] = "heatmap.png"
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
                        persisted = _read_persisted_handled_digest()
                        _handled_image_sig = sig
                        if sig[2] != persisted:
                            _persist_handled_digest(sig[2])
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
        cleanup_orphaned_temp_files()
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
