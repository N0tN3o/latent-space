# Latent Space - Quick Start Installation Guide

## 📋 Prerequisites Checklist

### Hardware
- [  ] Jetson Orin Nano A (8GB) with dual displays
- [  ] Jetson Orin Nano B (8GB)
- [  ] Raspberry Pi 4 with touchscreen
- [  ] USB Camera (for Jetson A)
- [  ] USB Printer (for RPi4)
- [  ] Mobile WiFi router

### Network Configuration
- [  ] All devices on same WiFi network
- [  ] Static IPs configured (via router DHCP reservation):
  - Jetson A: `192.168.1.10`
  - Jetson B: `192.168.1.11`
  - RPi4: `192.168.1.12`

### Software
- [  ] Jetson A: JetPack 5.x with Ubuntu 20.04+
- [  ] Jetson B: JetPack 5.x with Ubuntu 20.04+
- [  ] RPi4: Raspberry Pi OS (Bullseye or later)
- [  ] Internet connection for initial setup

---

## 🚀 Installation Steps

### Phase 1: Jetson B Setup (Do This FIRST!)

Jetson B is the NFS server, so it must be configured before the other devices.

```bash
# 1. Copy the LatentSpace folder to ~/art/
cd ~/art
git clone <repository> LatentSpace  # or copy via USB

# 2. Navigate to setup scripts
cd LatentSpace/setup

# 3. Make scripts executable
chmod +x setup_jetson_b.sh
chmod +x verify_installation.sh

# 4. Run setup (will take ~15 minutes)
sudo ./setup_jetson_b.sh

# 5. When complete, edit WiFi credentials
sudo nano /etc/netplan/01-netcfg.yaml
# Replace YourWiFiSSID and YourWiFiPassword

# 6. Apply network config
sudo netplan apply

# 7. Verify installation
./verify_installation.sh

# 8. Note the SSH public key shown at the end
# You'll need it for the other devices

# 9. Reboot
sudo reboot
```

**After reboot, verify Jetson B is ready:**
```bash
# Check NFS is running
sudo systemctl status nfs-kernel-server

# Check NFS exports
showmount -e localhost
# Should show: ~/art/LatentSpace/shared_output

# Test connectivity
ping 192.168.1.1  # Router
```

---

### Phase 2: Jetson A Setup

```bash
# 1. Copy LatentSpace folder
cd ~/art
# (Copy from USB or clone repo)

# 2. Navigate to setup
cd LatentSpace/setup

# 3. Make executable
chmod +x setup_jetson_a.sh

# 4. Run setup
sudo ./setup_jetson_a.sh

# 5. When prompted, copy the SSH public key shown

# 6. Add SSH key to Jetson B
ssh art@192.168.1.11
mkdir -p ~/.ssh
nano ~/.ssh/authorized_keys
# Paste the public key from Jetson A
# Save and exit

# 7. Back on Jetson A, verify NFS mount
mount | grep shared_output
# Should show: 192.168.1.11:~/art/LatentSpace/shared_output

# 8. Test NFS write access
touch ~/art/LatentSpace/shared_output/test.txt
ls -l ~/art/LatentSpace/shared_output/
# If successful, delete test file
rm ~/art/LatentSpace/shared_output/test.txt

# 9. Connect hardware
# - USB camera
# - Two displays (HDMI)

# 10. Verify installation
./verify_installation.sh

# 11. Reboot
sudo reboot
```

**After reboot, check service:**
```bash
sudo systemctl status gaze-painter
journalctl -u gaze-painter -f
```

---

### Phase 3: Raspberry Pi 4 Setup

