# Secure Multi-Redundant Installation Architecture

## Network Topology

```
┌─────────────────────────────────────────────────────────────┐
│                    Mobile WiFi Network                       │
│                  (WPA2/3 Encrypted)                          │
│                                                              │
│   Static IPs assigned via router DHCP reservation:          │
│   - Jetson A:  192.168.1.10                                 │
│   - Jetson B:  192.168.1.11                                 │
│   - RPi4:      192.168.1.12                                 │
└─────────────────────────────────────────────────────────────┘
```

## File Sharing - Triple Redundancy

### Layer 1: Primary - Secure NFS (NFSv4 + Kerberos)
```
Jetson B (NFS Server)
├── Exports: ~/art/shared_output
├── Security: sec=krb5p (privacy + integrity)
├── Ports: 2049 (NFS), 111 (portmap) - firewalled
└── Access: Certificate-based authentication

Jetson A & RPi4 (NFS Clients)
├── Mount: ~/art/shared_output
├── Options: sec=krb5p,hard,intr,_netdev
└── Fallback: auto-retry on network loss
```

**Advantages:**
- Real-time file access
- Encrypted traffic
- Automatic reconnection
- Native filesystem semantics

**Failure Scenarios Handled:**
- Network dropout: Auto-retry with `hard` mount
- Server reboot: Clients wait and reconnect
- Stale file handles: `intr` allows interruption

---

### Layer 2: Secondary - Encrypted rsync over SSH

```bash
# Runs every 30 seconds as backup sync
# Only activates if NFS mount check fails

Jetson A → Jetson B
rsync -avz -e "ssh -i ~/art/.ssh/id_rsa_sync" \
  ~/art/shared_output/image.png \
  art@192.168.1.11:~/art/shared_output/

Jetson B → RPi4
rsync -avz -e "ssh -i ~/art/.ssh/id_rsa_sync" \
  ~/art/shared_output/docs/*.pdf \
  art@192.168.1.12:~/art/shared_output/docs/
```

**Security Features:**
- SSH key-based auth (no passwords)
- Restricted SSH command execution
- Dedicated sync user with minimal permissions
- Traffic encrypted by SSH tunnel

**Activation Logic:**
```python
# In each system's sync script
if not os.path.ismount('~/art/shared_output'):
    log("NFS mount failed, activating rsync fallback")
    run_rsync_sync()
else:
    log("NFS healthy, rsync standby")
```

---

### Layer 3: Tertiary - Local Cache + State Recovery

```
Each device maintains local cache:

Jetson A:
├── ~/art/.cache/last_saved_image.png
└── ~/art/.cache/last_calibration.json

Jetson B:
├── ~/art/.cache/content.json
└── ~/art/.cache/last_analysis_state.json

RPi4:
├── ~/art/.cache/pending_prints/
└── ~/art/.cache/print_queue.json
```

**Behavior on Total Network Failure:**
- Jetson A: Continues drawing, saves locally, queues for sync
- Jetson B: Uses last image cache, generates analysis, queues output
- RPi4: Shows "Network Issue - Prints Queued" message, works from cache

**Recovery on Network Restore:**
```bash
# Automatic queue flush
if network_restored():
    sync_all_cached_files()
    resume_normal_operation()
```

---

## Security Implementation Details

### 1. Kerberos Setup (for NFSv4 sec=krb5p)

**Why Kerberos?**
- Mutual authentication (server verifies client, client verifies server)
- Encrypted NFS traffic (privacy mode)
- Integrity checking (prevents tampering)
- No passwords transmitted over network

**Simplified Setup Script:**
```bash
# On Jetson B (KDC + NFS Server)
sudo apt install krb5-kdc krb5-admin-server nfs-kernel-server

# Create realm: ARTINSTALL.LOCAL
sudo krb5_newrealm

# Add principals
kadmin.local -q "addprinc -randkey nfs/jetson-b.artinstall.local"
kadmin.local -q "addprinc -randkey host/jetson-a.artinstall.local"
kadmin.local -q "addprinc -randkey host/rpi4.artinstall.local"

# Export keytabs
kadmin.local -q "ktadd -k /etc/krb5.keytab nfs/jetson-b.artinstall.local"

# Configure NFSv4 exports with security
echo "~/art/shared_output *(rw,sync,sec=krb5p,no_subtree_check)" >> /etc/exports
```

**Alternative (Simpler but Less Secure):**
If Kerberos is too complex for your timeline, use **NFSv4 with AUTH_SYS + IPTables firewall**:
```bash
# Restrict NFS access to known IPs only
iptables -A INPUT -p tcp --dport 2049 -s 192.168.1.10 -j ACCEPT
iptables -A INPUT -p tcp --dport 2049 -s 192.168.1.12 -j ACCEPT
iptables -A INPUT -p tcp --dport 2049 -j DROP
```

---

### 2. SSH Hardening for rsync

```bash
# Create dedicated sync user
sudo useradd -m -s /bin/bash artsync

# Generate SSH keys (no passphrase for automation)
ssh-keygen -t ed25519 -f ~/art/.ssh/id_rsa_sync -N ""

# Copy public keys to authorized_keys on all devices
# Then restrict command execution:

# On Jetson B ~/artsync/.ssh/authorized_keys:
command="rsync --server --daemon .",no-port-forwarding,no-X11-forwarding,no-agent-forwarding ssh-ed25519 AAAAC3...

# This ensures the key can ONLY run rsync, nothing else
```

