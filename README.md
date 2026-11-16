# Latent Space: Through the Eyes of the Algorithm

An interactive art installation that transforms human gaze into AI-interpreted artwork, psychoanalytical readings, and poetry—bridging the gap between human perception and machine understanding.

---

## Key Features (Portable & Self-Hosted Edition)

- **Zero Google Drive Dependency**: Uses local storage (`shared_output/`) and built-in HTTP sync (`/api/drawings`, `/api/files/prints`)—no `rclone` or cloud drive mounts required.
- **Multi-Provider AI Support**: Works with **Ollama** (local/on-prem), **Google Gemini** (free tier available), **Anthropic (Claude)**, **OpenAI / Groq / OpenRouter / LM Studio**, or the legacy **BUas** server.
- **Abstract Art & Rorschach Perception**: Replaces ImageNet ResNet50 object labels with:
  - **Direct Multimodal Vision LLM** input (Gemini, Claude, Ollama LLaVA/Vision, GPT-4o)
  - **OpenCV Stroke & Rorschach Geometry Analyzer** (stroke density, 3×3 spatial fixation balance, bilateral symmetry, enclosed loops, and angularity vs. curvature)
  - **Optional CLIP Zero-Shot Abstract Art Analyzer** (`openai/clip-vit-base-patch32`)
- **Browser & Desktop Gaze Painting**:
  - **Browser Gaze Painter (`/draw`)**: Runs MediaPipe `FaceLandmarker` (iris tracking) directly in any modern browser via webcam, plus mouse/touch drawing support.
  - **Desktop Gaze Painter (`gaze_painter/run_gaze_painter.py`)**: Native OpenCV + MediaPipe application for dedicated local setups.
- **Web Print Kiosk (`/kiosk`) & Auto-Print Mode (No Raspberry Pi Needed)**:
  - Open `/kiosk` on any tablet, iPad, phone, or laptop to browse and print A4 artwork mementos via the browser print dialog or host printer.
  - Or enable **Auto-Print New Drawings** in `/admin` or `/kiosk` to automatically print every finished piece on the host machine's printer.
- **Live Attributions & Branding Switch**:
  - Toggle the **Kasboek** logo and full attribution credits (Website, Company LinkedIn, and 1–3 CEO/Co-Founder LinkedIn links) **ON or OFF** in real time from the Admin Command Center (`/admin`).

---

## Quick Start (Local / On-Prem / Laptop)

1. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

2. **Start the Unified Server & Web Suite**
   ```bash
   python start_everything.py
   ```
   *(Or run `python monitoring_server.py` directly)*

3. **Open the Web Interfaces** (default port `8000`):
   - **Live Gallery**: `http://localhost:8000/`
   - **Browser Gaze Painter**: `http://localhost:8000/draw`
   - **Web Print Kiosk**: `http://localhost:8000/kiosk`
   - **Admin Command Center**: `http://localhost:8000/admin`

### Launch Modes (`start_everything.py`)
```bash
# Default: Unified Web Server + AI Watcher + Browser Gaze (/draw) + Web Kiosk (/kiosk)
python start_everything.py --mode server

# Full Desktop Mode: Unified Web Server + Native OpenCV Gaze Painter window
python start_everything.py --mode full

# Native OpenCV Gaze Painter only (can upload to a remote server via LATENT_SPACE_SERVER_URL)
python start_everything.py --mode gaze
```

---

## Configuring AI Providers (`/admin` → `🧠 AI & Vision`)

You can switch providers and models live in `http://localhost:8000/admin` (saved in `config/settings.json`):

| Provider | Example Models | Setup |
|---|---|---|
| **Ollama (Local)** | `llama3.2`, `llava`, `qwen2.5` | Run `ollama serve` locally (`http://localhost:11434/v1`) |
| **Google Gemini** | `gemini-2.0-flash`, `gemini-2.5-flash` | Paste `GEMINI_API_KEY` in Admin UI or `.env` |
| **OpenAI-Compatible** | Groq (`llama-3.3-70b-versatile`), OpenRouter, LM Studio | Set Base URL & API key in Admin UI or `.env` |
| **Anthropic** | `claude-3-5-haiku-latest`, `claude-3-5-sonnet-latest` | Paste `ANTHROPIC_API_KEY` in Admin UI or `.env` |
| **BUas Legacy** | `llama3.3:latest` | Set `ACCESS_TOKEN` and `USER_KEY` in `analysis_watcher/.env` |

---

## Attributions Switch (`/admin` → `🏷️ Attributions`)

In the **Attributions (Kasboek)** tab of `/admin`:
- **When OFF**: Hides the Kasboek logo and attribution credits across the Gallery, Kiosk, and PDF prints.
- **When ON**: Displays the Kasboek logo alongside customizable attribution text and clickable links:
  > *"Made possible by Kasboek. To learn more, visit [Kasboek Website] · [Kasboek LinkedIn]. Thank you for letting us use this: [Founder 1 LinkedIn] · [Founder 2 LinkedIn] · [Founder 3 LinkedIn]."*

---

## Hosting on a Free Cloud Service or Docker

Because all web interfaces (`/`, `/draw`, `/kiosk`, `/admin`) and the analysis pipeline run inside a single server process on `$PORT`, you can deploy this repository directly to **Render**, **Hugging Face Spaces (Docker)**, **Railway**, **Fly.io**, or any Docker host:

```bash
docker build -t latent-space .
docker run -p 8000:8000 -e GEMINI_API_KEY=your_key_here latent-space
```

---

## Credits

**Project:** Latent Space  
**Maintainers:** Vince Németh, Bence Papp, Cristina Jadanov, Benedek Vass
