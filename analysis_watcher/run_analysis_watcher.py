"""
SYSTEM B: Analysis Watcher (v2) - MODIFIED with Network Health
- Lives in 'analysis_watcher/'
- Watches '../shared_output/image.png'
- Saves all output (heatmap, json, pdf) to '../shared_output/'
- Writes web-friendly paths to 'content.json'
- NETWORK HEALTH: Monitors NFS, stops operations on failure
"""

from chat import *
from analysis import run_classification
# from tts_gc import gc_tts_setup, run_gc_tts  # Commented out as per original
# from tts_el import el_tts_setup, run_el_tts  # Commented out as per original
from heatmap import draw_heatmap
import time
from datetime import datetime
import logging
import os
import asyncio
import sys
from pathlib import Path
from playwright.async_api import async_playwright
import json

# Add parent directory to path for network_health import
sys.path.append(str(Path(__file__).parent.parent / "scripts"))

# Import Network Health Monitor
try:
    from network_health import NetworkHealth, NetworkStatus
    NETWORK_HEALTH_AVAILABLE = True
except ImportError:
    print("WARNING: network_health module not found. Running without network monitoring.")
    NETWORK_HEALTH_AVAILABLE = False
    NetworkStatus = None

# --- PATHS ---
SCRIPT_DIR = os.path.dirname(__file__)
SHARED_OUTPUT_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "shared_output"))
PDF_DOCS_DIR = os.path.normpath(os.path.join(SHARED_OUTPUT_DIR, "docs"))
WEB_PATH_PREFIX = "shared_output"

# URL served by python -m http.server (MUST be run from LatentSpace folder)
URL = "http://localhost:8000/index.html"
OUT_FILE = "rendered_index.html"

# Network health check interval
HEALTH_CHECK_INTERVAL = 30  # Check every 30 seconds

# --- Chrome/Browser detection (unchanged) ---
CHROME_CANDIDATES = [
    # Windows - Chrome
    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
    r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe",
    r"%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe",
    r"%PROGRAMFILES%\Google\Chrome SxS\Application\chrome.exe",

    # Windows - Edge
    r"%PROGRAMFILES%\Microsoft\Edge\Application\msedge.exe",
    r"%PROGRAMFILES(X86)%\Microsoft\Edge\Application\msedge.exe",
    r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe",

    # Windows - Firefox, Brave, Opera, Vivaldi
    r"%PROGRAMFILES%\Mozilla Firefox\firefox.exe",
    r"%PROGRAMFILES(X86)%\Mozilla Firefox\firefox.exe",
    r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe",
    r"%PROGRAMFILES%\BraveSoftware\Brave-Browser\Application\brave.exe",
    r"%PROGRAMFILES%\Opera\launcher.exe",
    r"%PROGRAMFILES(X86)%\Opera\launcher.exe",
    r"%PROGRAMFILES%\Vivaldi\Vivaldi.exe",
    r"%PROGRAMFILES(X86)%\Vivaldi\Vivaldi.exe",

    # Linux
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/snap/bin/chromium",
    "/usr/bin/microsoft-edge",
    "/usr/bin/microsoft-edge-stable",
    "/usr/bin/brave-browser",
    "/usr/bin/brave",
    "/usr/bin/firefox",

    # macOS
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Firefox.app/Contents/MacOS/firefox",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Opera.app/Contents/MacOS/Opera",
]

def find_chrome():
    for p in CHROME_CANDIDATES:
        p = os.path.expandvars(p)
        if p and Path(p).exists():
            return str(p)
    return None

async def fetch_rendered_html(url, out_file):
    """Fetch rendered HTML (for testing)"""
    chrome_path = find_chrome()
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=chrome_path, headless=True) if chrome_path else await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(url, wait_until="networkidle")
        html = await page.content()
        await browser.close()
    Path(out_file).write_text(html, encoding="utf-8")
    print(f"Rendered HTML saved to {out_file}")

def save_print_pdf(url=URL, out_dir=PDF_DOCS_DIR):
    """Render the served URL with Playwright and save a PDF"""
    async def _render(url, out_dir):
        chrome_path = find_chrome()
        out_dir_path = Path(out_dir)
        out_dir_path.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        pdf_path = out_dir_path / f'print_{timestamp}.pdf'

        async with async_playwright() as p:
            if chrome_path:
                browser = await p.chromium.launch(executable_path=chrome_path, headless=True)
            else:
                browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(url, wait_until='networkidle')
            await page.pdf(
                path=str(pdf_path),
                format='A4',
                print_background=True,
                margin={'top': '0cm', 'right': '0cm', 'bottom': '0cm', 'left': '0cm'}
            )
            await browser.close()
        return str(pdf_path)

    try:
        return asyncio.run(_render(url, out_dir))
    except Exception as e:
        logging.error(f"Failed to render PDF: {e}")
        return None

