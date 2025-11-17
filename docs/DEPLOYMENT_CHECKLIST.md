# Latent Space - Deployment Checklist & Testing Plan

## 📋 Pre-Deployment Checklist

### Hardware Preparation

#### Jetson Orin Nano A
- [ ] Device powered on and accessible
- [ ] USB camera connected and detected (`ls /dev/video*`)
- [ ] Two displays connected (HDMI-0 and HDMI-1)
- [ ] Network: Connected to WiFi
- [ ] IP Address: 192.168.1.10 (verify with `ip addr`)

#### Jetson Orin Nano B
- [ ] Device powered on and accessible
- [ ] Network: Connected to WiFi
- [ ] IP Address: 192.168.1.11 (verify with `ip addr`)
- [ ] Sufficient storage (check `df -h`)

#### Raspberry Pi 4
- [ ] Device powered on and accessible
- [ ] Touchscreen connected and working
- [ ] USB printer connected (`lsusb`)
- [ ] Network: Connected to WiFi
- [ ] IP Address: 192.168.1.12 (verify with `ip addr`)

### Network Setup
- [ ] All devices on same WiFi network (SSID: _________)
- [ ] Router configured with DHCP reservations:
  - [ ] Jetson A → 192.168.1.10
  - [ ] Jetson B → 192.168.1.11
  - [ ] RPi4 → 192.168.1.12
- [ ] All devices can ping each other:
  ```bash
  # From each device, test:
  ping -c 3 192.168.1.10
  ping -c 3 192.168.1.11
  ping -c 3 192.168.1.12
  ping -c 3 192.168.1.1  # Router
  ```

### Software Preparation
- [ ] All devices have latest OS updates
- [ ] Project folder copied to `/home/art/LatentSpace/` on each device
- [ ] Setup scripts are executable (`chmod +x setup/*.sh`)
- [ ] WiFi credentials ready for netplan configuration

---

## 🚀 Installation Phase

### Day 1: Jetson B Setup (NFS Server)

**Time Estimate:** 20-30 minutes

#### Steps:
1. **Run Setup Script**
   ```bash
   cd /home/art/LatentSpace/setup
   sudo ./setup_jetson_b.sh
   ```
   - [ ] Script completed without errors
   - [ ] Note any warnings or errors: _______________

2. **Configure WiFi**
   ```bash
   sudo nano /etc/netplan/01-netcfg.yaml
   # Update SSID and password
   sudo netplan apply
   ```
   - [ ] WiFi connected
   - [ ] Verify IP: `ip addr show wlan0`

3. **Verify Installation**
   ```bash
   ./verify_installation.sh
   ```
   - [ ] All checks passed
   - [ ] NFS server running: `sudo systemctl status nfs-kernel-server`
   - [ ] NFS export visible: `showmount -e localhost`

4. **Copy SSH Public Key**
   ```bash
   cat /home/art/.ssh/id_rsa_sync.pub
   ```
   - [ ] Key copied to secure location (will need for other devices)

5. **Reboot**
   ```bash
   sudo reboot
   ```

6. **Post-Reboot Verification**
   ```bash
   # After reboot, verify services:
   sudo systemctl status analysis-watcher
   sudo systemctl status web-server
   sudo systemctl status nfs-kernel-server
   
   # Check web server
   curl http://localhost:8000
   ```
   - [ ] All services running
   - [ ] Web server accessible

**Jetson B Status:** ✅ Complete / ⚠️ Issues / ❌ Failed

---

### Day 2: Jetson A Setup (Gaze Painter)

**Time Estimate:** 25-35 minutes

#### Steps:
1. **Run Setup Script**
   ```bash
   cd /home/art/LatentSpace/setup
   sudo ./setup_jetson_a.sh
   ```
   - [ ] Script completed without errors

2. **Add SSH Key to Jetson B**
   ```bash
   # Copy public key shown at end of setup
   # Then on Jetson B:
   ssh art@192.168.1.11
   mkdir -p ~/.ssh
   nano ~/.ssh/authorized_keys
   # Paste Jetson A's public key
   ```
   - [ ] Key added successfully

