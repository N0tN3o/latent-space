import cv2
import time
import numpy as np
import os
import json
import sys
from datetime import datetime
from pathlib import Path

CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent
for p in (str(PROJECT_ROOT), str(CURRENT_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from config_manager import SHARED_DIR, DOCS_DIR, load_settings, ensure_directories
from eye_tracking import EyeTracker
from drawing import GazePainter
from utils import save_calibration, load_calibration
from eye_visualizer import EyeVisualizer

DRAW_DURATION_SEC = 15.0
GAZE_LOSS_TIMEOUT_SEC = 2.0

# --- Application States ---
STATE_SEARCHING = 0
STATE_CALIBRATING = 1
STATE_DRAWING = 2
STATE_SAVING = 3


class GazeTrackingPainter:
    def __init__(self, cap_index=0, remote_url=None):
        ensure_directories()
        self.remote_url = remote_url or os.environ.get("LATENT_SPACE_SERVER_URL", "")
        if not self.remote_url:
            settings = load_settings()
            self.remote_url = settings.get("server", {}).get("remote_server_url", "")

        self.cap = cv2.VideoCapture(cap_index)
        if not self.cap.isOpened():
            print(f"Could not open camera {cap_index}, trying alternative camera indices...")
            for idx in [1, 2, -1]:
                self.cap = cv2.VideoCapture(idx)
                if self.cap.isOpened():
                    print(f"Camera found at index {idx}")
                    break
            else:
                print("No camera found! Please check webcam connection or use the Browser Gaze Painter at /draw.")
                sys.exit(1)

        self.w = 1280
        self.h = 720
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.w)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.h)
        self.w = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 1280
        self.h = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 720

        self.tracker = EyeTracker(flip=True)
        self.painter = GazePainter(width=self.w, height=self.h)
        self.visualizer = EyeVisualizer()

        self.running = True
        self.state = STATE_SEARCHING
        self.status_text = "Searching for gaze..."

        self.last_time = time.time()
        self.dt = 0
        self.draw_start_time = None
        self.last_gaze_time = None
        self.frame_count = 0

        self.eyes_window = "Eyes"
        self.canvas_window = "Gaze Painter"

        calibration = load_calibration()
        if calibration:
            self.tracker.load_calibration(calibration)
            print("Existing calibration loaded.")

    def get_calibration_progress_text(self):
        progress = len(self.tracker.calibration_samples)
        total = self.tracker.calibration_needed
        percentage = int((progress / total) * 100)

        if percentage < 33:
            instruction = "Move your eyes around the screen"
        elif percentage < 66:
            instruction = "Look at all corners"
        else:
            instruction = "Almost done..."

        return f"Calibrating {percentage}% - {instruction}"

    def _upload_to_remote(self, filepath: Path):
        if not self.remote_url:
            return
        try:
            import requests
            url = f"{self.remote_url.rstrip('/')}/api/drawings"
            with open(filepath, "rb") as f:
                resp = requests.post(url, files={"file": ("image.png", f, "image/png")}, timeout=15)
            if resp.ok:
                print(f"Uploaded drawing to remote server: {url}")
            else:
                print(f"Remote upload returned {resp.status_code}: {resp.text}")
        except Exception as e:
            print(f"Warning: could not upload to remote server ({self.remote_url}): {e}")

    def save_and_reset(self):
        """Save drawing locally to shared_output/image.png (and optionally upload via HTTP)."""
        print("Saving drawing...")
        filepath = SHARED_DIR / "image.png"
        try:
            if not getattr(self.painter, "has_drawn", False) or np.all(self.painter.canvas == 255):
                print("Skipping save: canvas is blank")
                self._reset_after_save()
                return

            cv2.imwrite(str(filepath), self.painter.canvas)
            print(f"Drawing saved to {filepath} ({filepath.stat().st_size} bytes)")
            self._upload_to_remote(filepath)

        except Exception as e:
            print(f"Error saving file: {e}")
            backup_path = CURRENT_DIR / "local_backup"
            backup_path.mkdir(exist_ok=True)
            backup_file = backup_path / f"image_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
            try:
                cv2.imwrite(str(backup_file), self.painter.canvas)
                print(f"Saved backup to: {backup_file}")
            except Exception as e2:
                print(f"Backup save failed: {e2}")

        self._reset_after_save()

    def _reset_after_save(self):
        self.close_canvas()
        self.painter.clear()
        now = time.time()
        if self.last_gaze_time is not None and (now - self.last_gaze_time) < GAZE_LOSS_TIMEOUT_SEC:
            print("Gaze present. Starting new drawing session.")
            self.state = STATE_DRAWING
            self.draw_start_time = now
            self.open_canvas()
        else:
            print("Gaze lost. Returning to search state.")
            self.state = STATE_SEARCHING
            self.status_text = "Searching for gaze..."

    def run(self):
        print("=" * 60)
        print("Gaze Painter for Latent Space Installation")
        print(f"Output Directory: {SHARED_DIR}")
        if self.remote_url:
            print(f"Remote Sync URL:  {self.remote_url}")
        print("=" * 60)
        print("Controls: [q] Quit")
        print()

        cv2.namedWindow(self.eyes_window)

        while self.running:
            ret, frame = self.cap.read()
            if not ret:
                print("Failed to read from camera")
                break

            frame = cv2.flip(frame, 1)

            now = time.time()
            self.dt = now - self.last_time
            self.last_time = now
            self.frame_count += 1

            _, gaze_info, face_results = self.tracker.process_frame(frame, return_results=True)

            if gaze_info is not None:
                self.last_gaze_time = now

            norm_gaze = self.tracker.get_calibrated_gaze(gaze_info)

            if self.state == STATE_SEARCHING:
                self.status_text = "Searching for gaze..."
                self.close_canvas()

                if gaze_info is not None:
                    self.state = STATE_CALIBRATING
                    self.tracker.start_auto_calibration()
                    self.status_text = self.get_calibration_progress_text()

            elif self.state == STATE_CALIBRATING:
                if self.check_gaze_loss(now):
                    continue

                self.status_text = self.get_calibration_progress_text()
                calibration_done = self.tracker.add_calibration_sample(gaze_info)

                if calibration_done:
                    self.state = STATE_DRAWING
                    self.draw_start_time = now
                    self.painter.clear()
                    self.open_canvas()

            elif self.state == STATE_DRAWING:
                if self.check_gaze_loss(now):
                    continue

                elapsed = now - self.draw_start_time
                remaining = DRAW_DURATION_SEC - elapsed
                self.status_text = f"Drawing... {remaining:.1f}s"

                self.painter.update(norm_gaze, self.dt)

                if remaining <= 0:
                    self.state = STATE_SAVING
                    self.status_text = "Saving..."

            elif self.state == STATE_SAVING:
                self.status_text = "Saving drawing..."
                self.save_and_reset()

            eye_display = self.visualizer.create_eye_display(frame, face_results)
            eye_display_with_ui = self.visualizer.add_status_overlay(eye_display, self.status_text)
            cv2.imshow(self.eyes_window, eye_display_with_ui)

            if self.state == STATE_DRAWING:
                canvas_display = self.painter.get_display()
                cv2.imshow(self.canvas_window, canvas_display)

            key = cv2.waitKey(1) & 0xFF
            if key == ord("q"):
                self.running = False

        self.release()

    def check_gaze_loss(self, current_time):
        gaze_lost = self.last_gaze_time is None or (current_time - self.last_gaze_time) > GAZE_LOSS_TIMEOUT_SEC
        if gaze_lost and self.state in (STATE_DRAWING, STATE_CALIBRATING):
            print("Gaze lost. Saving and resetting.")
            self.state = STATE_SAVING
            self.status_text = "Saving..."
            return True
        return False

    def open_canvas(self):
        try:
            cv2.namedWindow(self.canvas_window)
            canvas_display = self.painter.get_display()
            cv2.imshow(self.canvas_window, canvas_display)
        except Exception as e:
            print(f"Error opening canvas: {e}")

    def close_canvas(self):
        try:
            cv2.destroyWindow(self.canvas_window)
        except cv2.error:
            pass

    def release(self):
        self.running = False
        self.cap.release()
        self.tracker.release()
        self.visualizer.release()
        cv2.destroyAllWindows()


def main():
    app = GazeTrackingPainter(cap_index=0)
    app.run()


if __name__ == "__main__":
    main()
