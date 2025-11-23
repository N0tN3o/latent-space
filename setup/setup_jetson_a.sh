#!/bin/bash
#
# Jetson Orin Nano A Setup (CLIENT)
# Role: Gaze Tracking + Drawing (Splitter Display)
# User: kasboek | IP: 192.168.1.10
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

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }

if [ "$EUID" -ne 0 ]; then echo "Please run as root (sudo)"; exit 1; fi

echo "=========================================="
echo "Jetson A Setup (Client) - $TARGET_USER"
echo "=========================================="

# 1. CLEANUP
log_info "Step 1: Cleaning up..."
if systemctl is-active --quiet gaze-painter; then
    systemctl stop gaze-painter
    systemctl disable gaze-painter
fi
rm -f /etc/systemd/system/gaze-painter.service
systemctl daemon-reload

# Remove Firewall
iptables -F
iptables -X
iptables -P INPUT ACCEPT
iptables -P FORWARD ACCEPT
iptables -P OUTPUT ACCEPT
netfilter-persistent save 2>/dev/null || true

# 2. USER
if ! id "$TARGET_USER" &>/dev/null; then
    useradd -m -s /bin/bash "$TARGET_USER"
    echo "$TARGET_USER:latentspace" | chpasswd
    usermod -aG sudo,video,render "$TARGET_USER"
fi

# 3. DEPENDENCIES (Added wmctrl)
log_info "Step 3: Installing dependencies..."
apt update && apt upgrade -y
apt install -y \
    python3.10 python3.10-venv python3-pip \
    nfs-common openssh-server rsync git curl wget \
    v4l-utils libopencv-dev python3-opencv \
    libqt5multimedia5-plugins python3-pyqt5.qtmultimedia \
    wmctrl

# 4. FILE STRUCTURE & COPY
log_info "Step 4: Setting up project files..."
mkdir -p "$PROJECT_DIR"
mkdir -p "$PROJECT_DIR"/{gaze-painter,shared_output,logs,scripts,.ssh}

# Copy files from current location
SOURCE_DIR=$(pwd)
if [ "$SOURCE_DIR" != "$PROJECT_DIR" ]; then
    log_info "Copying files from $SOURCE_DIR to $PROJECT_DIR..."
    rsync -av --exclude '.git' --exclude 'setup' "$SOURCE_DIR/" "$PROJECT_DIR/"
fi
chown -R "$TARGET_USER:$TARGET_USER" "$PROJECT_DIR"

# 5. PYTHON ENV
log_info "Step 5: Python environment..."
cd "$PROJECT_DIR/gaze-painter"
sudo -u "$TARGET_USER" python3.10 -m venv venv
sudo -u "$TARGET_USER" bash -c "
    source venv/bin/activate
    pip install --upgrade pip
    pip install opencv-python mediapipe numpy PyQt5
"

# 6. NFS MOUNT
log_info "Step 6: NFS Client..."
mkdir -p "$SHARED_DIR"
chown "$TARGET_USER:$TARGET_USER" "$SHARED_DIR"

# Fstab entry
MOUNT_ENTRY="$SERVER_IP:$SHARED_DIR $SHARED_DIR nfs defaults,_netdev,hard,intr 0 0"
if ! grep -q "$SERVER_IP" /etc/fstab; then
    echo "$MOUNT_ENTRY" >> /etc/fstab
fi
# Attempt mount
mount -a || echo "Note: NFS mount failed (Server B might be offline). Will retry on boot."

# 7. CAMERA PERMS
cat > /etc/udev/rules.d/99-camera.rules << EOF
KERNEL=="video[0-9]*", MODE="0666", GROUP="video"
EOF
udevadm control --reload-rules

# 8. DISPLAY MANAGER WRAPPER (The Fix)
log_info "Step 8: Creating Display Manager Wrapper..."
cat > "$SCRIPTS_DIR/display_manager.sh" << 'EOFDISPLAY'
#!/bin/bash
# Wrapper Script: Configures Screens -> Starts App -> Moves Windows
export DISPLAY=:0
export XAUTHORITY=/home/kasboek/.Xauthority