3. **Test SSH Connection**
   ```bash
   # From Jetson A:
   ssh -i ~/.ssh/id_rsa_sync art@192.168.1.11 'echo "SSH works"'
   ```
   - [ ] SSH connection successful

4. **Verify NFS Mount**
   ```bash
   mount | grep shared_output
   # Should show: 192.168.1.11:/home/art/LatentSpace/shared_output
   
   # Test write access
   touch /home/art/LatentSpace/shared_output/test.txt
   ls -l /home/art/LatentSpace/shared_output/test.txt
   rm /home/art/LatentSpace/shared_output/test.txt
   ```
   - [ ] NFS mounted
   - [ ] Write access confirmed

5. **Test Camera**
   ```bash
   v4l2-ctl --list-devices
   ls -l /dev/video0
   ```
   - [ ] Camera detected
   - [ ] Permissions correct (0666)

6. **Test Displays**
   ```bash
   DISPLAY=:0 xrandr --listmonitors
   ```
   - [ ] Two displays detected
   - [ ] Display names noted: __________, __________

7. **Verify Installation**
   ```bash
   ./verify_installation.sh
   ```
   - [ ] All checks passed

8. **Reboot**
   ```bash
   sudo reboot
   ```

9. **Post-Reboot Verification**
   ```bash
   sudo systemctl status gaze-painter
   journalctl -u gaze-painter -n 50
   ```
   - [ ] Service running
   - [ ] No errors in logs
   - [ ] Windows visible on displays

**Jetson A Status:** ✅ Complete / ⚠️ Issues / ❌ Failed

---

### Day 3: Raspberry Pi 4 Setup (Print Service)

**Time Estimate:** 25-35 minutes

#### Steps:
1. **Run Setup Script**
   ```bash
   cd /home/art/LatentSpace/setup
   sudo ./setup_rpi4.sh
   ```
   - [ ] Script completed without errors

2. **Add SSH Key to Jetson B**
   ```bash
   # Copy RPi4's public key
   # Then on Jetson B:
   ssh art@192.168.1.11
   nano ~/.ssh/authorized_keys
   # Paste RPi4's public key
   ```
   - [ ] Key added successfully

3. **Verify NFS Mount**
   ```bash
   mount | grep shared_output
   
   # Test read access
   ls -l /home/art/LatentSpace/shared_output/
   ```
   - [ ] NFS mounted
   - [ ] Can read shared files

4. **Configure Printer**
   - [ ] Open browser: http://192.168.1.12:631
   - [ ] Administration → Add Printer
   - [ ] Select USB printer
   - [ ] Set as default
   - [ ] Print test page
   - [ ] Note printer name: __________

5. **Update Print Service**
   ```bash
   nano /home/art/LatentSpace/print_service/print_kiosk_ui.py
   # Find: PRINTER_NAME = "default"
   # Change to actual printer name
   ```
   - [ ] Printer name updated

6. **Verify Installation**
   ```bash
   ./verify_installation.sh
   ```
   - [ ] All checks passed

7. **Reboot**
   ```bash
   sudo reboot
   ```

8. **Post-Reboot Verification**
   - [ ] Touchscreen displays print kiosk UI automatically
   - [ ] UI shows "Waiting for new artwork..."
   - [ ] Check service: `sudo systemctl status print-service`

**RPi4 Status:** ✅ Complete / ⚠️ Issues / ❌ Failed

---

## 🧪 Testing Phase

### Test 1: End-to-End Workflow

**Objective:** Verify complete pipeline from gaze to print

**Steps:**
1. **Trigger Drawing (Jetson A)**
   - [ ] Look at camera on Jetson A
   - [ ] "Eyes" window shows gaze detected
   - [ ] Calibration completes (30 samples)
   - [ ] "Gaze Painter" window opens
   - [ ] Move gaze to draw on canvas
   - [ ] Timer counts down from 15s
   - [ ] Drawing saves automatically

