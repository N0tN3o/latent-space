"""
SYSTEM A: Gaze Painter (Kiosk Mode) - MODIFIED with Full Canvas Calibration
- Shows 'Eyes' window as the main UI.
- 'Gaze Painter' canvas window is shown/hidden based on state.
- Automatic calibration captures full eye movement range.
- Gaze loss triggers save and reset.
- Timer expiry triggers save and loop (if gaze present).
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

# --- Configuration ---
SHARED_OUTPUT_DIR = "../shared_output"
DRAW_DURATION_SEC = 15.0
GAZE_LOSS_TIMEOUT_SEC = 2.0

# --- Application States ---
STATE_SEARCHING = 0
STATE_CALIBRATING = 1
STATE_DRAWING = 2
STATE_SAVING = 3
STATE_NETWORK_ERROR = 4

class GazeTrackingPainter:
    def __init__(self, cap_index=0):
        self.cap = cv2.VideoCapture(cap_index)
        if not self.cap.isOpened():
            print(f"Error: Could not open camera {cap_index}")
            exit()
            
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
        
        os.makedirs(SHARED_OUTPUT_DIR, exist_ok=True)
        
        calibration = load_calibration()
        if calibration:
            self.tracker.load_calibration(calibration)
            print("Existing calibration loaded.")
    
    def get_calibration_progress_text(self):
        """Generate helpful calibration status text with instructions."""
        progress = len(self.tracker.calibration_samples)
        total = self.tracker.calibration_needed
        percentage = int((progress / total) * 100)
        
        # Different messages based on progress
        if percentage < 33:
            instruction = "Move your eyes around the screen"
        elif percentage < 66:
            instruction = "Look at all corners"
        else:
            instruction = "Almost done..."
        
        return f"Calibrating {percentage}% - {instruction}"
    
    def run(self):
        print("Starting Gaze Painter (System A - Kiosk Mode)...")
        print("Controls: [q] Quit")
        cv2.namedWindow(self.eyes_window)

        while self.running:
            ret, frame = self.cap.read()
            if not ret:
                break
                
            frame = cv2.flip(frame, 1)
            
            now = time.time()
            self.dt = now - self.last_time
            self.last_time = now
            self.frame_count += 1
            
            # network health polling now runs in a background thread; avoid blocking here
            if self.frame_count % 30 == 0:
                pass
            
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
                self.status_text = "Saving..."
                self.save_and_reset()
            
            elif self.state == STATE_NETWORK_ERROR:
                self.status_text = "Connection Lost"
                if self.network_health:
                    self.network_health.display_error()
                self.running = False
                break
            
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
            elif self.state == STATE_SEARCHING:
                return False
        return False
        
    def save_and_reset(self):
        print("Saving drawing...")
        filepath = os.path.join(SHARED_OUTPUT_DIR, "image.png")
        cv2.imwrite(filepath, self.painter.canvas)
        print(f"Drawing saved to {filepath}. Watcher will process it.")
        
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

if __name__ == '__main__':
    app = GazeTrackingPainter(cap_index=0)
    app.run()