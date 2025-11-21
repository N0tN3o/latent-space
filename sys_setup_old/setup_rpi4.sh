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

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

if [ "$EUID" -ne 0 ]; then 
    log_error "Please run as root (sudo ./setup_rpi4.sh)"
    exit 1
fi

ACTUAL_USER=${SUDO_USER:-$USER}
if [ "$ACTUAL_USER" = "root" ]; then
    ACTUAL_USER="art"
fi

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
    usermod -aG sudo,lpadmin "$ACTUAL_USER"
fi

# ============================================
# 1. System Update & Dependencies
# ============================================
log_info "Step 1/10: Updating system packages..."
apt update
apt upgrade -y

log_info "Installing system dependencies..."
apt install -y \
    python3 \
    python3-venv \
    python3-pip \
    python3-pyqt5 \
    python3-pyqt5.qtmultimedia \
    nfs-common \
    openssh-server \
    rsync \
    cups \
    cups-client \
    printer-driver-all \
    git \
    curl \
    wget \
    vim \
    htop \
    iptables-persistent \
    net-tools \
    libopencv-dev

# ============================================
# 2. Create Directory Structure
# ============================================
log_info "Step 2/10: Creating directory structure..."
mkdir -p "$PROJECT_DIR"/{print_service,shared_output/docs,logs,scripts,.ssh}
chown -R "$ACTUAL_USER:$ACTUAL_USER" "$PROJECT_DIR"

# ============================================
# 3. Setup Python Virtual Environment
# ============================================
log_info "Step 3/10: Setting up Python environment for print service..."
cd "$PROJECT_DIR/print_service"

sudo -u "$ACTUAL_USER" python3 -m venv venv

sudo -u "$ACTUAL_USER" bash -c "
    source venv/bin/activate
    pip install --upgrade pip
    pip install PyQt5
    pip install PyMuPDF
    pip install watchdog
    pip install Pillow
"

log_info "Python environment created successfully"

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
    log_info "Added NFS mount to /etc/fstab"
fi

log_info "NFS client configured. Testing mount..."
mount -a || log_warn "NFS mount failed - Jetson B may not be ready yet"

# ============================================
# 5. Setup SSH Keys
# ============================================
log_info "Step 5/10: Generating SSH keys for sync..."

SSH_KEY="$HOME_DIR/.ssh/id_rsa_sync"
if [ ! -f "$SSH_KEY" ]; then
    sudo -u "$ACTUAL_USER" ssh-keygen -t ed25519 -f "$SSH_KEY" -N "" -C "rsync-sync@rpi4"
    log_info "SSH key generated: $SSH_KEY"
else
    log_warn "SSH key already exists, skipping generation"
fi

mkdir -p "$HOME_DIR/.ssh"
touch "$HOME_DIR/.ssh/authorized_keys"
chmod 700 "$HOME_DIR/.ssh"
chmod 600 "$HOME_DIR/.ssh/authorized_keys"
chown -R "$ACTUAL_USER:$ACTUAL_USER" "$HOME_DIR/.ssh"

log_warn "MANUAL STEP: Add this public key to Jetson B's authorized_keys"
log_warn "Public key location: $SSH_KEY.pub"

# ============================================
# 6. Setup CUPS Printing
# ============================================
log_info "Step 6/10: Configuring CUPS printing service..."

# Enable CUPS
systemctl enable cups
systemctl start cups

# Add user to lpadmin group
usermod -aG lpadmin "$ACTUAL_USER"

# Configure CUPS to allow network printing
cp /etc/cups/cupsd.conf /etc/cups/cupsd.conf.backup

# Allow local network access
sed -i 's/Listen localhost:631/Listen 631/' /etc/cups/cupsd.conf

log_info "CUPS configured. Access web interface at: http://192.168.1.12:631"
log_warn "MANUAL STEP: Connect USB printer and add it via CUPS web interface"

# ============================================
# 7. Create Print Kiosk UI
# ============================================
log_info "Step 7/10: Creating print kiosk UI..."

