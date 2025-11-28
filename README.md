# Latent Space: Through the Eyes of the Algorithm

An interactive art installation that transforms human gaze into AI-interpreted artwork, bridging the gap between human perception and machine understanding.

## Overview

**Latent Space** is an interactive art installation where visitors create drawings simply by looking at a screen. Their eye movements are tracked in real-time, translated into visual strokes, and then analyzed by AI systems that generate psychoanalytical interpretations and poetry based on what they "see" in the abstract marks. The experience culminates in a physical thermal print that visitors can take home as a memento.

The installation explores themes of observation, interpretation, and the increasingly blurred boundary between human creativity and artificial intelligence.

## How It Works

1. **Gaze Capture** — A camera tracks the visitor's eye movements using MediaPipe face mesh detection
2. **Drawing** — Eye position is mapped to canvas coordinates, creating abstract strokes as the visitor looks around
3. **AI Analysis** — The drawing is classified using ResNet50, then interpreted through custom LLM prompts
4. **Poetry Generation** — A second AI model generates a poem inspired by the psychoanalytical interpretation
5. **Web Display** — Results are displayed on a live web gallery with the artwork, analysis, heatmap, and audio
6. **Print Service** — Visitors can print their unique artwork at a dedicated kiosk

## Hardware Architecture

```
┌────────────────────────────────────────────────────────────────┐
│                      LATENT SPACE SYSTEM                       │
├────────────────────────────────────────────────────────────────┤
│                                                                │
│   ┌─────────────────────┐         ┌─────────────────────┐      │
│   │   JETSON ORIN NANO  │         │   RASPBERRY PI 4    │      │
│   │   "The Eye & Brain" │         │    "The Hand"       │      │
│   ├─────────────────────┤         ├─────────────────────┤      │
│   │ • Gaze Tracking     │         │ • Print Kiosk UI    │      │
│   │ • Drawing Canvas    │◄───────►│ • Thermal Printer   │      │
│   │ • AI Analysis       │  Google │ • Touch Interface   │      │
│   │ • Poem Generation   │  Drive  │                     │      │
│   │ • TTS (ElevenLabs)  │  Sync   │                     │      │
│   │ • Web Gallery       │         │                     │      │
│   │ • Admin Dashboard   │         │                     │      │
│   └─────────────────────┘         └─────────────────────┘      │
│              │                                                 │
│              ▼                                                 │
│   ┌─────────────────────┐                                      │
│   │    GOOGLE DRIVE     │                                      │
│   │   (Shared Storage)  │                                      │
│   ├─────────────────────┤                                      │
│   │ • Drawings (PNG)    │                                      │
│   │ • Heatmaps          │                                      │
│   │ • Print PDFs        │                                      │
│   │ • Audio files       │                                      │
│   │ • content.json      │                                      │
│   └─────────────────────┘                                      │
│                                                                │
└────────────────────────────────────────────────────────────────┘
```

## Project Structure

```
LatentSpace/
├── gaze_painter/           # Eye tracking and drawing system
│   ├── run_gaze_painter.py # Main gaze tracking application
│   ├── eye_tracking.py     # MediaPipe-based eye detection
│   ├── drawing.py          # Canvas and stroke rendering
│   ├── eye_visualizer.py   # Visual feedback display
│   └── calibrations/       # Stored calibration data
│
├── analysis_watcher/       # AI processing pipeline
│   ├── run_analysis_watcher.py  # File watcher and orchestrator
│   ├── analysis.py         # ResNet50 image classification
│   ├── chat.py             # LLM API integration (BUas AI platform)
│   ├── heatmap.py          # Attention visualization generator
│   ├── preprocess_image.py # Image normalization
│   ├── tts_el.py           # ElevenLabs text-to-speech
│   └── tts_gc.py           # Google Cloud TTS (alternative)
│
├── print_service/          # Raspberry Pi print kiosk
│   └── print_kiosk_ui.py   # PyQt6 touch interface for printing
│
├── admin_ui/               # Desktop admin tools
│   ├── main.py             # Tkinter admin console
│   └── delete_prints.py    # Print cleanup utilities
│
├── web_ui/                 # Web interfaces
│   ├── admin.html          # Browser-based admin dashboard
│   └── remote_ui.html      # Remote monitoring interface
│
├── index.html              # Main gallery web page
├── monitoring_server.py    # Flask API for system monitoring
├── start_everything.py     # Unified startup script
└── shared_output/          # Google Drive mount point
    ├── image.png           # Current drawing
    ├── heatmap.png         # Attention heatmap
    ├── content.json        # Gallery content data
    └── docs/               # Generated print PDFs
```

