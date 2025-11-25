# Latent Space - Modified Deployment Checklist

## 📋 Pre-Install
**User for all devices:** `kasboek`  
**Password:** `latentspace` (or your chosen password)  
**Network:** Private Router (No Internet required for runtime, but needed for install)

### 1. Network Config (Static IPs)
Before running scripts, ensure your router assigns these via MAC address, or set manually in Network Manager.

| Device | Role | IP Address | Interface (Likely) |
|--------|------|------------|--------------------|
| **Jetson A** | Gaze/Client | `192.168.1.10` | `wlp1s0` or `eth0` |
| **Jetson B** | Server/AI | `192.168.1.11` | `wlp1s0` or `eth0` |
| **RPi 4** | Printer | `192.168.1.12` | `wlan0` or `eth0` |

---

## 🚀 Installation Order

### Step 1: Jetson B (The Server)
1. Connect Jetson B to Internet.
2. Copy project folder to `~/Downloads/LatentSpace_Repo` (or similar).
3. Open terminal inside that folder.
4. Run:

```bash
sudo chmod +x setup_jetson_b.sh
sudo ./setup_jetson_b.sh
```

Wait for install. The script will delete old services, install dependencies, copy files to `/home/kasboek/LatentSpace`, and start NFS.

Verify with:

```bash
sudo ./verify_installation.sh
```

Reboot.

### Step 2: Jetson A (The Client)
1. Connect Jetson A to Internet.
2. Copy project folder to the device.
3. Run:

```bash
sudo chmod +x setup_jetson_a.sh
sudo ./setup_jetson_a.sh
```

Important: Ensure you have the display splitter connected. The script detects if you have 1 output (Splitter) or 2 outputs and configures accordingly.

Reboot.

### Step 3: RPi 4 (The Printer)
1. Connect RPi 4 to Internet.
2. Run:

```bash
sudo chmod +x setup_rpi4.sh
sudo ./setup_rpi4.sh
```

Configure Printer:

1. Open a browser on the RPi and go to `http://192.168.1.12:631`.
2. Add Printer -> Select USB Printer -> Share this printer.

Reboot.

## 🔐 SSH Key Exchange (Required for Fallback/Sync)
The scripts generate keys, but you must manually trust them.

1. Copy Jetson A Key -> Jetson B

On Jetson A:

```bash
cat /home/kasboek/.ssh/id_rsa_sync.pub
# Select and copy the output (starting with ssh-rsa...)
```

On Jetson B:

```bash
nano /home/kasboek/.ssh/authorized_keys
# Paste the key on a new line. Save (Ctrl+O) and exit (Ctrl+X).
```

2. Copy RPi 4 Key -> Jetson B

On RPi 4:

```bash
cat /home/kasboek/.ssh/id_rsa_sync.pub
# Copy output
```

On Jetson B:

```bash
nano /home/kasboek/.ssh/authorized_keys
# Paste on a new line. Save and exit.
```

3. Test connection from Jetson A:

```bash
ssh -i ~/.ssh/id_rsa_sync kasboek@192.168.1.11
```

(Should connect without a password.)

## 🚨 Troubleshooting

- **NFS Failed?** If `verify_installation.sh` reports NFS failed on clients:
   - Ensure Jetson B is ON and IP is `192.168.1.11`.
   - On Jetson B:

      ```bash
      sudo exportfs -ra
      sudo systemctl restart nfs-kernel-server
      ```

   - On the client:

      ```bash
      sudo mount -a
      ```

- **Services not starting?** Check logs:

   ```bash
   journalctl -u analysis-watcher -f
   ```

   (or `gaze-painter` / `print-service`)