```bash
# 1. Copy LatentSpace folder
cd ~/art
# (Copy from USB or clone repo)

# 2. Navigate to setup
cd LatentSpace/setup

# 3. Make executable
chmod +x setup_rpi4.sh

# 4. Run setup
sudo ./setup_rpi4.sh

# 5. Add SSH key to Jetson B (same as Jetson A step)
ssh art@192.168.1.11
nano ~/.ssh/authorized_keys
# Paste RPi4's public key

# 6. Back on RPi4, verify NFS mount
mount | grep shared_output

# 7. Connect USB printer

# 8. Configure printer via CUPS
# Open browser: http://192.168.1.12:631
# Administration → Add Printer
# Select your USB printer
# Set as default printer
# Note the printer name (e.g., "HP_LaserJet")

# 9. Update printer name in UI script
nano ~/art/LatentSpace/print_service/print_kiosk_ui.py
# Find line: PRINTER_NAME = "default"
# Change to: PRINTER_NAME = "HP_LaserJet" (your printer name)

# 10. Verify installation
./verify_installation.sh

# 11. Reboot
sudo reboot
```

**After reboot, the print kiosk should launch automatically on the touchscreen.**

---

## ✅ Post-Installation Verification

### 1. Check All Services

**On Jetson A:**
```bash
sudo systemctl status gaze-painter
```
Should show: `Active: active (running)`

**On Jetson B:**
```bash
sudo systemctl status analysis-watcher
sudo systemctl status web-server
sudo systemctl status nfs-kernel-server
```
All should be active.

**On RPi4:**
```bash
sudo systemctl status print-service
sudo systemctl status cups
```
Both should be active.

---

### 2. Test End-to-End Workflow

**Manual Test:**

1. **On Jetson A:**
   - Two windows should appear:
     - Small display: "Eyes" window
     - Large display: "Gaze Painter" canvas
   - Look at camera, your eyes should be detected
   - Move your gaze to draw on canvas

2. **File Creation:**
   ```bash
   # On Jetson B, watch for new files:
   watch -n 1 ls -lh ~/art/LatentSpace/shared_output/
   ```
   You should see `image.png` appear/update after drawing

3. **Analysis Pipeline:**
   ```bash
   # On Jetson B, check analysis logs:
   journalctl -u analysis-watcher -f
   ```
   Should show classification, psychoanalysis, poem generation

4. **PDF Generation:**
   ```bash
   # Check if PDF was created:
   ls -lh ~/art/LatentSpace/shared_output/docs/
   ```
   Should show `print_YYYYMMDD_HHMMSS.pdf`

5. **Print Service:**
   - On RPi4 touchscreen, should show "New artwork ready!"
   - Tap "PRINT YOUR COPY" button
   - Printer should print the PDF

---

### 3. Network Status Monitoring

Check network health logs on any device:
```bash
tail -f ~/art/LatentSpace/logs/jetson_a_network.log
tail -f ~/art/LatentSpace/logs/jetson_b_network.log
tail -f ~/art/LatentSpace/logs/rpi4_network.log
```

Status indicators:
- `PRIMARY` - All systems operational (green)
- `SECONDARY` - Using SSH fallback (yellow)
- `DEGRADED` - Retrying connection (orange)
- `STOPPED` - Hard stop, error displayed (red)

---

## 🔧 Troubleshooting

### Issue: NFS Mount Fails

**Symptoms:** `mount | grep shared_output` shows nothing

**Fix:**
```bash
# On Jetson B (server), check NFS is running:
sudo systemctl status nfs-kernel-server
sudo exportfs -ra
showmount -e localhost

# On client (Jetson A or RPi4), try manual mount:
sudo mount -t nfs 192.168.1.11:~/art/LatentSpace/shared_output ~/art/LatentSpace/shared_output

# Check firewall:
sudo iptables -L -n | grep 2049
```

---

### Issue: Camera Not Detected (Jetson A)

**Symptoms:** Gaze painter shows "Camera not found"

**Fix:**
```bash
# List video devices:
ls -l /dev/video*

# Check permissions:
sudo chmod 666 /dev/video0

# Test camera:
v4l2-ctl --list-devices
```

---

### Issue: Service Won't Start

**Symptoms:** `systemctl status` shows "failed"

