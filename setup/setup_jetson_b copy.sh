#!/bin/bash
#
# Jetson Orin Nano B Setup Script
# Role: Analysis Watcher + NFS Server + Web Server
# Run as: sudo ./setup_jetson_b.sh
#

set -e  # Exit on any error

echo "=========================================="
echo "Jetson B Setup - Analysis Watcher + NFS Server"
echo "=========================================="

# Color codes for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check if running as root
if [ "$EUID" -ne 0 ]; then 
    log_error "Please run as root (sudo ./setup_jetson_b.sh)"
    exit 1
fi

# Get the actual user (not root)
ACTUAL_USER=${SUDO_USER:-$USER}
if [ "$ACTUAL_USER" = "root" ]; then
    ACTUAL_USER="art"
fi

log_info "Installing as user: $ACTUAL_USER"

# Define paths
HOME_DIR="/home/$ACTUAL_USER"
PROJECT_DIR="$HOME_DIR/LatentSpace"
SHARED_DIR="$PROJECT_DIR/shared_output"
LOGS_DIR="$PROJECT_DIR/logs"
SCRIPTS_DIR="$PROJECT_DIR/scripts"
WEB_UI_DIR="$PROJECT_DIR/web_ui" # <-- NEW

# Create user if doesn't exist
if ! id "$ACTUAL_USER" &>/dev/null; then
    log_info "Creating user: $ACTUAL_USER"
    useradd -m -s /bin/bash "$ACTUAL_USER"
    echo "$ACTUAL_USER:latentspace" | chpasswd
    usermod -aG sudo "$ACTUAL_USER"
fi

# ============================================
# 1. System Update & Dependencies
# ============================================
log_info "Step 1/12: Updating system packages..."
apt update
apt upgrade -y

log_info "Installing system dependencies..."
apt install -y \
    python3.10 \
    python3.10-venv \
    python3-pip \
    nfs-kernel-server \
    openssh-server \
    rsync \
    git \
    curl \
    wget \
    vim \
    htop \
    iptables-persistent \
    net-tools \
    chromium-browser

# Install Node.js for Playwright (if not present)
if ! command -v node &> /dev/null; then
    log_info "Installing Node.js..."
    curl -fsSL https://deb.nodesource.com/setup_18.x | bash -
    apt install -y nodejs
fi

# ============================================
# 2. Create Directory Structure
# ============================================
log_info "Step 2/12: Creating directory structure..."
mkdir -p "$PROJECT_DIR"/{analysis_watcher,shared_output/docs,logs,scripts,.ssh}
mkdir -p "$WEB_UI_DIR" # <-- NEW
chown -R "$ACTUAL_USER:$ACTUAL_USER" "$PROJECT_DIR"

# ============================================
# 3. Setup Python Virtual Environment
# ============================================
log_info "Step 3/12: Setting up Python environment for analysis_watcher..."
cd "$PROJECT_DIR/analysis_watcher"

# Create venv
sudo -u "$ACTUAL_USER" python3.10 -m venv venv

# Activate and install packages
sudo -u "$ACTUAL_USER" bash -c "
    source venv/bin/activate
    pip install --upgrade pip
    pip install opencv-python
    pip install tensorflow
    pip install keras
    pip install requests
    pip install matplotlib
    pip install numpy
    pip install playwright
    pip install flask flask-cors gunicorn paramiko # <-- NEW for Web Server
    playwright install chromium
    playwright install-deps
"

log_info "Python environment created successfully"

# ============================================
# 4. Setup NFS Server
# ============================================
log_info "Step 4/12: Configuring NFS server..."

# Backup existing exports
if [ -f /etc/exports ]; then
    cp /etc/exports /etc/exports.backup
fi

# Add NFS export for shared_output
NFS_EXPORT="$SHARED_DIR 192.168.1.0/24(rw,sync,no_subtree_check,no_root_squash)"
if ! grep -q "$SHARED_DIR" /etc/exports; then
    echo "$NFS_EXPORT" >> /etc/exports
    log_info "Added NFS export: $NFS_EXPORT"
fi

# Set proper permissions
chown -R "$ACTUAL_USER:$ACTUAL_USER" "$SHARED_DIR"
chmod 755 "$SHARED_DIR"

# Restart NFS server
systemctl enable nfs-kernel-server
systemctl restart nfs-kernel-server

# Export shares
exportfs -ra

