import cv2
import time
import numpy as np
from collections import deque

try:
    import mediapipe as mp
except Exception as e:
    raise ImportError("mediapipe is required. Install with: pip install mediapipe") from e

# --- Constants ---
FACEMESH_LANDMARKS = {
    "left_eye_outer": 33,
    "left_eye_inner": 133,
    "left_eye_top": 159,
    "left_eye_bottom": 145,
    "right_eye_outer": 362,
    "right_eye_inner": 263,
    "right_eye_top": 386,
    "right_eye_bottom": 374,
    "nose_bridge": 168,
    "nose_tip": 1,
    "left_iris": [468, 469, 470, 471],
    "right_iris": [473, 474, 475, 476],
}

DEFAULT_HORIZ_THRESHOLD = 0.035
DEFAULT_VERT_THRESHOLD = 0.028
DEADZONE = 0.018
SACCADE_SPEED_THR = 0.25
SMOOTH_ALPHA = 0.55
ROLLING_BUFFER = 7
BLINK_EAR_THRESHOLD = 0.17  # Eye Aspect Ratio below which a blink is occurring
HEAD_POSE_WEIGHT = 0.28     # Blend factor for subtle head-orientation assist


def nplm_to_xy(lm, idx, w, h):
    p = lm[idx]
    return np.array([p.x * w, p.y * h], dtype=np.float32)


def center_of(points):
    arr = np.array(points, dtype=np.float32)
    return arr.mean(axis=0)


def eye_bbox(landmarks_pts):
    arr = np.array(landmarks_pts, dtype=np.float32)
    x_min, y_min = arr.min(axis=0)
    x_max, y_max = arr.max(axis=0)
    return (x_min, y_min, x_max, y_max)


def compute_canthus_normalized_iris(iris_center, outer_corner, inner_corner, top_lid, bottom_lid):
    """
    Measure iris position relative to the rigid canthus line (inner-to-outer eye corner axis),
    normalized by eye width in BOTH axes so vertical iris movement does NOT move the reference box.
    Also returns the Eye Aspect Ratio (EAR) to detect and reject blinks.
    """
    eye_vec = inner_corner - outer_corner
    eye_width = float(np.linalg.norm(eye_vec))
    if eye_width < 1.0:
        return np.array([0.0, 0.0], dtype=np.float32), 0.3

    # Unit basis vectors along the eye corner axis (u) and perpendicular vertical axis (v)
    u = eye_vec / eye_width
    v = np.array([-u[1], u[0]], dtype=np.float32)
    if v[1] < 0:
        v = -v

    eye_midpoint = 0.5 * (outer_corner + inner_corner)
    rel = iris_center - eye_midpoint

    # Normalize BOTH horizontal and vertical offsets by eye_width (stable bone-anchored distance)
    norm_x = float(np.dot(rel, u)) / eye_width
    norm_y = float(np.dot(rel, v)) / eye_width

    # Eye Aspect Ratio (EAR) for blink rejection
    lid_dist = float(np.linalg.norm(top_lid - bottom_lid))
    ear = lid_dist / eye_width

    return np.array([norm_x, norm_y], dtype=np.float32), ear


def soft_scurve(x: float, gain: float = 1.35) -> float:
    """
    Apply a gentle S-curve on [0, 1] centered at 0.5 so comfortable glances
    reach the canvas edges without requiring extreme eye strain.
    """
    centered = (x - 0.5) * 2.0  # [-1, 1]
    curved = np.tanh(centered * gain) / np.tanh(gain)
    return float(np.clip(0.5 + 0.5 * curved, 0.0, 1.0))


def classify_gaze(norm_offset, horiz_thr, vert_thr, deadzone):
    x, y = norm_offset
    if abs(x) < deadzone and abs(y) < deadzone:
        return "center"
    horiz = ""
    vert = ""
    if x < -horiz_thr:
        horiz = "left"
    elif x > horiz_thr:
        horiz = "right"
    if y < -vert_thr:
        vert = "up"
    elif y > vert_thr:
        vert = "down"
    if horiz and vert:
        return f"{horiz}-{vert}"
    return horiz or vert or "center"


