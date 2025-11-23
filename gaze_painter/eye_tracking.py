import cv2
import time
import numpy as np
import csv
from collections import deque

try:
    import mediapipe as mp
except Exception as e:
    raise ImportError("mediapipe is required. Install with: pip install mediapipe") from e

# --- Constants ---
FACEMESH_LANDMARKS = {
    "left_eye_outer": 33, "left_eye_inner": 133, "right_eye_outer": 362,
    "right_eye_inner": 263, "left_iris": [468, 469, 470, 471],
    "right_iris": [473, 474, 475, 476],
}
DEFAULT_HORIZ_THRESHOLD = 0.035
DEFAULT_VERT_THRESHOLD = 0.028
DEADZONE = 0.018
SACCADE_SPEED_THR = 0.2
SMOOTH_ALPHA = 0.6
MEDIAN_WINDOW = 5
ROLLING_BUFFER = 7

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

def normalize_by_eye(iris_center, eye_bbox_wh):
    x_min, y_min, x_max, y_max = eye_bbox_wh
    w = max(x_max - x_min, 1.0)
    h = max(y_max - y_min, 1.0)
    cx = (iris_center[0] - (x_min + w/2.0)) / w
    cy = (iris_center[1] - (y_min + h/2.0)) / h
    return np.array([cx, cy], dtype=np.float32)

def classify_gaze(norm_offset, horiz_thr, vert_thr, deadzone):
    x, y = norm_offset
    if abs(x) < deadzone and abs(y) < deadzone: return 'center'
    horiz = ''
    vert = ''
    if x < -horiz_thr: horiz = 'left'
    elif x > horiz_thr: horiz = 'right'
    if y < -vert_thr: vert = 'up'
    elif y > vert_thr: vert = 'down'
    if horiz and vert: return f"{horiz}-{vert}"
    return horiz or vert or 'center'