# --- A. CONFIGURE SCREENS (xrandr) ---
echo "Configuring screens..."
# Detect displays and resolutions
DISPLAYS=$(xrandr | grep " connected" | sed -e "s/\([A-Z0-9-]*\) connected.* \([0-9]*\)x\([0-9]*\)+.*/\1 \2 \3/")

# Sort by width (Largest is Canvas/Big, Smallest is Eyes/Small)
BIG_SCREEN=$(echo "$DISPLAYS" | sort -k2 -nr | head -n1)
SMALL_SCREEN=$(echo "$DISPLAYS" | sort -k2 -nr | tail -n1)

BIG_NAME=$(echo $BIG_SCREEN | awk '{print $1}')
BIG_WIDTH=$(echo $BIG_SCREEN | awk '{print $2}')
SMALL_NAME=$(echo $SMALL_SCREEN | awk '{print $1}')

echo "Canvas (Big): $BIG_NAME ($BIG_WIDTH px)"
echo "Eyes (Small): $SMALL_NAME"

# Apply Layout: Big = Primary, Small = Right of Big
if [ "$BIG_NAME" != "$SMALL_NAME" ] && [ ! -z "$BIG_NAME" ]; then
    xrandr --output "$BIG_NAME" --auto --primary --output "$SMALL_NAME" --auto --right-of "$BIG_NAME"
else
    # Fallback for single screen or splitter
    xrandr --auto
fi

# --- B. START APPLICATION ---
echo "Starting Gaze Painter..."
cd /home/kasboek/LatentSpace/gaze-painter
/home/kasboek/LatentSpace/gaze-painter/venv/bin/python3 run_gaze_painter.py &
APP_PID=$!

# --- C. MOVE & MAXIMIZE WINDOWS (Background Loop) ---
(
    MAX_RETRIES=45
    count=0
    echo "Waiting for windows..."
    
    while [ $count -lt $MAX_RETRIES ]; do
        # Check if windows exist using wmctrl
        if wmctrl -l | grep -q "Gaze Painter" && wmctrl -l | grep -q "Eyes"; then
            sleep 3 # Wait for render
            
            # 1. Move Canvas (Gaze Painter) to Big Screen (0,0) and Maximize
            echo "Maximizing Canvas..."
            wmctrl -r "Gaze Painter" -e 0,0,0,-1,-1
            wmctrl -r "Gaze Painter" -b add,maximized_vert,maximized_horz
            
            # 2. Move Eyes to Small Screen (Offset by Big Screen Width) and Maximize
            echo "Maximizing Eyes..."
            wmctrl -r "Eyes" -e 0,$BIG_WIDTH,0,-1,-1
            wmctrl -r "Eyes" -b add,maximized_vert,maximized_horz
            
            break
        fi
        sleep 1
        ((count++))
    done
) &

# --- D. KEEP ALIVE ---
wait $APP_PID
EOFDISPLAY

chmod +x "$SCRIPTS_DIR/display_manager.sh"
chown "$TARGET_USER:$TARGET_USER" "$SCRIPTS_DIR/display_manager.sh"

# 9. SERVICE (Updated to use Wrapper)
log_info "Step 9: Creating Service..."
cat > /etc/systemd/system/gaze-painter.service << EOF
[Unit]
Description=Latent Space - Gaze Painter
After=network.target graphical.target

[Service]
Type=simple
User=$TARGET_USER
Environment="DISPLAY=:0"
Environment="XAUTHORITY=/home/$TARGET_USER/.Xauthority"
Environment="LATENT_PROJECT_ROOT=$PROJECT_DIR"
WorkingDirectory=$PROJECT_DIR/gaze-painter
# Run the wrapper script which handles displays AND python execution
ExecStart=$SCRIPTS_DIR/display_manager.sh
Restart=on-failure
RestartSec=5

[Install]
WantedBy=graphical.target
EOF

systemctl daemon-reload
systemctl enable gaze-painter

# 10. VALIDATION
echo ""
log_info "Setup Phase Complete."
echo "Do you want to run the verification script now? (y/n)"
read -r response
if [[ "$response" =~ ^([yY][eE][sS]|[yY])+$ ]]; then
    chmod +x ./verify_installation.sh
    ./verify_installation.sh
fi