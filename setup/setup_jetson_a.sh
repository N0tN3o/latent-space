#!/bin/bash
#
# Jetson Orin Nano A Setup Script (CORRECTED)
# Role: Gaze Tracking + Drawing (Dual Display)
# Run as: sudo ./setup_jetson_a.sh
#

set -e

echo "=========================================="
echo "Jetson A Setup - Gaze Painter (System A)"
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

# Create user if doesn't exist
if ! id "$ACTUAL_USER" &>/dev/null; then
    log_info "Creating user: $ACTUAL_USER"
    useradd -m -s /bin/bash "$ACTUAL_USER"
    echo "$ACTUAL_USER:latentspace" | chpasswd
    usermod -aG sudo,video "$ACTUAL_USER"
fi

# ============================================
# 1. System Update & Dependencies
# ============================================
log_info "Step 1/10: Updating system packages..."
apt update && apt upgrade -y
apt install -y \
    python3.10 python3.10-venv python3-pip nfs-common openssh-server \
    rsync git curl wget vim htop iptables-persistent net-tools \
    v4l-utils libopencv-dev python3-opencv

# ============================================
# 2. Create Directory Structure
# ============================================
log_info "Step 2/10: Creating directory structure..."
mkdir -p "$PROJECT_DIR"/{gaze-painter,shared_output,logs,scripts,.ssh}
chown -R "$ACTUAL_USER:$ACTUAL_USER" "$PROJECT_DIR"

# ============================================
# 3. Setup Python Virtual Environment
# ============================================
log_info "Step 3/10: Setting up Python environment..."
cd "$PROJECT_DIR/gaze-painter"
sudo -u "$ACTUAL_USER" python3.10 -m venv venv
sudo -u "$ACTUAL_USER" bash -c "
    source venv/bin/activate
    pip install --upgrade pip
    pip install opencv-python mediapipe numpy
"

# ============================================
# 4. Setup NFS Client Mount
# ============================================
log_info "Step 4/10: Configuring NFS client mount..."
mkdir -p "$SHARED_DIR"
chown "$ACTUAL_USER:$ACTUAL_USER" "$SHARED_DIR"
cp /etc/fstab /etc/fstab.backup
NFS_MOUNT="192.168.1.11:$SHARED_DIR $SHARED_DIR nfs defaults,_netdev,hard,intr 0 0"
if ! grep -q "192.168.1.11" /etc/fstab; then
    echo "$NFS_MOUNT" >> /etc/fstab
fi
mount -a || log_warn "NFS mount failed - Jetson B may not be ready yet"

# ============================================
# 5. Setup SSH Keys for rsync Fallback
# ============================================
log_info "Step 5/10: Generating SSH keys for sync..."
SSH_KEY="$HOME_DIR/.ssh/id_rsa_sync"
if [ ! -f "$SSH_KEY" ]; then
    sudo -u "$ACTUAL_USER" ssh-keygen -t ed25519 -f "$SSH_KEY" -N "" -C "rsync-sync@jetson-a"
fi
mkdir -p "$HOME_DIR/.ssh"
touch "$HOME_DIR/.ssh/authorized_keys"
chmod 700 "$HOME_DIR/.ssh"
chmod 600 "$HOME_DIR/.ssh/authorized_keys"
chown -R "$ACTUAL_USER:$ACTUAL_USER" "$HOME_DIR/.ssh"

# ============================================
# 6. Configure Camera Permissions
# ============================================
log_info "Step 6/10: Configuring camera permissions..."
usermod -aG video "$ACTUAL_USER"

# Create udev rule for persistent permissions (CORRECTED - removed temporary chmod)
cat > /etc/udev/rules.d/99-camera.rules << 'EOFUDEV'
KERNEL=="video[0-9]*", MODE="0666", GROUP="video"
EOFUDEV
udevadm control --reload-rules

# ============================================
# 7. Configure Dual Display Setup
# ============================================
log_info "Step 7/10: Configuring dual display setup..."
cat > "$SCRIPTS_DIR/display_manager.sh" << 'EOFDISPLAY'
#!/bin/bash
export DISPLAY=:0
sleep 5

# Get connected displays (CORRECTED - more reliable detection)
PRIMARY=$(xrandr | grep " connected" | head -1 | awk '{print $1}')
SECONDARY=$(xrandr | grep " connected" | tail -1 | awk '{print $1}')

