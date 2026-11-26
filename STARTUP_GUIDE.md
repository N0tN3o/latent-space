# Latent Space — Startup & Exhibition Guide

This guide walks through setting up, configuring, and running the **Latent Space: Through the Eyes of the Algorithm** installation on a laptop, desktop, or exhibition server (online or 100% offline).

---

## 1. Prerequisites & Installation

### 1.1. Python Environment
Install Python **3.10+** and install the core dependencies from the project root:

```bash
python -m venv .venv

# Windows (PowerShell)
.\.venv\Scripts\Activate.ps1

# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

> **Note on Optional Modules:**
> * **High-Fidelity HTML-to-PDF Rendering (Recommended):**
>   ```bash
>   pip install playwright
>   playwright install chromium
>   ```
>   *(If Playwright is not installed, Latent Space automatically falls back to its built-in Matplotlib PDF renderer.)*
> * **Desktop OpenCV Gaze Painter (`gaze_painter/`):**
>   If you plan to use the native desktop OpenCV gaze window instead of the browser-based `/draw` page, install `gaze_painter/requirements.txt` in a dedicated virtual environment (`gaze_painter/.venv`) so `opencv-python` and `opencv-python-headless` do not conflict.

---

## 2. Optional: Pre-Download Assets for 100% Offline Web Gaze Tracking (`/draw`)

By default:
* The **Live Gallery (`/`)**, **QR Code Generator (`/api/qr.svg`)**, and **Web Print Kiosk (`/kiosk`)** work **100% offline** out of the box.
* The **Browser Gaze Painter (`/draw`)** loads MediaPipe's `FaceLandmarker` WASM & model files from `web_ui/vendor/mediapipe/` if present, or falls back to `cdn.jsdelivr.net` / `storage.googleapis.com` when online.

If your exhibition venue has **no internet access** (air-gapped Wi-Fi/LAN) and you want to use the web-based Gaze Painter (`/draw`), run the offline asset downloader once while you still have internet:

```bash
python download_offline_assets.py
```

*(Use `python download_offline_assets.py --force` to re-download the files.)*  
This saves `face_landmarker.task`, `vision_bundle.mjs`, and the `wasm/` runtime files into `web_ui/vendor/mediapipe/`, allowing `/draw` to run completely offline.

---

## 3. Starting the Installation

### Option A: Web-Based Suite (Recommended — Default)
Starts the Unified Web Server, Analysis Pipeline, Browser Gaze Painter (`/draw`), Web Print Kiosk (`/kiosk`), Admin Command Center (`/admin`), and local Ollama server (if configured):

```bash
python start_everything.py
```
*(Or run `python monitoring_server.py` directly.)*

### Option B: Full Desktop + Web Suite
Starts everything in Option A **plus** the native desktop OpenCV Gaze Painter window:

```bash
python start_everything.py --mode full
```

### Option C: Custom Port
```bash
python start_everything.py --port 8080
```

---

## 4. Web Interfaces & URLs

Once running (default port `8000`), open the following URLs on the host machine or any tablet/laptop on the same LAN:

| Interface | Local URL | Purpose |
| :--- | :--- | :--- |
| **Live Gallery** | `http://localhost:8000/` | Displays the latest gaze drawing, archetype/fractal metrics, psychoanalytical reading, poem, audio, QR memento code, and past artwork history (`Prev` / `Next` or `←` / `→` arrow keys). |
| **Browser Gaze Painter** | `http://localhost:8000/draw` | Webcam eye-tracking canvas (plus mouse/touch support). Press **Spacebar** while drawing to re-center the cursor to your current gaze, **C** to recalibrate, or **M** to switch between *Stabilized Direct* and *Gaze Flow* modes. |
| **Web Print Kiosk** | `http://localhost:8000/kiosk` | Touch-friendly kiosk for visitors or staff to search (`today`, `yesterday`, time, archetype, poem) and print A4 memento sheets via browser print or the host printer. Resets search filter after 60s of inactivity. |
| **Admin Command Center** | `http://localhost:8000/admin` | Passcode-protected dashboard for live monitoring, switching AI & Vision models, toggling TTS audio, configuring Kasboek attributions, managing prints/auto-print, and viewing logs. |

---

## 5. Admin Authentication

When the server starts, it prints the Admin Passcode in the terminal:
* You can set a custom password via the `LATENT_SPACE_ADMIN_PASSWORD` (or `ADMIN_PASSWORD`) environment variable in `.env`.
* If not set in `.env`, a random 8-character passcode is generated once and saved in `config/admin_pin.txt`.

---

## 6. Configuring AI, Vision, Audio & Printing (`/admin`)

1. **AI & Vision Tab:**
   * Choose your **Self-Hosted Abstract Art Perception Mode** (`auto`, `ollama_vision`, `siglip`, `clip`, `stroke_metrics`, `vision`, or `resnet50`).
   * Configure the **Analysis Model (Psychoanalyst)** and **Poem Model (Lyrical Poet)** independently or side-by-side (supports **Ollama**, **Google Gemini**, **OpenAI-Compatible / Groq / OpenRouter / LM Studio**, **Anthropic Claude**, and **BUas**).
   * Configure **Failover Provider** (`auto` automatically fails over across configured cloud/local providers if the primary provider times out or goes offline).
   * Click **Test Analysis Model** / **Test Poem Model** to verify connectivity immediately.
2. **Attributions (Kasboek) Tab:**
   * Toggle **Enable Kasboek Attributions** ON or OFF in real time and preview the footer before saving.
3. **Prints & Auto-Print Tab:**
   * Toggle **Auto-Print Every New Drawing** ON to automatically send every newly generated PDF to the configured host printer (`default` or a specific printer name).
   * Paper format defaults to **A4** (configurable to `A4`, `A3`, `A5`, `Letter`, or `Legal`).
   * Optionally set a **Public / QR Base URL** if the server is accessed through a custom domain or reverse proxy.

---

## 7. Running the Automated Test Suite

To verify all computer vision, gaze calibration, AI failover, PDF/QR generation, and security lockdown tests:

```bash
python -m unittest discover -s tests -v
```

---

## 8. Recommended Local Ollama Models (8 GB VRAM GPUs)

For exhibition laptops or desktops with an 8 GB VRAM GPU (such as an RTX 4060):

- **Dedicated Hungarian Translation (`mode: "ollama"`):**
  ```bash
  ollama pull translategemma:4b
  ```
  Requires ~3.3 GB VRAM and uses Google's TranslateGemma prompt template in `analysis_watcher/translator.py`.

- **Single Multimodal + Multilingual All-Rounder:**
  ```bash
  ollama pull gemma3:4b
  ```
  Requires ~3.3 GB VRAM and supports vision perception, creative interpretation, poetry, and translation with zero model swapping.

- **CPU-Only Zero-VRAM Translation (`mode: "local_nmt"`):**
  Uses `Helsinki-NLP/opus-mt-tc-big-en-hu` on CPU (0 MB GPU VRAM) so the GPU remains dedicated to your primary Ollama model. Pre-download before offline exhibitions via **Admin -> 3. Multilingual Translation -> Download / Preload Model** or `python download_offline_assets.py --with-translation`.
