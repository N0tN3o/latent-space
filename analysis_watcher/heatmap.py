import os
from pathlib import Path
import threading
import cv2
import numpy as np
from matplotlib.backends.backend_agg import FigureCanvasAgg as FigureCanvas
from matplotlib.figure import Figure

from preprocess_image import extract_ink_mask


def normalize_dwell_grid(dwell_grid):
    """Validate and convert an optional 3x3 gaze dwell matrix into a float32 array."""
    if dwell_grid is None:
        return None
    try:
        arr = np.asarray(dwell_grid, dtype=np.float32)
        if arr.shape != (3, 3):
            return None
        if not np.all(np.isfinite(arr)):
            return None
        arr = np.maximum(arr, 0.0)
        if float(arr.max()) <= 1e-6:
            return None
        return arr
    except Exception:
        return None


def compute_zone_matrix(scribble_filename=None, dwell_grid=None) -> np.ndarray:
    """
    Compute the raw 3x3 spatial attention matrix (non-negative float32).
    Prefers real recorded gaze dwell time when provided; otherwise computes
    pixel stroke density across the 3x3 regions of the uncropped canvas.
    """
    dwell = normalize_dwell_grid(dwell_grid)
    if dwell is not None:
        return dwell

    heatmap = np.zeros((3, 3), dtype=np.float32)
    if not scribble_filename:
        return heatmap

    try:
        img = cv2.imread(str(scribble_filename), cv2.IMREAD_UNCHANGED)
        if img is None or img.size == 0:
            raise ValueError(f"Could not read scribble image: {scribble_filename}")

        ink_mask, _, _ = extract_ink_mask(img)
        binary = (ink_mask > 0).astype(np.float32)
        h, w = binary.shape
        h_step, w_step = max(1, h // 3), max(1, w // 3)

        for i in range(3):
            y_end = h if i == 2 else (i + 1) * h_step
            for j in range(3):
                x_end = w if j == 2 else (j + 1) * w_step
                region = binary[i * h_step:y_end, j * w_step:x_end]
                heatmap[i, j] = float(np.mean(region)) if region.size > 0 else 0.0
    except Exception as e:
        print(f"[heatmap] Warning: heatmap computation failed ({e}); returning zero matrix.")

    return heatmap


def draw_heatmap(scribble_filename="image.png", heatmap_filename="heatmap.png", dwell_grid=None):
    """
    Create a 3x3 attention heatmap visualization illustrating either real gaze dwell
    duration (when available) or stroke density across the 3x3 canvas regions.
    """
    heatmap = compute_zone_matrix(scribble_filename=scribble_filename, dwell_grid=dwell_grid)
    max_val = float(heatmap.max())
    if max_val > 0.0:
        heatmap = heatmap / max_val

    out_path = Path(heatmap_filename)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_name(f".tmp_{os.getpid()}_{threading.get_ident()}_{out_path.name}")

    fig = Figure(figsize=(3, 3), dpi=100)
    FigureCanvas(fig)
    ax = fig.add_subplot(111)
    ax.imshow(heatmap, cmap="gray", interpolation="nearest", vmin=0.0, vmax=1.0)
    ax.axis("off")
    fig.subplots_adjust(left=0, right=1, top=1, bottom=0)
    try:
        fig.savefig(str(tmp_path), format="png", bbox_inches="tight", pad_inches=0)
        os.replace(str(tmp_path), str(out_path))
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass
    return str(heatmap_filename)
