#!/usr/bin/env python3
"""
Latent Space Analysis Pipeline & File Watcher
Cross-platform, Google-Drive-free, multi-provider AI pipeline.
"""

import asyncio
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
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
    load_settings,
    load_env_vars,
    ensure_directories,
)
from chat import LLMModel
from analysis import analyze_drawing
from heatmap import draw_heatmap
from preprocess_image import preprocess_image

ensure_directories()

# Logger initialization (stored in both LOGS_DIR and analysis_watcher for backwards compatibility)
def _setup_logger(name, filename, fmt="%(asctime)s;%(name)s;%(levelname)s;%(message)s"):
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        for directory in (LOGS_DIR, CURRENT_DIR):
            try:
                fh = logging.FileHandler(directory / filename, encoding="utf-8")
                fh.setFormatter(logging.Formatter(fmt))
                logger.addHandler(fh)
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
    Fallback PDF renderer using Matplotlib if Playwright/Chrome is unavailable.
    Renders the artwork, heatmap, analysis, poem, and optional Kasboek attribution onto an A4 PDF.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import cv2

    fig = plt.figure(figsize=(8.27, 11.69), dpi=150)  # A4 portrait
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

    fig.savefig(str(pdf_path), format="pdf")
    plt.close(fig)
    return str(pdf_path)


def save_print_pdf(url=None, out_dir=None, website_data=None):
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
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    pdf_path = out_dir_path / f"print_{timestamp}.pdf"
    settings = load_settings()
    paper_format = settings.get("printing", {}).get("paper_format", "A4")

    async def _render():
        from playwright.async_api import async_playwright
        chrome_path = find_chrome()
        async with async_playwright() as p:
            browser = (
                await p.chromium.launch(executable_path=chrome_path, headless=True)
                if chrome_path
                else await p.chromium.launch(headless=True)
            )
            page = await browser.new_page()
            await page.goto(url, wait_until="networkidle", timeout=15000)
            await page.pdf(
                path=str(pdf_path),
                format=paper_format,
                print_background=True,
                margin={"top": "0cm", "right": "0cm", "bottom": "0cm", "left": "0cm"},
            )
            await browser.close()
        return str(pdf_path)

    try:
        return asyncio.run(_render())
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
    """Record a printed PDF in shared_output/docs/print_history.json."""
    print_log = DOCS_DIR / "print_history.json"
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
    print_log.write_text(json.dumps(history, indent=2), encoding="utf-8")


def send_pdf_to_printer(pdf_path: str, printer_name: str = "default") -> dict:
    """
    Cross-platform print helper for Windows, macOS, and Linux.
    """
    path = Path(pdf_path)
    if not path.exists():
        return {"status": "error", "error": f"File not found: {pdf_path}"}

    sys_name = platform.system()
    try:
        if sys_name == "Windows":
            # Use Windows shell print verb or PowerShell
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
            if printer_name and printer_name != "default":
                cmd.extend(["-d", printer_name])
            cmd.append(str(path))
            subprocess.run(cmd, check=True, capture_output=True, text=True)

        mark_file_printed(path.name)
        main_logger.info(f"Sent PDF to printer ({printer_name}): {path.name}")
        return {"status": "success", "filename": path.name, "printer": printer_name}
    except Exception as e:
        main_logger.error(f"Printing failed for {path.name}: {e}")
        return {"status": "error", "error": str(e)}