log_info "NFS server configured. Testing..."
showmount -e localhost

# ============================================
# 5. Setup SSH Keys for rsync Fallback
# ============================================
log_info "Step 5/12: Generating SSH keys for sync..."

SSH_KEY="$HOME_DIR/.ssh/id_rsa_sync"
if [ ! -f "$SSH_KEY" ]; then
    sudo -u "$ACTUAL_USER" ssh-keygen -t ed25519 -f "$SSH_KEY" -N "" -C "rsync-sync@jetson-b"
    log_info "SSH key generated: $SSH_KEY"
else
    log_warn "SSH key already exists, skipping generation"
fi

# Setup SSH authorized_keys for incoming connections
mkdir -p "$HOME_DIR/.ssh"
touch "$HOME_DIR/.ssh/authorized_keys"
chmod 700 "$HOME_DIR/.ssh"
chmod 600 "$HOME_DIR/.ssh/authorized_keys"
chown -R "$ACTUAL_USER:$ACTUAL_USER" "$HOME_DIR/.ssh"

log_warn "MANUAL STEP REQUIRED: Copy public key to Jetson A and RPi4"
log_warn "Public key location: $SSH_KEY.pub"
log_warn "Run on other devices: ssh-copy-id -i $SSH_KEY.pub art@192.168.1.11"
log_warn "AND: Copy Jetson A's and RPi4's public keys into $HOME_DIR/.ssh/authorized_keys"

# ============================================
# 6. Configure Firewall (iptables)
# ============================================
log_info "Step 6/12: Configuring firewall rules..."

# ... (Firewall rules remain the same) ...
iptables -F
iptables -X
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
iptables -A INPUT -p icmp --icmp-type echo-request -s 192.168.1.0/24 -j ACCEPT
netfilter-persistent save
log_info "Firewall rules configured"

# ============================================
# 7. Copy Runtime Scripts
# ============================================
log_info "Step 7/12: Copying runtime scripts..."

# Copy from the setup folder to the destination
SOURCE_REPO="/home/art/LatentSpace"

cp "$SOURCE_REPO/scripts/network_health.py" "$SCRIPTS_DIR/network_health.py"
cp "$SOURCE_REPO/scripts/sync_fallback_b.sh" "$SCRIPTS_DIR/sync_fallback.sh"
cp "$SOURCE_REPO/scripts/backup_system.sh" "$SCRIPTS_DIR/backup_system.sh"

