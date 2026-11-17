import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent
for p in (str(PROJECT_ROOT), str(CURRENT_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import config_manager
from config_manager import ensure_directories, load_settings
from drawing import GazePainter
from eye_tracking import EyeTracker
from eye_visualizer import EyeVisualizer
from utils import load_calibration

DRAW_DURATION_SEC = 15.0
GAZE_LOSS_TIMEOUT_SEC = 2.0
CONSECUTIVE_FACE_FRAMES = 5

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
        self.face_streak = 0
        self.frame_count = 0

        self.eyes_window = "Eyes"
        self.canvas_window = "Gaze Painter"

        calibration = load_calibration()
        if calibration:
            self.tracker.load_calibration(calibration)
        self.has_calibration = False

    def get_calibration_percentage(self) -> int:
        return int(round(self.tracker.get_calibration_progress()))

    def get_calibration_progress_text(self):
        percentage = self.get_calibration_percentage()

        if percentage <= 33:
            instruction = "Look at the center target"
        elif percentage < 75:
            instruction = "Follow the moving target"
        else:
            instruction = "Almost done..."

        return f"Calibrating {percentage}% - {instruction}"

    def _upload_to_remote(self, png_bytes: bytes, dwell_grid=None) -> bool:
        if not self.remote_url:
            return False
        try:
            import requests
            url = f"{self.remote_url.rstrip('/')}/api/drawings"
            files = {"file": ("image.png", png_bytes, "image/png")}
            if dwell_grid is not None:
                files["dwell_grid"] = ("dwell.json", json.dumps(dwell_grid).encode("utf-8"), "application/json")
            resp = requests.post(url, files=files, timeout=15)
            if resp.ok:
                print(f"Uploaded drawing to remote server: {url}")
                return True
            print(f"Remote upload returned {resp.status_code}: {resp.text}")
            return False
        except Exception as e:
            print(f"Warning: could not upload to remote server ({self.remote_url}): {e}")
            return False

    def save_and_reset(self):
        """Save drawing atomically to shared_output/image.png or upload via HTTP (never both to avoid double runs)."""
        print("Saving drawing...")
        filepath = config_manager.SHARED_DIR / "image.png"
        tmp_path = config_manager.SHARED_DIR / "image.tmp.png"
        try:
            if not getattr(self.painter, "has_drawn", False) or np.all(self.painter.canvas == 255):
                print("Skipping save: canvas is blank")
                self._reset_after_save()
                return

            ok, encoded = cv2.imencode(".png", self.painter.canvas)
            if not ok:
                raise RuntimeError("Failed to encode drawing canvas as PNG")
            png_bytes = encoded.tobytes()
            dwell_list = self.painter.dwell_grid.tolist() if hasattr(self.painter, "dwell_grid") else None

            if self.remote_url and self._upload_to_remote(png_bytes, dwell_grid=dwell_list):
                self._reset_after_save()
                return

            tmp_path.write_bytes(png_bytes)
            os.replace(str(tmp_path), str(filepath))
            print(f"Drawing saved to {filepath} ({filepath.stat().st_size} bytes)")

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

    def transition_to(self, new_state: int):
        """Explicit state machine transition handling all side effects in one place."""
        self.state = new_state
        if new_state == STATE_SEARCHING:
            self.has_calibration = False
            self.face_streak = 0
            self.tracker.is_calibrating = False
            self.tracker.reset_visitor_state()
            self.close_canvas()
            self.status_text = "Searching for gaze..."
        elif new_state == STATE_CALIBRATING:
            self.has_calibration = False
            self.painter.clear()
            self.tracker.start_auto_calibration()
            self.open_canvas()
            self.status_text = self.get_calibration_progress_text()
        elif new_state == STATE_DRAWING:
            self.has_calibration = True
            self.painter.clear()
            self.draw_start_time = time.time()
            self.open_canvas()
        elif new_state == STATE_SAVING:
            self.status_text = "Saving drawing..."

    def _reset_after_save(self):
        self.close_canvas()
        self.painter.clear()
        print("Session saved. Returning to search/calibration state for next session.")
        self.transition_to(STATE_SEARCHING)

    def run(self):
        print("=" * 60)
        print("Gaze Painter for Latent Space Installation")
        print(f"Output Directory: {config_manager.SHARED_DIR}")
        if self.remote_url:
            print(f"Remote Sync URL:  {self.remote_url}")
        print("=" * 60)
        print("Controls: [q] Quit | [Space] Center Cursor Here | [c] Recalibrate | [m] Toggle Hybrid/Flow")
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
                self.face_streak += 1
            else:
                self.face_streak = 0

            norm_gaze = self.tracker.get_calibrated_gaze(gaze_info, dt=self.dt)

            if self.state == STATE_SEARCHING:
                self.status_text = "Searching for gaze..."
                if self.face_streak >= CONSECUTIVE_FACE_FRAMES:
                    self.transition_to(STATE_CALIBRATING)

            elif self.state == STATE_CALIBRATING:
                if not self.check_gaze_loss(now):
                    self.status_text = self.get_calibration_progress_text()
                    calibration_done = self.tracker.add_calibration_sample(gaze_info, now=now)
                    if calibration_done:
                        self.transition_to(STATE_DRAWING)

            elif self.state == STATE_DRAWING:
                if not self.check_gaze_loss(now):
                    elapsed = now - self.draw_start_time
                    remaining = DRAW_DURATION_SEC - elapsed
                    mode_tag = self.tracker.control_mode.upper()
                    self.status_text = f"Drawing ({mode_tag})... {remaining:.1f}s"

                    if not (gaze_info and gaze_info.get("is_blinking", False)):
                        self.painter.update(norm_gaze, self.dt)

                    if remaining <= 0:
                        self.transition_to(STATE_SAVING)

            # Execute STATE_SAVING immediately in the same frame iteration (no delayed save/skip)
            if self.state == STATE_SAVING:
                self.status_text = "Saving drawing..."
                self.save_and_reset()

            eye_display = self.visualizer.create_eye_display(frame, face_results)
            eye_display_with_ui = self.visualizer.add_status_overlay(eye_display, self.status_text)
            cv2.imshow(self.eyes_window, eye_display_with_ui)

            if self.state == STATE_CALIBRATING:
                calib_display = self.painter.get_calibration_canvas(self.get_calibration_percentage())
                cv2.imshow(self.canvas_window, calib_display)
            elif self.state == STATE_DRAWING:
                canvas_display = self.painter.get_canvas_with_cursor(norm_gaze)
                cv2.imshow(self.canvas_window, canvas_display)

            key = cv2.waitKey(1) & 0xFF
            if key == ord("q"):
                self.running = False
            elif key == ord("c"):
                if self.state != STATE_SAVING:
                    print("Manual recalibration triggered.")
                    self.transition_to(STATE_CALIBRATING)
            elif key == ord("m"):
                self.tracker.toggle_control_mode()
            elif key == ord(" ") and self.state == STATE_DRAWING:
                self.tracker.recenter_now()
                self.painter.prev_point = None

        self.release()

    def check_gaze_loss(self, current_time):
        gaze_lost = self.last_gaze_time is None or (current_time - self.last_gaze_time) > GAZE_LOSS_TIMEOUT_SEC
        if gaze_lost:
            if self.state == STATE_CALIBRATING:
                print("Gaze lost during calibration. Returning to search state.")
                self.transition_to(STATE_SEARCHING)
                return True
            elif self.state == STATE_DRAWING:
                print("Gaze lost during drawing. Saving and resetting.")
                self.transition_to(STATE_SAVING)
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