def _run_optional_tts(poem: str, settings: dict) -> str:
    """Run optional TTS if enabled in settings."""
    tts_cfg = settings.get("tts", {})
    if not tts_cfg.get("enabled", False):
        return ""

    provider = tts_cfg.get("provider", "elevenlabs")
    env = load_env_vars()
    output_mp3 = SHARED_DIR / "output.mp3"

    if provider == "elevenlabs":
        api_key = env.get("API_KEY") or env.get("ELEVENLABS_API_KEY")
        if not api_key:
            main_logger.warning("TTS enabled with ElevenLabs, but API_KEY is not set.")
            return ""
        try:
            from elevenlabs.client import ElevenLabs
            from tts_el import run_el_tts
            from postprocess_audio import decrease_volume, overlay_audio

            client = ElevenLabs(api_key=api_key)
            v1 = tts_cfg.get("voice_id_1", "45QiOxkuEAY6ckHqLMe8")
            v2 = tts_cfg.get("voice_id_2", "tJHJUEHzOkMoPmJJ5jo2")
            rae_file = str(SHARED_DIR / "output_rae.mp3")
            ryan_file = str(SHARED_DIR / "output_ryan.mp3")
            run_el_tts(client=client, voice_id=v1, output=rae_file, text=poem, speed=1.2)
            run_el_tts(client=client, voice_id=v2, output=ryan_file, text=poem, speed=1.3)
            decrease_volume(ryan_file, dB=-10)
            overlay_audio(ryan_file, rae_file, output_name=str(output_mp3))
            return "output.mp3"
        except Exception as e:
            main_logger.error(f"ElevenLabs TTS failed: {e}")
            return ""
    elif provider == "google_cloud":
        try:
            from tts_gc import gc_tts_setup, run_gc_tts
            client, voice, audio_config = gc_tts_setup()
            run_gc_tts(client, voice, audio_config, poem, str(SHARED_DIR / "output"))
            return "output.mp3"
        except Exception as e:
            main_logger.error(f"Google Cloud TTS failed: {e}")
            return ""
    return ""


