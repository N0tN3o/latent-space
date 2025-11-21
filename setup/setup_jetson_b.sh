#!/bin/bash
#
# Jetson Orin Nano B Setup Script (CORRECTED)
# Role: Analysis Watcher + NFS Server + Web Servers
# Run as: sudo ./setup_jetson_b.sh
#

set -e

echo "=========================================="
echo "Jetson B Setup - Analysis + NFS + Web"
echo "=========================================="

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

if [ "$EUID" -ne 0 ]; then log_error "Run as root" && exit 1; fi

ACTUAL_USER=${SUDO_USER:-$USER}
if [ "$ACTUAL_USER" = "root" ]; then ACTUAL_USER="art"; fi

log_info "Installing as user: $ACTUAL_USER"

HOME_DIR="/home/$ACTUAL_USER"
PROJECT_DIR="$HOME_DIR/LatentSpace"
SHARED_DIR="$PROJECT_DIR/shared_output"
LOGS_DIR="$PROJECT_DIR/logs"
SCRIPTS_DIR="$PROJECT_DIR/scripts"
WEB_UI_DIR="$PROJECT_DIR/web_ui"

if ! id "$ACTUAL_USER" &>/dev/null; then
    log_info "Creating user: $ACTUAL_USER"
    useradd -m -s /bin/bash "$ACTUAL_USER"
    echo "$ACTUAL_USER:latentspace" | chpasswd
    usermod -aG sudo "$ACTUAL_USER"
fi

# ============================================
# 1. System Update & Dependencies
# ============================================
log_info "Step 1/13: Updating system packages..."
apt update && apt upgrade -y

log_info "Installing system dependencies..."
apt install -y \
    python3.10 python3.10-venv python3-pip python3-tk nfs-kernel-server \
    openssh-server rsync git curl wget vim htop \
    iptables-persistent net-tools chromium-browser

if ! command -v node &> /dev/null; then
    log_info "Installing Node.js..."
    curl -fsSL https://deb.nodesource.com/setup_18.x | bash -
    apt install -y nodejs
fi

# ============================================
# 2. Create Directory Structure
# ============================================
log_info "Step 2/13: Creating directory structure..."
mkdir -p "$PROJECT_DIR"/{analysis_watcher,shared_output/docs,logs,scripts,.ssh}
mkdir -p "$WEB_UI_DIR"
chown -R "$ACTUAL_USER:$ACTUAL_USER" "$PROJECT_DIR"

# ============================================
# 3. Setup Python Virtual Environment
# ============================================
log_info "Step 3/13: Setting up Python environment..."
cd "$PROJECT_DIR/analysis_watcher"

sudo -u "$ACTUAL_USER" python3.10 -m venv venv

sudo -u "$ACTUAL_USER" bash -c "
    source venv/bin/activate
    pip install --upgrade pip
    pip install opencv-python tensorflow keras requests matplotlib numpy
    pip install playwright flask flask-cors gunicorn paramiko
    playwright install chromium
    playwright install-deps
"

log_info "Python environment created successfully"

# ============================================
# 4. Setup NFS Server
# ============================================
log_info "Step 4/13: Configuring NFS server..."

if [ -f /etc/exports ]; then
    cp /etc/exports /etc/exports.backup
fi

NFS_EXPORT="$SHARED_DIR 192.168.1.0/24(rw,sync,no_subtree_check,no_root_squash)"
if ! grep -q "$SHARED_DIR" /etc/exports; then
    echo "$NFS_EXPORT" >> /etc/exports
fi

chown -R "$ACTUAL_USER:$ACTUAL_USER" "$SHARED_DIR"
chmod 755 "$SHARED_DIR"

systemctl enable nfs-kernel-server
systemctl restart nfs-kernel-server
exportfs -ra

log_info "NFS server configured. Testing..."
showmount -e localhost

# ============================================
# 5. Setup SSH Keys
# ============================================
log_info "Step 5/13: Generating SSH keys..."

