import json
import os
from pathlib import Path
import numpy as np

# Define folder paths anchored to gaze_painter directory (not CWD)
BASE_DIR = Path(__file__).resolve().parent
CALIBRATION_DIR = str(BASE_DIR / "calibrations")
CALIBRATION_JSON = str(BASE_DIR / "calibrations" / "calibration.json")


def ensure_directories():
    """Ensure required directories exist."""
    if not os.path.exists(CALIBRATION_DIR):
        os.makedirs(CALIBRATION_DIR)


def save_calibration(data, path=None):
    """Save calibration data atomically to JSON file without mutating caller dict."""
    ensure_directories()
    if path is None:
        path = CALIBRATION_JSON

    serializable = {}
    if isinstance(data, dict):
        for key, value in data.items():
            serializable[key] = value.tolist() if isinstance(value, np.ndarray) else value
    else:
        serializable = data

    target = Path(path)
    tmp_path = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(serializable, f, indent=2)
        os.replace(str(tmp_path), str(target))
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass
    print(f"Calibration saved to {path}")


def load_calibration(path=None):
    """Load calibration data from JSON file."""
    ensure_directories()
    if path is None:
        path = CALIBRATION_JSON

    if not os.path.exists(path):
        return None

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        print(f"Calibration loaded from {path}")
        return data
    except Exception as e:
        print(f"Error loading calibration: {e}")
        return None
