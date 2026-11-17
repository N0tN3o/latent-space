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
BLINK_EAR_THRESHOLD = 0.165  # Eye Aspect Ratio below which a blink is occurring
HEAD_POSE_WEIGHT = 0.22     # Gentle head-orientation assist


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


def compute_canthus_normalized_iris(iris_center, corner_a, corner_b, top_lid, bottom_lid):
    """
    Measure iris position relative to the rigid canthus line (eye corner axis),
    always oriented left-to-right (+X) and top-to-bottom (+Y) so both eyes
    always agree in sign and never cancel each other out.
    """
    # Always orient from smaller X (left on screen) to larger X (right on screen)
    if corner_a[0] <= corner_b[0]:
        left_pt, right_pt = corner_a, corner_b
    else:
        left_pt, right_pt = corner_b, corner_a

    eye_vec = right_pt - left_pt
    eye_width = float(np.linalg.norm(eye_vec))
    if eye_width < 1.0:
        return np.array([0.0, 0.0], dtype=np.float32), 0.3

    # Unit basis vectors: u points +X (rightward), v points +Y (downward)
    u = eye_vec / eye_width
    v = np.array([-u[1], u[0]], dtype=np.float32)
    if v[1] < 0:
        v = -v

    eye_midpoint = 0.5 * (left_pt + right_pt)
    rel = iris_center - eye_midpoint

    norm_x = float(np.dot(rel, u)) / eye_width
    norm_y = float(np.dot(rel, v)) / eye_width

    lid_dist = float(np.linalg.norm(top_lid - bottom_lid))
    ear = lid_dist / eye_width

    return np.array([norm_x, norm_y], dtype=np.float32), ear


