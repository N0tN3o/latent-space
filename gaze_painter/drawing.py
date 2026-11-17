import numpy as np
import cv2
import time


class GazePainter:
    def __init__(self, width=1280, height=720, dwell_radius=0.03):
        """
        Initialize the gaze painter with a canvas
        """
        self.w = width
        self.h = height
        self.canvas = np.ones((self.h, self.w, 3), dtype=np.uint8) * 255
        self.prev_point = None
        self.prev_time = None
        self.dwell_time = 0.0
        self.dwell_radius = dwell_radius
        self.stroke_color = (0, 0, 0)  # Black in BGR
        self.stroke_history = []  # Store stroke points for smoother lines
        self.has_drawn = False
        self.dwell_grid = np.zeros((3, 3), dtype=np.float32)

    def clear(self):
        """Clear the canvas"""
        self.canvas[:] = 255
        self.prev_point = None
        self.prev_time = None
        self.dwell_time = 0.0
        self.stroke_history = []
        self.has_drawn = False
        self.dwell_grid.fill(0.0)

    def update(self, gaze_norm, dt):
        """
        Update the drawing based on normalized gaze position.
        gaze_norm should be (x, y) in [0, 1] range
        """
        if gaze_norm is None:
            self.prev_point = None
            self.dwell_time = 0.0
            return
        
        # Map normalized coordinates to canvas pixels
        # The gaze_norm should already be in [0, 1] range from tracking
        gx = int(np.clip(gaze_norm[0], 0, 1) * self.w)
        gy = int(np.clip(gaze_norm[1], 0, 1) * self.h)
        cur_point = (gx, gy)

        if dt and dt > 0:
            col = min(2, max(0, int(np.clip(gaze_norm[0], 0.0, 0.9999) * 3)))
            row = min(2, max(0, int(np.clip(gaze_norm[1], 0.0, 0.9999) * 3)))
            self.dwell_grid[row, col] += float(min(dt, 0.25))
        
        # Initialize on first point
        if self.prev_point is None:
            self.prev_point = cur_point
            self.prev_time = time.time()
            self.dwell_time = 0.0
            self.stroke_history = [cur_point]
            return
        
        # Calculate distance in normalized space
        dx = (cur_point[0] - self.prev_point[0]) / float(self.w)
        dy = (cur_point[1] - self.prev_point[1]) / float(self.h)
        dist = np.hypot(dx, dy)
        
        # Update dwell time
        if dist < self.dwell_radius:
            self.dwell_time += dt
        else:
            self.dwell_time = 0.0
        
        # Calculate line width based on dwell time and movement speed
        base_width = 2
        dwell_bonus = int(min(20, self.dwell_time * 10))
        speed_factor = max(0.2, min(1.0, dist * 50))  # Slower = thicker
        line_width = int((base_width + dwell_bonus) * (2.0 - speed_factor))
        line_width = max(1, min(40, line_width))
        
        # Add current point to history for smoothing
        self.stroke_history.append(cur_point)
        if len(self.stroke_history) > 5:
            self.stroke_history.pop(0)
        
        # Draw with smoothing if we have enough history
        if len(self.stroke_history) >= 2:
            # Use polylines for smoother curves
            pts = np.array(self.stroke_history, np.int32)
            if len(pts) >= 2:
                for i in range(len(pts) - 1):
                    # Vary color slightly based on speed
                    color_var = int(255 * speed_factor)
                    color = (max(0, self.stroke_color[0] - color_var//3),
                            self.stroke_color[1],
                            min(255, self.stroke_color[2] + color_var//3))
                    cv2.line(self.canvas, tuple(pts[i]), tuple(pts[i+1]), color, line_width)
                # Mark that we've drawn something
                self.has_drawn = True
        else:
            # Simple line for initial points
            cv2.line(self.canvas, self.prev_point, cur_point, self.stroke_color, line_width)
            self.has_drawn = True
        
        # Draw a small circle at current position for visual feedback
        cv2.circle(self.canvas, cur_point, max(1, line_width//2), self.stroke_color, -1)
        self.has_drawn = True
        
        self.prev_point = cur_point
    
    def get_display(self):
        """Return the drawing canvas image."""
        return self.canvas
    
    def set_color(self, color):
        """Set stroke color (BGR tuple)"""
        self.stroke_color = color
    
    def get_canvas_with_cursor(self, gaze_norm):
        """
        Get canvas with a cursor indicator at current gaze position
        """
        display = self.canvas.copy()
        
        if gaze_norm is not None:
            # Draw cursor at gaze position
            gx = int(np.clip(gaze_norm[0], 0, 1) * self.w)
            gy = int(np.clip(gaze_norm[1], 0, 1) * self.h)
            
            # Draw crosshair cursor
            cursor_size = 20
            cursor_color = (0, 255, 0)  # Green
            cv2.line(display, (gx - cursor_size, gy), (gx + cursor_size, gy), cursor_color, 1)
            cv2.line(display, (gx, gy - cursor_size), (gx, gy + cursor_size), cursor_color, 1)
            cv2.circle(display, (gx, gy), 8, cursor_color, 1)
        
        return display

    def get_calibration_canvas(self, progress_pct: int):
        """
        Render an interactive guided calibration screen on a clean white canvas.
        0-35%: Look at the center bullseye target to anchor resting gaze.
        35-100%: Follow the orbiting target bullseye around the canvas.
        """
        display = np.ones((self.h, self.w, 3), dtype=np.uint8) * 255
        pct = int(np.clip(progress_pct, 0, 100))

        if pct <= 33:
            tx = int(self.w * 0.5)
            ty = int(self.h * 0.5)
            instruction = "Step 1/2: Look steadily at the center target"
        else:
            angle = ((pct - 33) / 67.0) * (2.0 * np.pi)
            tx = int(self.w * (0.5 + 0.36 * np.cos(angle)))
            ty = int(self.h * (0.5 + 0.34 * np.sin(angle)))
            instruction = "Step 2/2: Follow the moving target with your eyes"

        # Draw subtle guide ring when orbiting
        if pct > 33:
            axes = (int(self.w * 0.36), int(self.h * 0.34))
            cv2.ellipse(display, (self.w // 2, self.h // 2), axes, 0, 0, 360, (230, 230, 230), 1, cv2.LINE_AA)

        # Target bullseye (emerald green in BGR: 129, 185, 16)
        target_color = (129, 185, 16)
        cv2.circle(display, (tx, ty), 28, target_color, 3, cv2.LINE_AA)
        cv2.circle(display, (tx, ty), 8, target_color, -1, cv2.LINE_AA)
        cv2.line(display, (tx - 38, ty), (tx + 38, ty), target_color, 1, cv2.LINE_AA)
        cv2.line(display, (tx, ty - 38), (tx, ty + 38), target_color, 1, cv2.LINE_AA)

        # Progress bar at bottom
        bar_w = int(self.w * 0.5)
        bar_h = 10
        bar_x = (self.w - bar_w) // 2
        bar_y = self.h - 55
        cv2.rectangle(display, (bar_x, bar_y), (bar_x + bar_w, bar_y + bar_h), (220, 220, 220), -1)
        fill_w = int(bar_w * (pct / 100.0))
        if fill_w > 0:
            cv2.rectangle(display, (bar_x, bar_y), (bar_x + fill_w, bar_y + bar_h), target_color, -1)

        # Instruction text
        label = f"Calibrating Gaze ({pct}%) - {instruction}"
        (tw, _), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.75, 2)
        cv2.putText(
            display,
            label,
            ((self.w - tw) // 2, bar_y - 16),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.75,
            (40, 40, 40),
            2,
            cv2.LINE_AA,
        )

        return display