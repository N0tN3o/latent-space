"""
SYSTEM A: Gaze Painter (Kiosk Mode) - MODIFIED with Network Health
- Shows 'Eyes' window as the main UI.
- 'Gaze Painter' canvas window is shown/hidden based on state.
- No UI text is drawn on the 'Gaze Painter' canvas.
- Automatic calibration on gaze detection.
- Gaze loss triggers save and reset.
- Timer expiry triggers save and loop (if gaze present).
- NETWORK HEALTH: Monitors NFS connection, displays errors on failure.
"""

import cv2
import time
import numpy as np
import os
import json
import sys
from datetime import datetime
from pathlib import Path

# Add parent directory to path for imports
sys.path.append(str(Path(__file__).parent.parent / "scripts"))

# Imports for Gaze Painting
from eye_tracking import EyeTracker
from drawing import GazePainter
from utils import save_calibration, load_calibration
from eye_visualizer import EyeVisualizer

# Import Network Health Monitor
try:
    from network_health import NetworkHealth, NetworkStatus
    NETWORK_HEALTH_AVAILABLE = True
except ImportError:
    print("WARNING: network_health module not found. Running without network monitoring.")
    NETWORK_HEALTH_AVAILABLE = False
    NetworkStatus = None

# --- Configuration ---
SHARED_OUTPUT_DIR = "../shared_output"
DRAW_DURATION_SEC = 15.0  # 15-second drawing timer
GAZE_LOSS_TIMEOUT_SEC = 2.0  # Wait 2s after gaze loss before reset

# Network health check interval (frames)
NETWORK_CHECK_INTERVAL = 150  # ~5 seconds at 30fps