SSH_KEY="$HOME_DIR/.ssh/id_rsa_sync"
if [ ! -f "$SSH_KEY" ]; then
    sudo -u "$ACTUAL_USER" ssh-keygen -t ed25519 -f "$SSH_KEY" -N "" -C "rsync-sync@jetson-b"
fi

mkdir -p "$HOME_DIR/.ssh"
touch "$HOME_DIR/.ssh/authorized_keys"
chmod 700 "$HOME_DIR/.ssh"
chmod 600 "$HOME_DIR/.ssh/authorized_keys"
chown -R "$ACTUAL_USER:$ACTUAL_USER" "$HOME_DIR/.ssh"

# ============================================
# 6. Configure Firewall (CORRECTED - added port 8001)
# ============================================
log_info "Step 6/13: Configuring firewall rules..."

iptables -F && iptables -X
iptables -P INPUT DROP
iptables -P FORWARD DROP
iptables -P OUTPUT ACCEPT
iptables -A INPUT -i lo -j ACCEPT
iptables -A INPUT -m state --state ESTABLISHED,RELATED -j ACCEPT
iptables -A INPUT -p tcp --dport 22 -s 192.168.1.0/24 -j ACCEPT
iptables -A INPUT -p tcp --dport 2049 -s 192.168.1.10 -j ACCEPT
iptables -A INPUT -p tcp --dport 2049 -s 192.168.1.12 -j ACCEPT
iptables -A INPUT -p tcp --dport 111 -s 192.168.1.10 -j ACCEPT
iptables -A INPUT -p tcp --dport 111 -s 192.168.1.12 -j ACCEPT
iptables -A INPUT -p tcp --dport 8000 -s 192.168.1.0/24 -j ACCEPT
iptables -A INPUT -p tcp --dport 8001 -s 192.168.1.0/24 -j ACCEPT  # ADDED: Monitoring port
iptables -A INPUT -p icmp --icmp-type echo-request -s 192.168.1.0/24 -j ACCEPT
netfilter-persistent save

log_info "Firewall rules configured"

# ============================================
# 7. Copy Runtime Scripts
# ============================================
log_info "Step 7/13: Installing runtime scripts..."

# Network health module already exists in scripts/
# Just ensure it's executable
if [ -f "$PROJECT_DIR/scripts/network_health.py" ]; then
    chown "$ACTUAL_USER:$ACTUAL_USER" "$PROJECT_DIR/scripts/network_health.py"
    chmod +x "$PROJECT_DIR/scripts/network_health.py"
fi

# ============================================
# 8. Create Unified Sync Fallback Scripts
# ============================================
log_info "Step 8/13: Creating sync fallback scripts..."

# Generic dispatcher (calls role-specific script)
cat > "$SCRIPTS_DIR/sync_fallback.sh" << 'EOFSYNC'
#!/bin/bash
# Generic sync fallback - detects role and dispatches
HOSTNAME=$(hostname)
SCRIPTS_DIR="/home/art/LatentSpace/scripts"

if [[ "$HOSTNAME" == *"jetson-a"* ]] || [ -d "/home/art/LatentSpace/gaze-painter" ]; then
    bash "$SCRIPTS_DIR/sync_fallback_a.sh"
elif [[ "$HOSTNAME" == *"jetson-b"* ]] || [ -d "/home/art/LatentSpace/analysis_watcher" ]; then
    bash "$SCRIPTS_DIR/sync_fallback_b.sh"
elif [[ "$HOSTNAME" == *"rpi"* ]] || [ -d "/home/art/LatentSpace/print_service" ]; then
    bash "$SCRIPTS_DIR/sync_fallback_rpi.sh"
else
    echo "Unknown role, cannot determine sync target"
    exit 1
fi
EOFSYNC

