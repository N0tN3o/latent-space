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