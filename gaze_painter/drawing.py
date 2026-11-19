import cv2
import numpy as np


BRUSH_THEMES = ("classic", "ember", "indigo")


class GazePainter:
    def __init__(self, width=1280, height=720, dwell_radius=0.03, theme="classic"):
        """Initialize the gaze painter with a blank canvas and 3x3 dwell tracker."""
        self.w = width
        self.h = height
        self.canvas = np.ones((self.h, self.w, 3), dtype=np.uint8) * 255
        self.prev_point = None
        self.dwell_time = 0.0
        self.dwell_radius = dwell_radius
        self.stroke_color = (0, 0, 0)  # Black in BGR
        self.theme = theme if theme in BRUSH_THEMES else "classic"
        self.has_drawn = False
        self.dwell_grid = np.zeros((3, 3), dtype=np.float32)

    def cycle_theme(self) -> str:
        """Cycle through Classic Obsidian, Velocity Ember, and Midnight Indigo brush styles."""
        idx = BRUSH_THEMES.index(self.theme) if self.theme in BRUSH_THEMES else 0
        self.theme = BRUSH_THEMES[(idx + 1) % len(BRUSH_THEMES)]
        print(f"Brush ink theme switched to: {self.theme.upper()}")
        return self.theme

    def clear(self):
        """Clear the canvas and reset stroke/dwell state."""
        self.canvas[:] = 255
        self.prev_point = None
        self.dwell_time = 0.0
        self.has_drawn = False
        self.dwell_grid.fill(0.0)

    def update(self, gaze_norm, dt):
        """Update the drawing based on normalized gaze position (x, y) in [0, 1]."""
        if gaze_norm is None:
            self.prev_point = None
            self.dwell_time = 0.0
            return

        gx = int(np.clip(gaze_norm[0], 0, 1) * self.w)
        gy = int(np.clip(gaze_norm[1], 0, 1) * self.h)
        cur_point = (gx, gy)

        if dt and dt > 0:
            col = min(2, max(0, int(np.clip(gaze_norm[0], 0.0, 0.9999) * 3)))
            row = min(2, max(0, int(np.clip(gaze_norm[1], 0.0, 0.9999) * 3)))
            self.dwell_grid[row, col] += float(min(dt, 0.25))

        if self.prev_point is None:
            self.prev_point = cur_point
            self.dwell_time = 0.0
            return

        dx = (cur_point[0] - self.prev_point[0]) / float(self.w)
        dy = (cur_point[1] - self.prev_point[1]) / float(self.h)
        dist = float(np.hypot(dx, dy))

        if dist < self.dwell_radius:
            self.dwell_time += dt
        else:
            self.dwell_time = 0.0

        base_width = 2
        dwell_bonus = int(min(20, self.dwell_time * 10))
        speed_factor = max(0.2, min(1.0, dist * 50))  # Slower = thicker
        line_width = int((base_width + dwell_bonus) * (2.0 - speed_factor))
        line_width = max(1, min(40, line_width))

        color_var = int(255 * speed_factor)
        if self.theme == "ember":
            # BGR: deep charcoal -> crimson -> warm amber with speed
            r = min(230, 50 + int(180 * speed_factor))
            g = min(140, 20 + int(110 * speed_factor))
            b = max(10, 45 - int(35 * speed_factor))
            color = (b, g, r)
        elif self.theme == "indigo":
            # BGR: midnight indigo -> cobalt blue with speed
            r = min(100, 15 + int(65 * speed_factor))
            g = min(140, 25 + int(90 * speed_factor))
            b = min(230, 80 + int(145 * speed_factor))
            color = (b, g, r)
        else:
            color = (
                max(0, self.stroke_color[0] - color_var // 3),
                self.stroke_color[1],
                min(255, self.stroke_color[2] + color_var // 3),
            )
        cv2.line(self.canvas, self.prev_point, cur_point, color, line_width, cv2.LINE_AA)
        cv2.circle(self.canvas, cur_point, max(1, line_width // 2), color, -1, cv2.LINE_AA)
        self.has_drawn = True
        self.prev_point = cur_point

    def get_display(self):
        """Return the drawing canvas image."""
        return self.canvas

    def get_canvas_with_cursor(self, gaze_norm, mode_label: str = ""):
        """Get canvas with a cursor indicator and subtle HUD hint at current gaze position."""
        display = self.canvas.copy()
        if gaze_norm is not None:
            gx = int(np.clip(gaze_norm[0], 0, 1) * self.w)
            gy = int(np.clip(gaze_norm[1], 0, 1) * self.h)
            cursor_size = 20
            cursor_color = (246, 130, 59) if mode_label.upper() == "FLOW" else (0, 200, 80)
            cv2.line(display, (gx - cursor_size, gy), (gx + cursor_size, gy), cursor_color, 1)
            cv2.line(display, (gx, gy - cursor_size), (gx, gy + cursor_size), cursor_color, 1)
            cv2.circle(display, (gx, gy), 8, cursor_color, 1)
        hud = f"Mode: {(mode_label or 'DIRECT').upper()} [m]  |  Brush: {self.theme.upper()} [b]  |  [Space] Center  |  [c] Recalibrate"
        cv2.putText(display, hud, (16, self.h - 14), cv2.FONT_HERSHEY_SIMPLEX, 0.52, (120, 120, 120), 1, cv2.LINE_AA)
        return display

    def get_calibration_canvas(self, progress_pct: int):
        """
        Render an interactive guided calibration screen on a clean white canvas.
        0-33%: Look at the center bullseye target to anchor resting gaze.
        34-100%: Follow the orbiting target bullseye around the canvas.
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

        if pct > 33:
            axes = (int(self.w * 0.36), int(self.h * 0.34))
            cv2.ellipse(display, (self.w // 2, self.h // 2), axes, 0, 0, 360, (230, 230, 230), 1, cv2.LINE_AA)

        target_color = (129, 185, 16)
        cv2.circle(display, (tx, ty), 28, target_color, 3, cv2.LINE_AA)
        cv2.circle(display, (tx, ty), 8, target_color, -1, cv2.LINE_AA)
        cv2.line(display, (tx - 38, ty), (tx + 38, ty), target_color, 1, cv2.LINE_AA)
        cv2.line(display, (tx, ty - 38), (tx, ty + 38), target_color, 1, cv2.LINE_AA)

        bar_w = int(self.w * 0.5)
        bar_h = 10
        bar_x = (self.w - bar_w) // 2
        bar_y = self.h - 55
        cv2.rectangle(display, (bar_x, bar_y), (bar_x + bar_w, bar_y + bar_h), (220, 220, 220), -1)
        fill_w = int(bar_w * (pct / 100.0))
        if fill_w > 0:
            cv2.rectangle(display, (bar_x, bar_y), (bar_x + fill_w, bar_y + bar_h), target_color, -1)

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