echo "Primary Display: $PRIMARY"
echo "Secondary Display: $SECONDARY"

if [ ! -z "$PRIMARY" ] && [ ! -z "$SECONDARY" ]; then
    xrandr --output $PRIMARY --auto --primary
    xrandr --output $SECONDARY --auto --right-of $PRIMARY
    echo "Dual display configured"
else
    echo "Warning: Only one display detected"
fi
EOFDISPLAY
chmod +x "$SCRIPTS_DIR/display_manager.sh"
chown "$ACTUAL_USER:$ACTUAL_USER" "$SCRIPTS_DIR/display_manager.sh"

# ============================================
# 8. Configure Firewall
# ============================================
log_info "Step 8/10: Configuring firewall rules..."
iptables -F && iptables -X
iptables -P INPUT DROP
iptables -P FORWARD DROP
iptables -P OUTPUT ACCEPT
iptables -A INPUT -i lo -j ACCEPT
iptables -A INPUT -m state --state ESTABLISHED,RELATED -j ACCEPT
iptables -A INPUT -p tcp --dport 22 -s 192.168.1.0/24 -j ACCEPT
iptables -A INPUT -s 192.168.1.11 -j ACCEPT
iptables -A INPUT -p icmp --icmp-type echo-request -s 192.168.1.0/24 -j ACCEPT
netfilter-persistent save

# ============================================
# 9. Create Systemd Service (CORRECTED - added env var)
# ============================================
log_info "Step 9/10: Creating systemd service..."
cat > /etc/systemd/system/gaze-painter.service << EOFSERVICE
[Unit]
Description=Latent Space - Gaze Painter (System A)
After=network-online.target graphical.target
Wants=network-online.target

[Service]
Type=simple
User=$ACTUAL_USER
Environment="DISPLAY=:0"
Environment="XAUTHORITY=/home/$ACTUAL_USER/.Xauthority"
Environment="LATENT_PROJECT_ROOT=$PROJECT_DIR"
WorkingDirectory=$PROJECT_DIR/gaze-painter
ExecStartPre=/bin/sleep 10
ExecStartPre=$SCRIPTS_DIR/display_manager.sh
ExecStart=$PROJECT_DIR/gaze-painter/venv/bin/python3 run_gaze_painter.py
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=graphical.target
EOFSERVICE
systemctl daemon-reload
systemctl enable gaze-painter

# ============================================
# 10. Add Sudoers for Remote Control
# ============================================
log_info "Step 10/10: Configuring sudoers for remote control..."
cat > /etc/sudoers.d/99-latent-space << EOFSUDO
$ACTUAL_USER ALL=(ALL) NOPASSWD: \
    /bin/systemctl restart gaze-painter, \
    /bin/systemctl stop gaze-painter
EOFSUDO
chmod 440 /etc/sudoers.d/99-latent-space

# ============================================
# Final Configuration
# ============================================
log_info "Final configuration..."
mkdir -p "$LOGS_DIR"
chown "$ACTUAL_USER:$ACTUAL_USER" "$LOGS_DIR"
touch "$LOGS_DIR/jetson_a_network.log"
chown "$ACTUAL_USER:$ACTUAL_USER" "$LOGS_DIR"/*.log
cat > /etc/X11/Xwrapper.config << EOFX11
allowed_users=anybody
needs_root_rights=yes
EOFX11
chown -R "$ACTUAL_USER:$ACTUAL_USER" "$PROJECT_DIR"

# ============================================
# Summary
# ============================================
echo ""
log_info "Jetson A Setup Complete!"
echo "Summary:"
echo "  ✓ NFS client configured"
echo "  ✓ Python environment setup"
echo "  ✓ SSH keys generated"
echo "  ✓ Camera permissions set (via udev)"
echo "  ✓ Dual display manager created"
echo "  ✓ Firewall configured"
echo "  ✓ Service created (gaze-painter)"
echo "  ✓ Sudoers configured for remote control"
echo ""
echo "Next Steps:"
echo "  1. Connect camera and two displays"
echo "  2. Copy SSH public key to Jetson B:"
echo "     ssh-copy-id -i $SSH_KEY art@192.168.1.11"
echo "  3. Reboot: sudo reboot"
echo ""
echo "Public SSH key for Jetson B:"
cat "$SSH_KEY.pub"
echo ""