# Jetson B specific (already exists, ensure it's there)
cat > "$SCRIPTS_DIR/sync_fallback_b.sh" << 'EOFSYNCA'
#!/bin/bash
# Rsync fallback for Jetson B - pulls from Jetson A
SHARED_DIR="/home/art/LatentSpace/shared_output"
JETSON_A="art@192.168.1.10"
SSH_KEY="/home/art/.ssh/id_rsa_sync"

rsync -avz -e "ssh -i $SSH_KEY -o StrictHostKeyChecking=no" \
    "$JETSON_A:/home/art/LatentSpace/shared_output/image.png" \
    "$SHARED_DIR/" \
    2>/dev/null

rsync -avz -e "ssh -i $SSH_KEY -o StrictHostKeyChecking=no" \
    "$JETSON_A:/home/art/LatentSpace/shared_output/heatmap.png" \
    "$SHARED_DIR/" \
    2>/dev/null

exit 0
EOFSYNCA

chmod +x "$SCRIPTS_DIR"/sync_fallback*.sh
chown "$ACTUAL_USER:$ACTUAL_USER" "$SCRIPTS_DIR"/sync_fallback*.sh

# ============================================
# 9. Create Systemd Services (CORRECTED)
# ============================================
log_info "Step 9/13: Creating systemd services..."

# Analysis Watcher Service
cat > /etc/systemd/system/analysis-watcher.service << EOFSERVICE
[Unit]
Description=Latent Space - Analysis Watcher (System B)
After=network-online.target nfs-kernel-server.service
Wants=network-online.target

[Service]
Type=simple
User=$ACTUAL_USER
Environment="LATENT_PROJECT_ROOT=$PROJECT_DIR"
WorkingDirectory=$PROJECT_DIR/analysis_watcher
Environment="PATH=$PROJECT_DIR/analysis_watcher/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
ExecStart=$PROJECT_DIR/analysis_watcher/venv/bin/python3 run_analysis_watcher.py
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOFSERVICE

# Gallery Web Server (port 8000)
cat > /etc/systemd/system/web-server.service << EOFWEB
[Unit]
Description=Latent Space - Gallery Web Server
After=network-online.target analysis-watcher.service
Wants=network-online.target

[Service]
Type=simple
User=$ACTUAL_USER
WorkingDirectory=$PROJECT_DIR
ExecStart=/usr/bin/python3 -m http.server 8000
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOFWEB

# Monitoring Dashboard Server (port 8001) - CORRECTED PORT
cat > /etc/systemd/system/monitoring-server.service << EOFMON
[Unit]
Description=Latent Space - Monitoring Dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$ACTUAL_USER
WorkingDirectory=$PROJECT_DIR
Environment="PATH=$PROJECT_DIR/analysis_watcher/venv/bin"
ExecStart=$PROJECT_DIR/analysis_watcher/venv/bin/gunicorn \
    --workers 2 \
    --bind 0.0.0.0:8001 \
    --log-level info \
    monitoring_server:app
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOFMON

systemctl daemon-reload
systemctl enable analysis-watcher
systemctl enable web-server
systemctl enable monitoring-server

log_info "Systemd services created and enabled"

# ============================================
# 10. Copy Monitoring Server (CORRECTED - update port)
# ============================================
log_info "Step 10/13: Installing monitoring server..."

# Copy monitoring_server.py if it exists, otherwise create placeholder
if [ -f "$PROJECT_DIR/monitoring_server.py" ]; then
    # Update port in existing file
    sed -i 's/app.run(host=.*port=8000/app.run(host='"'"'0.0.0.0'"'"', port=8001/' "$PROJECT_DIR/monitoring_server.py"
    chown "$ACTUAL_USER:$ACTUAL_USER" "$PROJECT_DIR/monitoring_server.py"
else
    log_warn "monitoring_server.py not found, skipping"
fi

# Copy remote UI
if [ -f "$WEB_UI_DIR/remote_ui.html" ]; then
    chown "$ACTUAL_USER:$ACTUAL_USER" "$WEB_UI_DIR/remote_ui.html"