2. **Verify File Creation (Jetson B)**
   ```bash
   # On Jetson B, watch for file
   watch -n 1 ls -lh /home/art/LatentSpace/shared_output/
   ```
   - [ ] `image.png` appears
   - [ ] File timestamp updates

3. **Monitor Analysis (Jetson B)**
   ```bash
   journalctl -u analysis-watcher -f
   ```
   - [ ] Classification runs (ResNet50)
   - [ ] Label detected: __________
   - [ ] Psychoanalysis API called
   - [ ] Poem API called
   - [ ] Heatmap generated
   - [ ] content.json updated
   - [ ] PDF generated in `docs/`

4. **Check Web Interface**
   - [ ] Open: http://192.168.1.11:8000
   - [ ] Page loads with drawing
   - [ ] Psychoanalysis text visible
   - [ ] Poem text visible
   - [ ] Heatmap image visible

5. **Test Print Service (RPi4)**
   - [ ] Touchscreen shows "New artwork ready!"
   - [ ] PDF filename displayed
   - [ ] Tap "PRINT YOUR COPY" button
   - [ ] Printer prints PDF
   - [ ] UI returns to "Waiting for new artwork..."

**Test 1 Status:** ✅ Pass / ⚠️ Partial / ❌ Fail

**Notes:** _______________________________________________

---

### Test 2: Network Redundancy (NFS Failover)

**Objective:** Verify SSH fallback works when NFS fails

**Steps:**
1. **Baseline Check**
   ```bash
   # On all devices:
   tail -f /home/art/LatentSpace/logs/*_network.log
   ```
   - [ ] All show "PRIMARY" status

2. **Simulate NFS Failure**
   ```bash
   # On Jetson B (NFS server):
   sudo systemctl stop nfs-kernel-server
   ```

3. **Monitor Jetson A**
   - [ ] Within 30s, status changes to "DEGRADED"
   - [ ] "Reconnecting..." message appears
   - [ ] After 30s, status changes to "SECONDARY"
   - [ ] "⚠ Connection Slow" indicator shows
   - [ ] Drawing continues to work

4. **Monitor RPi4**
   - [ ] Similar behavior to Jetson A
   - [ ] Print service continues with warning

5. **Restore NFS**
   ```bash
   # On Jetson B:
   sudo systemctl start nfs-kernel-server
   ```

6. **Verify Recovery**
   - [ ] Within 60s, all devices return to "PRIMARY"
   - [ ] Warning indicators disappear
   - [ ] Normal operation resumes

**Test 2 Status:** ✅ Pass / ⚠️ Partial / ❌ Fail

**Recovery Time:** ________ seconds

---

### Test 3: Network Total Failure (Hard Stop)

**Objective:** Verify graceful shutdown on complete network loss

**Steps:**
1. **Simulate Complete Network Loss**
   ```bash
   # On Jetson B:
   sudo systemctl stop nfs-kernel-server
   sudo systemctl stop sshd
   # OR disconnect from WiFi
   ```

2. **Monitor Jetson A (2 minutes)**
   - [ ] 0-30s: "DEGRADED" status, retrying
   - [ ] 30-120s: "DEGRADED" (SSH also fails)
   - [ ] 120s: Status changes to "STOPPED"
   - [ ] Error screens appear on both displays:
     - Eyes window: "Connection Lost / Please contact staff"
     - Canvas window: "System Offline / Technical difficulties"
   - [ ] Service stops gracefully

3. **Monitor Jetson B**
   - [ ] Creates `/home/art/LatentSpace/logs/SYSTEM_STOPPED.flag`
   - [ ] Logs critical error
   - [ ] Waits for manual recovery

4. **Monitor RPi4**
   - [ ] After 120s, shows error screen
   - [ ] "Service temporarily unavailable"
   - [ ] Creates stop marker flag

5. **Restore Network**
   ```bash
   # On Jetson B:
   sudo systemctl start nfs-kernel-server
   sudo systemctl start sshd
   ```