cat > "$PROJECT_DIR/print_service/print_kiosk_ui.py" << 'EOFUI'
#!/usr/bin/env python3
"""
Enhanced Print Kiosk Touch UI with Queue and Search
- Browse all available PDFs
- Search by date/time
- Print any drawing from history
- Mark as printed (but keep in archive)
- Show thumbnails of drawings
"""

import sys
import os
import subprocess
import time
import json
from datetime import datetime, timedelta
from pathlib import Path
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QPushButton, QLabel, QListWidget,
                             QMessageBox, QStackedWidget, QLineEdit, QScrollArea,
                             QListWidgetItem, QGridLayout)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QThread, QSize
from PyQt5.QtGui import QFont, QPixmap, QImage, QIcon
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import fitz  # PyMuPDF for PDF thumbnails

sys.path.append(str(Path(__file__).parent.parent / "scripts"))

try:
    from network_health import NetworkHealth, NetworkStatus
except ImportError:
    NetworkHealth = None
    NetworkStatus = None

SHARED_DIR = Path("/home/art/LatentSpace/shared_output")
DOCS_DIR = SHARED_DIR / "docs"
PRINTED_DIR = DOCS_DIR / "printed"
PRINT_LOG = DOCS_DIR / "print_history.json"
PRINTER_NAME = "default"

class PrintRecord:
    """Represents a single print/drawing"""
    def __init__(self, pdf_path):
        self.path = Path(pdf_path)
        self.filename = self.path.name
        
        # Parse timestamp from filename: print_20250117_143022.pdf
        try:
            date_str = self.filename.split('_')[1]
            time_str = self.filename.split('_')[2].split('.')[0]
            self.timestamp = datetime.strptime(f"{date_str}_{time_str}", "%Y%m%d_%H%M%S")
        except:
            self.timestamp = datetime.fromtimestamp(self.path.stat().st_mtime)
        
        self.printed = self._check_if_printed()
    
    def _check_if_printed(self):
        """Check if this drawing has been printed"""
        if not PRINT_LOG.exists():
            return False
        
        try:
            with open(PRINT_LOG, 'r') as f:
                history = json.load(f)
            return self.filename in history.get('printed', [])
        except:
            return False
    
    def mark_as_printed(self):
        """Mark this drawing as printed in the log"""
        history = {'printed': [], 'prints': []}
        
        if PRINT_LOG.exists():
            try:
                with open(PRINT_LOG, 'r') as f:
                    history = json.load(f)
            except:
                pass
        
        if self.filename not in history['printed']:
            history['printed'].append(self.filename)
        
        history['prints'].append({
            'filename': self.filename,
            'timestamp': datetime.now().isoformat(),
            'user_timestamp': self.timestamp.isoformat()
        })
        
        with open(PRINT_LOG, 'w') as f:
            json.dump(history, f, indent=2)
        
        self.printed = True
    
    def get_thumbnail(self, size=(200, 280)):
        """Generate thumbnail from PDF"""
        try:
            doc = fitz.open(self.path)
            page = doc[0]
            
            # Calculate zoom to fit size
            mat = page.get_pixmap(matrix=fitz.Matrix(2, 2))
            img_data = mat.tobytes("ppm")
            
            qimg = QImage.fromData(img_data)
            pixmap = QPixmap.fromImage(qimg)
            
            # Scale to thumbnail size
            pixmap = pixmap.scaled(size[0], size[1], 
                                   Qt.KeepAspectRatio, 
                                   Qt.SmoothTransformation)
            
            doc.close()
            return pixmap
            
        except Exception as e:
            print(f"Thumbnail error: {e}")
            # Return placeholder
            pixmap = QPixmap(size[0], size[1])
            pixmap.fill(Qt.lightGray)
            return pixmap
    
    def get_display_text(self):
        """Get text for list display"""
        date_str = self.timestamp.strftime("%B %d, %Y")
        time_str = self.timestamp.strftime("%I:%M %p")
        status = "✓ Printed" if self.printed else "○ Not Printed"
        return f"{date_str} at {time_str}\n{status}"