def soft_scurve(x: float, gain: float = 1.2) -> float:
    centered = (x - 0.5) * 2.0
    curved = np.tanh(centered * gain) / np.tanh(gain)
    return float(np.clip(0.5 + 0.5 * curved, 0.0, 1.0))


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
        self.left_hist = deque(maxlen=5)
        self.right_hist = deque(maxlen=5)
        self.smoothed = None
        self.prev = None
        self.prev_time = None
        self.saccade_count = 0
        self.horiz_thr = DEFAULT_HORIZ_THRESHOLD
        self.vert_thr = DEFAULT_VERT_THRESHOLD
        self.deadzone = DEADZONE
        self.is_blinking = False

        # Calibration data
        self.center_norm = np.array([0.0, 0.0], dtype=np.float32)
        self.range_min = np.array([-0.08, -0.055], dtype=np.float32)
        self.range_max = np.array([0.08, 0.055], dtype=np.float32)

        # Calibration state
        self.is_calibrating = False
        self.calibration_samples = []
        self.center_samples = []
        self.calibration_needed = 90

        # Anti-snap-back & Flow steering state
        self.control_mode = "hybrid"
        self.cursor_pos = np.array([0.5, 0.5], dtype=np.float32)

    def toggle_control_mode(self) -> str:
        self.control_mode = "flow" if self.control_mode == "hybrid" else "hybrid"
        print(f"Gaze control mode switched to: {self.control_mode.upper()}")
        return self.control_mode

    def recenter_now(self):
        """Anchor (0.5, 0.5) to the current smoothed gaze position (great for glasses wearers)."""
        if self.smoothed is not None:
            half_span = 0.5 * (self.range_max - self.range_min)
            self.center_norm = self.smoothed.copy()
            self.range_min = self.center_norm - half_span
            self.range_max = self.center_norm + half_span
        self.cursor_pos = np.array([0.5, 0.5], dtype=np.float32)
        print("Gaze center re-anchored to current eye position.")

    def start_auto_calibration(self):
        """Resets and starts the guided calibration process."""
        self.calibration_samples = []
        self.center_samples = []
        self.is_calibrating = True
        self.cursor_pos = np.array([0.5, 0.5], dtype=np.float32)
        print("Starting calibration - look at the center first, then follow the target.")

    def add_calibration_sample(self, gaze_info):
        if not self.is_calibrating or gaze_info is None:
            return False

        if gaze_info.get("is_blinking", False):
            return False

        sample = gaze_info["smoothed_norm"]
        self.calibration_samples.append(sample)
        # First 30% of samples capture the user's natural resting center gaze
        if len(self.calibration_samples) <= int(self.calibration_needed * 0.3):
            self.center_samples.append(sample)

        if len(self.calibration_samples) >= self.calibration_needed:
            self.finish_calibration()
            return True

        return False

    def finish_calibration(self):
        if not self.calibration_samples:
            self.is_calibrating = False
            return

        arr = np.array(self.calibration_samples, dtype=np.float32)
        if self.center_samples:
            self.center_norm = np.median(np.array(self.center_samples, dtype=np.float32), axis=0)
        else:
            self.center_norm = np.median(arr, axis=0)

        p_low = np.percentile(arr, 10, axis=0)
        p_high = np.percentile(arr, 90, axis=0)

        # Enforce a realistic physiological minimum span so staring straight ahead never creates a tiny hypersensitive box
        span = np.maximum(p_high - p_low, np.array([0.11, 0.075], dtype=np.float32))
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
        r_min = np.array(data.get("range_min", [-0.08, -0.055]), dtype=np.float32)
        r_max = np.array(data.get("range_max", [0.08, 0.055]), dtype=np.float32)
        span = np.maximum(r_max - r_min, np.array([0.11, 0.075], dtype=np.float32))
        self.range_min = self.center_norm - 0.5 * span
        self.range_max = self.center_norm + 0.5 * span
        self.cursor_pos = np.array([0.5, 0.5], dtype=np.float32)

    def get_calibration_data(self):
        return {
            "center_norm": self.center_norm.tolist(),
            "range_min": self.range_min.tolist(),
            "range_max": self.range_max.tolist(),
        }

    def get_calibrated_gaze(self, gaze_info, dt=0.033):
        if gaze_info is None:
            return None

        if gaze_info.get("is_blinking", False):
            return (float(self.cursor_pos[0]), float(self.cursor_pos[1]))

        raw_norm = gaze_info["smoothed_norm"]

        span_x = max(float(self.range_max[0] - self.range_min[0]), 0.08)
        span_y = max(float(self.range_max[1] - self.range_min[1]), 0.055)
        lin_x = 0.5 + (float(raw_norm[0]) - float(self.center_norm[0])) / span_x
        lin_y = 0.5 + (float(raw_norm[1]) - float(self.center_norm[1])) / span_y

        lin_x = float(np.clip(np.nan_to_num(lin_x, nan=0.5), 0.0, 1.0))
        lin_y = float(np.clip(np.nan_to_num(lin_y, nan=0.5), 0.0, 1.0))

        target = np.array([soft_scurve(lin_x), soft_scurve(lin_y)], dtype=np.float32)

        if self.control_mode == "flow":
            offset = target - np.array([0.5, 0.5], dtype=np.float32)
            mag = float(np.linalg.norm(offset))
            if mag > 0.08:
                velocity = offset * 0.85
                self.cursor_pos = np.clip(self.cursor_pos + velocity * max(0.01, min(dt, 0.1)), 0.02, 0.98)
            return (float(self.cursor_pos[0]), float(self.cursor_pos[1]))

        center_pt = np.array([0.5, 0.5], dtype=np.float32)
        prev_dist_from_center = float(np.linalg.norm(self.cursor_pos - center_pt))
        new_dist_from_center = float(np.linalg.norm(target - center_pt))
        step_dist = float(np.linalg.norm(target - self.cursor_pos))

        is_center_snapback = (
            prev_dist_from_center > 0.22
            and new_dist_from_center < prev_dist_from_center - 0.06
            and step_dist > 0.08
        )

        alpha = 0.05 if is_center_snapback else 0.22
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
            right_norm, right_ear = compute_canthus_normalized_iris(right_center, r_outer, r_inner, r_top, r_bot)

            avg_ear = 0.5 * (left_ear + right_ear)
            is_blinking = avg_ear < BLINK_EAR_THRESHOLD
            self.is_blinking = is_blinking

            # Glasses glare / single-eye outlier rejection:
            # Compare each eye against its short-term median and weight the steadier eye higher
            self.left_hist.append(left_norm)
            self.right_hist.append(right_norm)
            l_med = np.median(np.array(self.left_hist), axis=0)
            r_med = np.median(np.array(self.right_hist), axis=0)
            l_jitter = float(np.linalg.norm(left_norm - l_med)) + 1e-4
            r_jitter = float(np.linalg.norm(right_norm - r_med)) + 1e-4
            w_left = r_jitter / (l_jitter + r_jitter)
            w_right = l_jitter / (l_jitter + r_jitter)
            eye_only_norm = w_left * left_norm + w_right * right_norm

            nose_pt = nplm_to_xy(lm, FACEMESH_LANDMARKS["nose_tip"], w, h)
            head_offset = np.array([
                (nose_pt[0] / float(w)) - 0.5,
                (nose_pt[1] / float(h)) - 0.5,
            ], dtype=np.float32)

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

            gaze_info = {
                "left_center": left_center,
                "right_center": right_center,
                "left_bbox": left_eye_bbox,
                "right_bbox": right_eye_bbox,
                "eye_norm": raw_norm,
                "smoothed_norm": self.smoothed.copy(),
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
