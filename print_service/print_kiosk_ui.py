from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import json
from pathlib import Path
import shutil
import subprocess
import sys

# --- PyQt6 Imports ---
from PyQt6.QtCore import QEvent, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QScroller,
    QVBoxLayout,
    QWidget,
)
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config_manager
from config_manager import load_settings, mark_file_printed, send_pdf_to_printer, validate_printer_name

INACTIVITY_TIMEOUT_SEC = 60  # Reset search after 60s of no activity
_ENGLISH_MONTHS = (
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


def _format_english_date(dt: datetime) -> str:
    return f"{_ENGLISH_MONTHS[dt.month]} {dt.day:02d}, {dt.year}"


class PrintRecord:
    """Represents a single print/drawing."""

    def __init__(self, pdf_path, mtime: float = None):
        self.path = Path(pdf_path)
        self.filename = self.path.name
        try:
            date_str = self.filename.split("_")[1]
            time_str = self.filename.split("_")[2].split(".")[0]
            self.timestamp = datetime.strptime(f"{date_str}_{time_str}", "%Y%m%d_%H%M%S")
        except (IndexError, ValueError):
            if mtime is not None:
                self.timestamp = datetime.fromtimestamp(mtime)
            else:
                try:
                    self.timestamp = datetime.fromtimestamp(self.path.stat().st_mtime)
                except Exception:
                    self.timestamp = datetime.now()
        self.printed = self._check_if_printed()

    def _check_if_printed(self):
        print_log = config_manager.DOCS_DIR / "print_history.json"
        if not print_log.exists():
            return False
        try:
            history = json.loads(print_log.read_text(encoding="utf-8"))
            return self.filename in history.get("printed", [])
        except Exception:
            return False

    def mark_as_printed(self):
        mark_file_printed(self.filename, extra_meta={"user_timestamp": self.timestamp.isoformat()})
        self.printed = True

    def ensure_thumbnail_file(self) -> Path | None:
        """Return path to sidecar PNG thumbnail, generating it off the UI thread if missing."""
        png_path = self.path.with_suffix(".png")
        if png_path.exists():
            return png_path
        if not self.path.exists():
            return None
        try:
            import fitz
            doc = fitz.open(str(self.path))
            try:
                if len(doc) > 0:
                    pix = doc[0].get_pixmap(dpi=120)
                    pix.save(str(png_path))
                    if png_path.exists():
                        return png_path
            finally:
                doc.close()
        except Exception:
            pass

        try:
            if shutil.which("pdftoppm"):
                root = str(png_path.with_suffix(""))
                subprocess.run(
                    ["pdftoppm", "-png", "-singlefile", str(self.path), root],
                    check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                if png_path.exists():
                    return png_path
        except Exception:
            pass

        im_bin = shutil.which("magick") or (shutil.which("convert") if sys.platform != "win32" else None)
        if im_bin:
            try:
                subprocess.run(
                    [im_bin, f"{str(self.path)}[0]", str(png_path)],
                    check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                if png_path.exists():
                    return png_path
            except Exception:
                pass
        return None


class PDFWatcher(FileSystemEventHandler):
    def __init__(self, callback):
        self.callback = callback

    @staticmethod
    def _is_final_print_pdf(path_str: str) -> bool:
        name = Path(path_str).name
        return name.startswith("print_") and name.endswith(".pdf") and not name.startswith(".")

    def on_created(self, event):
        if not getattr(event, "is_directory", False) and self._is_final_print_pdf(event.src_path):
            self.callback(event.src_path)

    def on_moved(self, event):
        if not getattr(event, "is_directory", False) and self._is_final_print_pdf(event.dest_path):
            self.callback(event.dest_path)


class PrintQueueItem(QWidget):
    print_clicked = pyqtSignal(object)

    def __init__(self, record: PrintRecord):
        super().__init__()
        self.record = record
        layout = QHBoxLayout()
        layout.setContentsMargins(10, 10, 10, 10)

        self.thumb_label = QLabel()
        self.thumb_label.setFixedSize(120, 160)
        placeholder = QPixmap(120, 160)
        placeholder.fill(Qt.GlobalColor.lightGray)
        self.thumb_label.setPixmap(placeholder)
        png_path = record.path.with_suffix(".png")
        if png_path.exists():
            self.set_thumbnail_file(str(png_path))
        layout.addWidget(self.thumb_label)

        info_layout = QVBoxLayout()
        date_label = QLabel(_format_english_date(record.timestamp))
        date_label.setFont(QFont("Arial", 16, QFont.Weight.Bold))
        info_layout.addWidget(date_label)
        time_label = QLabel(record.timestamp.strftime("%I:%M %p"))
        time_label.setFont(QFont("Arial", 14))
        info_layout.addWidget(time_label)
        self.status_label = QLabel("Already Printed" if record.printed else "Available")
        self.status_label.setFont(QFont("Arial", 12))
        self.status_label.setStyleSheet("color: #888;" if record.printed else "color: #4CAF50;")
        info_layout.addWidget(self.status_label)
        info_layout.addStretch()
        layout.addLayout(info_layout, stretch=1)

        print_btn = QPushButton("PRINT")
        print_btn.setFont(QFont("Arial", 18, QFont.Weight.Bold))
        print_btn.setFixedSize(150, 80)
        print_btn.clicked.connect(lambda: self.print_clicked.emit(self.record))
        layout.addWidget(print_btn)
        self.setLayout(layout)
        self.setStyleSheet(
            "QWidget { background-color: #2a2a2a; border: 2px solid #444; border-radius: 10px; margin: 5px; } "
            "QPushButton { background-color: #4CAF50; color: white; border: none; border-radius: 10px; } "
            "QPushButton:hover { background-color: #45a049; }"
        )

    def set_thumbnail_file(self, png_path: str):
        pixmap = QPixmap(png_path)
        if not pixmap.isNull():
            scaled = pixmap.scaled(
                120, 160,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self.thumb_label.setPixmap(scaled)


class EnhancedPrintKioskUI(QMainWindow):
    file_event_signal = pyqtSignal(str)
    thumbnail_ready_signal = pyqtSignal(str, str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Latent Space - Print Service")

        self.records = []
        self.filtered_records = []
        self._items_by_name = {}
        self._display_sig = None
        self._thumb_pool = ThreadPoolExecutor(max_workers=2)

        self.file_event_signal.connect(self.on_new_pdf)
        self.thumbnail_ready_signal.connect(self._on_thumbnail_ready)

        self.inactivity_timer = QTimer(self)
        self.inactivity_timer.setInterval(INACTIVITY_TIMEOUT_SEC * 1000)
        self.inactivity_timer.timeout.connect(self.reset_on_inactivity)
        self.inactivity_timer.start()

        QApplication.instance().installEventFilter(self)

        self.setup_ui()
        self.setup_file_watcher()
        self.refresh_queue()

        self.sync_timer = QTimer(self)
        self.sync_timer.setInterval(10000)
        self.sync_timer.timeout.connect(self.refresh_queue)
        self.sync_timer.start()

    def eventFilter(self, obj, event):
        if event.type() in [QEvent.Type.MouseButtonPress, QEvent.Type.KeyPress, QEvent.Type.TouchBegin, QEvent.Type.Wheel]:
            self.inactivity_timer.start()
        return super().eventFilter(obj, event)

    def reset_on_inactivity(self):
        if self.search_input.text():
            print("Inactivity detected - resetting search")
            self.search_input.clear()

    def setup_ui(self):
        central = self.create_main_screen()
        self.setCentralWidget(central)
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
        search_label = QLabel("Search by date:")
        search_label.setFont(QFont("Arial", 16))
        search_layout.addWidget(search_label)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("e.g., 'today', 'yesterday', 'January 15'")
        self.search_input.textChanged.connect(self.on_search_changed)
        search_layout.addWidget(self.search_input, stretch=1)
        refresh_btn = QPushButton("Refresh")
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

    def setup_file_watcher(self):
        config_manager.DOCS_DIR.mkdir(parents=True, exist_ok=True)
        self.observer = Observer()
        self.observer.schedule(PDFWatcher(lambda p: self.file_event_signal.emit(p)), str(config_manager.DOCS_DIR), recursive=False)
        self.observer.start()

    def refresh_queue(self):
        docs_dir = config_manager.DOCS_DIR
        if not docs_dir.exists():
            return
        entries = []
        for pdf in docs_dir.glob("print_*.pdf"):
            try:
                entries.append((pdf.stat().st_mtime, pdf))
            except FileNotFoundError:
                continue
            except Exception:
                continue
        entries.sort(key=lambda item: item[0], reverse=True)
        self.records = [PrintRecord(pdf, mtime=mtime) for mtime, pdf in entries]
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
                eng_date = _format_english_date(record.timestamp).lower()
                iso_date = record.timestamp.strftime("%Y-%m-%d")
                loc_date = record.timestamp.strftime("%B %d, %Y").lower()
                loc_time = record.timestamp.strftime("%I:%M %p").lower()
                if text == "today" and record.timestamp.date() == now.date():
                    self.filtered_records.append(record)
                elif text == "yesterday" and record.timestamp.date() == (now - timedelta(days=1)).date():
                    self.filtered_records.append(record)
                elif text in eng_date or text in iso_date or text in loc_date or text in loc_time:
                    self.filtered_records.append(record)
        self.update_queue_display()

    def _on_thumbnail_ready(self, filename: str, png_path: str):
        item = self._items_by_name.get(filename)
        if item is not None:
            item.set_thumbnail_file(png_path)

    def _schedule_thumbnail(self, record: PrintRecord):
        if record.path.with_suffix(".png").exists():
            return
        def _worker():
            png = record.ensure_thumbnail_file()
            if png is not None:
                self.thumbnail_ready_signal.emit(record.filename, str(png))
        self._thumb_pool.submit(_worker)

    def update_queue_display(self):
        sig = tuple((r.filename, r.printed) for r in self.filtered_records)
        if sig == self._display_sig:
            return
        self._display_sig = sig
        self._items_by_name.clear()

        while self.queue_layout.count():
            item = self.queue_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        total = len(self.records)
        filtered = len(self.filtered_records)
        if filtered == 0:
            self.queue_info.setText("No drawings found.")
        else:
            self.queue_info.setText(f"Showing {filtered} of {total} drawings")

        for record in self.filtered_records:
            item = PrintQueueItem(record)
            item.print_clicked.connect(self.print_pdf)
            self._items_by_name[record.filename] = item
            self.queue_layout.addWidget(item)
            self._schedule_thumbnail(record)
        self.queue_layout.addStretch()

    def on_new_pdf(self, pdf_path):
        print(f"File Event Detected: {pdf_path}")
        QTimer.singleShot(300, self.refresh_queue)

    def print_pdf(self, record):
        reply = QMessageBox.question(
            self, "Confirm Print", f"Print drawing from {record.timestamp.strftime('%H:%M')}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        try:
            printer_name = validate_printer_name(load_settings().get("printing", {}).get("printer_name", "default") or "default")
        except ValueError as e:
            QMessageBox.critical(self, "Error", f"Invalid printer configuration: {e}")
            return

        res = send_pdf_to_printer(
            str(record.path),
            printer_name=printer_name,
            extra_meta={"user_timestamp": record.timestamp.isoformat()},
        )
        if res.get("status") == "success":
            record.printed = True
            QMessageBox.information(self, "Success", "Print job sent!")
            self.update_queue_display()
        else:
            QMessageBox.critical(self, "Error", f"Print failed: {res.get('error', 'Unknown error')}")

    def closeEvent(self, event):
        if hasattr(self, "observer"):
            self.observer.stop()
        if hasattr(self, "_thumb_pool"):
            self._thumb_pool.shutdown(wait=False)
        event.accept()


def main():
    app = QApplication(sys.argv)
    window = EnhancedPrintKioskUI()
    window.showFullScreen()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
