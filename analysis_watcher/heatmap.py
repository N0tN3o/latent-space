import cv2
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt


def draw_heatmap(scribble_filename="image.png", heatmap_filename="heatmap.png"):
    """
    Create a 3x3 attention heatmap visualization illustrating the distribution
    of stroke density across regions of the scribble image.
    """
    img = cv2.imread(str(scribble_filename), cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise FileNotFoundError(f"Could not read scribble image: {scribble_filename}")

    _, binary = cv2.threshold(img, 200, 1, cv2.THRESH_BINARY_INV)

    h, w = binary.shape
    h_step, w_step = max(1, h // 3), max(1, w // 3)

    heatmap = np.zeros((3, 3), dtype=np.float32)

    for i in range(3):
        for j in range(3):
            region = binary[i * h_step:(i + 1) * h_step, j * w_step:(j + 1) * w_step]
            heatmap[i, j] = float(np.mean(region)) if region.size > 0 else 0.0

    max_val = float(heatmap.max())
    if max_val > 0:
        heatmap /= max_val

    fig, ax = plt.subplots(figsize=(3, 3), dpi=100)
    ax.imshow(heatmap, cmap='gray', interpolation='nearest', vmin=0.0, vmax=1.0)
    ax.axis('off')
    fig.subplots_adjust(left=0, right=1, top=1, bottom=0)
    fig.savefig(str(heatmap_filename), bbox_inches='tight', pad_inches=0)
    plt.close(fig)

    return str(heatmap_filename)
