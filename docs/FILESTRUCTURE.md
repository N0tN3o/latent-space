# Latent Space - Complete File Structure Reference

## 📋 Key File Purposes

### Setup Scripts (`setup/`)
- **setup_jetson_a.sh**: Installs gaze tracking system, configures NFS client, sets up dual displays
- **setup_jetson_b.sh**: Installs analysis pipeline, NFS server, web servers (ports 8000 & 8001)
- **setup_rpi4.sh**: Installs print kiosk, NFS client, CUPS printing
- **verify_installation.sh**: Validates all installations

### Gaze Painter (`gaze-painter/`)
- **run_gaze_painter.py**: Main loop with state machine (searching → calibrating → drawing → saving)
- **eye_tracking.py**: MediaPipe face mesh + iris tracking with auto-calibration
- **eye_visualizer.py**: Creates "Eyes" window with filtered display
- **drawing.py**: Canvas management with variable-width strokes
- **utils.py**: Calibration save/load helpers

### Analysis Watcher (`analysis_watcher/`)
- **run_analysis_watcher.py**: File watcher that triggers analysis on new images
- **analysis.py**: ResNet50 ImageNet classification
- **chat.py**: BUas AI Assistants API client (psychoanalysis + poem)
- **heatmap.py**: 3x3 grid visualization of pixel density
- **tts_*.py**: Text-to-speech modules (currently unused)

### Print Service (`print_service/`)
- **print_kiosk_ui.py**: Full-featured PyQt5 touch UI with:
  - PDF thumbnail gallery
  - Date/time search
  - Print history tracking
  - Network status display

### Scripts (`scripts/`)
- **network_health.py**: Three-tier monitoring (PRIMARY → SECONDARY → STOPPED)
- **sync_fallback.sh**: Auto-detects role and dispatches to device-specific sync
- **sync_fallback_*.sh**: Role-specific rsync over SSH
- **display_manager.sh**: Configures dual monitors on Jetson A
- **backup_system.sh**: Daily backups via systemd timer

### Web Interfaces
- **index.html**: Gallery page (port 8000) - shows latest artwork + analysis
- **web_ui/remote_ui.html**: Monitoring dashboard (port 8001) - device status + control

### Core Files
- **monitoring_server.py**: Flask backend for remote monitoring (port 8001)
- **README.md**: Main project documentation
- **korolev-medium.otf**: Custom typography
- **kasboek-logo.jpg**: Event branding

---

## 🔄 Communication Flow

```
┌─────────────┐
│  Jetson A   │  Gaze Tracking + Drawing
│  (Port -)   │  • Saves image.png to NFS
└──────┬──────┘  • Fallback: rsync to Jetson B
       │
       │ NFS / SSH
       ↓
┌─────────────┐
│  Jetson B   │  Analysis + Web Servers
│ (8000/8001) │  • Watches for new images
└──────┬──────┘  • Runs ResNet50 + AI APIs
       │         • Generates heatmap + PDF
       │         • Serves gallery (8000)
       │         • Serves monitoring (8001)
       │ NFS / SSH
       ↓
┌─────────────┐
│    RPi4     │  Print Kiosk
│  (Port 631) │  • Displays PDF gallery
└─────────────┘  • Sends to CUPS printer
```

---

## 🔧 Configuration Checklist

### Jetson A
- [ ] Camera connected: `/dev/video0`
- [ ] Two displays: HDMI-0 (canvas) + HDMI-1 (eyes)
- [ ] NFS mounted: `192.168.1.11:/home/art/LatentSpace/shared_output`
- [ ] SSH key shared with Jetson B
- [ ] Service running: `gaze-painter`

### Jetson B
- [ ] NFS server running: `showmount -e localhost`
- [ ] Firewall open: ports 8000, 8001, 2049, 111
- [ ] SSH keys from Jetson A + RPi4 in `authorized_keys`
- [ ] Services running: `analysis-watcher`, `web-server`, `monitoring-server`
- [ ] Gallery accessible: `http://192.168.1.11:8000`
- [ ] Monitoring accessible: `http://192.168.1.11:8001`

### RPi4
- [ ] Touchscreen connected
- [ ] USB printer connected: `lsusb`
- [ ] Printer configured: `http://192.168.1.12:631`
- [ ] NFS mounted: `192.168.1.11:/home/art/LatentSpace/shared_output`
- [ ] SSH key shared with Jetson B
- [ ] Service running: `print-service`
- [ ] Auto-login enabled

---

## 📊 Port Reference

| Port | Service | Device | Purpose |
|------|---------|--------|---------|
| 22   | SSH     | All    | Remote access |
| 631  | CUPS    | RPi4   | Printer web UI |
| 2049 | NFS     | Jetson B | File sharing |
| 8000 | HTTP    | Jetson B | Gallery webpage |
| 8001 | Flask   | Jetson B | Monitoring dashboard |

---

## 🚀 Quick Commands

### Check Status
```bash
# On any device
cd /home/art/LatentSpace/setup
./verify_installation.sh

# Check specific service
sudo systemctl status gaze-painter        # Jetson A
sudo systemctl status analysis-watcher    # Jetson B
sudo systemctl status print-service       # RPi4
```

### View Logs
```bash
# Service logs
journalctl -u gaze-painter -f
journalctl -u analysis-watcher -f
journalctl -u print-service -f

# Network health
tail -f /home/art/LatentSpace/logs/*_network.log
```

### Manual Sync (if NFS fails)
```bash
# Run sync fallback manually
cd /home/art/LatentSpace/scripts
./sync_fallback.sh
```

### Restart Everything
```bash
# Jetson A
sudo systemctl restart gaze-painter

# Jetson B
sudo systemctl restart analysis-watcher web-server monitoring-server

# RPi4
sudo systemctl restart print-service
```

---

## 📝 Version History

- **v1.0** - Initial setup with basic NFS
- **v1.1** - Added network health monitoring
- **v1.2** - Added SSH rsync fallback
- **v1.3** (CURRENT) - Fixed port conflicts, unified sync, updated requirements

---

**Last Updated:** November 2025  
**Maintainer:** Vince Németh