6. **Verify Manual Recovery Required**
   - [ ] Devices detect network restored
   - [ ] But remain stopped (by design)
   - [ ] Manual restart needed:
     ```bash
     sudo systemctl restart gaze-painter
     sudo systemctl restart analysis-watcher
     sudo systemctl restart print-service
     ```

**Test 3 Status:** ✅ Pass / ⚠️ Partial / ❌ Fail

**Stop Time:** ________ seconds (should be ~120s)

---

### Test 4: Camera Failure Handling

**Objective:** Verify behavior when camera disconnected

**Steps:**
1. **Disconnect Camera (Jetson A)**
   - Physically disconnect USB camera while system running

2. **Observe Behavior**
   - [ ] "Eyes" window shows error or placeholder
   - [ ] "Searching for gaze..." message persists
   - [ ] No crash or service failure
   - [ ] Logs show camera error

3. **Reconnect Camera**
   - [ ] System detects camera
   - [ ] Gaze detection resumes
   - [ ] Normal operation restored

**Test 4 Status:** ✅ Pass / ⚠️ Partial / ❌ Fail

---

### Test 5: Printer Failure Handling

**Objective:** Verify behavior when printer fails

**Steps:**
1. **Simulate Printer Error**
   - Disconnect printer USB cable, OR
   - Turn off printer, OR
   - Remove paper to trigger error

2. **Attempt Print (RPi4)**
   - [ ] Tap "PRINT YOUR COPY"
   - [ ] Error message displays
   - [ ] System doesn't crash
   - [ ] Can retry after fixing issue

3. **Restore Printer**
   - [ ] Reconnect/fix printer
   - [ ] Print succeeds

**Test 5 Status:** ✅ Pass / ⚠️ Partial / ❌ Fail

---

### Test 6: Power Cycle Resilience

**Objective:** Verify auto-start after power loss

**Steps:**
1. **Power Off All Devices**
   ```bash
   sudo shutdown -h now
   ```

2. **Power On All Devices**
   - Wait for full boot (~2-3 minutes)

3. **Verify Auto-Start**
   - [ ] Jetson B: analysis-watcher, web-server, nfs-server running
   - [ ] Jetson A: gaze-painter running, displays active
   - [ ] RPi4: print-service running, kiosk UI visible

4. **Test Full Workflow**
   - [ ] End-to-end test works (Test 1)

**Test 6 Status:** ✅ Pass / ⚠️ Partial / ❌ Fail

---

### Test 7: Performance & Timing

**Objective:** Measure system performance

**Metrics to Record:**

| Metric | Target | Actual | Status |
|--------|--------|--------|--------|
| Gaze detection latency | < 100ms | _____ ms | ⬜ |
| Calibration time | ~30s | _____ s | ⬜ |
| Drawing session | 15s | _____ s | ⬜ |
| Save to NFS | < 1s | _____ s | ⬜ |
| Classification time | < 5s | _____ s | ⬜ |
| Analysis API calls | < 10s each | _____ s | ⬜ |
| PDF generation | < 15s | _____ s | ⬜ |
| Total pipeline | < 60s | _____ s | ⬜ |
| Print time | < 30s | _____ s | ⬜ |

**Test 7 Status:** ✅ Pass / ⚠️ Partial / ❌ Fail

---

### Test 8: Multi-User Stress Test

**Objective:** Test rapid consecutive uses

**Steps:**
1. **Rapid Drawing Cycles**
   - [ ] Complete 5 drawing cycles back-to-back
   - [ ] Each cycle: gaze → draw → save → analyze → PDF

2. **Monitor System Resources**
   ```bash
   htop  # On each device
   tegrastats  # On Jetsons (GPU usage)
   ```
   - [ ] CPU usage: _____ % avg
   - [ ] Memory usage: _____ % avg
   - [ ] No memory leaks detected
   - [ ] No service crashes

