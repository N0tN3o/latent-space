# Latent Space - Troubleshooting Guide

## 🔍 Quick Diagnostics

### Check System Status
```bash
# Run on each device to get quick overview
cd ~/art/LatentSpace/setup
./verify_installation.sh
```

### View Live Logs
```bash
# Jetson A
journalctl -u gaze-painter -f

# Jetson B
journalctl -u analysis-watcher -f
journalctl -u web-server -f

# RPi4
journalctl -u print-service -f

# Network health (all devices)
tail -f ~/art/LatentSpace/logs/*_network.log
```

---

## 🚨 Critical Issues

### Issue: "NFS Mount Failed" on Jetson A or RPi4

**Symptoms:**
- `mount | grep shared_output` shows nothing
- Services can't access shared files
- Network status shows DEGRADED or STOPPED

**Diagnosis:**
```bash
# Check if NFS server is running (on Jetson B)
sudo systemctl status nfs-kernel-server

# Check if export is configured (on Jetson B)
showmount -e localhost

# Check network connectivity
ping 192.168.1.11

# Check firewall (on Jetson B)
sudo iptables -L -n | grep 2049
```

**Solutions:**

**Solution 1: Restart NFS Server**
```bash
# On Jetson B
sudo systemctl restart nfs-kernel-server
sudo exportfs -ra

# On client (Jetson A or RPi4)
sudo mount -a
```

**Solution 2: Manual Mount**
```bash
# On client
sudo mount -t nfs 192.168.1.11:~/art/LatentSpace/shared_output \
                   ~/art/LatentSpace/shared_output
```

**Solution 3: Check /etc/fstab**
```bash
# On client, verify fstab entry
cat /etc/fstab | grep shared_output

# Should contain:
# 192.168.1.11:~/art/LatentSpace/shared_output ~/art/LatentSpace/shared_output nfs defaults,_netdev,hard,intr 0 0

# If missing, add it and mount:
sudo nano /etc/fstab
sudo mount -a
```

**Solution 4: Firewall Issue**
```bash
# On Jetson B, ensure NFS ports are open
sudo iptables -I INPUT -p tcp --dport 2049 -s 192.168.1.10 -j ACCEPT
sudo iptables -I INPUT -p tcp --dport 2049 -s 192.168.1.12 -j ACCEPT
sudo iptables -I INPUT -p tcp --dport 111 -s 192.168.1.10 -j ACCEPT
sudo iptables -I INPUT -p tcp --dport 111 -s 192.168.1.12 -j ACCEPT
sudo netfilter-persistent save
```

---

### Issue: "Camera Not Detected" on Jetson A

**Symptoms:**
- Gaze painter shows black screen or error
- `ls /dev/video*` shows no devices
- Service logs show camera open error

**Diagnosis:**
```bash
# List USB devices
lsusb

# List video devices
ls -l /dev/video*

# Check video device info
v4l2-ctl --list-devices

# Check permissions
ls -l /dev/video0
```

**Solutions:**

**Solution 1: Check USB Connection**
- Physically reconnect camera
- Try different USB port
- Check USB power (some ports may be low-power)

**Solution 2: Fix Permissions**
```bash
sudo chmod 666 /dev/video0
sudo usermod -aG video art
```

**Solution 3: Reload USB Drivers**
```bash
# Unload and reload camera drivers
sudo modprobe -r uvcvideo
sudo modprobe uvcvideo
```

**Solution 4: Create Udev Rule**
```bash
# Ensure persistent permissions
sudo nano /etc/udev/rules.d/99-camera.rules
# Add:
# KERNEL=="video[0-9]*", MODE="0666", GROUP="video"

sudo udevadm control --reload-rules
sudo udevadm trigger
```

---

### Issue: "Network Status STOPPED" - System Halted

**Symptoms:**
- Error screens on all displays
- Network log shows "STOPPED"
- Services are halted
- Files created: `SYSTEM_STOPPED.flag` or `PRINT_SERVICE_STOPPED.flag`

