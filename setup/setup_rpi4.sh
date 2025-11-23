#!/bin/bash
#
# Raspberry Pi 4 Setup (PRINT)
# Role: Print Kiosk
# User: kasboek | IP: 192.168.1.12
#

set -e

# --- CONFIGURATION ---
TARGET_USER="kasboek"
SERVER_IP="192.168.1.11" # Jetson B

# Paths
HOME_DIR="/home/$TARGET_USER"
PROJECT_DIR="$HOME_DIR/LatentSpace"
SHARED_DIR="$PROJECT_DIR/shared_output"
SCRIPTS_DIR="$PROJECT_DIR/scripts"

GREEN='\033[0;32m'
NC='\033[0m'

if [ "$EUID" -ne 0 ]; then echo "Run as root"; exit 1; fi

echo "=========================================="
echo "RPi4 Setup (Printer) - $TARGET_USER"
echo "=========================================="

# 1. CLEANUP
if systemctl is-active --quiet print-service; then
    systemctl stop print-service
    systemctl disable print-service
fi
rm -f /etc/systemd/system/print-service.service
systemctl daemon-reload

# Firewall removal
iptables -F
iptables -P INPUT ACCEPT
netfilter-persistent save 2>/dev/null || true

# 2. USER
if ! id "$TARGET_USER" &>/dev/null; then
    useradd -m -s /bin/bash "$TARGET_USER"
    echo "$TARGET_USER:latentspace" | chpasswd
    usermod -aG sudo,lpadmin,video "$TARGET_USER"
fi

# 3. DEPENDENCIES
echo "Installing dependencies..."
apt update && apt upgrade -y
apt install -y \
    python3 python3-venv python3-pip python3-pyqt5 \
    nfs-common openssh-server cups cups-client printer-driver-all \
    cups-pdf python3-pyqt5.qtmultimedia \
    git curl wget

# 4. FILE COPY
echo "Setting up files..."
mkdir -p "$PROJECT_DIR"
mkdir -p "$PROJECT_DIR"/{print_service,shared_output/docs,logs,scripts}

SOURCE_DIR=$(pwd)
if [ "$SOURCE_DIR" != "$PROJECT_DIR" ]; then
    rsync -av --exclude '.git' --exclude 'setup' "$SOURCE_DIR/" "$PROJECT_DIR/"
fi
chown -R "$TARGET_USER:$TARGET_USER" "$PROJECT_DIR"

# 5. PYTHON
cd "$PROJECT_DIR/print_service"
sudo -u "$TARGET_USER" python3 -m venv --system-site-packages venv
sudo -u "$TARGET_USER" bash -c "
    source venv/bin/activate
    pip install --upgrade pip
    pip install PyMuPDF watchdog Pillow pdf2image
"

# 6. NFS
mkdir -p "$SHARED_DIR"
chown "$TARGET_USER:$TARGET_USER" "$SHARED_DIR"
if ! grep -q "$SERVER_IP" /etc/fstab; then
    echo "$SERVER_IP:$SHARED_DIR $SHARED_DIR nfs defaults,_netdev,hard,intr 0 0" >> /etc/fstab
fi
mount -a || echo "NFS mount failed (Server likely offline)"

# 7. CUPS
systemctl enable cups && systemctl start cups
usermod -aG lpadmin "$TARGET_USER"
# Open CUPS to local network
sed -i 's/Listen localhost:631/Listen 631/' /etc/cups/cupsd.conf
systemctl restart cups

# 8. SERVICE
cat > /etc/systemd/system/print-service.service << EOF
[Unit]
Description=Latent Space - Print Service
After=graphical.target cups.service

[Service]
Type=simple
User=$TARGET_USER
Environment="DISPLAY=:0"
Environment="XAUTHORITY=/home/$TARGET_USER/.Xauthority"
Environment="LATENT_PROJECT_ROOT=$PROJECT_DIR"
WorkingDirectory=$PROJECT_DIR/print_service
ExecStartPre=/bin/sleep 10
ExecStart=$PROJECT_DIR/print_service/venv/bin/python3 print_kiosk_ui.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=graphical.target
EOF

systemctl daemon-reload
systemctl enable print-service

# 9. AUTO-LOGIN / DESKTOP SETTINGS
mkdir -p "/etc/systemd/system/getty@tty1.service.d/"
cat > "/etc/systemd/system/getty@tty1.service.d/autologin.conf" << EOF
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin $TARGET_USER --noclear %I \$TERM
EOF

mkdir -p "$HOME_DIR/.config/lxsession/LXDE-pi"
cat > "$HOME_DIR/.config/lxsession/LXDE-pi/autostart" << EOF
@xset s off
@xset -dpms
@xset s noblank
@/home/$TARGET_USER/LatentSpace/print_service/venv/bin/python3 /home/$TARGET_USER/LatentSpace/print_service/print_kiosk_ui.py
EOF
chown -R "$TARGET_USER:$TARGET_USER" "$HOME_DIR/.config"

# 10. SUDOERS
cat > /etc/sudoers.d/99-latentspace << EOF
$TARGET_USER ALL=(ALL) NOPASSWD: /bin/systemctl restart print-service, /bin/systemctl restart cups
EOF
chmod 440 /etc/sudoers.d/99-latentspace

echo "Do you want to run the verification script now? (y/n)"
read -r response
if [[ "$response" =~ ^([yY][eE][sS]|[yY])+$ ]]; then
    chmod +x ./verify_installation.sh
    ./verify_installation.sh
fi