# --- Application States ---
STATE_SEARCHING = 0
STATE_CALIBRATING = 1
STATE_DRAWING = 2
STATE_SAVING = 3
STATE_NETWORK_ERROR = 4  # NEW: Network error state

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
        
        # Frame counter for periodic checks
        self.frame_count = 0
        
        # Window names
        self.eyes_window = "Eyes"
        self.canvas_window = "Gaze Painter"
        
        # Ensure shared directory exists
        os.makedirs(SHARED_OUTPUT_DIR, exist_ok=True)
        
        # Load existing calibration if it exists
        calibration = load_calibration()
        if calibration:
            self.tracker.load_calibration(calibration)
            print("Existing calibration loaded.")
        
        # Initialize Network Health Monitor
        if NETWORK_HEALTH_AVAILABLE:
            self.network_health = NetworkHealth(role="jetson_a")
            self.network_status = NetworkStatus.PRIMARY
            print("Network health monitoring enabled")
        else:
            self.network_health = None
            self.network_status = None
    
    def check_network_health(self):
        """
        Check network health status
        Returns True if should continue, False if should stop
        """
        if not NETWORK_HEALTH_AVAILABLE or self.network_health is None:
            return True  # Continue without health checks
        
        try:
            status = self.network_health.check()
            
            # Update status
            old_status = self.network_status
            self.network_status = status
            
            # Handle status changes
            if status == NetworkStatus.STOPPED:
                print("✗ Network failure - stopping operations")
                self.state = STATE_NETWORK_ERROR
                return False
            
            elif status == NetworkStatus.SECONDARY and old_status == NetworkStatus.PRIMARY:
                print("⚠ Network degraded - using SSH fallback")
            
            elif status == NetworkStatus.PRIMARY and old_status != NetworkStatus.PRIMARY:
                print("✓ Network restored")
            
            return True
            
        except Exception as e:
            print(f"Network health check error: {e}")
            return True  # Continue on check error
    
    def get_network_indicator_text(self):
        """Get status text for network indicator overlay"""
        if not NETWORK_HEALTH_AVAILABLE or self.network_status is None:
            return None
        
        if self.network_status == NetworkStatus.PRIMARY:
            return None  # No indicator needed when all is well
        elif self.network_status == NetworkStatus.SECONDARY:
            return "⚠ Connection Slow"
        elif self.network_status == NetworkStatus.DEGRADED:
            return "⚠ Reconnecting..."
        elif self.network_status == NetworkStatus.STOPPED:
            return "✗ Connection Lost"
        
        return None
    
    def draw_network_indicator(self, frame):
        """Draw network status indicator on frame if needed"""
        indicator_text = self.get_network_indicator_text()
        if indicator_text is None:
            return frame
        
        # Get color based on status
        if self.network_health:
            color = self.network_health.get_status_color()
        else:
            color = (128, 128, 128)  # Gray default
        
        # Draw small indicator in corner
        h, w = frame.shape[:2]
        
        # Draw circle indicator
        cv2.circle(frame, (w - 30, 30), 10, color, -1)
        cv2.circle(frame, (w - 30, 30), 10, (255, 255, 255), 2)
        
        # Draw text below if not primary
        if self.network_status != NetworkStatus.PRIMARY:
            font = cv2.FONT_HERSHEY_SIMPLEX
            text_size = cv2.getTextSize(indicator_text, font, 0.5, 1)[0]
            text_x = w - text_size[0] - 10
            text_y = 60
            
            # Background for text
            cv2.rectangle(frame, 
                         (text_x - 5, text_y - 20),
                         (text_x + text_size[0] + 5, text_y + 5),
                         (40, 40, 40), -1)
            
            cv2.putText(frame, indicator_text, (text_x, text_y),
                       font, 0.5, (255, 255, 255), 1)
        
        return frame
    
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
            
            # Increment frame counter
            self.frame_count += 1
            
            # Periodic network health check (every ~5 seconds)
            if self.frame_count % NETWORK_CHECK_INTERVAL == 0:
                if not self.check_network_health():
                    # Network failure detected
                    break
            
            # --- Process Frame ---
            _, gaze_info, face_results = self.tracker.process_frame(
                frame, 
                return_results=True
            )
            
            # Update gaze presence timer
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
                    self.status_text = "Calibrating... Look at the camera."

            elif self.state == STATE_CALIBRATING:
                if self.check_gaze_loss(now):
                    continue
                    
                self.status_text = f"Calibrating... {len(self.tracker.calibration_samples)}/{self.tracker.calibration_needed}"
                
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
                # Network error state - display error and wait
                self.status_text = "Connection Lost"
                if self.network_health:
                    self.network_health.display_error()
                self.running = False
                break
            
            # --- Update Windows ---
            
            # Update and show the "Eyes" window
            eye_display = self.visualizer.create_eye_display(frame, face_results)
            eye_display_with_ui = self.visualizer.add_status_overlay(eye_display, self.status_text)
            
            # Add network indicator to eyes window
            eye_display_with_ui = self.draw_network_indicator(eye_display_with_ui)
            
            cv2.imshow(self.eyes_window, eye_display_with_ui)
            
            # Update the "Gaze Painter" window (only if drawing)
            if self.state == STATE_DRAWING:
                canvas_display = self.painter.get_display()
                
                # Add small network indicator to canvas too
                canvas_display = self.draw_network_indicator(canvas_display)
                
                cv2.imshow(self.canvas_window, canvas_display)

            # --- Key Handling ---
            key = cv2.waitKey(1) & 0xFF
            if key == ord('q'):
                self.running = False
        
        self.release()

    def check_gaze_loss(self, current_time):
        """Check if gaze has been lost for too long"""
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
        """Save the canvas and decide what to do next"""
        print("Saving drawing...")
        filepath = os.path.join(SHARED_OUTPUT_DIR, "image.png")
        cv2.imwrite(filepath, self.painter.canvas)
        print(f"Drawing saved to {filepath}. Watcher will process it.")
        
        self.close_canvas()
        self.painter.clear()
        
        # Check if gaze is still present
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
        """Show the canvas window"""
        try:
            cv2.namedWindow(self.canvas_window)
            canvas_display = self.painter.get_display()
            cv2.imshow(self.canvas_window, canvas_display)
        except Exception as e:
            print(f"Error opening canvas: {e}")

    def close_canvas(self):
        """Close the canvas window safely"""
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