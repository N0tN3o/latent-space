from collections import deque
import time
import cv2
import numpy as np

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
    "right_eye_outer": 263,
    "right_eye_inner": 362,
    "right_eye_top": 386,
    "right_eye_bottom": 374,
    "nose_tip": 1,
    "left_iris": [468, 469, 470, 471],
    "right_iris": [473, 474, 475, 476],
}

SMOOTH_ALPHA = 0.55
ROLLING_BUFFER = 7
BLINK_EAR_THRESHOLD = 0.135  # Adaptive default Eye Aspect Ratio threshold (prevents downward-gaze false positives)
HEAD_POSE_WEIGHT = 0.22      # Translation-invariant head yaw/pitch assist
CALIB_DURATION_SEC = 4.5
CALIB_STEP1_RATIO = 0.33
CALIB_STEP2_SKIP_SEC = 0.20


def calibration_target_at_progress(pct: float) -> tuple:
    """Return normalized (tx, ty) calibration target position for progress pct in [0, 100]."""
    p = float(np.clip(pct, 0.0, 100.0))
    if p <= 33.0:
        return (0.5, 0.5)
    angle = ((p - 33.0) / 67.0) * (2.0 * np.pi)
    tx = 0.5 + 0.36 * np.cos(angle)
    ty = 0.5 + 0.34 * np.sin(angle)
    return (float(tx), float(ty))


def fit_ridge_affine_axis(dx: np.ndarray, dy: np.ndarray, target_offset: np.ndarray,
                          primary_axis: int, min_gain: float, max_gain: float,
                          default_gain: float) -> np.ndarray:
    """
    Fit target_offset ~= b + w_x * dx + w_y * dy with Ridge regularization,
    clamping gain bounds so motionless calibration stays safely responsive.
    """
    n = len(dx)
    if n < 6:
        w = np.zeros(3, dtype=np.float32)
        w[primary_axis + 1] = default_gain
        return w

    X = np.column_stack([np.ones(n, dtype=np.float64), dx.astype(np.float64), dy.astype(np.float64)])
    y = target_offset.astype(np.float64)
    lam = 1.5e-5 * n
    reg = np.diag([1e-6, lam, lam])
    try:
        w = np.linalg.solve(X.T @ X + reg, X.T @ y)
    except np.linalg.LinAlgError:
        w = np.array([0.0, default_gain if primary_axis == 0 else 0.0, default_gain if primary_axis == 1 else 0.0])

    # Clamp primary and cross gains to physiological bounds
    p_idx = primary_axis + 1
    c_idx = (1 - primary_axis) + 1
    w[0] = float(np.clip(w[0], -0.15, 0.15))
    w[p_idx] = float(np.clip(w[p_idx], min_gain, max_gain))
    w[c_idx] = float(np.clip(w[c_idx], -4.0, 4.0))
    return w.astype(np.float32)


def nplm_to_xy(lm, idx, w, h):
    p = lm[idx]
    return np.array([p.x * w, p.y * h], dtype=np.float32)


def center_of(points):
    arr = np.array(points, dtype=np.float32)
    return arr.mean(axis=0)


def compute_canthus_normalized_iris(iris_center, corner_a, corner_b, top_lid, bottom_lid):
    """
    Measure iris position relative to the rigid canthus line (eye corner axis),
    always oriented left-to-right (+X) and top-to-bottom (+Y) so both eyes
    always agree in sign and never cancel each other out.
    """
    if corner_a[0] <= corner_b[0]:
        left_pt, right_pt = corner_a, corner_b
    else:
        left_pt, right_pt = corner_b, corner_a

    eye_vec = right_pt - left_pt
    eye_width = float(np.linalg.norm(eye_vec))
    if eye_width < 1.0:
        return np.array([0.0, 0.0], dtype=np.float32), 0.3

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


def _window_spread(hist: deque) -> float:
    arr = np.array(hist, dtype=np.float32)
    mean = arr.mean(axis=0)
    return float(np.max(np.linalg.norm(arr - mean, axis=1)))