**Diagnosis:**
```bash
# Check network health logs
cat ~/art/LatentSpace/logs/jetson_a_network.log
cat ~/art/LatentSpace/logs/jetson_b_network.log
cat ~/art/LatentSpace/logs/rpi4_network.log

# Check if stop flags exist
ls -l ~/art/LatentSpace/logs/*.flag

# Test network connectivity
ping 192.168.1.1  # Router
ping 192.168.1.11  # Jetson B
```

**Solutions:**

**Solution 1: Fix Network Issue**
```bash
# Reconnect to WiFi
sudo nmcli device wifi connect "<SSID>" password "<PASSWORD>"

# Or restart NetworkManager
sudo systemctl restart NetworkManager
```

**Solution 2: Restart NFS**
```bash
# On Jetson B
sudo systemctl restart nfs-kernel-server
sudo exportfs -ra

# On clients
sudo mount -a
```

**Solution 3: Manual Service Recovery**
```bash
# Remove stop flags
rm ~/art/LatentSpace/logs/*.flag

# Restart services
sudo systemctl restart gaze-painter  # Jetson A
sudo systemctl restart analysis-watcher  # Jetson B
sudo systemctl restart print-service  # RPi4
```

---

### Issue: "Printer Not Responding" on RPi4

**Symptoms:**
- Print button does nothing
- Error message when trying to print
- `lpstat` shows printer offline

**Diagnosis:**
```bash
# Check printer status
lpstat -p

# Check CUPS service
sudo systemctl status cups

# Check print queue
lpq

# List printers
lpstat -t
```

**Solutions:**

**Solution 1: Resume Printer**
```bash
# Enable and resume printer
cupsenable <printer-name>
cupsaccept <printer-name>

# Or via CUPS web interface
# Visit: http://192.168.1.12:631
# Administration → Resume Printer
```

**Solution 2: Clear Print Queue**
```bash
# Cancel all jobs
lprm -

# Restart CUPS
sudo systemctl restart cups
```

**Solution 3: Test Print**
```bash
# Send test page
echo "Test Page" | lp -d <printer-name>

# If fails, check USB connection:
lsusb  # Should show printer
```

**Solution 4: Reconfigure Printer**
- Open CUPS: http://192.168.1.12:631
- Administration → Delete Printer
- Administration → Add Printer
- Select USB printer, configure
- Test print

**Solution 5: Update Printer Name in Code**
```bash
# If printer name changed
nano ~/art/LatentSpace/print_service/print_kiosk_ui.py

# Update line:
# PRINTER_NAME = "your-actual-printer-name"

# Restart service
sudo systemctl restart print-service
```

---

## ⚠️ Common Warnings

### Warning: "Using SSH Fallback" (Network Status SECONDARY)

**Meaning:**
- NFS mount is down
- System is using SSH rsync as backup
- Performance may be slower

**Action:**
- This is expected during network issues
- System will auto-recover when NFS restores
- Check NFS server status on Jetson B
- If persists > 5 minutes, investigate NFS issue

**Check:**
```bash
# On Jetson B
sudo systemctl status nfs-kernel-server
showmount -e localhost
```

---

### Warning: "Reconnecting..." (Network Status DEGRADED)

**Meaning:**
- Network connectivity issues
- System is retrying connection
- May switch to SSH fallback soon

**Action:**
- Monitor for 1-2 minutes
- Will either recover or switch to SECONDARY
- Check WiFi signal strength
- Check router is online

**Check:**
```bash
# Check WiFi signal
iwconfig wlan0

# Check ping times
ping -c 10 192.168.1.1
```

---

## 🐛 Service-Specific Issues

### Gaze Painter (Jetson A)

#### Issue: Windows Not Appearing
**Symptoms:** No "Eyes" or "Gaze Painter" windows visible

