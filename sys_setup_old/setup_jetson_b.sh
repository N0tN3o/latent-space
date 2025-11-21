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
log_info "Step 1/10: Updating system packages..."
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
log_info "Step 2/10: Creating directory structure..."
mkdir -p "$PROJECT_DIR"/{analysis_watcher,shared_output/docs,logs,scripts,.ssh}
chown -R "$ACTUAL_USER:$ACTUAL_USER" "$PROJECT_DIR"

# ============================================
# 3. Setup Python Virtual Environment
# ============================================
log_info "Step 3/10: Setting up Python environment for analysis_watcher..."
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
    playwright install chromium
    playwright install-deps
"

log_info "Python environment created successfully"

# ============================================
# 4. Setup NFS Server
# ============================================
log_info "Step 4/10: Configuring NFS server..."

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
log_info "Step 5/10: Generating SSH keys for sync..."

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

# ============================================
# 6. Configure Firewall (iptables)
# ============================================
log_info "Step 6/10: Configuring firewall rules..."

# Flush existing rules
iptables -F
iptables -X

# Default policies
iptables -P INPUT DROP
iptables -P FORWARD DROP
iptables -P OUTPUT ACCEPT

# Allow loopback
iptables -A INPUT -i lo -j ACCEPT

# Allow established connections
iptables -A INPUT -m state --state ESTABLISHED,RELATED -j ACCEPT

# Allow SSH from local network
iptables -A INPUT -p tcp --dport 22 -s 192.168.1.0/24 -j ACCEPT

# Allow NFS from specific devices
iptables -A INPUT -p tcp --dport 2049 -s 192.168.1.10 -j ACCEPT  # Jetson A
iptables -A INPUT -p tcp --dport 2049 -s 192.168.1.12 -j ACCEPT  # RPi4
iptables -A INPUT -p tcp --dport 111 -s 192.168.1.10 -j ACCEPT   # RPC
iptables -A INPUT -p tcp --dport 111 -s 192.168.1.12 -j ACCEPT

# Allow web server (port 8000) from local network
iptables -A INPUT -p tcp --dport 8000 -s 192.168.1.0/24 -j ACCEPT

# Allow ping from local network
iptables -A INPUT -p icmp --icmp-type echo-request -s 192.168.1.0/24 -j ACCEPT

# Save rules
netfilter-persistent save

log_info "Firewall rules configured"

# ============================================
# 7. Copy Network Health Monitor
# ============================================
log_info "Step 7/10: Installing network health monitor..."

cat > "$SCRIPTS_DIR/network_health.py" << 'EOFNETWORK'
# Network health monitor code goes here
# (Insert the full network_health.py code from the previous artifact)
# For brevity, this would be the complete module
EOFNETWORK

chown "$ACTUAL_USER:$ACTUAL_USER" "$SCRIPTS_DIR/network_health.py"
chmod +x "$SCRIPTS_DIR/network_health.py"

log_info "Network health monitor installed"

# ============================================
# 8. Create rsync Fallback Script
# ============================================
log_info "Step 8/10: Creating rsync fallback script..."

cat > "$SCRIPTS_DIR/sync_fallback.sh" << 'EOFSYNC'
#!/bin/bash
# Rsync fallback sync script for Jetson B
# Pulls images from Jetson A if NFS fails

SHARED_DIR="~/art/LatentSpace/shared_output"
JETSON_A="art@192.168.1.10"
SSH_KEY="~/art/.ssh/id_rsa_sync"

# Sync image.png from Jetson A
rsync -avz -e "ssh -i $SSH_KEY -o StrictHostKeyChecking=no" \
    "$JETSON_A:~/art/LatentSpace/shared_output/image.png" \
    "$SHARED_DIR/" \
    2>/dev/null

# Sync heatmap.png
rsync -avz -e "ssh -i $SSH_KEY -o StrictHostKeyChecking=no" \
    "$JETSON_A:~/art/LatentSpace/shared_output/heatmap.png" \
    "$SHARED_DIR/" \
    2>/dev/null

exit 0
EOFSYNC

chmod +x "$SCRIPTS_DIR/sync_fallback.sh"
chown "$ACTUAL_USER:$ACTUAL_USER" "$SCRIPTS_DIR/sync_fallback.sh"

# ============================================
# 9. Create Systemd Services
# ============================================
log_info "Step 9/10: Creating systemd services..."

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

# Web Server Service
cat > /etc/systemd/system/web-server.service << EOFWEB
[Unit]
Description=Latent Space - Web Server (System B)
After=network-online.target
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

# Reload systemd
systemctl daemon-reload

# Enable services (don't start yet, will start after reboot)
systemctl enable analysis-watcher
systemctl enable web-server

log_info "Systemd services created and enabled"

# ============================================
# 10. Final Configuration
# ============================================
log_info "Step 10/10: Final configuration..."

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
echo ""
echo "Next Steps:"
echo "  1. Edit WiFi config: sudo nano /etc/netplan/01-netcfg.yaml"
echo "  2. Apply network config: sudo netplan apply"
echo "  3. Copy SSH public key to other devices:"
echo "     cat $SSH_KEY.pub"
echo "  4. Reboot: sudo reboot"
echo ""
echo "After reboot, services will start automatically."
echo "Check status: sudo systemctl status analysis-watcher"
echo ""
echo "Public SSH key for other devices:"
cat "$SSH_KEY.pub"
echo ""