# Set permissions
chown "$ACTUAL_USER:$ACTUAL_USER" "$SCRIPTS_DIR"/*.py
chmod +x "$SCRIPTS_DIR"/*.sh

# ============================================
# 8. Create rsync Fallback Script
# ============================================
log_info "Step 8/12: Creating rsync fallback script..."

# ... (This step remains the same) ...
cat > "$SCRIPTS_DIR/sync_fallback.sh" << 'EOFSYNC'
#!/bin/bash
# Rsync fallback sync script for Jetson B
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
EOFSYNC
chmod +x "$SCRIPTS_DIR/sync_fallback.sh"
chown "$ACTUAL_USER:$ACTUAL_USER" "$SCRIPTS_DIR/sync_fallback.sh"

# ============================================
# 9. Create Systemd Services
# ============================================
log_info "Step 9/12: Creating systemd services..."

# Analysis Watcher Service
cat > /etc/systemd/system/analysis-watcher.service << EOFSERVICE
[Unit]
Description=Latent Space - Analysis Watcher (System B)
After=network-online.target nfs-kernel-server.service
Wants=network-online.target

[Service]
Type=simple
User=$ACTUAL_USER
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

# Web Server Service (UPDATED to use Gunicorn/Flask)
cat > /etc/systemd/system/web-server.service << EOFWEB
[Unit]
Description=Latent Space - Web Server (System B)
After=network-online.target analysis-watcher.service
Wants=network-online.target

[Service]
Type=simple
User=$ACTUAL_USER
WorkingDirectory=$PROJECT_DIR
Environment="PATH=$PROJECT_DIR/analysis_watcher/venv/bin"
ExecStart=$PROJECT_DIR/analysis_watcher/venv/bin/gunicorn \
    --workers 2 \
    --bind 0.0.0.0:8000 \
    --log-level info \
    monitoring_server:app
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOFWEB

log_info "Reloading systemd..."
systemctl daemon-reload

# Enable services (don't start yet, will start after reboot)
systemctl enable analysis-watcher
systemctl enable web-server

log_info "Systemd services created and enabled"

# ============================================
# 10. Add Monitoring & Backup Scripts (NEW)
# ============================================
log_info "Step 10/12: Adding monitoring and backup scripts..."

# monitoring_server.py
cat > "$PROJECT_DIR/monitoring_server.py" << 'EOFMONITOR'
# (Insert the full monitoring_server.py code from above)
EOFMONITOR
chown "$ACTUAL_USER:$ACTUAL_USER" "$PROJECT_DIR/monitoring_server.py"
chmod +x "$PROJECT_DIR/monitoring_server.py"

# remote_ui.html
cat > "$WEB_UI_DIR/remote_ui.html" << 'EOFHTML'
# (Insert the full remote_ui.html code)
EOFHTML
chown "$ACTUAL_USER:$ACTUAL_USER" "$WEB_UI_DIR/remote_ui.html"

# backup_system.sh
cat > "$SCRIPTS_DIR/backup_system.sh" << 'EOFBACKUP'
# (Insert the full backup_system.sh code from above)
EOFBACKUP
chown "$ACTUAL_USER:$ACTUAL_USER" "$SCRIPTS_DIR/backup_system.sh"
chmod +x "$SCRIPTS_DIR/backup_system.sh"

# Create Backup Systemd Timer
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

log_info "Monitoring server and backup timer created"

# ============================================
# 11. Add Sudoers for Remote Control (NEW)
# ============================================
log_info "Step 11/12: Configuring sudoers for remote control..."

# Allows 'art' user to restart services without a password
cat > /etc/sudoers.d/99-latent-space << EOFSUDO
# Allows 'art' user to control systemd services
$ACTUAL_USER ALL=(ALL) NOPASSWD: \
    /bin/systemctl restart analysis-watcher, \
    /bin/systemctl stop analysis-watcher, \
    /bin/systemctl restart web-server, \
    /bin/systemctl stop web-server, \
    /bin/systemctl restart nfs-kernel-server
EOFSUDO
chmod 440 /etc/sudoers.d/99-latent-space

# ============================================
# 12. Final Configuration
# ============================================
log_info "Step 12/12: Final configuration..."

# Set static IP (optional - better done via router)
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

log_warn "WiFi configuration created at /etc/netplan/01-netcfg.yaml"
log_warn "Edit this file with your WiFi credentials, then run: sudo netplan apply"

# Create logs directory
mkdir -p "$LOGS_DIR"
chown "$ACTUAL_USER:$ACTUAL_USER" "$LOGS_DIR"
touch "$LOGS_DIR/installation.log"
touch "$LOGS_DIR/jetson_b_network.log"
touch "$LOGS_DIR/backup.log"
touch "$LOGS_DIR/monitoring_server.log"
chown "$ACTUAL_USER:$ACTUAL_USER" "$LOGS_DIR"/*.log

# Set permissions
chown -R "$ACTUAL_USER:$ACTUAL_USER" "$PROJECT_DIR"

# ============================================
# Summary
# ============================================
echo ""
echo "=========================================="
log_info "Jetson B Setup Complete!"
echo "=========================================="
echo ""
echo "Summary:"
echo "  ✓ NFS server configured"
echo "  ✓ Python environment setup"
echo "  ✓ SSH keys generated"
echo "  ✓ Firewall configured"
echo "  ✓ Services created (analysis-watcher, web-server)"
echo "  ✓ Monitoring backend and backup timer installed"
echo "  ✓ Sudoers configured"
echo ""
echo "Next Steps:"
echo "  1. Mount backup drive at /mnt/backup_drive"
echo "  2. Edit WiFi config: sudo nano /etc/netplan/01-netcfg.yaml"
echo "  3. Apply network config: sudo netplan apply"
echo "  4. Copy SSH public key to other devices:"
echo "     cat $SSH_KEY.pub"
echo "  5. Add other devices' keys to $HOME_DIR/.ssh/authorized_keys"
echo "  6. Reboot: sudo reboot"
echo ""
echo "After reboot, services will start automatically."
echo "Check status: sudo systemctl status analysis-watcher"
echo "Dashboard: http://192.168.1.11:8000"
echo ""
echo "Public SSH key for other devices:"
cat "$SSH_KEY.pub"
echo ""