**Solutions:**
```bash
# Check DISPLAY variable
echo $DISPLAY  # Should be :0

# Check X11 permissions
ls -l ~/.Xauthority

# Restart X11 session
sudo systemctl restart display-manager

# Manual start to see errors
cd ~/art/LatentSpace/gaze-painter
source venv/bin/activate
DISPLAY=:0 python3 run_gaze_painter.py
```

#### Issue: Calibration Stuck
**Symptoms:** Calibration never completes, stuck at "Calibrating..."

**Solutions:**
- Ensure face is clearly visible to camera
- Check lighting conditions
- Remove glasses if causing issues
- Restart service: `sudo systemctl restart gaze-painter`

#### Issue: Drawing Not Saving
**Symptoms:** Timer expires but no file in shared_output

**Check:**
```bash
# Verify NFS mount
mount | grep shared_output

# Check write permissions
touch ~/art/LatentSpace/shared_output/test.txt
rm ~/art/LatentSpace/shared_output/test.txt

# Check disk space
df -h ~/art/LatentSpace/shared_output
```

---

### Analysis Watcher (Jetson B)

#### Issue: Not Processing Images
**Symptoms:** Images saved but no analysis happens

**Check:**
```bash
# Is service running?
sudo systemctl status analysis-watcher

# Check logs
journalctl -u analysis-watcher -n 50

# Check if watching correct file
ls -l ~/art/LatentSpace/shared_output/image.png

# Manual test
cd ~/art/LatentSpace/analysis_watcher
source venv/bin/activate
python3 run_analysis_watcher.py
```

#### Issue: API Calls Failing
**Symptoms:** Classification works but psychoanalysis/poem fails

**Check:**
```bash
# Check internet connection
ping 8.8.8.8

# Check API endpoint
curl https://ai-assistants.buas.nl

# Review logs for API errors
journalctl -u analysis-watcher | grep "ERROR"
```

**Solutions:**
- Verify API keys are correct
- Check internet connectivity
- Verify BUas API is online
- Check firewall isn't blocking HTTPS

#### Issue: PDF Not Generating
**Symptoms:** Analysis completes but no PDF in docs/

**Check:**
```bash
# Check Playwright installation
cd ~/art/LatentSpace/analysis_watcher
source venv/bin/activate
python3 -c "from playwright.sync_api import sync_playwright; print('OK')"

# Check web server is running
curl http://localhost:8000/index.html

# Check docs directory permissions
ls -ld ~/art/LatentSpace/shared_output/docs/
```

**Solutions:**
```bash
# Reinstall Playwright
cd ~/art/LatentSpace/analysis_watcher
source venv/bin/activate
playwright install chromium
playwright install-deps
```

---

### Print Service (RPi4)

#### Issue: Kiosk UI Not Starting
**Symptoms:** Boots to desktop instead of kiosk UI

**Check:**
```bash
# Is service running?
sudo systemctl status print-service

# Check logs
journalctl -u print-service -n 50

# Check auto-login
cat /etc/systemd/system/getty@tty1.service.d/autologin.conf
```

**Solutions:**
```bash
# Restart service
sudo systemctl restart print-service

# Manual start to see errors
cd ~/art/LatentSpace/print_service
source venv/bin/activate
DISPLAY=:0 python3 print_kiosk_ui.py
```

#### Issue: UI Shows Error Screen
**Symptoms:** Red error screen, "Service temporarily unavailable"

**Cause:** Network stopped (120s timeout reached)

**Solutions:**
- Fix network issue (see Network Status STOPPED above)
- Restart service after network restored

#### Issue: PDF Not Appearing in UI
**Symptoms:** PDF exists but UI shows "Waiting..."

**Check:**
```bash
# Check NFS mount
mount | grep shared_output

# Check PDF exists
ls -l ~/art/LatentSpace/shared_output/docs/print_*.pdf

# Check file permissions
ls -l ~/art/LatentSpace/shared_output/docs/
```

**Solutions:**
```bash
# Remount NFS
sudo mount -a

# Restart print service
sudo systemctl restart print-service
```

