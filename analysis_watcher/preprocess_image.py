from pathlib import Path
import sys
import cv2
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config_manager import atomic_write_bytes

INK_LIGHT_THRESHOLD = 240
INK_DARK_THRESHOLD = 15


class DrawingValidationError(ValueError):
    """Raised when the input drawing is missing, unreadable, or blank."""


def composite_alpha_on_white(img: np.ndarray) -> np.ndarray:
    """If img has 4 channels (BGRA), alpha-composite over pure white BGR."""
    if img.ndim == 3 and img.shape[2] == 4:
        alpha = img[:, :, 3:4].astype(np.float32) / 255.0
        bgr = img[:, :, :3].astype(np.float32)
        composited = bgr * alpha + 255.0 * (1.0 - alpha)
        return np.clip(composited, 0, 255).astype(np.uint8)
    return img


def to_grayscale(img: np.ndarray) -> np.ndarray:
    """Convert BGR/BGRA/gray ndarray safely to 2D uint8 grayscale."""
    img = composite_alpha_on_white(img)
    if img.ndim == 3:
        return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return img


def extract_ink_mask(img: np.ndarray) -> tuple:
    """
    Extract a binary foreground ink mask (255 = ink, 0 = background) consistently
    across preprocessing, heatmap generation, and stroke geometry analysis.
    Supports both light canvases (white/cream bg, dark strokes) and dark canvases
    (black/dark bg, light strokes) without inverting dark backgrounds into ink.
    Returns (mask_uint8, is_dark_bg, bg_val).
    """
    gray = to_grayscale(img)
    bg_val = float(np.median(gray))
    is_dark_bg = bg_val <= 127.0
    if is_dark_bg:
        thresh_val = max(INK_DARK_THRESHOLD, int(round(bg_val)) + 5)
        _, mask = cv2.threshold(gray, thresh_val, 255, cv2.THRESH_BINARY)
    else:
        thresh_val = min(INK_LIGHT_THRESHOLD, int(round(bg_val)) - 5)
        _, mask = cv2.threshold(gray, thresh_val, 255, cv2.THRESH_BINARY_INV)
    return mask, is_dark_bg, bg_val


def preprocess_image(input_filename="image.png", output_filename="image_processed.png"):
    """
    Validate that the input scribble is readable and non-blank, crop the original
    anti-aliased/colored scribble to its bounding square (with safe padding),
    and resize atomically to 512x512.
    """
    raw_img = cv2.imread(str(input_filename), cv2.IMREAD_UNCHANGED)
    if raw_img is None or raw_img.size == 0:
        raise DrawingValidationError(f"Drawing image is missing or unreadable: {input_filename}")

    src_img = composite_alpha_on_white(raw_img)
    gray = to_grayscale(src_img)
    h, w = gray.shape[:2]

    unique_vals = np.unique(gray)
    if len(unique_vals) < 2:
        raise DrawingValidationError("Drawing canvas is blank (no strokes detected).")

    img_filtered, is_dark_bg, bg_val = extract_ink_mask(gray)
    if int(np.count_nonzero(img_filtered)) == 0:
        raise DrawingValidationError("Drawing canvas contains no visible ink strokes.")

    vertical_boundaries = np.where(img_filtered.max(axis=1) > 0)[0]
    horizontal_boundaries = np.where(img_filtered.max(axis=0) > 0)[0]
    if len(vertical_boundaries) == 0 or len(horizontal_boundaries) == 0:
        raise DrawingValidationError("Drawing canvas bounds are empty (no strokes detected).")

    upper_y = int(vertical_boundaries.min())
    lower_y = int(vertical_boundaries.max())
    upper_x = int(horizontal_boundaries.min())
    lower_x = int(horizontal_boundaries.max())

    x_dim = max(1, lower_x - upper_x)
    y_dim = max(1, lower_y - upper_y)
    side = max(x_dim, y_dim)
    pad = max(10, int(side * 0.05))
    target_side = side + 2 * pad

    cx = (upper_x + lower_x) // 2
    cy = (upper_y + lower_y) // 2

    half = target_side // 2
    src_x1 = max(0, cx - half)
    src_y1 = max(0, cy - half)
    src_x2 = min(w, cx + half)
    src_y2 = min(h, cy + half)

    # Crop from the original image to preserve anti-aliasing and Ember/Indigo RGB shading
    cropped = src_img[src_y1:src_y2, src_x1:src_x2].copy()
    if is_dark_bg:
        cropped = 255 - cropped
        fill_val = 255
    else:
        fill_val = 255 if bg_val >= 250.0 else int(round(bg_val))

    if cropped.ndim == 3:
        square = np.full((target_side, target_side, cropped.shape[2]), fill_val, dtype=np.uint8)
    else:
        square = np.full((target_side, target_side), fill_val, dtype=np.uint8)

    dst_y1 = max(0, (target_side - cropped.shape[0]) // 2)
    dst_x1 = max(0, (target_side - cropped.shape[1]) // 2)
    square[dst_y1:dst_y1 + cropped.shape[0], dst_x1:dst_x1 + cropped.shape[1]] = cropped

    img_resized = cv2.resize(square, (512, 512), interpolation=cv2.INTER_AREA)
    ok, encoded = cv2.imencode(".png", img_resized)
    if not ok:
        raise RuntimeError("Failed to encode preprocessed drawing as PNG")

    out_path = Path(output_filename)
    atomic_write_bytes(out_path, encoded.tobytes())
    return str(output_filename)