def run_analysis_pipeline():
    """
    Run the complete Latent Space pipeline:
      1. Preprocess drawing image
      2. Analyze abstract art composition, stroke geometry, and Rorschach pareidolia
      3. Run Psychoanalysis LLM (supports Vision + Text across Ollama, Gemini, Anthropic, OpenAI, BUas)
      4. Run Poet LLM
      5. Optional TTS audio generation
      6. Generate 3x3 Attention Heatmap
      7. Update shared_output/content.json
      8. Render Print PDF + thumbnail (and auto-print if enabled)
    """
    start_time = datetime.now()
    main_logger.info(f"Started analysis pipeline at {start_time.isoformat()}")
    settings = load_settings()
    vision_mode = settings.get("ai", {}).get("vision_mode", "auto")

    input_img = SHARED_DIR / "image.png"
    processed_img = SHARED_DIR / "image_processed.png"

    if not input_img.exists():
        main_logger.error(f"Input image does not exist: {input_img}")
        return False

    try:
        image_path = preprocess_image(
            input_filename=str(input_img),
            output_filename=str(processed_img),
        )

        # 1. Model setup
        main_logger.info("Setting up AI models")
        analysis_model = LLMModel("analysis")
        poem_model = LLMModel("poem")
        init_response_a = analysis_model.initialize()
        response_logger.info(init_response_a)
        init_response_p = poem_model.initialize()
        response_logger.info(init_response_p)

        # 2. Abstract Art & Visual Perception Analysis
        main_logger.info(f"Starting visual perception analysis (mode={vision_mode})")
        perception = analyze_drawing(str(image_path), mode=vision_mode)
        summary_label = perception["label"]
        prompt_context = perception["prompt_context"]
        prediction_logger.info(summary_label)
        main_logger.info(f"Visual perception archetype: {summary_label}")

        # 3. Psychoanalytical Interpretation (passes image_path too for Vision-capable models)
        main_logger.info(f"Starting psychoanalysis via {analysis_model._provider} ({analysis_model._llm})")
        analysis_text = analysis_model.chat(message=prompt_context, image_path=str(image_path))
        main_logger.info("Psychoanalysis complete")

        # 4. Poem Generation
        main_logger.info(f"Starting poem generation via {poem_model._provider} ({poem_model._llm})")
        poem_text = poem_model.chat(message=analysis_text)
        main_logger.info("Poem complete")

        # 5. Optional Text-to-Speech
        poem_audio = _run_optional_tts(poem_text, settings)
        if not poem_audio and (SHARED_DIR / "output.mp3").exists():
            poem_audio = "output.mp3"

        # 6. Attention Heatmap
        main_logger.info("Generating attention heatmap")
        draw_heatmap(
            scribble_filename=str(input_img),
            heatmap_filename=str(SHARED_DIR / "heatmap.png"),
        )
        main_logger.info("Heatmap complete")

        # 7. Update content.json for Gallery & Web Kiosk
        geom = perception.get("geometry", {})
        website_data = {
            "scribblePath": "image_processed.png",
            "heatmapPath": "heatmap.png",
            "archetype": summary_label,
            "fractalDimension": geom.get("fractal_dimension"),
            "spatialEntropy": geom.get("spatial_entropy"),
            "primaryZone": geom.get("primary_zone"),
            "psychoanalysis": analysis_text,
            "poem": poem_text,
            "poemAudio": poem_audio,
            "attributions": settings.get("attributions", {}),
            "timestamp": datetime.now().isoformat(),
        }

        content_file = SHARED_DIR / "content.json"
        content_file.write_text(json.dumps(website_data, indent=2, ensure_ascii=False), encoding="utf-8")
        main_logger.info("Website content.json updated")

        # 8. Render Print PDF and save thumbnail
        main_logger.info("Rendering print PDF")
        pdf_file = save_print_pdf(get_gallery_url(), out_dir=str(DOCS_DIR), website_data=website_data)
        if pdf_file:
            main_logger.info(f"Saved print PDF: {pdf_file}")
            pdf_path = Path(pdf_file)
            try:
                thumb_path = pdf_path.with_suffix(".png")
                shutil.copy(str(processed_img), str(thumb_path))
                # Also save metadata sidecar so the Web Print Kiosk can show poem/analysis previews
                meta_path = pdf_path.with_suffix(".json")
                meta_path.write_text(json.dumps(website_data, indent=2, ensure_ascii=False), encoding="utf-8")
            except Exception as e:
                main_logger.error(f"Failed to save thumbnail/metadata: {e}")

            website_data["printPDF"] = f"docs/{pdf_path.name}"
            content_file.write_text(json.dumps(website_data, indent=2, ensure_ascii=False), encoding="utf-8")

            # Auto-print if enabled in settings
            printing_cfg = settings.get("printing", {})
            if printing_cfg.get("auto_print", False):
                printer_name = printing_cfg.get("printer_name", "default")
                main_logger.info(f"Auto-print enabled — printing {pdf_path.name} on '{printer_name}'")
                send_pdf_to_printer(str(pdf_path), printer_name=printer_name)

        main_logger.info(f"Pipeline concluded in {datetime.now() - start_time}")
        return True

    except Exception as e:
        main_logger.error(f"Error during analysis pipeline: {e}")
        print(f"[analysis_watcher] Error occurred: {e}")
        return False


def watch_loop(stop_event=None):
    """Continuously watch shared_output/image.png for changes and trigger the pipeline."""
    image_path = SHARED_DIR / "image.png"
    last_modified_time = image_path.stat().st_mtime if image_path.exists() else None

    print(f"[analysis_watcher] Watching for changes in {image_path}...")
    while stop_event is None or not stop_event.is_set():
        try:
            if image_path.exists():
                current_mtime = image_path.stat().st_mtime
                if last_modified_time is None:
                    last_modified_time = current_mtime
                elif current_mtime != last_modified_time:
                    print("\n[analysis_watcher] Detected new drawing in image.png, running analysis pipeline...")
                    last_modified_time = current_mtime
                    run_analysis_pipeline()
        except Exception as e:
            print(f"[analysis_watcher] Watch error: {e}")
        time.sleep(1.0)


if __name__ == "__main__":
    try:
        watch_loop()
    except KeyboardInterrupt:
        print("\nStopping file watch...")