class EyeTracker:
    def __init__(self, flip=True):
        self.mp_face_mesh = mp.solutions.face_mesh
        self.face_mesh = self.mp_face_mesh.FaceMesh(
            static_image_mode=False,
            max_num_faces=1,
            refine_landmarks=True,
            min_detection_confidence=0.5,
            min_tracking_confidence=0.5,
        )
        self.flip = flip
        self.buffer = deque(maxlen=ROLLING_BUFFER)
        self.smoothed = None
        self.prev = None
        self.prev_time = None
        self.saccade_count = 0
        self.horiz_thr = DEFAULT_HORIZ_THRESHOLD
        self.vert_thr = DEFAULT_VERT_THRESHOLD
        self.deadzone = DEADZONE
        self.logging = False
        self.logfile = None
        self.is_blinking = False

        # Calibration data
        self.center_norm = np.array([0.0, 0.0], dtype=np.float32)
        self.range_min = np.array([-0.10, -0.06], dtype=np.float32)
        self.range_max = np.array([0.10, 0.06], dtype=np.float32)

        # Calibration state
        self.is_calibrating = False
        self.calibration_samples = []
        self.calibration_needed = 90

        # Anti-snap-back & Flow steering state
        self.control_mode = "hybrid"  # "hybrid" (stabilized direct + anti-snap) or "flow" (gaze joystick steering)
        self.cursor_pos = np.array([0.5, 0.5], dtype=np.float32)
        self.last_calibrated_target = np.array([0.5, 0.5], dtype=np.float32)

    def toggle_flip(self):
        self.flip = not self.flip

    def toggle_control_mode(self) -> str:
        self.control_mode = "flow" if self.control_mode == "hybrid" else "hybrid"
        print(f"Gaze control mode switched to: {self.control_mode.upper()}")
        return self.control_mode

    def start_auto_calibration(self):
        """Resets and starts the automatic calibration process."""
        self.calibration_samples = []
        self.is_calibrating = True
        self.cursor_pos = np.array([0.5, 0.5], dtype=np.float32)
        print("Starting automatic calibration - move your eyes comfortably around the screen!")

    def add_calibration_sample(self, gaze_info):
        """
        Adds a non-blink gaze sample to the calibration buffer.
        Returns True when calibration is complete, False otherwise.
        """
        if not self.is_calibrating or gaze_info is None:
            return False

        if gaze_info.get("is_blinking", False):
            return False

        self.calibration_samples.append(gaze_info["eye_norm"])

        if len(self.calibration_samples) >= self.calibration_needed:
            self.finish_calibration()
            return True

        return False

    def finish_calibration(self):
        """
        Calculates calibration bounds using the 12th-88th percentiles (contracting rather
        than expanding the denominator so the user can comfortably reach all 4 corners).
        """
        if not self.calibration_samples:
            print("Calibration failed: No samples collected.")
            self.is_calibrating = False
            return

        arr = np.array(self.calibration_samples, dtype=np.float32)
        self.center_norm = np.median(arr, axis=0)

        # Use 12th and 88th percentiles so comfortable glances reach [0, 1] edges
        p_low = np.percentile(arr, 12, axis=0)
        p_high = np.percentile(arr, 88, axis=0)

        span = np.maximum(p_high - p_low, np.array([0.03, 0.02], dtype=np.float32))
        self.range_min = self.center_norm - 0.5 * span
        self.range_max = self.center_norm + 0.5 * span

        stds = arr.std(axis=0)
        self.horiz_thr = max(DEFAULT_HORIZ_THRESHOLD, float(stds[0]) * 2.5)
        self.vert_thr = max(DEFAULT_VERT_THRESHOLD, float(stds[1]) * 2.5)
        self.deadzone = max(DEADZONE, float(np.mean(stds)) * 1.5)

        self.is_calibrating = False
        self.cursor_pos = np.array([0.5, 0.5], dtype=np.float32)
        print("Calibration complete!")
        print(f"  Center:  {self.center_norm}")
        print(f"  Range X: [{self.range_min[0]:.4f}, {self.range_max[0]:.4f}]")
        print(f"  Range Y: [{self.range_min[1]:.4f}, {self.range_max[1]:.4f}]")

        from utils import save_calibration
        save_calibration(self.get_calibration_data())

    def load_calibration(self, data):
        self.center_norm = np.array(data.get("center_norm", [0.0, 0.0]), dtype=np.float32)
        self.range_min = np.array(data.get("range_min", [-0.10, -0.06]), dtype=np.float32)
        self.range_max = np.array(data.get("range_max", [0.10, 0.06]), dtype=np.float32)
        print("Loaded calibration.")

    def get_calibration_data(self):
        return {
            "center_norm": self.center_norm.tolist(),
            "range_min": self.range_min.tolist(),
            "range_max": self.range_max.tolist(),
        }

    def get_calibrated_gaze(self, gaze_info, dt=0.033):
        """
        Map raw eye position to [0, 1] canvas coordinates with:
          - Blink hold (prevents vertical spikes during blinks)
          - S-curve edge reach amplification
          - Centripetal return-to-center dampening (stops cursor snapping back to middle)
          - Optional 'flow' steering mode
        """
        if gaze_info is None:
            return None

        if gaze_info.get("is_blinking", False):
            return (float(self.cursor_pos[0]), float(self.cursor_pos[1]))

        raw_norm = gaze_info["smoothed_norm"]

        span_x = max(float(self.range_max[0] - self.range_min[0]), 1e-4)
        span_y = max(float(self.range_max[1] - self.range_min[1]), 1e-4)
        lin_x = (float(raw_norm[0]) - float(self.range_min[0])) / span_x
        lin_y = (float(raw_norm[1]) - float(self.range_min[1])) / span_y

        lin_x = float(np.clip(np.nan_to_num(lin_x, nan=0.5), 0.0, 1.0))
        lin_y = float(np.clip(np.nan_to_num(lin_y, nan=0.5), 0.0, 1.0))

        target = np.array([soft_scurve(lin_x), soft_scurve(lin_y)], dtype=np.float32)

        if self.control_mode == "flow":
            # Gaze Flow / Steering mode: looking away from center steers the brush;
            # relaxing eyes back to center pauses the brush in place!
            offset = target - np.array([0.5, 0.5], dtype=np.float32)
            mag = float(np.linalg.norm(offset))
            if mag > 0.08:
                velocity = offset * 0.85
                self.cursor_pos = np.clip(self.cursor_pos + velocity * max(0.01, min(dt, 0.1)), 0.02, 0.98)
            return (float(self.cursor_pos[0]), float(self.cursor_pos[1]))

        # Hybrid Stabilized Direct Mode with Anti-Center-Snap Dampening:
        # Check if the eye is snapping rapidly back toward (0.5, 0.5) from the periphery
        center_pt = np.array([0.5, 0.5], dtype=np.float32)
        prev_dist_from_center = float(np.linalg.norm(self.cursor_pos - center_pt))
        new_dist_from_center = float(np.linalg.norm(target - center_pt))
        step_vec = target - self.cursor_pos
        step_dist = float(np.linalg.norm(step_vec))

        is_center_snapback = (
            prev_dist_from_center > 0.22
            and new_dist_from_center < prev_dist_from_center - 0.06
            and step_dist > 0.08
        )

        if is_center_snapback:
            # Strongly dampen centripetal snap-back so the brush stays near the outer stroke
            alpha = 0.04
        else:
            # Responsive outward / lateral tracking
            alpha = 0.28

        self.cursor_pos = (1.0 - alpha) * self.cursor_pos + alpha * target
        self.cursor_pos = np.clip(self.cursor_pos, 0.0, 1.0)
        return (float(self.cursor_pos[0]), float(self.cursor_pos[1]))

    def process_frame(self, frame, only_compute=False, return_results=False):
        h, w = frame.shape[:2]
        img = frame.copy()
        img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        results = self.face_mesh.process(img_rgb)

        gaze_info = None
        if results.multi_face_landmarks:
            lm = results.multi_face_landmarks[0].landmark

            left_iris_pts = [nplm_to_xy(lm, i, w, h) for i in FACEMESH_LANDMARKS["left_iris"]]
            right_iris_pts = [nplm_to_xy(lm, i, w, h) for i in FACEMESH_LANDMARKS["right_iris"]]
            left_center = center_of(left_iris_pts)
            right_center = center_of(right_iris_pts)

            l_outer = nplm_to_xy(lm, FACEMESH_LANDMARKS["left_eye_outer"], w, h)
            l_inner = nplm_to_xy(lm, FACEMESH_LANDMARKS["left_eye_inner"], w, h)
            l_top = nplm_to_xy(lm, FACEMESH_LANDMARKS["left_eye_top"], w, h)
            l_bot = nplm_to_xy(lm, FACEMESH_LANDMARKS["left_eye_bottom"], w, h)

            r_outer = nplm_to_xy(lm, FACEMESH_LANDMARKS["right_eye_outer"], w, h)
            r_inner = nplm_to_xy(lm, FACEMESH_LANDMARKS["right_eye_inner"], w, h)
            r_top = nplm_to_xy(lm, FACEMESH_LANDMARKS["right_eye_top"], w, h)
            r_bot = nplm_to_xy(lm, FACEMESH_LANDMARKS["right_eye_bottom"], w, h)

            left_norm, left_ear = compute_canthus_normalized_iris(left_center, l_outer, l_inner, l_top, l_bot)
            # For right eye, outer is on the right side and inner is on the left side
            right_norm, right_ear = compute_canthus_normalized_iris(right_center, r_inner, r_outer, r_top, r_bot)

            avg_ear = 0.5 * (left_ear + right_ear)
            is_blinking = avg_ear < BLINK_EAR_THRESHOLD
            self.is_blinking = is_blinking

            # Subtle head-pose assist using nose tip relative to frame center
            nose_pt = nplm_to_xy(lm, FACEMESH_LANDMARKS["nose_tip"], w, h)
            head_offset = np.array([
                (nose_pt[0] / float(w)) - 0.5,
                (nose_pt[1] / float(h)) - 0.5,
            ], dtype=np.float32)

            eye_only_norm = 0.5 * (left_norm + right_norm)
            combined_norm = eye_only_norm + HEAD_POSE_WEIGHT * head_offset
            raw_norm = combined_norm.copy()

            if not is_blinking:
                self.buffer.append(combined_norm)

            if len(self.buffer) > 0:
                med = np.median(np.array(self.buffer), axis=0)
                if self.smoothed is None:
                    self.smoothed = med
                elif not is_blinking:
                    self.smoothed = SMOOTH_ALPHA * self.smoothed + (1.0 - SMOOTH_ALPHA) * med
            else:
                self.smoothed = raw_norm

            left_eye_bbox = eye_bbox(left_iris_pts + [l_outer, l_inner, l_top, l_bot])
            right_eye_bbox = eye_bbox(right_iris_pts + [r_outer, r_inner, r_top, r_bot])
            inter_ocular = float(np.linalg.norm(center_of([l_outer, l_inner]) - center_of([r_outer, r_inner])))

            now = time.time()
            dt = 1 / 30.0 if self.prev_time is None else max(1e-6, now - self.prev_time)
            self.prev_time = now

            if self.prev is not None and not is_blinking:
                delta = self.smoothed - self.prev
                speed = float(np.linalg.norm(delta) / dt)
                is_saccade = speed > SACCADE_SPEED_THR
                if is_saccade:
                    self.saccade_count += 1
                sacc_speed = speed
            else:
                is_saccade = False
                sacc_speed = 0.0
            self.prev = self.smoothed.copy()

            direction = classify_gaze(self.smoothed, self.horiz_thr, self.vert_thr, self.deadzone)

            gaze_info = {
                "left_center": left_center,
                "right_center": right_center,
                "left_bbox": left_eye_bbox,
                "right_bbox": right_eye_bbox,
                "eye_norm": raw_norm,
                "smoothed_norm": self.smoothed.copy(),
                "direction": direction,
                "saccade": is_saccade,
                "saccade_speed": float(sacc_speed),
                "saccade_count": int(self.saccade_count),
                "inter_ocular": inter_ocular,
                "ear": float(avg_ear),
                "is_blinking": bool(is_blinking),
            }

            if only_compute:
                return (frame, gaze_info, results) if return_results else (frame, gaze_info)

            draw = frame
            color = (0, 165, 255) if is_blinking else (0, 255, 0)
            for pt in left_iris_pts + right_iris_pts:
                cv2.circle(draw, tuple(pt.astype(int)), 2, color, -1)

            return (draw, gaze_info, results) if return_results else (draw, gaze_info)

        return (frame, None, None) if return_results else (frame, None)

    def release(self):
        self.face_mesh.close()
        if self.logfile:
            self.logfile.close()
