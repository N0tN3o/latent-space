"""
SYSTEM A: Gaze Painter - Updated for Google Drive
User: kasboek
No systemd dependencies - just run with Python
"""

import cv2
import time
import numpy as np
import os
import json
import sys
from datetime import datetime
from pathlib import Path
import threading

from eye_tracking import EyeTracker
from drawing import GazePainter
from utils import save_calibration, load_calibration
from eye_visualizer import EyeVisualizer

# --- Configuration for kasboek user ---
USER = "kasboek"
SHARED_OUTPUT_DIR = f"/home/{USER}/LatentSpace/shared_output"
DRAW_DURATION_SEC = 15.0
GAZE_LOSS_TIMEOUT_SEC = 2.0

# --- Application States ---
STATE_SEARCHING = 0
STATE_CALIBRATING = 1
STATE_DRAWING = 2
STATE_SAVING = 3

class GazeTrackingPainter:
    def __init__(self, cap_index=0):
        self.cap = cv2.VideoCapture(cap_index)
        if not self.cap.isOpened():
            print(f"Error: Could not open camera {cap_index}")
            print("Trying alternative camera indices...")
            for idx in [1, 2, -1]:
                self.cap = cv2.VideoCapture(idx)
                if self.cap.isOpened():
                    print(f"Camera found at index {idx}")
                    break
            else:
                print("No camera found! Please check connection.")
                exit(1)
            
        self.w = 1280
        self.h = 720
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.w)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.h)
        self.w = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.h = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        
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
        
        # Ensure shared output directory exists
        Path(SHARED_OUTPUT_DIR).mkdir(parents=True, exist_ok=True)
        Path(f"{SHARED_OUTPUT_DIR}/docs").mkdir(parents=True, exist_ok=True)
        
        # Load calibration if exists
        calibration = load_calibration()
        if calibration:
            self.tracker.load_calibration(calibration)
            print("Existing calibration loaded.")
    
    def check_gdrive_mount(self):
        """Check if Google Drive is mounted"""
        try:
            # Simple check - can we write to the directory?
            test_file = Path(SHARED_OUTPUT_DIR) / ".mount_test"
            test_file.touch()
            test_file.unlink()
            return True
        except:
            return False
    
    def wait_for_gdrive(self):
        """Wait for Google Drive to be mounted"""
        print("Checking Google Drive mount...")
        retry_count = 0
        while retry_count < 10:
            if self.check_gdrive_mount():
                print("Google Drive is mounted and accessible!")
                return True
            
            print(f"Google Drive not accessible, waiting... ({retry_count+1}/10)")
            time.sleep(3)
            retry_count += 1
        
        print("WARNING: Google Drive may not be properly mounted!")
        print("Files will be saved locally to:", SHARED_OUTPUT_DIR)
        return False
    
    def get_calibration_progress_text(self):
        """Generate helpful calibration status text with instructions."""
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
    
    def save_and_reset(self):
        """Save drawing to Google Drive shared folder"""
        print("Saving drawing to Google Drive...")
        filepath = Path(SHARED_OUTPUT_DIR) / "image.png"
        # Validate that something was actually drawn before saving
        try:
            if not getattr(self.painter, 'has_drawn', False):
                print('Skipping save: no strokes recorded on canvas')
                # Reset and return without writing
                self.close_canvas()
                self.painter.clear()
                now = time.time()
                if self.last_gaze_time is not None and \
                   (now - self.last_gaze_time) < GAZE_LOSS_TIMEOUT_SEC:
                    print('Gaze present. Starting new drawing session.')
                    self.state = STATE_DRAWING
                    self.draw_start_time = now
                    self.open_canvas()
                else:
                    print('Gaze lost. Returning to search state.')
                    self.state = STATE_SEARCHING
                    self.status_text = 'Searching for gaze...'
                return

            # Safety-net: check for all-white (blank) canvas
            if np.all(self.painter.canvas == 255):
                print('Skipping save: canvas is blank (all white)')
                self.close_canvas()
                self.painter.clear()
                now = time.time()
                if self.last_gaze_time is not None and \
                   (now - self.last_gaze_time) < GAZE_LOSS_TIMEOUT_SEC:
                    print('Gaze present. Starting new drawing session.')
                    self.state = STATE_DRAWING
                    self.draw_start_time = now
                    self.open_canvas()
                else:
                    print('Gaze lost. Returning to search state.')
                    self.state = STATE_SEARCHING
                    self.status_text = 'Searching for gaze...'
                return

            # Save the image
            cv2.imwrite(str(filepath), self.painter.canvas)

            # Try to fsync the file system if available
            try:
                os.sync()
            except Exception:
                pass

            print(f"Drawing saved to {filepath}")
            print("Analysis watcher will process it...")

            # Log for debugging
            if filepath.exists():
                size = filepath.stat().st_size
                print(f"File size: {size} bytes")

        except Exception as e:
            print(f"Error saving file: {e}")
            # Try to save locally as backup inside repository
            backup_path = Path.cwd() / "local_backup"
            backup_path.mkdir(exist_ok=True)
            backup_file = backup_path / f"image_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
            try:
                cv2.imwrite(str(backup_file), self.painter.canvas)
                print(f"Saved backup to: {backup_file}")
            except Exception as e2:
                print(f"Backup save failed: {e2}")
        
        # Reset for next drawing
        self.close_canvas()
        self.painter.clear()
        
        now = time.time()
        if self.last_gaze_time is not None and \
           (now - self.last_gaze_time) < GAZE_LOSS_TIMEOUT_SEC:
            print("Gaze present. Starting new drawing session.")
            self.state = STATE_DRAWING
            self.draw_start_time = now
            self.open_canvas()
        else:
            print("Gaze lost. Returning to search state.")
            self.state = STATE_SEARCHING
            self.status_text = "Searching for gaze..."
    
    def run(self):
        print("="*60)
        print("Gaze Painter for Latent Space Installation")
        print(f"User: {USER}")
        print(f"Output: {SHARED_OUTPUT_DIR}")
        print("="*60)
        print("Controls: [q] Quit")
        print()
        
        # Check Google Drive mount
        self.wait_for_gdrive()
        
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
            
            _, gaze_info, face_results = self.tracker.process_frame(
                frame, 
                return_results=True
            )
            
            if gaze_info is not None:
                self.last_gaze_time = now
            
            norm_gaze = self.tracker.get_calibrated_gaze(gaze_info)
            
            # --- State Machine Logic ---
            
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
                self.status_text = "Saving to Google Drive..."
                self.save_and_reset()
            
            # --- Update Windows ---
            
            eye_display = self.visualizer.create_eye_display(frame, face_results)
            eye_display_with_ui = self.visualizer.add_status_overlay(eye_display, self.status_text)
            cv2.imshow(self.eyes_window, eye_display_with_ui)
            
            if self.state == STATE_DRAWING:
                canvas_display = self.painter.get_display()
                cv2.imshow(self.canvas_window, canvas_display)

            key = cv2.waitKey(1) & 0xFF
            if key == ord('q'):
                self.running = False
        
        self.release()

    def check_gaze_loss(self, current_time):
        gaze_lost = self.last_gaze_time is None or \
                    (current_time - self.last_gaze_time) > GAZE_LOSS_TIMEOUT_SEC
        
        if gaze_lost:
            if self.state == STATE_DRAWING or self.state == STATE_CALIBRATING:
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
    print("Starting Gaze Painter...")
    print("Make sure Google Drive is mounted first!")
    print("Run: python3 start_everything.py")
    print()
    
    app = GazeTrackingPainter(cap_index=0)
    app.run()


if __name__ == '__main__':
    main()