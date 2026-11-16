import cv2
import numpy as np


def preprocess_image(input_filename='image.png', output_filename='image_processed.png'):
    """
    Crop the scribble to its bounding square (with safe padding) and resize to 512x512
    (or 224x224 compatible) while preserving crisp strokes.
    """
    img = cv2.imread(str(input_filename), cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise FileNotFoundError(f"Could not read input image: {input_filename}")

    h, w = img.shape[:2]
    unique_vals = np.unique(img)
    if len(unique_vals) < 2:
        # Blank or uniform image - save resized copy directly
        img_resized = cv2.resize(img, (512, 512))
        cv2.imwrite(str(output_filename), img_resized)
        return str(output_filename)

    # Filter image -> binary image (strokes = 255, background = 0)
    thresh_val = min(240, int(unique_vals[-1]) - 5) if unique_vals[-1] > 127 else int(unique_vals[0])
    _, img_filtered = cv2.threshold(img, thresh_val, 255, cv2.THRESH_BINARY_INV)

    vertical_boundaries = np.where(img_filtered.max(axis=1) > 0)[0]
    horizontal_boundaries = np.where(img_filtered.max(axis=0) > 0)[0]

    if len(vertical_boundaries) == 0 or len(horizontal_boundaries) == 0:
        img_resized = cv2.resize(img, (512, 512))
        cv2.imwrite(str(output_filename), img_resized)
        return str(output_filename)

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

    # Create a clean white square canvas and place the cropped region centered inside it
    half = target_side // 2
    src_x1 = max(0, cx - half)
    src_y1 = max(0, cy - half)
    src_x2 = min(w, cx + half)
    src_y2 = min(h, cy + half)

    cropped_inv = img_filtered[src_y1:src_y2, src_x1:src_x2]
    cropped = cv2.bitwise_not(cropped_inv)

    square = np.ones((target_side, target_side), dtype=np.uint8) * 255
    dst_y1 = max(0, (target_side - cropped.shape[0]) // 2)
    dst_x1 = max(0, (target_side - cropped.shape[1]) // 2)
    square[dst_y1:dst_y1 + cropped.shape[0], dst_x1:dst_x1 + cropped.shape[1]] = cropped

    img_resized = cv2.resize(square, (512, 512), interpolation=cv2.INTER_AREA)
    cv2.imwrite(str(output_filename), img_resized)

    return str(output_filename)