3. **Check Print Queue**
   ```bash
   lpq  # On RPi4
   ```
   - [ ] Multiple PDFs queued correctly
   - [ ] All prints complete successfully

**Test 8 Status:** ✅ Pass / ⚠️ Partial / ❌ Fail

---

## 📊 Final Deployment Verification

### Pre-Launch Checklist

**48 Hours Before Event:**
- [ ] All tests passed (Tests 1-8)
- [ ] Network configured and stable
- [ ] Printer loaded with paper (_____ sheets)
- [ ] Spare printer paper available
- [ ] All cables secured and labeled
- [ ] Backup power plan in place
- [ ] Contact information posted for tech support

**24 Hours Before Event:**
- [ ] Full system reboot test
- [ ] End-to-end workflow test × 3
- [ ] Print 10 test pages
- [ ] Clear all old PDFs: `rm /home/art/LatentSpace/shared_output/docs/print_*.pdf`
- [ ] Verify disk space: `df -h` (should have > 10GB free)

**1 Hour Before Event:**
- [ ] Power on all devices
- [ ] Verify all services running
- [ ] Test one complete workflow
- [ ] Position displays for visibility
- [ ] Clean camera lens
- [ ] Printer test page
- [ ] Network signal strength check

**During Event - Monitoring:**
- [ ] Check logs every 30 minutes:
  ```bash
  tail -f /home/art/LatentSpace/logs/*_network.log
  journalctl -u gaze-painter -n 20
  journalctl -u analysis-watcher -n 20
  journalctl -u print-service -n 20
  ```
- [ ] Monitor printer paper level
- [ ] Check disk space: `df -h /home/art/LatentSpace/shared_output`

---

## 🚨 Troubleshooting Quick Reference

### Issue: Service Won't Start
```bash
sudo systemctl status <service-name>
journalctl -u <service-name> --since today
# Try manual start:
cd /home/art/LatentSpace/<directory>
source venv/bin/activate
python3 run_*.py
```

### Issue: NFS Mount Failed
```bash
sudo mount -a
showmount -e 192.168.1.11
sudo systemctl restart nfs-kernel-server  # On Jetson B
```

### Issue: Network Indicator Shows Warning
- Check: `tail -f /home/art/LatentSpace/logs/*_network.log`
- If SECONDARY: SSH fallback active, performance may be slower
- If DEGRADED: Network issues, check WiFi
- If STOPPED: Service halted, check network/NFS

### Issue: Printer Not Working
```bash
lpstat -p  # Check printer status
lpq  # Check print queue
sudo systemctl restart cups
# Clear jammed jobs: lprm -
```

### Emergency Reset
```bash
# Stop all services
sudo systemctl stop gaze-painter analysis-watcher web-server print-service

# Clear shared data
rm /home/art/LatentSpace/shared_output/*.png
rm /home/art/LatentSpace/shared_output/*.json

# Restart services
sudo systemctl start nfs-kernel-server  # Jetson B first
sudo systemctl start analysis-watcher web-server  # Jetson B
sudo systemctl start gaze-painter  # Jetson A
sudo systemctl start print-service  # RPi4
```

---

## 📝 Test Results Summary

**Date:** _____________  
**Tester:** _____________  
**Location:** _____________

| Test | Status | Notes |
|------|--------|-------|
| Test 1: End-to-End | ⬜ | __________ |
| Test 2: NFS Failover | ⬜ | __________ |
| Test 3: Hard Stop | ⬜ | __________ |
| Test 4: Camera Failure | ⬜ | __________ |
| Test 5: Printer Failure | ⬜ | __________ |
| Test 6: Power Cycle | ⬜ | __________ |
| Test 7: Performance | ⬜ | __________ |
| Test 8: Stress Test | ⬜ | __________ |

**Overall System Status:** ✅ Ready / ⚠️ Needs Work / ❌ Not Ready

**Critical Issues:** _______________________________________________

**Recommendations:** _______________________________________________

---

**Sign-Off:**

Technical Lead: _________________ Date: _______  
Installation Manager: _________________ Date: _______