**Additional SSH Hardening:**
```bash
# /etc/ssh/sshd_config
PermitRootLogin no
PasswordAuthentication no
PubkeyAuthentication yes
AllowUsers artsync
```

---

### 3. HTTPS for Web Interface

```bash
# Generate self-signed certificate for local network
openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
  -keyout ~/art/ssl/server.key \
  -out ~/art/ssl/server.crt \
  -subj "/CN=192.168.1.11"

# Run Python HTTPS server instead of HTTP
# (Modified in startup script)
```

```python
# Modified server script
import http.server
import ssl

httpd = http.server.HTTPServer(('0.0.0.0', 8443), 
                                http.server.SimpleHTTPRequestHandler)
httpd.socket = ssl.wrap_socket(httpd.socket,
                                keyfile='~/art/ssl/server.key',
                                certfile='~/art/ssl/server.crt',
                                server_side=True)
httpd.serve_forever()
```

---

### 4. API Security for BUas Assistants

```bash
# Store API keys securely (not in code)
# Use environment variables loaded from encrypted file

# Install: sudo apt install gnupg

# Encrypt .env file
gpg --symmetric --cipher-algo AES256 .env
# Creates .env.gpg

# In startup script:
gpg --decrypt --quiet .env.gpg > /tmp/.env
source /tmp/.env
# Run application
rm /tmp/.env  # Clean up
```

---

## Monitoring & Health Checks

### Redundancy Health Monitor (Optional but Recommended)

```python
# ~/art/scripts/health_monitor.py
import os
import time
import logging

def check_nfs_mount():
    """Check if NFS is mounted and responsive"""
    return os.path.ismount('~/art/shared_output') and \
           os.access('~/art/shared_output', os.W_OK)

def check_ssh_connectivity(target_ip):
    """Check if SSH is reachable"""
    return os.system(f"ssh -q -o BatchMode=yes -o ConnectTimeout=5 artsync@{target_ip} exit") == 0

def check_network():
    """Check if network is up"""
    return os.system("ping -c 1 -W 2 192.168.1.1 > /dev/null 2>&1") == 0

def main():
    while True:
        status = {
            'network': check_network(),
            'nfs': check_nfs_mount(),
            'ssh_jetson_b': check_ssh_connectivity('192.168.1.11'),
        }
        
        # Log status
        logging.info(f"Health: {status}")
        
        # Trigger fallbacks if needed
        if not status['nfs'] and status['ssh_jetson_b']:
            logging.warning("NFS down, activating rsync fallback")
            os.system('~/art/scripts/sync_fallback.sh')
        
        time.sleep(30)

if __name__ == '__main__':
    main()
```

---

## Failure Scenario Matrix

| Failure | Detection | Response | Recovery Time |
|---------|-----------|----------|---------------|
| WiFi dropout (5s) | NFS timeout | Retry NFS mount | < 10s |
| WiFi dropout (30s) | Mount check fails | Activate rsync | < 60s |
| Jetson B crash | SSH/NFS unreachable | Local cache mode | Manual restart |
| Power loss (all) | N/A | Resume from last state | < 2min boot |
| NFS corruption | File I/O errors | Remount NFS, rsync sync | < 30s |
| Malicious actor | Firewall blocks unknown IP | Log + ignore | N/A |

---

## Security Threat Model

### Threats Mitigated:
✅ **Man-in-the-Middle**: Encrypted NFS + SSH prevents eavesdropping  
✅ **Unauthorized Access**: Kerberos/SSH keys + firewall rules  
✅ **Data Tampering**: NFSv4 integrity checking (sec=krb5p)  
✅ **Replay Attacks**: Kerberos ticket expiration  
✅ **Network Scanning**: Closed ports, non-standard services  

### Threats NOT Mitigated:
❌ **Physical Access**: If attacker has physical access to devices  
❌ **Compromised Router**: If WiFi router is hacked  
❌ **Supply Chain**: Pre-compromised hardware/software  

**Recommendation**: Physical security (locked cabinet) + trusted hardware suppliers

---

## Implementation Priority

**Week 1 (MVP - Moderate Security):**
1. ✅ Basic NFS with IP firewall rules
2. ✅ SSH key-based rsync fallback
3. ✅ Local cache implementation
4. ✅ Health monitoring script

**Future Enhancement (High Security):**
5. ⏳ Kerberos for NFSv4 (if needed)
6. ⏳ HTTPS web interface
7. ⏳ Encrypted .env storage
8. ⏳ Intrusion detection logs

---

## Configuration Files Summary

```
~/art/
├── scripts/
│   ├── setup_nfs_primary.sh       # Primary NFS setup
│   ├── setup_ssh_fallback.sh      # SSH rsync fallback
│   ├── health_monitor.py          # Continuous monitoring
│   └── sync_fallback.sh           # Manual sync trigger
├── .ssh/
│   ├── id_rsa_sync                # Dedicated sync key
│   └── id_rsa_sync.pub
├── .cache/                        # Local fallback storage
├── ssl/                           # HTTPS certificates
└── shared_output/                 # NFS mount point
    ├── image.png
    ├── heatmap.png
    ├── content.json
    └── docs/*.pdf
```

This architecture provides defense-in-depth with graceful degradation.