## Key Components

### Gaze Painter (`gaze_painter/`)
The eye tracking system uses MediaPipe's face mesh to detect iris positions. After an automatic calibration phase, eye movements are mapped to canvas coordinates where strokes are rendered with variable thickness based on dwell time and movement speed.

### Analysis Pipeline (`analysis_watcher/`)
Monitors the shared output folder for new drawings. When detected:
- Preprocesses the image (crops, resizes to 224×224)
- Classifies using ResNet50 pretrained on ImageNet
- Sends the classification to an LLM for psychoanalytical interpretation
- Generates a poem from the interpretation
- Creates a heatmap showing stroke density
- Optionally generates audio narration via ElevenLabs
- Updates `content.json` for the web gallery

### Print Kiosk (`print_service/`)
A PyQt6 touch-friendly interface running on the Raspberry Pi. Displays available prints with thumbnails, supports date-based search, and handles thermal printing via CUPS.

### Web Gallery (`index.html`)
Auto-refreshing web page displaying the current artwork, AI analysis, poem, heatmap, and audio player. Designed for large display screens at the installation.

### Admin Dashboard (`web_ui/admin.html`, `monitoring_server.py`)
Browser-based control panel for:
- Monitoring system health and service status
- Configuring AI model parameters
- Managing print files
- Viewing system logs
- Troubleshooting guide

## Setup & Installation

### Prerequisites
- Jetson Orin Nano with JetPack
- Raspberry Pi 4 with thermal printer
- USB webcam
- Google Drive account with rclone configured
- Python 3.8+

### Quick Start

1. **Clone the repository**
   ```bash
   git clone <repository-url> /home/kasboek/LatentSpace
   cd /home/kasboek/LatentSpace
   ```

2. **Configure rclone for Google Drive**
   ```bash
   rclone config
   # Create a remote named "latent_space" for Google Drive
   ```

3. **Set up environment files**
   ```bash
   # In analysis_watcher/.env
   API_KEY=your_elevenlabs_key
   ACCESS_TOKEN=your_buas_ai_token
   USER_KEY=your_buas_user_key
   ```

4. **Install dependencies**
   ```bash
   # Jetson - Gaze Painter
   cd gaze_painter
   python3 -m venv virtual_environment
   source virtual_environment/bin/activate
   pip install -r requirements.txt

   # Jetson - Analysis
   cd ../analysis_watcher
   python3 -m venv virtual_environment
   source virtual_environment/bin/activate
   pip install -r requirements.txt

   # Raspberry Pi - Print Service
   cd ../print_service
   pip install -r requirements.txt
   ```

5. **Start the system** (run on each device from the root `LatentSpace` folder)
   ```bash
   python3 start_everything.py
   ```

## Web Endpoints

| Port | Service | Description |
|------|---------|-------------|
| 8000 | Gallery | Public-facing artwork display |
| 8001 | Admin API | Monitoring and control API |
| 8002 | File Server | Direct access to shared files |

## Configuration

### AI Models
The installation uses two LLM assistants via the BUas AI platform:
- **Analysis Model** — Interprets drawings psychoanalytically
- **Poem Model** — Generates poetry from interpretations

Configure via the Admin Dashboard or directly through the API.

### Calibration
Eye tracking calibration is stored in `gaze_painter/calibrations/calibration.json`. The system performs automatic calibration when a new user is detected.

## Maintenance

### Automatic Cleanup
Old print files are automatically deleted at 23:59 each day if and while the admin app runs. For production, consider setting up a cron job.

### Manual Cleanup
```bash
python3 admin_ui/delete_prints.py
```

### Logs
- `analysis_watcher/installation.log` — Main system log
- `analysis_watcher/predictions.csv` — Classification history
- `analysis_watcher/credits.csv` — ElevenLabs usage tracking

## Troubleshooting

| Issue | Solution |
|-------|----------|
| No gaze detected | Check camera connection and lighting |
| Google Drive not syncing | Verify rclone mount with `mountpoint -q /path/to/shared_output` |
| Printer not working | Check CUPS status: `sudo systemctl status cups` |
| AI analysis failing | Verify `.env` credentials and network connectivity |

## Credits

**Project:** Latent Space '25  
**Maintainer:** Vince Németh, Bence Papp, Cristina Jadanov, Bendek Vass
**Location:** BUas Innovation Lab