# -------------------- EyeTracker Class --------------------
class EyeTracker:
    def __init__(self, flip=True):
        self.mp_face_mesh = mp.solutions.face_mesh
        self.face_mesh = self.mp_face_mesh.FaceMesh(static_image_mode=False,
                                                    max_num_faces=1,
                                                    refine_landmarks=True,
                                                    min_detection_confidence=0.5,
                                                    min_tracking_confidence=0.5)
        self.flip = flip
        self.buffer = deque(maxlen=ROLLING_BUFFER)
        self.smoothed = None
        self.prev = None
        self.prev_time = None
        self.saccade_count = 0
        self.horiz_thr = DEFAULT_HORIZ_THRESHOLD
        self.vert_thr = DEFAULT_VERT_THRESHOLD
        self.logging = False
        self.logfile = None
        
        # Calibration data - now stores actual range instead of just center
        self.center_norm = np.array([0.0, 0.0])
        self.range_min = np.array([-0.15, -0.15])  # Expanded default range
        self.range_max = np.array([0.15, 0.15])
        
        # Calibration state
        self.is_calibrating = False
        self.calibration_samples = []
        self.calibration_needed = 90  # Increased for better range coverage

    def toggle_flip(self):
        self.flip = not self.flip

    def toggle_logging(self):
        pass
        
    def start_auto_calibration(self):
        """Resets and starts the automatic calibration process."""
        self.calibration_samples = []
        self.is_calibrating = True
        print("Starting automatic calibration - move your eyes around!")

    def add_calibration_sample(self, gaze_info):
        """
        Adds a gaze sample to the calibration buffer.
        Returns True when calibration is complete, False otherwise.
        """
        if not self.is_calibrating or gaze_info is None:
            return False
        
        self.calibration_samples.append(gaze_info['eye_norm'])
        
        if len(self.calibration_samples) >= self.calibration_needed:
            self.finish_calibration()
            return True
        
        return False

    def finish_calibration(self):
        """Calculates calibration data from collected samples - now captures full range."""
        if not self.calibration_samples:
            print("Calibration failed: No samples collected.")
            self.is_calibrating = False
            return

        arr = np.array(self.calibration_samples)
        
        # Calculate center
        self.center_norm = arr.mean(axis=0)
        
        # Calculate actual observed range with some padding
        # Use percentiles to avoid outliers
        self.range_min = np.percentile(arr, 5, axis=0)   # 5th percentile
        self.range_max = np.percentile(arr, 95, axis=0)  # 95th percentile
        
        # Add padding to ensure we can reach edges (20% expansion)
        range_span = self.range_max - self.range_min
        padding = range_span * 0.2
        self.range_min -= padding
        self.range_max += padding
        
        # Update thresholds
        stds = arr.std(axis=0)
        self.horiz_thr = max(DEFAULT_HORIZ_THRESHOLD, stds[0] * 3.0)
        self.vert_thr = max(DEFAULT_VERT_THRESHOLD, stds[1] * 3.0)
        
        global DEADZONE
        DEADZONE = max(DEADZONE, np.mean(stds) * 2.0)
        
        self.is_calibrating = False
        print(f"Calibration complete!")
        print(f"  Center: {self.center_norm}")
        print(f"  Range X: [{self.range_min[0]:.4f}, {self.range_max[0]:.4f}]")
        print(f"  Range Y: [{self.range_min[1]:.4f}, {self.range_max[1]:.4f}]")
        
        # Save calibration
        from utils import save_calibration
        save_calibration(self.get_calibration_data())

    def load_calibration(self, data):
        self.center_norm = np.array(data.get('center_norm', [0.0, 0.0]))
        self.range_min = np.array(data.get('range_min', [-0.15, -0.15]))
        self.range_max = np.array(data.get('range_max', [0.15, 0.15]))
        print(f"Loaded calibration.")
        print(f"  Center: {self.center_norm}")
        print(f"  Range X: [{self.range_min[0]:.4f}, {self.range_max[0]:.4f}]")
        print(f"  Range Y: [{self.range_min[1]:.4f}, {self.range_max[1]:.4f}]")

    def get_calibration_data(self):
        return {
            'center_norm': self.center_norm.tolist(),
            'range_min': self.range_min.tolist(),
            'range_max': self.range_max.tolist()
        }

    def get_calibrated_gaze(self, gaze_info):
        """
        Map raw eye position to full canvas coordinates [0, 1].
        This now properly maps the observed range to the full screen.
        """
        if gaze_info is None:
            return None
        
        raw_norm = gaze_info['smoothed_norm']
        
        # Map from observed range to [0, 1] canvas coordinates
        # Formula: (value - min) / (max - min)
        calib_x = (raw_norm[0] - self.range_min[0]) / (self.range_max[0] - self.range_min[0])
        calib_y = (raw_norm[1] - self.range_min[1]) / (self.range_max[1] - self.range_min[1])
        
        # Clamp to [0, 1] range
        calib_x = np.clip(calib_x, 0.0, 1.0)
        calib_y = np.clip(calib_y, 0.0, 1.0)
        
        return (calib_x, calib_y)

    def process_frame(self, frame, only_compute=False, return_results=False):
        h, w = frame.shape[:2]
        img = frame.copy()
        img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        results = self.face_mesh.process(img_rgb)

        gaze_info = None
        if results.multi_face_landmarks:
            lm = results.multi_face_landmarks[0].landmark
            
            left_iris_pts = [nplm_to_xy(lm, i, w, h) for i in FACEMESH_LANDMARKS['left_iris']]
            right_iris_pts = [nplm_to_xy(lm, i, w, h) for i in FACEMESH_LANDMARKS['right_iris']]
            left_center = center_of(left_iris_pts)
            right_center = center_of(right_iris_pts)
            left_corners = [nplm_to_xy(lm, FACEMESH_LANDMARKS['left_eye_outer'], w, h), 
                           nplm_to_xy(lm, FACEMESH_LANDMARKS['left_eye_inner'], w, h)]
            right_corners = [nplm_to_xy(lm, FACEMESH_LANDMARKS['right_eye_outer'], w, h), 
                            nplm_to_xy(lm, FACEMESH_LANDMARKS['right_eye_inner'], w, h)]
            left_eye_bbox = eye_bbox(left_iris_pts + left_corners)
            right_eye_bbox = eye_bbox(right_iris_pts + right_corners)
            left_norm = normalize_by_eye(left_center, left_eye_bbox)
            right_norm = normalize_by_eye(right_center, right_eye_bbox)
            avg_norm = (left_norm + right_norm) / 2.0
            raw_norm = avg_norm.copy()
            self.buffer.append(avg_norm)
            med = np.median(np.array(self.buffer), axis=0)
            if self.smoothed is None: 
                self.smoothed = med
            else: 
                self.smoothed = SMOOTH_ALPHA * self.smoothed + (1 - SMOOTH_ALPHA) * med
            
            left_corner_center = center_of(left_corners)
            right_corner_center = center_of(right_corners)
            inter_ocular = np.linalg.norm(left_corner_center - right_corner_center)
            
            now = time.time()
            if self.prev_time is None: 
                dt = 1/30.0
            else: 
                dt = max(1e-6, now - self.prev_time)
            self.prev_time = now
            
            if self.prev is not None:
                delta = self.smoothed - self.prev
                speed = np.linalg.norm(delta) / dt
                is_saccade = speed > SACCADE_SPEED_THR
                if is_saccade: 
                    self.saccade_count += 1
                sacc_speed = speed
            else:
                is_saccade = False
                sacc_speed = 0.0
            self.prev = self.smoothed.copy()
            
            direction = classify_gaze(self.smoothed, self.horiz_thr, self.vert_thr, DEADZONE)

            gaze_info = {
                'left_center': left_center, 
                'right_center': right_center,
                'left_bbox': left_eye_bbox, 
                'right_bbox': right_eye_bbox,
                'eye_norm': raw_norm, 
                'smoothed_norm': self.smoothed.copy(),
                'direction': direction, 
                'saccade': is_saccade,
                'saccade_speed': float(sacc_speed), 
                'saccade_count': int(self.saccade_count),
                'inter_ocular': float(inter_ocular)
            }

            if only_compute:
                if return_results: 
                    return frame, gaze_info, results
                else: 
                    return frame, gaze_info
            
            draw = frame
            for pt in left_iris_pts + right_iris_pts:
                cv2.circle(draw, tuple(pt.astype(int)), 1, (0, 255, 0), -1)
            
            if return_results: 
                return draw, gaze_info, results
            else: 
                return draw, gaze_info

        if return_results: 
            return frame, None, None
        else: 
            return frame, None

    def release(self):
        self.face_mesh.close()
        if self.logfile:
            self.logfile.close()