**Fix:**
```bash
# Check detailed logs:
journalctl -u <service-name> --since today

# Try manual start:
cd ~/art/LatentSpace/gaze-painter  # or analysis_watcher, print_service
source venv/bin/activate
python3 run_gaze_painter.py  # or run_analysis_watcher.py, print_kiosk_ui.py

# Check for missing dependencies:
pip list
```

---

### Issue: Printer Not Working (RPi4)

**Symptoms:** Print button doesn't work or shows error

**Fix:**
```bash
# Check printer status:
lpstat -p
lpstat -t

# Check CUPS:
sudo systemctl status cups

# Check print queue:
lpq

# Test print:
echo "Test page" | lp

# If printer is jammed/offline:
# Visit CUPS web interface: http://192.168.1.12:631
# Resume printer or clear jobs
```

---

### Issue: Displays Not Assigned Correctly (Jetson A)

**Symptoms:** Windows appear on wrong screens

**Fix:**
```bash
# List displays:
xrandr --listmonitors

# Manually configure:
nano ~/art/LatentSpace/scripts/display_manager.sh

# Adjust xrandr commands for your setup
# Then restart service:
sudo systemctl restart gaze-painter
```

---

## 📊 Monitoring & Maintenance

### View Logs
```bash
# Real-time service logs:
journalctl -u gaze-painter -f
journalctl -u analysis-watcher -f
journalctl -u print-service -f

# Network health:
tail -f ~/art/LatentSpace/logs/*_network.log

# System resources:
htop

# GPU usage (Jetson only):
tegrastats
```

### Clear Old Data
```bash
# On Jetson B, clear old PDFs:
rm ~/art/LatentSpace/shared_output/docs/print_*.pdf

# Or move to archive:
mkdir -p ~/art/archive
mv ~/art/LatentSpace/shared_output/docs/print_*.pdf ~/art/archive/
```

### Restart Services
```bash
# Restart individual service:
sudo systemctl restart gaze-painter
sudo systemctl restart analysis-watcher
sudo systemctl restart print-service

# Restart all (from Jetson B):
ssh art@192.168.1.10 'sudo systemctl restart gaze-painter'
sudo systemctl restart analysis-watcher web-server
ssh art@192.168.1.12 'sudo systemctl restart print-service'
```

---

## 🚨 Emergency Procedures

### Complete System Reset
```bash
# Stop all services:
sudo systemctl stop gaze-painter analysis-watcher web-server print-service

# Clear shared data:
rm -rf ~/art/LatentSpace/shared_output/*

# Restart all services:
sudo systemctl start gaze-painter analysis-watcher web-server print-service
```

### Network Failure Recovery
If all network checks fail after 2 minutes, error screens will appear.

**To recover:**
1. Check WiFi router is online
2. Verify all devices have network connection: `ping 192.168.1.1`
3. Restart network: `sudo systemctl restart NetworkManager`
4. Remount NFS: `sudo mount -a`
5. Restart services

---

## 📞 Quick Reference

| Device | IP | Services | Web Interface |
|--------|----|----|---------------|
| Jetson A | 192.168.1.10 | gaze-painter | - |
| Jetson B | 192.168.1.11 | analysis-watcher, web-server, nfs-server | http://192.168.1.11:8000 |
| RPi4 | 192.168.1.12 | print-service, cups | http://192.168.1.12:631 |

**Default Login:**
- Username: `art`
- Password: `latentspace`

**SSH Access:**
```bash
ssh art@192.168.1.10  # Jetson A
ssh art@192.168.1.11  # Jetson B
ssh art@192.168.1.12  # RPi4
```

---

## 🎉 You're Ready!

If all checks pass, your installation is complete and running!

The system will:
1. Detect gaze and create drawing (15s cycle)
2. Classify drawing with ResNet50
3. Generate psychoanalysis via AI
4. Generate poem via AI
5. Create PDF with all content
6. Allow users to print via touch interface