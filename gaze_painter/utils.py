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
    """Save calibration data to JSON file."""
    ensure_directories()
    if path is None:
        path = CALIBRATION_JSON

    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, np.ndarray):
                data[key] = value.tolist()

    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"Calibration saved to {path}")


def load_calibration(path=None):
    """Load calibration data from JSON file."""
    ensure_directories()
    if path is None:
        path = CALIBRATION_JSON

    if not os.path.exists(path):
        return None

    try:
        with open(path, "r") as f:
            data = json.load(f)
        print(f"Calibration loaded from {path}")
        return data
    except Exception as e:
        print(f"Error loading calibration: {e}")
        return None