class PDFWatcher(FileSystemEventHandler):
    def __init__(self, callback):
        self.callback = callback
    
    def on_created(self, event):
        if event.src_path.endswith('.pdf') and 'print_' in event.src_path:
            self.callback(event.src_path)

class NetworkMonitorThread(QThread):
    status_changed = pyqtSignal(str)
    
    def __init__(self):
        super().__init__()
        self.running = True
        if NetworkHealth:
            self.health = NetworkHealth(role="rpi4")
        else:
            self.health = None
    
    def run(self):
        while self.running:
            if self.health:
                status = self.health.check()
                self.status_changed.emit(status.value)
            time.sleep(5)
    
    def stop(self):
        self.running = False

class PrintQueueItem(QWidget):
    """Custom widget for queue list item"""
    print_clicked = pyqtSignal(object)
    
    def __init__(self, record: PrintRecord):
        super().__init__()
        self.record = record
        
        layout = QHBoxLayout()
        layout.setContentsMargins(10, 10, 10, 10)
        
        # Thumbnail
        thumb_label = QLabel()
        thumb_label.setPixmap(record.get_thumbnail((120, 160)))
        thumb_label.setFixedSize(120, 160)
        layout.addWidget(thumb_label)
        
        # Info
        info_layout = QVBoxLayout()
        
        date_label = QLabel(record.timestamp.strftime("%B %d, %Y"))
        date_label.setFont(QFont("Arial", 16, QFont.Bold))
        info_layout.addWidget(date_label)
        
        time_label = QLabel(record.timestamp.strftime("%I:%M %p"))
        time_label.setFont(QFont("Arial", 14))
        info_layout.addWidget(time_label)
        
        status_label = QLabel("✓ Already Printed" if record.printed else "○ Available")
        status_label.setFont(QFont("Arial", 12))
        status_label.setStyleSheet("color: #888;" if record.printed else "color: #4CAF50;")
        info_layout.addWidget(status_label)
        
        info_layout.addStretch()
        layout.addLayout(info_layout, stretch=1)
        
        # Print button
        print_btn = QPushButton("🖨️ PRINT")
        print_btn.setFont(QFont("Arial", 18, QFont.Bold))
        print_btn.setFixedSize(150, 80)
        print_btn.clicked.connect(lambda: self.print_clicked.emit(self.record))
        layout.addWidget(print_btn)
        
        self.setLayout(layout)
        
        # Styling
        self.setStyleSheet("""
            QWidget {
                background-color: #2a2a2a;
                border: 2px solid #444;
                border-radius: 10px;
                margin: 5px;
            }
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                border-radius: 10px;
            }
            QPushButton:hover {
                background-color: #45a049;
            }
            QPushButton:pressed {
                background-color: #3d8b40;
            }
        """)

class EnhancedPrintKioskUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Latent Space - Print Service")
        self.showFullScreen()
        
        # State
        self.records = []
        self.filtered_records = []
        self.network_status = "primary"
        
        # Setup UI
        self.setup_ui()
        
        # Start file watcher
        self.setup_file_watcher()
        
        # Start network monitor
        if NetworkHealth:
            self.network_thread = NetworkMonitorThread()
            self.network_thread.status_changed.connect(self.on_network_status_changed)
            self.network_thread.start()
        
        # Load existing PDFs
        self.refresh_queue()
    
    def setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        
        # Stacked widget for different screens
        self.stack = QStackedWidget()
        
        # Main screen (queue view)
        self.main_screen = self.create_main_screen()
        self.stack.addWidget(self.main_screen)
        
        # Error screen
        self.error_screen = self.create_error_screen()
        self.stack.addWidget(self.error_screen)
        
        layout = QVBoxLayout()
        layout.addWidget(self.stack)
        layout.setContentsMargins(0, 0, 0, 0)
        central.setLayout(layout)
        
        # Dark theme
        self.setStyleSheet("""
            QMainWindow {
                background-color: #1a1a1a;
            }
            QLabel {
                color: white;
            }
            QPushButton {
                background-color: #2a2a2a;
                color: white;
                border: 2px solid #444;
                border-radius: 10px;
                padding: 15px;
                font-size: 18px;
            }
            QPushButton:hover {
                background-color: #3a3a3a;
            }
            QPushButton:pressed {
                background-color: #4a4a4a;
            }
            QLineEdit {
                background-color: #2a2a2a;
                color: white;
                border: 2px solid #444;
                border-radius: 5px;
                padding: 10px;
                font-size: 16px;
            }
            QListWidget {
                background-color: #1a1a1a;
                border: none;
            }
            QScrollArea {
                border: none;
            }
        """)
    
    def create_main_screen(self):
        widget = QWidget()
        layout = QVBoxLayout()
        
        # Header
        header = QHBoxLayout()
        
        title = QLabel("Latent Space - Print Your Drawing")
        title.setFont(QFont("Arial", 32, QFont.Bold))
        header.addWidget(title)
        
        header.addStretch()
        
        # Network indicator
        self.network_indicator = QLabel("● Network: OK")
        self.network_indicator.setFont(QFont("Arial", 14))
        self.network_indicator.setStyleSheet("color: #00ff00;")
        header.addWidget(self.network_indicator)
        
        layout.addLayout(header)
        
        # Search bar
        search_layout = QHBoxLayout()
        
        search_label = QLabel("🔍 Search by date:")
        search_label.setFont(QFont("Arial", 16))
        search_layout.addWidget(search_label)
        
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("e.g., 'today', 'yesterday', 'January 15', or leave blank for all")
        self.search_input.textChanged.connect(self.on_search_changed)
        search_layout.addWidget(self.search_input, stretch=1)
        
        refresh_btn = QPushButton("🔄 Refresh")
        refresh_btn.clicked.connect(self.refresh_queue)
        search_layout.addWidget(refresh_btn)
        
        layout.addLayout(search_layout)
        
        # Queue info
        self.queue_info = QLabel("Loading drawings...")
        self.queue_info.setFont(QFont("Arial", 14))
        self.queue_info.setStyleSheet("color: #888; padding: 10px;")
        layout.addWidget(self.queue_info)
        
        # Scroll area for queue
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        
        self.queue_container = QWidget()
        self.queue_layout = QVBoxLayout()
        self.queue_layout.setSpacing(10)
        self.queue_layout.setContentsMargins(20, 20, 20, 20)
        self.queue_container.setLayout(self.queue_layout)
        
        scroll.setWidget(self.queue_container)
        layout.addWidget(scroll)
        
        widget.setLayout(layout)
        return widget
    
    def create_error_screen(self):
        widget = QWidget()
        layout = QVBoxLayout()
        
        layout.addStretch()
        
        error_title = QLabel("Service Temporarily Unavailable")
        error_title.setAlignment(Qt.AlignCenter)
        error_title.setFont(QFont("Arial", 36, QFont.Bold))
        error_title.setStyleSheet("color: #ff4444;")
        layout.addWidget(error_title)
        
        layout.addSpacing(40)
        
        error_msg = QLabel("We're experiencing technical difficulties.\nPlease contact staff for assistance.")
        error_msg.setAlignment(Qt.AlignCenter)
        error_msg.setFont(QFont("Arial", 24))
        error_msg.setWordWrap(True)
        layout.addWidget(error_msg)
        
        layout.addStretch()
        
        widget.setLayout(layout)
        return widget
    
    def setup_file_watcher(self):
        """Watch for new PDFs"""
        DOCS_DIR.mkdir(parents=True, exist_ok=True)
        PRINTED_DIR.mkdir(parents=True, exist_ok=True)
        
        self.observer = Observer()
        event_handler = PDFWatcher(self.on_new_pdf)
        self.observer.schedule(event_handler, str(DOCS_DIR), recursive=False)
        self.observer.start()
    
    def refresh_queue(self):
        """Reload all PDFs from directory"""
        if not DOCS_DIR.exists():
            return
        
        # Get all PDFs
        pdf_files = sorted(DOCS_DIR.glob("print_*.pdf"), 
                          key=lambda x: x.stat().st_mtime, 
                          reverse=True)
        
        self.records = [PrintRecord(pdf) for pdf in pdf_files]
        self.filtered_records = self.records.copy()
        
        self.update_queue_display()
    
    def on_search_changed(self, text):
        """Filter records based on search"""
        text = text.lower().strip()
        
        if not text:
            self.filtered_records = self.records.copy()
        else:
            now = datetime.now()
            self.filtered_records = []
            
            for record in self.records:
                # Parse search terms
                if text == "today":
                    if record.timestamp.date() == now.date():
                        self.filtered_records.append(record)
                
                elif text == "yesterday":
                    yesterday = now - timedelta(days=1)
                    if record.timestamp.date() == yesterday.date():
                        self.filtered_records.append(record)
                
                elif text in ["this week", "week"]:
                    week_ago = now - timedelta(days=7)
                    if record.timestamp >= week_ago:
                        self.filtered_records.append(record)
                
                else:
                    # Try to match date string
                    date_str = record.timestamp.strftime("%B %d, %Y").lower()
                    time_str = record.timestamp.strftime("%I:%M %p").lower()
                    
                    if text in date_str or text in time_str:
                        self.filtered_records.append(record)
        
        self.update_queue_display()
    
    def update_queue_display(self):
        """Update the visual queue"""
        # Clear existing items
        for i in reversed(range(self.queue_layout.count())):
            widget = self.queue_layout.itemAt(i).widget()
            if widget:
                widget.setParent(None)
        
        # Update info label
        total = len(self.records)
        filtered = len(self.filtered_records)
        unprinted = sum(1 for r in self.filtered_records if not r.printed)
        
        if filtered == 0:
            self.queue_info.setText("No drawings found. Create a drawing at the gaze station!")
        else:
            info_text = f"Showing {filtered} of {total} drawings"
            if unprinted > 0:
                info_text += f" ({unprinted} not yet printed)"
            self.queue_info.setText(info_text)
        
        # Add queue items
        for record in self.filtered_records:
            item_widget = PrintQueueItem(record)
            item_widget.print_clicked.connect(self.print_pdf)
            self.queue_layout.addWidget(item_widget)
        
        # Add stretch at end
        self.queue_layout.addStretch()
    
    def on_new_pdf(self, pdf_path):
        """Called when a new PDF is detected"""
        print(f"New PDF detected: {pdf_path}")
        self.refresh_queue()
    
    def print_pdf(self, record: PrintRecord):
        """Send PDF to printer"""
        try:
            # Confirmation dialog
            reply = QMessageBox.question(
                self,
                "Confirm Print",
                f"Print drawing from {record.timestamp.strftime('%B %d, %Y at %I:%M %p')}?",
                QMessageBox.Yes | QMessageBox.No
            )
            
            if reply != QMessageBox.Yes:
                return
            
            # Print using lp command
            result = subprocess.run(
                ["lp", "-d", PRINTER_NAME, str(record.path)],
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if result.returncode == 0:
                # Mark as printed
                record.mark_as_printed()
                
                QMessageBox.information(
                    self,
                    "Success",
                    "Print job sent successfully!\nThank you!"
                )
                
                # Refresh display
                self.update_queue_display()
            else:
                QMessageBox.critical(
                    self,
                    "Print Error",
                    f"Failed to print:\n{result.stderr}"
                )
        
        except Exception as e:
            QMessageBox.critical(
                self,
                "Error",
                f"Printing failed:\n{str(e)}"
            )
    
    def on_network_status_changed(self, status):
        """Handle network status updates"""
        self.network_status = status
        
        if status == "primary":
            self.network_indicator.setText("● Network: OK")
            self.network_indicator.setStyleSheet("color: #00ff00;")
            self.stack.setCurrentIndex(0)
        
        elif status == "secondary":
            self.network_indicator.setText("⚠ Network: Slow")
            self.network_indicator.setStyleSheet("color: #ffaa00;")
            self.stack.setCurrentIndex(0)
        
        elif status == "degraded":
            self.network_indicator.setText("⚠ Reconnecting...")
            self.network_indicator.setStyleSheet("color: #ff8800;")
            self.stack.setCurrentIndex(0)
        
        elif status == "stopped":
            self.stack.setCurrentIndex(1)
    
    def closeEvent(self, event):
        if hasattr(self, 'observer'):
            self.observer.stop()
            self.observer.join()
        if hasattr(self, 'network_thread'):
            self.network_thread.stop()
            self.network_thread.wait()
        event.accept()

def main():
    app = QApplication(sys.argv)
    window = EnhancedPrintKioskUI()
    window.show()
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()
EOFUI

chmod +x "$PROJECT_DIR/print_service/print_kiosk_ui.py"
chown "$ACTUAL_USER:$ACTUAL_USER" "$PROJECT_DIR/print_service/print_kiosk_ui.py"

log_info "Print kiosk UI created"

# ============================================
# 8. Configure Firewall
# ============================================
log_info "Step 8/10: Configuring firewall rules..."

iptables -F
iptables -X

iptables -P INPUT DROP
iptables -P FORWARD DROP
iptables -P OUTPUT ACCEPT

iptables -A INPUT -i lo -j ACCEPT
iptables -A INPUT -m state --state ESTABLISHED,RELATED -j ACCEPT

# Allow SSH
iptables -A INPUT -p tcp --dport 22 -s 192.168.1.0/24 -j ACCEPT

# Allow NFS from Jetson B
iptables -A INPUT -s 192.168.1.11 -j ACCEPT

# Allow CUPS web interface (for printer setup)
iptables -A INPUT -p tcp --dport 631 -s 192.168.1.0/24 -j ACCEPT

# Allow ping
iptables -A INPUT -p icmp --icmp-type echo-request -s 192.168.1.0/24 -j ACCEPT

netfilter-persistent save

log_info "Firewall rules configured"

# ============================================
# 9. Create Systemd Service
# ============================================
log_info "Step 9/10: Creating systemd service..."

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

log_info "Systemd service created and enabled"

# ============================================
# 10. Configure Auto-login (for kiosk mode)
# ============================================
log_info "Step 10/10: Configuring auto-login..."

# Create autologin directory
mkdir -p /etc/systemd/system/getty@tty1.service.d/

cat > /etc/systemd/system/getty@tty1.service.d/autologin.conf << EOFAUTOLOGIN
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin $ACTUAL_USER --noclear %I \$TERM
EOFAUTOLOGIN

# Configure X11 auto-start
mkdir -p "$HOME_DIR/.config/lxsession/LXDE-pi"
cat > "$HOME_DIR/.config/lxsession/LXDE-pi/autostart" << EOFAUTOSTART
@xset s off
@xset -dpms
@xset s noblank
@/home/$ACTUAL_USER/LatentSpace/print_service/venv/bin/python3 /home/$ACTUAL_USER/LatentSpace/print_service/print_kiosk_ui.py
EOFAUTOSTART

chown -R "$ACTUAL_USER:$ACTUAL_USER" "$HOME_DIR/.config"

log_info "Auto-login configured"

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
echo "=========================================="
log_info "Raspberry Pi 4 Setup Complete!"
echo "=========================================="
echo ""
echo "Summary:"
echo "  ✓ NFS client configured"
echo "  ✓ Python environment setup"
echo "  ✓ Print kiosk UI created"
echo "  ✓ CUPS printing configured"
echo "  ✓ Auto-login enabled"
echo "  ✓ Service created (print-service)"
echo ""
echo "Next Steps:"
echo "  1. Connect USB printer"
echo "  2. Add printer via CUPS: http://192.168.1.12:631"
echo "  3. Update PRINTER_NAME in print_kiosk_ui.py if needed"
echo "  4. Copy SSH public key to Jetson B:"
echo "     ssh-copy-id -i $SSH_KEY art@192.168.1.11"
echo "  5. Verify NFS mount:"
echo "     mount | grep shared_output"
echo "  6. Reboot: sudo reboot"
echo ""
echo "After reboot, the print kiosk will launch automatically."
echo "Check status: sudo systemctl status print-service"
echo ""
echo "Public SSH key for Jetson B:"
cat "$SSH_KEY.pub"
echo ""