def fuse_binocular_eyes(
    left_norm: np.ndarray,
    right_norm: np.ndarray,
    left_ear: float,
    right_ear: float,
    left_hist: deque,
    right_hist: deque,
    blink_ear_thr: float,
    is_blinking: bool,
) -> np.ndarray:
    """
    Blend left and right normalized iris signals while suppressing single-eye glare
    artifacts (frozen reflection lock or single-eye jump spike) and partial lid occlusion.
    Histories are only updated on open-eye frames so blinks do not poison the window.
    """
    if is_blinking:
        return 0.5 * (left_norm + right_norm)

    w_left, w_right = 0.5, 0.5

    # 1. Partial single-eye lid occlusion / wink
    if left_ear < blink_ear_thr and right_ear >= blink_ear_thr * 1.15:
        w_left, w_right = 0.1, 0.9
    elif right_ear < blink_ear_thr and left_ear >= blink_ear_thr * 1.15:
        w_left, w_right = 0.9, 0.1

    # 2. Anatomical range sanity check
    l_oob = float(np.max(np.abs(left_norm))) > 0.45
    r_oob = float(np.max(np.abs(right_norm))) > 0.45
    if l_oob and not r_oob:
        w_left, w_right = 0.1, 0.9
    elif r_oob and not l_oob:
        w_left, w_right = 0.9, 0.1

    # 3. Glare-frozen iris check: if one eye stays virtually frozen while the other actively tracks
    if len(left_hist) >= 4 and len(right_hist) >= 4 and not (l_oob or r_oob):
        l_spread = _window_spread(left_hist)
        r_spread = _window_spread(right_hist)
        if l_spread < 0.0015 and r_spread > 0.012:
            w_left, w_right = 0.15, 0.85
        elif r_spread < 0.0015 and l_spread > 0.012:
            w_left, w_right = 0.85, 0.15

    # 4. Single-eye glare jump spike: only one eye teleports while the other remains steady
    if len(left_hist) >= 1 and len(right_hist) >= 1:
        l_step = float(np.linalg.norm(left_norm - left_hist[-1]))
        r_step = float(np.linalg.norm(right_norm - right_hist[-1]))
        if l_step > 0.08 and r_step < 0.035:
            w_left, w_right = 0.15, 0.85
        elif r_step > 0.08 and l_step < 0.035:
            w_left, w_right = 0.85, 0.15

    left_hist.append(left_norm.copy())
    right_hist.append(right_norm.copy())
    return (w_left * left_norm + w_right * right_norm).astype(np.float32)


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
        self.buffer = deque(maxlen=ROLLING_BUFFER)
        self.left_hist = deque(maxlen=5)
        self.right_hist = deque(maxlen=5)
        self.smoothed = None
        self.blink_ear_thr = BLINK_EAR_THRESHOLD
        self.is_blinking = False

        # Calibration data
        self.center_norm = np.array([0.0, 0.0], dtype=np.float32)
        self.weights_x = np.array([0.0, 9.0, 0.0], dtype=np.float32)
        self.weights_y = np.array([0.0, 0.0, 13.0], dtype=np.float32)
        self._loaded_calibration = None

        # Time-based Calibration state
        self.is_calibrating = False
        self.calibration_duration_sec = CALIB_DURATION_SEC
        self.calibration_start_time = None
        self.calibration_samples = []
        self.calibration_targets = []
        self.center_samples = []
        self.ear_samples = []

        # Anti-snap-back & Flow steering state
        self.control_mode = "hybrid"
        self.cursor_pos = np.array([0.5, 0.5], dtype=np.float32)

    def reset_visitor_state(self):
        """Reset per-visitor filtering, blink threshold, and calibration state cleanly."""
        self.buffer.clear()
        self.left_hist.clear()
        self.right_hist.clear()
        self.smoothed = None
        self.is_blinking = False
        self.blink_ear_thr = BLINK_EAR_THRESHOLD
        self.center_norm = np.array([0.0, 0.0], dtype=np.float32)
        self.weights_x = np.array([0.0, 9.0, 0.0], dtype=np.float32)
        self.weights_y = np.array([0.0, 0.0, 13.0], dtype=np.float32)
        self.cursor_pos = np.array([0.5, 0.5], dtype=np.float32)

    def toggle_control_mode(self) -> str:
        self.control_mode = "flow" if self.control_mode == "hybrid" else "hybrid"
        print(f"Gaze control mode switched to: {self.control_mode.upper()}")
        return self.control_mode

    def recenter_now(self):
        """Anchor (0.5, 0.5) to the current smoothed gaze position (great for glasses wearers)."""
        if self.smoothed is not None:
            self.center_norm = self.smoothed.copy()
            self.weights_x[0] = 0.0
            self.weights_y[0] = 0.0
        self.cursor_pos = np.array([0.5, 0.5], dtype=np.float32)
        print("Gaze center re-anchored to current eye position.")

    def start_auto_calibration(self):
        """Reset visitor blink/gaze buffers and start time-based guided calibration."""
        self.reset_visitor_state()
        self.calibration_samples = []
        self.calibration_targets = []
        self.center_samples = []
        self.ear_samples = []
        self.calibration_start_time = time.time()
        self.is_calibrating = True
        print("Starting calibration - look at the center first, then follow the target.")

    def get_calibration_progress(self, now: float = None) -> float:
        """Return elapsed calibration progress [0.0, 100.0] independent of camera FPS."""
        if not self.is_calibrating or self.calibration_start_time is None:
            return 0.0
        cur = time.time() if now is None else now
        elapsed = max(0.0, cur - self.calibration_start_time)
        return float(np.clip((elapsed / self.calibration_duration_sec) * 100.0, 0.0, 100.0))

    def add_calibration_sample(self, gaze_info, now: float = None):
        if not self.is_calibrating:
            return False

        cur = time.time() if now is None else now
        if self.calibration_start_time is None:
            self.calibration_start_time = cur

        elapsed = max(0.0, cur - self.calibration_start_time)
        progress = self.get_calibration_progress(cur)
        step1_dur = self.calibration_duration_sec * CALIB_STEP1_RATIO

        if gaze_info is not None and not gaze_info.get("is_blinking", False):
            sample = gaze_info.get("eye_norm", gaze_info["smoothed_norm"])
            tx, ty = calibration_target_at_progress(progress)

            if progress <= 33.0:
                self.center_samples.append(sample)
                if "ear" in gaze_info:
                    self.ear_samples.append(float(gaze_info["ear"]))
                self.calibration_samples.append(sample)
                self.calibration_targets.append([tx, ty])
            elif (elapsed - step1_dur) >= CALIB_STEP2_SKIP_SEC:
                self.calibration_samples.append(sample)
                self.calibration_targets.append([tx, ty])

        if progress >= 100.0:
            self.finish_calibration()
            return True

        return False

    def finish_calibration(self):
        if len(self.calibration_samples) < 6:
            if self._loaded_calibration is not None:
                self.load_calibration(self._loaded_calibration)
            self.is_calibrating = False
            return

        if len(self.ear_samples) > 3:
            baseline_ear = float(np.median(np.array(self.ear_samples, dtype=np.float32)))
            self.blink_ear_thr = float(np.clip(baseline_ear * 0.52, 0.10, 0.155))

        arr = np.array(self.calibration_samples, dtype=np.float32)
        t_arr = np.array(self.calibration_targets, dtype=np.float32) if self.calibration_targets else None

        if len(self.center_samples) > 3:
            self.center_norm = np.median(np.array(self.center_samples, dtype=np.float32), axis=0)
        else:
            self.center_norm = np.median(arr, axis=0)

        dx = arr[:, 0] - self.center_norm[0]
        dy = arr[:, 1] - self.center_norm[1]
        if t_arr is not None and len(t_arr) == len(arr):
            self.weights_x = fit_ridge_affine_axis(dx, dy, t_arr[:, 0] - 0.5, primary_axis=0, min_gain=4.5, max_gain=14.0, default_gain=9.0)
            self.weights_y = fit_ridge_affine_axis(dx, dy, t_arr[:, 1] - 0.5, primary_axis=1, min_gain=6.0, max_gain=18.0, default_gain=13.0)
        else:
            self.weights_x = np.array([0.0, 9.0, 0.0], dtype=np.float32)
            self.weights_y = np.array([0.0, 0.0, 13.0], dtype=np.float32)

        self.is_calibrating = False
        self.cursor_pos = np.array([0.5, 0.5], dtype=np.float32)
        print("Calibration complete!")
        print(f"  Center:    {self.center_norm}")
        print(f"  Weights X: {self.weights_x}")
        print(f"  Weights Y: {self.weights_y}")

        from utils import save_calibration
        save_calibration(self.get_calibration_data())

    def load_calibration(self, data):
        if isinstance(data, dict):
            self._loaded_calibration = dict(data)
        self.center_norm = np.array(data.get("center_norm", [0.0, 0.0]), dtype=np.float32)
        if "weights_x" in data and "weights_y" in data:
            self.weights_x = np.array(data["weights_x"], dtype=np.float32)
            self.weights_y = np.array(data["weights_y"], dtype=np.float32)
        else:
            r_min = np.array(data.get("range_min", [-0.08, -0.055]), dtype=np.float32)
            r_max = np.array(data.get("range_max", [0.08, 0.055]), dtype=np.float32)
            span = np.maximum(r_max - r_min, np.array([0.11, 0.075], dtype=np.float32))
            self.weights_x = np.array([0.0, 1.0 / float(span[0]), 0.0], dtype=np.float32)
            self.weights_y = np.array([0.0, 0.0, 1.0 / float(span[1])], dtype=np.float32)
        if "blink_ear_thr" in data:
            self.blink_ear_thr = float(np.clip(float(data["blink_ear_thr"]), 0.10, 0.165))
        self.cursor_pos = np.array([0.5, 0.5], dtype=np.float32)

    def get_calibration_data(self):
        return {
            "center_norm": self.center_norm.tolist(),
            "weights_x": self.weights_x.tolist(),
            "weights_y": self.weights_y.tolist(),
            "blink_ear_thr": float(self.blink_ear_thr),
        }

    def get_calibrated_gaze(self, gaze_info, dt=0.033):
        if gaze_info is None:
            return None

        raw_norm = gaze_info.get("smoothed_norm")
        if gaze_info.get("is_blinking", False) or raw_norm is None:
            return (float(self.cursor_pos[0]), float(self.cursor_pos[1]))

        dx = float(raw_norm[0]) - float(self.center_norm[0])
        dy = float(raw_norm[1]) - float(self.center_norm[1])

        lin_x = 0.5 + float(self.weights_x[0]) + float(self.weights_x[1]) * dx + float(self.weights_x[2]) * dy
        lin_y = 0.5 + float(self.weights_y[0]) + float(self.weights_y[1]) * dx + float(self.weights_y[2]) * dy

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

    def process_frame(self, frame, return_results=False):
        h, w = frame.shape[:2]
        img_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        results = self.face_mesh.process(img_rgb)

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
            min_ear = min(left_ear, right_ear)
            effective_thr = min(self.blink_ear_thr, 0.115) if self.is_calibrating else self.blink_ear_thr
            is_blinking = (avg_ear < effective_thr) and (min_ear < effective_thr * 1.1)
            self.is_blinking = is_blinking

            eye_only_norm = fuse_binocular_eyes(
                left_norm, right_norm, left_ear, right_ear,
                self.left_hist, self.right_hist, effective_thr, is_blinking,
            )

            # Translation-invariant head yaw/pitch relative to eye midpoint (independent of chair position)
            eye_mid = 0.5 * (l_outer + r_outer)
            outer_dist = max(float(np.linalg.norm(r_outer - l_outer)), 1.0)
            nose_pt = nplm_to_xy(lm, FACEMESH_LANDMARKS["nose_tip"], w, h)
            head_offset = np.array([
                (nose_pt[0] - eye_mid[0]) / outer_dist,
                (nose_pt[1] - eye_mid[1]) / outer_dist - 0.35,
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
            elif not is_blinking:
                self.smoothed = raw_norm

            gaze_info = {
                "left_center": left_center,
                "right_center": right_center,
                "eye_norm": raw_norm,
                "smoothed_norm": self.smoothed.copy() if self.smoothed is not None else None,
                "ear": float(avg_ear),
                "is_blinking": bool(is_blinking),
            }

            color = (0, 165, 255) if is_blinking else (0, 255, 0)
            for pt in left_iris_pts + right_iris_pts:
                cv2.circle(frame, tuple(pt.astype(int)), 2, color, -1)

            return (frame, gaze_info, results) if return_results else (frame, gaze_info)

        return (frame, None, None) if return_results else (frame, None)

    def release(self):
        self.face_mesh.close()
