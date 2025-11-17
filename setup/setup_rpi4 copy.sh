#!/bin/bash
#
# Raspberry Pi 4 Setup Script
# Role: Print Kiosk with Touch UI
# Run as: sudo ./setup_rpi4.sh
#

set -e

echo "=========================================="
echo "Raspberry Pi 4 Setup - Print Service"
echo "=========================================="

# ... (All logging functions and user setup remain the same) ...

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
if ! id "$ACTUAL_USER" &>/dev/null; then
    log_info "Creating user: $ACTUAL_USER"
    useradd -m -s /bin/bash "$ACTUAL_USER"
    echo "$ACTUAL_USER:latentspace" | chpasswd
    usermod -aG sudo,lpadmin "$ACTUAL_USER"
fi

# ============================================
# 1. System Update & Dependencies
# ============================================
log_info "Step 1/11: Updating system packages..."
apt update && apt upgrade -y
apt install -y \
    python3 python3-venv python3-pip python3-pyqt5 python3-pyqt5.qtmultimedia \
    nfs-common openssh-server rsync cups cups-client printer-driver-all \
    git curl wget vim htop iptables-persistent net-tools libopencv-dev

# ============================================
# 2. Create Directory Structure
# ============================================
log_info "Step 2/11: Creating directory structure..."
mkdir -p "$PROJECT_DIR"/{print_service,shared_output/docs,logs,scripts,.ssh}
chown -R "$ACTUAL_USER:$ACTUAL_USER" "$PROJECT_DIR"

# ============================================
# 3. Setup Python Virtual Environment
# ============================================
log_info "Step 3/11: Setting up Python environment..."
cd "$PROJECT_DIR/print_service"
sudo -u "$ACTUAL_USER" python3 -m venv venv
sudo -u "$ACTUAL_USER" bash -c "
    source venv/bin/activate
    pip install --upgrade pip
    pip install PyQt5 PyMuPDF watchdog Pillow
"

# ============================================
# 4. Setup NFS Client Mount
# ============================================
log_info "Step 4/11: Configuring NFS client mount..."
mkdir -p "$SHARED_DIR"
chown "$ACTUAL_USER:$ACTUAL_USER" "$SHARED_DIR"
cp /etc/fstab /etc/fstab.backup
NFS_MOUNT="192.168.1.11:$SHARED_DIR $SHARED_DIR nfs defaults,_netdev,hard,intr 0 0"
if ! grep -q "192.168.1.11" /etc/fstab; then
    echo "$NFS_MOUNT" >> /etc/fstab
fi
mount -a || log_warn "NFS mount failed"

# ============================================
# 5. Setup SSH Keys
# ============================================
log_info "Step 5/11: Generating SSH keys for sync..."
SSH_KEY="$HOME_DIR/.ssh/id_rsa_sync"
if [ ! -f "$SSH_KEY" ]; then
    sudo -u "$ACTUAL_USER" ssh-keygen -t ed25519 -f "$SSH_KEY" -N "" -C "rsync-sync@rpi4"
fi
mkdir -p "$HOME_DIR/.ssh"
touch "$HOME_DIR/.ssh/authorized_keys"
chmod 700 "$HOME_DIR/.ssh"
chmod 600 "$HOME_DIR/.ssh/authorized_keys"
chown -R "$ACTUAL_USER:$ACTUAL_USER" "$HOME_DIR/.ssh"
log_warn "MANUAL STEP: Add this public key to Jetson B's authorized_keys"
log_warn "AND: Add Jetson B's public key to $HOME_DIR/.ssh/authorized_keys"

# ============================================
# 6. Setup CUPS Printing
# ============================================
log_info "Step 6/11: Configuring CUPS printing service..."
systemctl enable cups && systemctl start cups
usermod -aG lpadmin "$ACTUAL_USER"
cp /etc/cups/cupsd.conf /etc/cups/cupsd.conf.backup
sed -i 's/Listen localhost:631/Listen 631/' /etc/cups/cupsd.conf
log_warn "MANUAL STEP: Connect printer and add it via http://192.168.1.12:631"

# ============================================
# 7. Create Print Kiosk UI
# ============================================
log_info "Step 7/11: Creating print kiosk UI..."
cat > "$PROJECT_DIR/print_service/print_kiosk_ui.py" << 'EOFUI'
# (Insert the full print_kiosk_ui.py code)
EOFUI
chmod +x "$PROJECT_DIR/print_service/print_kiosk_ui.py"
chown "$ACTUAL_USER:$ACTUAL_USER" "$PROJECT_DIR/print_service/print_kiosk_ui.py"