fi

# ============================================
# 11. Setup Backup System
# ============================================
log_info "Step 11/13: Setting up backup system..."

if [ -f "$SCRIPTS_DIR/backup_system.sh" ]; then
    chmod +x "$SCRIPTS_DIR/backup_system.sh"
    chown "$ACTUAL_USER:$ACTUAL_USER" "$SCRIPTS_DIR/backup_system.sh"
fi

# Create Backup Timer
cat > /etc/systemd/system/backup.service << EOFBACKUPSVC
[Unit]
Description=Latent Space - Daily Backup Service
Wants=network-online.target

[Service]
Type=oneshot
User=$ACTUAL_USER
ExecStart=$SCRIPTS_DIR/backup_system.sh
StandardOutput=journal
StandardError=journal
EOFBACKUPSVC

cat > /etc/systemd/system/backup.timer << EOFBACKUPTIMER
[Unit]
Description=Run Latent Space backup daily

[Timer]
OnCalendar=daily
Persistent=true
Unit=backup.service

[Install]
WantedBy=timers.target
EOFBACKUPTIMER

systemctl daemon-reload
systemctl enable backup.timer
systemctl start backup.timer

# ============================================
# 12. Add Sudoers for Remote Control
# ============================================
log_info "Step 12/13: Configuring sudoers..."

cat > /etc/sudoers.d/99-latent-space << EOFSUDO
$ACTUAL_USER ALL=(ALL) NOPASSWD: \
    /bin/systemctl restart analysis-watcher, \
    /bin/systemctl stop analysis-watcher, \
    /bin/systemctl restart web-server, \
    /bin/systemctl stop web-server, \
    /bin/systemctl restart monitoring-server, \
    /bin/systemctl stop monitoring-server, \
    /bin/systemctl restart nfs-kernel-server
EOFSUDO
chmod 440 /etc/sudoers.d/99-latent-space

# ============================================
# 13. Final Configuration
# ============================================
log_info "Step 13/13: Final configuration..."

cat > /etc/netplan/01-netcfg.yaml << EOFNETPLAN
network:
  version: 2
  renderer: NetworkManager
  ethernets:
    eth0:
      dhcp4: yes
  wifis:
    wlan0:
      dhcp4: yes
      access-points:
        "YourWiFiSSID":
          password: "YourWiFiPassword"
EOFNETPLAN

mkdir -p "$LOGS_DIR"
chown "$ACTUAL_USER:$ACTUAL_USER" "$LOGS_DIR"
touch "$LOGS_DIR"/{installation.log,jetson_b_network.log,backup.log}
chown "$ACTUAL_USER:$ACTUAL_USER" "$LOGS_DIR"/*.log
chown -R "$ACTUAL_USER:$ACTUAL_USER" "$PROJECT_DIR"

# ============================================
# Summary
# ============================================
echo ""
log_info "Jetson B Setup Complete!"
echo "Summary:"
echo "  ✓ NFS server configured"
echo "  ✓ Python environment setup"
echo "  ✓ SSH keys generated"
echo "  ✓ Firewall configured (ports 8000, 8001)"
echo "  ✓ Services created (analysis-watcher, web-server, monitoring-server)"
echo "  ✓ Backup timer installed"
echo "  ✓ Sudoers configured"
echo ""
echo "Next Steps:"
echo "  1. Edit WiFi config: sudo nano /etc/netplan/01-netcfg.yaml"
echo "  2. Apply network: sudo netplan apply"
echo "  3. Copy SSH keys to/from other devices"
echo "  4. Reboot: sudo reboot"
echo ""
echo "After reboot:"
echo "  Gallery: http://192.168.1.11:8000"
echo "  Monitoring: http://192.168.1.11:8001"
echo ""
echo "Public SSH key:"
cat "$SSH_KEY.pub"
echo ""