import sys
import os
import subprocess
import time
import json
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# --- PyQt6 Imports ---
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QPushButton, QLabel, QMessageBox, 
                             QStackedWidget, QLineEdit, QScrollArea, QGridLayout,
                             QScroller)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QSize, QEvent
from PyQt6.QtGui import QFont, QPixmap, QIcon

sys.path.append(str(Path(__file__).parent.parent / "scripts"))

# Configuration
INACTIVITY_TIMEOUT_SEC = 60  # Reset search after 60s of no activity

SHARED_DIR = Path("/home/kasboek/LatentSpace/shared_output")
DOCS_DIR = SHARED_DIR / "docs"
PRINTED_DIR = DOCS_DIR / "printed"
PRINT_LOG = DOCS_DIR / "print_history.json"
PRINTER_NAME = "default"

class PrintRecord:
    """Represents a single print/drawing"""
    def __init__(self, pdf_path):
        self.path = Path(pdf_path)
        self.filename = self.path.name
        try:
            date_str = self.filename.split('_')[1]
            time_str = self.filename.split('_')[2].split('.')[0]
            self.timestamp = datetime.strptime(f"{date_str}_{time_str}", "%Y%m%d_%H%M%S")
        except:
            self.timestamp = datetime.fromtimestamp(self.path.stat().st_mtime)
        self.printed = self._check_if_printed()
    
    def _check_if_printed(self):
        if not PRINT_LOG.exists(): return False
        try:
            with open(PRINT_LOG, 'r') as f:
                history = json.load(f)
            return self.filename in history.get('printed', [])
        except: return False
    
    def mark_as_printed(self):
        history = {'printed': [], 'prints': []}
        if PRINT_LOG.exists():
            try:
                with open(PRINT_LOG, 'r') as f: history = json.load(f)
            except: pass
        if self.filename not in history['printed']:
            history['printed'].append(self.filename)
        history['prints'].append({
            'filename': self.filename,
            'timestamp': datetime.now().isoformat(),
            'user_timestamp': self.timestamp.isoformat()
        })
        with open(PRINT_LOG, 'w') as f: json.dump(history, f, indent=2)
        self.printed = True
    
    def get_thumbnail(self, size=(200, 280)):
        png_path = self.path.with_suffix('.png')
        try:
            # If a PNG already exists, load it
            if png_path.exists():
                pixmap = QPixmap(str(png_path))
                if not pixmap.isNull():
                    return pixmap.scaled(size[0], size[1], Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)

            # Try to generate a PNG from the PDF using common system tools
            if self.path.exists():
                generated = False
                # Use pdftoppm (poppler) if available
                try:
                    if shutil.which('pdftoppm'):
                        root = str(png_path.with_suffix(''))
                        subprocess.run(['pdftoppm', '-png', '-singlefile', str(self.path), root], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                        if png_path.exists():
                            generated = True
                except Exception:
                    generated = False

                # Fallback to ImageMagick `convert` if available
                if not generated:
                    try:
                        if shutil.which('convert'):
                            # render only the first page
                            subprocess.run(['convert', f"{str(self.path)}[0]", str(png_path)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                            if png_path.exists():
                                generated = True
                    except Exception:
                        generated = False

                # If generation succeeded, try loading it
                if generated and png_path.exists():
                    pixmap = QPixmap(str(png_path))
                    if not pixmap.isNull():
                        return pixmap.scaled(size[0], size[1], Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)

        except Exception as e:
            print(f"Thumbnail generation error for {self.path}: {e}")

        # Final fallback: neutral placeholder
        pixmap = QPixmap(size[0], size[1])
        pixmap.fill(Qt.GlobalColor.lightGray)
        return pixmap

class PDFWatcher(FileSystemEventHandler):
    def __init__(self, callback): self.callback = callback
    def on_created(self, event):
        # FIX 1: Listen for PDF OR PNG. 
        is_valid_file = event.src_path.endswith('.pdf') or event.src_path.endswith('.png')
        if is_valid_file and 'print_' in event.src_path:
            self.callback(event.src_path)

class PrintQueueItem(QWidget):
    print_clicked = pyqtSignal(object)
    def __init__(self, record: PrintRecord):
        super().__init__()
        self.record = record
        layout = QHBoxLayout()
        layout.setContentsMargins(10, 10, 10, 10)
        
        thumb_label = QLabel()
        thumb_label.setPixmap(record.get_thumbnail((120, 160)))
        thumb_label.setFixedSize(120, 160)
        layout.addWidget(thumb_label)
        
        info_layout = QVBoxLayout()
        date_label = QLabel(record.timestamp.strftime("%B %d, %Y"))
        date_label.setFont(QFont("Arial", 16, QFont.Weight.Bold))
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
        
        print_btn = QPushButton("🖨️ PRINT")
        print_btn.setFont(QFont("Arial", 18, QFont.Weight.Bold))
        print_btn.setFixedSize(150, 80)
        print_btn.clicked.connect(lambda: self.print_clicked.emit(self.record))
        layout.addWidget(print_btn)
        self.setLayout(layout)
        self.setStyleSheet("QWidget { background-color: #2a2a2a; border: 2px solid #444; border-radius: 10px; margin: 5px; } QPushButton { background-color: #4CAF50; color: white; border: none; border-radius: 10px; } QPushButton:hover { background-color: #45a049; }")

class EnhancedPrintKioskUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Latent Space - Print Service")
        self.showFullScreen()
        
        # State
        self.records = []
        self.filtered_records = []
        
        # Timer
        self.inactivity_timer = QTimer(self)
        self.inactivity_timer.setInterval(INACTIVITY_TIMEOUT_SEC * 1000)
        self.inactivity_timer.timeout.connect(self.reset_on_inactivity)
        self.inactivity_timer.start()
        
        QApplication.instance().installEventFilter(self)
        
        self.setup_ui()
        self.setup_file_watcher()
        self.refresh_queue()

    def eventFilter(self, obj, event):
        if event.type() in [QEvent.Type.MouseButtonPress, QEvent.Type.KeyPress, QEvent.Type.TouchBegin, QEvent.Type.Wheel]:
            self.inactivity_timer.start()
        return super().eventFilter(obj, event)

    def reset_on_inactivity(self):
        if self.search_input.text():
            print("Inactivity detected - resetting search")
            self.search_input.clear()
            self.stack.setCurrentIndex(0)

    def setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        self.stack = QStackedWidget()
        self.main_screen = self.create_main_screen()
        self.stack.addWidget(self.main_screen)
        self.error_screen = self.create_error_screen()
        self.stack.addWidget(self.error_screen)
        layout = QVBoxLayout()
        layout.addWidget(self.stack)
        layout.setContentsMargins(0, 0, 0, 0)
        central.setLayout(layout)
        
        # FIX 3: Updated Stylesheet to include QMessageBox colors
        self.setStyleSheet("""
            QMainWindow { background-color: #1a1a1a; } 
            QLabel { color: white; } 
            QPushButton { background-color: #2a2a2a; color: white; border: 2px solid #444; border-radius: 10px; padding: 15px; font-size: 18px; } 
            QPushButton:hover { background-color: #3a3a3a; } 
            QLineEdit { background-color: #2a2a2a; color: white; border: 2px solid #444; border-radius: 5px; padding: 10px; font-size: 16px; } 
            QScrollArea { border: none; }
            QMessageBox { background-color: #2a2a2a; }
            QMessageBox QLabel { color: white; }
        """)

    def create_main_screen(self):
        widget = QWidget()
        layout = QVBoxLayout()
        header = QHBoxLayout()
        title = QLabel("Print Your Drawing")
        title.setFont(QFont("Arial", 32, QFont.Weight.Bold))
        header.addWidget(title)
        header.addStretch()
        layout.addLayout(header)
        
        search_layout = QHBoxLayout()
        search_label = QLabel("🔍 Search by date:")
        search_label.setFont(QFont("Arial", 16))
        search_layout.addWidget(search_label)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("e.g., 'today', 'yesterday', 'January 15'")
        self.search_input.textChanged.connect(self.on_search_changed)
        search_layout.addWidget(self.search_input, stretch=1)
        refresh_btn = QPushButton("🔄 Refresh")
        refresh_btn.clicked.connect(self.refresh_queue)
        search_layout.addWidget(refresh_btn)
        layout.addLayout(search_layout)
        
        self.queue_info = QLabel("Loading drawings...")
        self.queue_info.setFont(QFont("Arial", 14))
        self.queue_info.setStyleSheet("color: #888; padding: 10px;")
        layout.addWidget(self.queue_info)
        
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        
        # FIX 2: Enable Kinetic/Touch Scrolling - CORRECTED ENUM
        QScroller.grabGesture(scroll.viewport(), QScroller.ScrollerGestureType.TouchGesture)
        
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
        error_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        error_title.setFont(QFont("Arial", 36, QFont.Weight.Bold))
        error_title.setStyleSheet("color: #ff4444;")
        layout.addWidget(error_title)
        layout.addSpacing(40)
        error_msg = QLabel("We're experiencing technical difficulties.\nPlease contact staff.")
        error_msg.setAlignment(Qt.AlignmentFlag.AlignCenter)
        error_msg.setFont(QFont("Arial", 24))
        error_msg.setWordWrap(True)
        layout.addWidget(error_msg)
        layout.addStretch()
        widget.setLayout(layout)
        return widget

    def setup_file_watcher(self):
        DOCS_DIR.mkdir(parents=True, exist_ok=True)
        PRINTED_DIR.mkdir(parents=True, exist_ok=True)
        self.observer = Observer()
        self.observer.schedule(PDFWatcher(self.on_new_pdf), str(DOCS_DIR), recursive=False)
        self.observer.start()

    def refresh_queue(self):
        if not DOCS_DIR.exists(): return
        pdf_files = sorted(DOCS_DIR.glob("print_*.pdf"), key=lambda x: x.stat().st_mtime, reverse=True)
        self.records = [PrintRecord(pdf) for pdf in pdf_files]
        if self.search_input.text():
            self.on_search_changed(self.search_input.text())
        else:
            self.filtered_records = self.records.copy()
            self.update_queue_display()

    def on_search_changed(self, text):
        text = text.lower().strip()
        if not text:
            self.filtered_records = self.records.copy()
        else:
            now = datetime.now()
            self.filtered_records = []
            for record in self.records:
                if text == "today" and record.timestamp.date() == now.date(): self.filtered_records.append(record)
                elif text == "yesterday" and record.timestamp.date() == (now - timedelta(days=1)).date(): self.filtered_records.append(record)
                elif (text in record.timestamp.strftime("%B %d, %Y").lower() or text in record.timestamp.strftime("%I:%M %p").lower()): self.filtered_records.append(record)
        self.update_queue_display()

    def update_queue_display(self):
        for i in reversed(range(self.queue_layout.count())): 
            w = self.queue_layout.itemAt(i).widget()
            if w: w.setParent(None)
        
        total = len(self.records)
        filtered = len(self.filtered_records)
        if filtered == 0: self.queue_info.setText("No drawings found.")
        else: self.queue_info.setText(f"Showing {filtered} of {total} drawings")
        
        for record in self.filtered_records:
            item = PrintQueueItem(record)
            item.print_clicked.connect(self.print_pdf)
            self.queue_layout.addWidget(item)
        self.queue_layout.addStretch()

    def on_new_pdf(self, pdf_path):
        print(f"File Event Detected: {pdf_path}")
        self.refresh_queue()

    def print_pdf(self, record):
        reply = QMessageBox.question(self, "Confirm Print", f"Print drawing from {record.timestamp.strftime('%H:%M')}?", 
                                   QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes: return
        
        try:
            subprocess.run(["lp", "-d", PRINTER_NAME, str(record.path)], check=True)
            record.mark_as_printed()
            QMessageBox.information(self, "Success", "Print job sent!")
            self.update_queue_display()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Print failed: {e}")

    def closeEvent(self, event):
        if hasattr(self, 'observer'): self.observer.stop()
        event.accept()

def main():
    app = QApplication(sys.argv)
    window = EnhancedPrintKioskUI()
    window.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()