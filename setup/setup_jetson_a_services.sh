#!/bin/bash
# setup_jetson_a_services.sh
# Minimal installer: only creates/enables/restarts the `gaze-painter` systemd service
# Usage: sudo ./setup/setup_jetson_a_services.sh [--user <username>] [--project-dir /home/<user>/LatentSpace]

set -e

if [ "$EUID" -ne 0 ]; then
    echo "Run as root: sudo $0"
    exit 1
fi

# Defaults
ACTUAL_USER=${SUDO_USER:-$USER}
PROJECT_DIR="/home/$ACTUAL_USER/LatentSpace"

# Parse args
while [[ $# -gt 0 ]]; do
  case "$1" in
    --user)
      ACTUAL_USER="$2"
      shift 2
      ;;
    --project-dir)
      PROJECT_DIR="$2"
      shift 2
      ;;
    *)
      shift
      ;;
  esac
done

HOME_DIR="/home/$ACTUAL_USER"
SERVICE_PATH="/etc/systemd/system/gaze-painter.service"

echo "Creating service for user: $ACTUAL_USER"
echo "Project dir: $PROJECT_DIR"

# Ensure the project directories exist (do not modify contents beyond ownership/perms)
mkdir -p "$PROJECT_DIR/gaze-painter"
mkdir -p "$PROJECT_DIR/scripts"
chown -R "$ACTUAL_USER:$ACTUAL_USER" "$PROJECT_DIR" || true

# Make display_manager executable if present
if [ -f "$PROJECT_DIR/scripts/display_manager.sh" ]; then
  chmod +x "$PROJECT_DIR/scripts/display_manager.sh" || true
fi

cat > "$SERVICE_PATH" << EOFSERVICE
[Unit]
Description=Latent Space - Gaze Painter
After=network.target
Wants=network.target

[Service]
User=$ACTUAL_USER
Environment=DISPLAY=:0
Environment=XAUTHORITY=/home/$ACTUAL_USER/.Xauthority
WorkingDirectory=$PROJECT_DIR/gaze-painter
ExecStartPre=$PROJECT_DIR/scripts/display_manager.sh
ExecStart=$PROJECT_DIR/gaze-painter/venv/bin/python3 run_gaze_painter.py
Restart=on-failure
RestartSec=5
StandardOutput=syslog
StandardError=syslog
SyslogIdentifier=gaze-painter

[Install]
WantedBy=multi-user.target
EOFSERVICE

chmod 644 "$SERVICE_PATH"

echo "Reloading systemd and enabling service..."
systemctl daemon-reload
systemctl enable gaze-painter

echo "Attempting to start (or restart) gaze-painter now..."
if systemctl restart gaze-painter; then
  echo "gaze-painter restarted successfully. Monitor logs with: sudo journalctl -u gaze-painter -f"
else
  echo "Warning: service restart returned non-zero. Check logs: sudo journalctl -u gaze-painter -n 50"
fi

echo "Done."