# ============================================
# 8. Configure Firewall
# ============================================
log_info "Step 8/11: Configuring firewall rules..."
iptables -F && iptables -X
iptables -P INPUT DROP
iptables -P FORWARD DROP
iptables -P OUTPUT ACCEPT
iptables -A INPUT -i lo -j ACCEPT
iptables -A INPUT -m state --state ESTABLISHED,RELATED -j ACCEPT
iptables -A INPUT -p tcp --dport 22 -s 192.168.1.0/24 -j ACCEPT
iptables -A INPUT -s 192.168.1.11 -j ACCEPT
iptables -A INPUT -p tcp --dport 631 -s 192.168.1.0/24 -j ACCEPT
iptables -A INPUT -p icmp --icmp-type echo-request -s 192.168.1.0/24 -j ACCEPT
netfilter-persistent save

# ============================================
# 9. Create Systemd Service
# ============================================
log_info "Step 9/11: Creating systemd service..."
cat > /etc/systemd/system/print-service.service << EOFSERVICE
[Unit]
Description=Latent Space - Print Service (RPi4)
After=network-online.target graphical.target cups.service
Wants=network-online.target

[Service]
Type=simple
User=$ACTUAL_USER
Environment="DISPLAY=:0"
Environment="XAUTHORITY=/home/$ACTUAL_USER/.Xauthority"
WorkingDirectory=$PROJECT_DIR/print_service
ExecStartPre=/bin/sleep 10
ExecStart=$PROJECT_DIR/print_service/venv/bin/python3 print_kiosk_ui.py
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=graphical.target
EOFSERVICE
systemctl daemon-reload
systemctl enable print-service

# ============================================
# 10. Configure Auto-login (for kiosk mode)
# ============================================
log_info "Step 10/11: Configuring auto-login..."
mkdir -p /etc/systemd/system/getty@tty1.service.d/
cat > /etc/systemd/system/getty@tty1.service.d/autologin.conf << EOFAUTOLOGIN
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin $ACTUAL_USER --noclear %I \$TERM
EOFAUTOLOGIN
mkdir -p "$HOME_DIR/.config/lxsession/LXDE-pi"
cat > "$HOME_DIR/.config/lxsession/LXDE-pi/autostart" << EOFAUTOSTART
@xset s off
@xset -dpms
@xset s noblank
@/home/$ACTUAL_USER/LatentSpace/print_service/venv/bin/python3 /home/$ACTUAL_USER/LatentSpace/print_service/print_kiosk_ui.py
EOFAUTOSTART
chown -R "$ACTUAL_USER:$ACTUAL_USER" "$HOME_DIR/.config"

# ============================================
# 11. Add Sudoers for Remote Control (NEW)
# ============================================
log_info "Step 11/11: Configuring sudoers for remote control..."

# Allows 'art' user to be controlled by Jetson B
cat > /etc/sudoers.d/99-latent-space << EOFSUDO
# Allows 'art' user to control systemd services
$ACTUAL_USER ALL=(ALL) NOPASSWD: \
    /bin/systemctl restart print-service, \
    /bin/systemctl stop print-service, \
    /bin/systemctl restart cups, \
    /bin/systemctl stop cups
EOFSUDO
chmod 440 /etc/sudoers.d/99-latent-space

# ============================================
# Final Configuration
# ============================================
log_info "Final configuration..."
mkdir -p "$LOGS_DIR"
chown "$ACTUAL_USER:$ACTUAL_USER" "$LOGS_DIR"
touch "$LOGS_DIR/rpi4_network.log"
chown "$ACTUAL_USER:$ACTUAL_USER" "$LOGS_DIR"/*.log
chown -R "$ACTUAL_USER:$ACTUAL_USER" "$PROJECT_DIR"

# ============================================
# Summary
# ============================================
echo ""
log_info "Raspberry Pi 4 Setup Complete!"
echo "Summary:"
echo "  ✓ NFS client configured"
echo "  ✓ Python environment setup"
echo "  ✓ Print kiosk UI created"
echo "  ✓ CUPS printing configured"
echo "  ✓ Auto-login enabled"
echo "  ✓ Service created (print-service)"
echo "  ✓ Sudoers configured for remote control"
echo ""
echo "Next Steps:"
echo "  1. Connect USB printer"
echo "  2. Add printer via CUPS: http://192.168.1.12:631"
echo "  3. Copy SSH public key to Jetson B:"
echo "     ssh-copy-id -i $SSH_KEY art@192.168.1.11"
echo "  4. Reboot: sudo reboot"
echo ""
echo "Public SSH key for Jetson B:"
cat "$SSH_KEY.pub"
echo ""