---

## 🔧 System Maintenance

### Clear Old PDFs
```bash
# On Jetson B
cd ~/art/LatentSpace/shared_output/docs

# Archive old PDFs
mkdir -p archive
mv print_*.pdf archive/

# Or delete old ones (older than today)
find . -name "print_*.pdf" -mtime +1 -delete
```

### Check Disk Space
```bash
# Check all partitions
df -h

# Check shared_output size
du -sh ~/art/LatentSpace/shared_output

# Clean if needed
rm ~/art/LatentSpace/shared_output/docs/print_*.pdf
```

### Rotate Logs
```bash
# View log sizes
du -sh ~/art/LatentSpace/logs/*.log

# Archive old logs
cd ~/art/LatentSpace/logs
tar -czf logs_$(date +%Y%m%d).tar.gz *.log
rm *.log

# Services will recreate logs on next write
```

### Update Network Health Timeouts
If you need different timeout values:

```bash
nano ~/art/LatentSpace/scripts/network_health.py

# Modify these values:
# self.NFS_TIMEOUT = 30  # Seconds before SSH fallback
# self.TOTAL_TIMEOUT = 120  # Seconds before hard stop

# Restart services after changes
sudo systemctl restart gaze-painter analysis-watcher print-service
```

---

## 📱 Contact & Escalation

### Self-Service Checks (Try First)
1. Run verify script: `./verify_installation.sh`
2. Check service status: `systemctl status <service>`
3. View recent logs: `journalctl -u <service> -n 50`
4. Test network: `ping 192.168.1.11`
5. Restart service: `sudo systemctl restart <service>`

### If Issue Persists
- Document error messages
- Save logs: `journalctl -u <service> > error.log`
- Note what was happening when issue occurred
- Try emergency reset (see below)

### Emergency Reset Procedure
```bash
# STOP ALL SERVICES
ssh art@192.168.1.10 'sudo systemctl stop gaze-painter'
ssh art@192.168.1.11 'sudo systemctl stop analysis-watcher web-server'
ssh art@192.168.1.12 'sudo systemctl stop print-service'

# CLEAR SHARED DATA (on Jetson B)
ssh art@192.168.1.11 'rm -rf ~/art/LatentSpace/shared_output/*.png'
ssh art@192.168.1.11 'rm -rf ~/art/LatentSpace/shared_output/*.json'

# RESTART NFS (on Jetson B)
ssh art@192.168.1.11 'sudo systemctl restart nfs-kernel-server'
ssh art@192.168.1.11 'sudo exportfs -ra'

# RESTART ALL SERVICES
ssh art@192.168.1.11 'sudo systemctl start analysis-watcher web-server'
sleep 5
ssh art@192.168.1.10 'sudo mount -a && sudo systemctl start gaze-painter'
ssh art@192.168.1.12 'sudo mount -a && sudo systemctl start print-service'

# VERIFY
ssh art@192.168.1.10 'systemctl status gaze-painter'
ssh art@192.168.1.11 'systemctl status analysis-watcher'
ssh art@192.168.1.12 'systemctl status print-service'
```

---

## 📚 Log Interpretation

### Network Health Logs
```
✓ PRIMARY - All systems go (green indicator)
⚠ SECONDARY - SSH fallback active (yellow indicator)
⚠ DEGRADED - Retrying connection (orange indicator)
✗ STOPPED - System halted (red indicator)
```

### Service Status
```
Active: active (running) - Service OK
Active: failed - Service crashed, check logs
Active: inactive (dead) - Service stopped
```

### Common Error Messages

**"NFS server not responding"**
- NFS server down or network issue
- Check Jetson B NFS service

**"Camera open failed"**
- Camera not connected or no permissions
- Check USB connection and /dev/video0

**"Connection refused"**
- Target service not running
- Check firewall or service status

**"Permission denied"**
- File/directory permission issue
- Check ownership: `ls -l`

---

**Last Updated:** 2025
**Version:** 1.0