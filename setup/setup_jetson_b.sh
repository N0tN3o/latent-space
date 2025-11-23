#!/bin/bash
#
# Jetson Orin Nano B Setup (SERVER)
# Role: Analysis Watcher + NFS Server + Web Servers
# User: kasboek | IP: 192.168.1.11
#

set -e

# --- CONFIGURATION ---
TARGET_USER="kasboek"
TARGET_IP="192.168.1.11"
WIFI_IFACE="wlp1s0"  # Common for Jetson Orin. Change to wlp1p1 if needed.

# Paths
HOME_DIR="/home/$TARGET_USER"
PROJECT_DIR="$HOME_DIR/LatentSpace"
SHARED_DIR="$PROJECT_DIR/shared_output"
SCRIPTS_DIR="$PROJECT_DIR/scripts"

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }

if [ "$EUID" -ne 0 ]; then echo "Please run as root (sudo)"; exit 1; fi

echo "=========================================="
echo "Jetson B Setup (Server) - $TARGET_USER"
echo "=========================================="

# 1. CLEANUP PREVIOUS INSTALLATIONS
log_info "Step 1: Cleaning up previous installations..."
SYSTEMD_SERVICES=("analysis-watcher" "web-server" "monitoring-server" "nfs-kernel-server")
for SVC in "${SYSTEMD_SERVICES[@]}"; do
    if systemctl is-active --quiet "$SVC"; then
        systemctl stop "$SVC"
        systemctl disable "$SVC"
    fi
    rm -f "/etc/systemd/system/$SVC.service"
done
systemctl daemon-reload

# Remove Firewall Rules (Private Network Mode)
iptables -F
iptables -X
iptables -P INPUT ACCEPT
iptables -P FORWARD ACCEPT
iptables -P OUTPUT ACCEPT
netfilter-persistent save 2>/dev/null || true

# 2. USER CREATION
log_info "Step 2: Ensuring user '$TARGET_USER' exists..."
if ! id "$TARGET_USER" &>/dev/null; then
    useradd -m -s /bin/bash "$TARGET_USER"
    echo "$TARGET_USER:latentspace" | chpasswd
    usermod -aG sudo,video,render "$TARGET_USER"
    log_info "User created."
fi

# 3. DEPENDENCIES (Bare Metal Assumption)
log_info "Step 3: Installing system dependencies..."
apt update && apt upgrade -y
apt install -y \
    python3.10 python3.10-venv python3-pip python3-tk \
    nfs-kernel-server openssh-server rsync git curl wget vim htop \
    net-tools libopencv-dev build-essential \
    chromium-browser

# Node.js check/install
if ! command -v node &> /dev/null; then
    curl -fsSL https://deb.nodesource.com/setup_18.x | bash -
    apt install -y nodejs
fi

# 4. FILE STRUCTURE & COPY
log_info "Step 4: Setting up project files..."
mkdir -p "$PROJECT_DIR"
mkdir -p "$PROJECT_DIR"/{analysis_watcher,shared_output/docs,logs,scripts,.ssh,web_ui}

# Copy files from current location (Installer source) to Target
# We assume the script is run from inside the downloaded repo
SOURCE_DIR=$(pwd)
if [ "$SOURCE_DIR" != "$PROJECT_DIR" ]; then
    log_info "Copying files from $SOURCE_DIR to $PROJECT_DIR..."
    rsync -av --exclude '.git' --exclude 'setup' "$SOURCE_DIR/" "$PROJECT_DIR/"
fi

# Fix permissions
chown -R "$TARGET_USER:$TARGET_USER" "$PROJECT_DIR"

# 5. PYTHON ENVIRONMENT
log_info "Step 5: Setting up Python environment..."
cd "$PROJECT_DIR/analysis_watcher"
sudo -u "$TARGET_USER" python3.10 -m venv venv
sudo -u "$TARGET_USER" bash -c "
    source venv/bin/activate
    pip install --upgrade pip
    pip install opencv-python tensorflow keras requests matplotlib numpy elevenlabs
    pip install playwright flask flask-cors gunicorn paramiko
    playwright install chromium
    playwright install-deps
"

# 6. NFS SERVER SETUP
log_info "Step 6: Configuring NFS Server..."
if [ -f /etc/exports ]; then cp /etc/exports /etc/exports.backup; fi

# Clear old LatentSpace exports and add new one
sed -i '/LatentSpace/d' /etc/exports
echo "$SHARED_DIR 192.168.1.0/24(rw,sync,no_subtree_check,no_root_squash)" >> /etc/exports

systemctl enable nfs-kernel-server
systemctl restart nfs-kernel-server
exportfs -ra

# 7. SERVICES SETUP
log_info "Step 7: Creating Systemd Services..."

# Service: Analysis Watcher
cat > /etc/systemd/system/analysis-watcher.service << EOF
[Unit]
Description=Latent Space - Analysis Watcher
After=network.target nfs-kernel-server.service

[Service]
Type=simple
User=$TARGET_USER
WorkingDirectory=$PROJECT_DIR/analysis_watcher
ExecStart=$PROJECT_DIR/analysis_watcher/venv/bin/python3 run_analysis_watcher.py
Restart=always
RestartSec=10
Environment="LATENT_PROJECT_ROOT=$PROJECT_DIR"

[Install]
WantedBy=multi-user.target
EOF

# Service: Web Gallery
cat > /etc/systemd/system/web-server.service << EOF
[Unit]
Description=Latent Space - Gallery Web Server
After=network.target

[Service]
Type=simple
User=$TARGET_USER
WorkingDirectory=$PROJECT_DIR
ExecStart=/usr/bin/python3 -m http.server 8000
Restart=always

[Install]
WantedBy=multi-user.target
EOF

# Service: Monitoring
cat > /etc/systemd/system/monitoring-server.service << EOF
[Unit]
Description=Latent Space - Monitoring
After=network.target

[Service]
Type=simple
User=$TARGET_USER
WorkingDirectory=$PROJECT_DIR
Environment="PATH=$PROJECT_DIR/analysis_watcher/venv/bin"
ExecStart=$PROJECT_DIR/analysis_watcher/venv/bin/gunicorn --workers 2 --bind 0.0.0.0:8001 monitoring_server:app
Restart=always

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable analysis-watcher web-server monitoring-server

# 8. SUDOERS
cat > /etc/sudoers.d/99-latentspace << EOF
$TARGET_USER ALL=(ALL) NOPASSWD: /bin/systemctl restart analysis-watcher, /bin/systemctl restart web-server, /bin/systemctl restart nfs-kernel-server
EOF
chmod 440 /etc/sudoers.d/99-latentspace

# 9. VALIDATION
echo ""
log_info "Setup Phase Complete."
echo "Do you want to run the verification script now? (y/n)"
read -r response
if [[ "$response" =~ ^([yY][eE][sS]|[yY])+$ ]]; then
    chmod +x ./verify_installation.sh
    ./verify_installation.sh
fi

log_info "Next Steps:"
echo "1. Reboot this device."
echo "2. Ensure IP is static at $TARGET_IP"