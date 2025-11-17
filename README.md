# Latent Space Installation - Setup Package

## Overview
Complete automated setup for the Latent Space art installation across 3 devices:
- **Jetson Orin Nano A**: Gaze tracking + drawing (real-time)
- **Jetson Orin Nano B**: Analysis pipeline + web server (NFS server)
- **Raspberry Pi 4**: Print kiosk (NFS client)

## Network Architecture
```
Mobile WiFi Router (192.168.1.1)
├── Jetson A: 192.168.1.10 (gaze-painter)
├── Jetson B: 192.168.1.11 (analysis-watcher, NFS server)
└── RPi4:     192.168.1.12 (print-service)
```

## Quick Start

### Prerequisites
1. All devices on same WiFi network
2. Internet connection for package downloads
3. Fresh Ubuntu installation (or Jetson/Raspbian OS)
4. User account: `art` (will be created if not exists)

### Installation Order

#### 1. Jetson B (NFS Server) - Setup FIRST
```bash
cd /home/art/LatentSpace/setup
chmod +x setup_jetson_b.sh
sudo ./setup_jetson_b.sh
```
Wait for completion (~15 min), then reboot.

#### 2. Jetson A (Gaze Painter)
```bash
cd /home/art/LatentSpace/setup
chmod +x setup_jetson_a.sh
sudo ./setup_jetson_a.sh
```
Wait for completion (~10 min), then reboot.

#### 3. Raspberry Pi 4 (Print Service)
```bash
cd /home/art/LatentSpace/setup
chmod +x setup_rpi4.sh
sudo ./setup_rpi4.sh
```
Wait for completion (~10 min), then reboot.

## Post-Installation

### Verify Setup
```bash
# On any device
cd /home/art/LatentSpace/setup
./verify_installation.sh
```

### Manual Service Control
```bash
# Jetson A
sudo systemctl status gaze-painter
sudo systemctl restart gaze-painter

# Jetson B
sudo systemctl status analysis-watcher
sudo systemctl status web-server

# RPi4
sudo systemctl status print-service
```

### View Logs
```bash
# Real-time logs
journalctl -u gaze-painter -f
journalctl -u analysis-watcher -f
journalctl -u print-service -f

# Network health logs
tail -f /home/art/logs/jetson_a_network.log
tail -f /home/art/logs/jetson_b_network.log
tail -f /home/art/logs/rpi4_network.log
```

## File Structure After Setup
```
/home/art/LatentSpace/
├── setup/
│   ├── setup_jetson_a.sh
│   ├── setup_jetson_b.sh
│   ├── setup_rpi4.sh
│   ├── verify_installation.sh
│   └── README.md (this file)
├── gaze-painter/
│   ├── venv/
│   ├── run_gaze_painter.py (modified with network health)
│   └── ... (other files)
├── analysis_watcher/
│   ├── venv/
│   ├── run_analysis_watcher.py (modified with network health)
│   └── ... (other files)
├── print_service/
│   ├── venv/
│   ├── print_kiosk_ui.py (NEW)
│   └── requirements.txt
├── shared_output/ (NFS mount point)
│   ├── image.png
│   ├── heatmap.png
│   ├── content.json
│   └── docs/
├── scripts/
│   ├── network_health.py
│   ├── display_manager.sh
│   ├── sync_fallback.sh
│   └── firewall_rules.sh
├── logs/
│   ├── jetson_a_network.log
│   ├── jetson_b_network.log
│   ├── rpi4_network.log
│   └── installation.log
└── .ssh/
    ├── id_rsa_sync (SSH key for rsync fallback)
    └── id_rsa_sync.pub
```

## Network Configuration

### Static IP Assignment (Router DHCP Reservation)
Configure your WiFi router to reserve:
- `192.168.1.10` → Jetson A MAC address
- `192.168.1.11` → Jetson B MAC address
- `192.168.1.12` → RPi4 MAC address

### Firewall Rules (Auto-configured)
- NFS (port 2049): Only devices 10, 11, 12
- SSH (port 22): Only devices 10, 11, 12
- Web (port 8000): Local network only

## Troubleshooting

### NFS Mount Issues
```bash
# Check mount status
mount | grep shared_output

# Manual remount
sudo mount -a

# Check NFS server (on Jetson B)
sudo systemctl status nfs-kernel-server
showmount -e localhost
```

### SSH Key Issues
```bash
# Regenerate sync keys
ssh-keygen -t ed25519 -f ~/.ssh/id_rsa_sync -N ""

# Copy to other devices
ssh-copy-id -i ~/.ssh/id_rsa_sync.pub art@192.168.1.11
```

### Display Assignment Issues (Jetson A)
```bash
# List displays
xrandr --listmonitors

# Manually assign windows
DISPLAY=:0 python3 run_gaze_painter.py
```

### Service Won't Start
```bash
# Check service status
sudo systemctl status <service-name>

# Check logs
journalctl -u <service-name> --since today

# Manual test
cd /home/art/LatentSpace/gaze-painter
source venv/bin/activate
python3 run_gaze_painter.py
```

## Security Notes

### SSH Keys
- Dedicated key for rsync: `~/.ssh/id_rsa_sync`
- No passphrase (for automation)
- Restricted to rsync command only

### Firewall
- Only allows traffic between known devices
- All other external access blocked

### File Permissions
- NFS exports with `no_root_squash` for art user only
- All scripts owned by art:art

## Manual Operations

### Stop All Services
```bash
# Run on each device
sudo systemctl stop gaze-painter      # Jetson A
sudo systemctl stop analysis-watcher  # Jetson B
sudo systemctl stop web-server        # Jetson B
sudo systemctl stop print-service     # RPi4
```

### Emergency Network Reset
```bash
# On Jetson B (NFS server)
cd /home/art/LatentSpace/setup
sudo ./emergency_network_reset.sh
```

### Clear All Data (Fresh Start)
```bash
# WARNING: Deletes all drawings/PDFs
rm -rf /home/art/LatentSpace/shared_output/*
```

## Performance Monitoring

### System Resources
```bash
# CPU/Memory usage
htop

# GPU usage (Jetson only)
tegrastats

# Network usage
iftop
```

### Network Health Status
```bash
# Check current status
cat /home/art/logs/jetson_a_network.log | tail -20
```

## Backup & Recovery

### Backup Configuration
```bash
# Backup SSH keys and configs
tar -czf latent_backup_$(date +%Y%m%d).tar.gz \
  ~/.ssh/id_rsa_sync* \
  /etc/exports \
  /etc/fstab \
  /etc/systemd/system/gaze-painter.service \
  /etc/systemd/system/analysis-watcher.service \
  /etc/systemd/system/print-service.service
```

### Restore After Crash
```bash
# Re-run setup script for affected device
sudo ./setup_jetson_a.sh  # or _b, _rpi4
```

## Development Mode

### Disable Auto-Start (for testing)
```bash
sudo systemctl disable gaze-painter
sudo systemctl disable analysis-watcher
sudo systemctl disable print-service
```

### Run Manually
```bash
cd /home/art/LatentSpace/gaze-painter
source venv/bin/activate
python3 run_gaze_painter.py
```

### Re-enable Auto-Start
```bash
sudo systemctl enable gaze-painter
sudo systemctl start gaze-painter
```

## Contact & Support
For issues during exhibition, check logs first:
```bash
journalctl -u <service> --since "10 minutes ago"
```

Created by: Latent Space Installation Team
Version: 1.0
Date: 2025