def run_analysis_pipeline():
    '''
    Function to run the pipeline.
    Reads from and writes to the SHARED_OUTPUT_DIR.
    '''
    
    # Define all file paths explicitly
    image_file = os.path.join(SHARED_OUTPUT_DIR, "image.png")
    heatmap_file = os.path.join(SHARED_OUTPUT_DIR, "heatmap.png")
    json_file = os.path.join(SHARED_OUTPUT_DIR, "content.json")
    
    # Setup logging
    logging.basicConfig(filename='installation.log', level=logging.INFO, 
                       format='%(asctime)s;%(name)s;%(levelname)s;%(message)s')
    start_time = datetime.now()
    logging.info(f"Started script at {start_time}")

    try:
        logging.info("Setting up models...")
        ASSISTANT_KEY_DPA, USER_KEY, init_endpoint_url_dpa, ask_endpoint_url_dpa, headers = credentials('8a79f5ec-c5dd-448c-9611-99610792a04b')
        ASSISTANT_KEY_POESIE, USER_KEY, init_endpoint_url_poesie, ask_endpoint_url_poesie, headers = credentials('959058ad-4417-49e3-9e71-252ee2fb033d')
        response_dpa = initialize_ai_assistant(ASSISTANT_KEY_DPA, USER_KEY, init_endpoint_url_dpa, headers)
        response_poesie = initialize_ai_assistant(ASSISTANT_KEY_POESIE, USER_KEY, init_endpoint_url_poesie, headers)
        logging.info("Setup complete!")

        # Classification
        logging.info("Starting classification...")
        label = run_classification(image_file)
        logging.info(f"Result of the classification: {label}")

        # Psychoanalysis
        logging.info("Starting psychoanalysis...")
        psychoanalysis = chat_with_ai_assistant(response=response_dpa, message=label, USER_KEY=USER_KEY, ask_endpoint_url=ask_endpoint_url_dpa, ASSISTANT_KEY=ASSISTANT_KEY_DPA, headers=headers)
        logging.info("Psychoanalysis complete!")

        # Poem
        logging.info("Starting writing the poem...")
        poem = chat_with_ai_assistant(response=response_poesie, message=psychoanalysis, USER_KEY=USER_KEY, ask_endpoint_url=ask_endpoint_url_poesie, ASSISTANT_KEY=ASSISTANT_KEY_POESIE, headers=headers)
        logging.info("Poem complete!")

        # Heatmap
        logging.info("Generating heatmap...")
        heatmap_filename = draw_heatmap(scribble_filename=image_file, heatmap_filename=heatmap_file)
        logging.info("Heatmap complete!")

        # Update website JSON
        logging.info("Updating website...")
        website_data = {
            "scribblePath": f"{WEB_PATH_PREFIX}/image.png",
            "heatmapPath": f"{WEB_PATH_PREFIX}/heatmap.png",
            "psychoanalysis": psychoanalysis,
            "poem": poem,
            "poemAudio": "output.mp3",
            "timestamp": datetime.now().isoformat()
        }
        
        with open(json_file, 'w', encoding='utf-8') as f:
            json.dump(website_data, f, indent=2, ensure_ascii=False)
        
        logging.info("Website updated!")

        # Save PDF
        pdf_file = save_print_pdf(URL, out_dir=PDF_DOCS_DIR)
        if pdf_file:
            logging.info(f"Saved print PDF: {pdf_file}")
            pdf_web_path = f"{WEB_PATH_PREFIX}/docs/{os.path.basename(pdf_file)}"
            website_data['printPDF'] = pdf_web_path.replace('\\', '/')
            with open(json_file, 'w', encoding='utf-8') as f:
                json.dump(website_data, f, indent=2, ensure_ascii=False)

        logging.info(f"Program concluded in {datetime.now() - start_time}.")
        return True
    
    except Exception as e:
        logging.error(f"Error during analysis: {str(e)}")
        print(f"Error occurred: {str(e)}")
        return False

if __name__ == "__main__":
    # Initialize Network Health Monitor
    if NETWORK_HEALTH_AVAILABLE:
        network_health = NetworkHealth(role="jetson_b")
        network_status = NetworkStatus.PRIMARY
        print("Network health monitoring enabled")
    else:
        network_health = None
        network_status = None
    
    # Watch file path
    image_path = os.path.join(SHARED_OUTPUT_DIR, "image.png")
    last_modified_time = None
    last_health_check = time.time()
    
    # Ensure shared directory exists
    os.makedirs(SHARED_OUTPUT_DIR, exist_ok=True)
    
    print(f"Watching for changes in {image_path}... Press Ctrl+C to stop.")
    
    try:
        while True:
            current_time = time.time()
            
            # Periodic network health check
            if network_health and (current_time - last_health_check) >= HEALTH_CHECK_INTERVAL:
                status = network_health.check()
                
                # Update status
                old_status = network_status
                network_status = status
                
                # Handle status changes
                if status == NetworkStatus.STOPPED:
                    print("✗ Network failure - stopping analysis watcher")
                    network_health.display_error()
                    break
                
                elif status == NetworkStatus.SECONDARY and old_status == NetworkStatus.PRIMARY:
                    print("⚠ Network degraded - using SSH fallback")
                
                elif status == NetworkStatus.PRIMARY and old_status != NetworkStatus.PRIMARY:
                    print("✓ Network restored")
                
                last_health_check = current_time
            
            # Check for file changes
            if os.path.exists(image_path):
                current_modified_time = os.path.getmtime(image_path)
                
                if last_modified_time is None:
                    # First run
                    print(f"[{datetime.now()}] Found initial file. Running analysis...")
                    
                    # Check network health before processing
                    if network_health:
                        status = network_health.check()
                        if status == NetworkStatus.STOPPED:
                            print("✗ Network unavailable, cannot process file")
                            network_health.display_error()
                            break
                    
                    run_analysis_pipeline()
                    last_modified_time = current_modified_time

                elif current_modified_time != last_modified_time:
                    # File has been modified
                    print(f"\n[{datetime.now()}] Detected change in {image_path}, rerunning analysis...")
                    
                    # Check network health before processing
                    if network_health:
                        status = network_health.check()
                        if status == NetworkStatus.STOPPED:
                            print("✗ Network unavailable, cannot process file")
                            network_health.display_error()
                            break
                    
                    run_analysis_pipeline()
                    last_modified_time = current_modified_time

            time.sleep(1)
            
    except KeyboardInterrupt:
        print("\nStopping file watch...")
    
    finally:
        if network_health:
            print("Analysis watcher stopped")