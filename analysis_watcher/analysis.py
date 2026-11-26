import base64
import importlib.util
import os
from pathlib import Path
import re
import sys
import threading
import time
import cv2
import numpy as np
import requests

# Add project root and current dir so config_manager and heatmap can be imported
CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent
for p in (str(PROJECT_ROOT), str(CURRENT_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from config_manager import (
    DEFAULT_VISION_DESCRIPTION_PROMPT,
    DEFAULT_VISION_LABEL_PROMPT,
    THINKING_MODEL_HINTS,
    fast_local_ollama_url,
    get_configured_ollama_host,
    is_thinking_model_name,
    is_vision_model_name,
    load_env_vars,
    load_settings,
)
from heatmap import compute_zone_matrix
from preprocess_image import extract_ink_mask

_CACHE_LOCK = threading.Lock()
_CLIP_CACHE = {"model": None, "processor": None, "failed": False, "last_error": ""}
_SIGLIP_CACHE = {"model": None, "processor": None, "failed": False, "last_error": ""}
_RESNET_CACHE = {"model": None, "failed": False, "last_error": ""}
_LAST_OLLAMA_VISION_ERROR = ""
_VISION_TLS = threading.local()


def _set_last_ollama_vision_error(msg: str) -> None:
    global _LAST_OLLAMA_VISION_ERROR
    _LAST_OLLAMA_VISION_ERROR = msg
    _VISION_TLS.last_error = msg


def _get_last_ollama_vision_error() -> str:
    return getattr(_VISION_TLS, "last_error", None) or _LAST_OLLAMA_VISION_ERROR

ABSTRACT_FORM_PROMPTS = [
    ("a spiraling vortex of concentric loops", "Spiraling Vortex"),
    ("frantic jagged intersecting lines and sharp angles", "Jagged Intersections"),
    ("a minimalist delicate wandering single thread", "Wandering Thread"),
    ("a dense tangled web of overlapping strokes", "Tangled Web"),
    ("sweeping horizontal horizon-like eye movements", "Horizon Sweep"),
    ("vertical ascending and descending pillars of motion", "Vertical Ascent"),
    ("rhythmic oscillating waves and fluid curves", "Fluid Waves"),
    ("a scattered constellation of bursts and focal clusters", "Scattered Constellation"),
]

EMOTIONAL_TONE_PROMPTS = [
    ("restless tension and urgent searching energy", "Restless Urgency"),
    ("serene meditative calm and unhurried flow", "Meditative Flow"),
    ("explosive chaotic intensity and emotional release", "Chaotic Intensity"),
    ("hesitant cautious exploration of boundaries", "Cautious Exploration"),
    ("focused obsessive fixation on a central anchor", "Central Fixation"),
    ("dreamlike drifting introspection", "Dreamlike Drift"),
]

RORSCHACH_ARCHETYPE_PROMPTS = [
    ("an abstract inkblot resembling watching eyes or a mask", "Watching Eyes / Mask"),
    ("an abstract drawing resembling a bird or wings in flight", "Wings in Flight"),
    ("an abstract drawing resembling branching roots or a tree", "Branching Roots"),
    ("an abstract drawing resembling a winding labyrinth or maze", "Labyrinth"),
    ("an abstract drawing resembling a silhouette or twin figures", "Twin Silhouettes"),
    ("an abstract drawing resembling a cosmic nebula or star map", "Cosmic Star Map"),
    ("an abstract drawing resembling an infinity loop or hourglass", "Infinity Loop"),
    ("an abstract drawing resembling ripples and reflections on water", "Water Reflections"),
]


def compute_fractal_dimension(binary_mask: np.ndarray) -> float:
    """
    Compute the Minkowski-Bouligand Box-Counting Fractal Dimension (Df in [1.0, 2.0])
    of the visitor's gaze strokes, padding non-power-of-two canvases instead of
    truncating edge ink.
    """
    Z = binary_mask > 0
    if not np.any(Z):
        return 1.0

    p = min(Z.shape)
    n = int(np.floor(np.log2(p)))
    if n < 3:
        return 1.1

    max_exp = n - 2 if n >= 4 else n - 1
    sizes = 2 ** np.arange(max_exp, 0, -1)
    counts = []
    valid_sizes = []

    h, w = Z.shape
    for size in sizes:
        size_int = int(size)
        pad_h = (size_int - (h % size_int)) % size_int
        pad_w = (size_int - (w % size_int)) % size_int
        grid = np.pad(Z, ((0, pad_h), (0, pad_w)), mode="constant", constant_values=False) if (pad_h or pad_w) else Z
        gh, gw = grid.shape
        reshaped = grid.reshape(gh // size_int, size_int, gw // size_int, size_int)
        box_has_ink = np.any(reshaped, axis=(1, 3))
        cnt = int(np.sum(box_has_ink))
        if cnt > 0:
            counts.append(cnt)
            valid_sizes.append(size_int)

    if len(counts) < 2:
        return 1.15

    coeffs = np.polyfit(np.log(1.0 / np.array(valid_sizes)), np.log(counts), 1)
    return float(np.clip(coeffs[0], 1.0, 2.0))


def compute_spatial_entropy(zone_densities: list) -> float:
    """
    Compute normalized Shannon Spatial Entropy [0.0, 1.0] across the 3x3 gaze grid.
    0.0 = total fixation in one spot; 1.0 = uniform exploration across the entire canvas.
    """
    arr = np.array(zone_densities, dtype=np.float64)
    total = float(np.sum(arr))
    if total <= 1e-9:
        return 0.0
    probs = arr / total
    probs = probs[probs > 1e-9]
    entropy = -float(np.sum(probs * np.log2(probs)))
    max_entropy = float(np.log2(9.0))
    return float(np.clip(entropy / max_entropy, 0.0, 1.0))


def _dilated_dice(mask_a: np.ndarray, mask_b: np.ndarray) -> float:
    """Compute Sorensen-Dice overlap between two boolean masks (0.0 if both empty)."""
    denom = int(np.sum(mask_a)) + int(np.sum(mask_b))
    if denom == 0:
        return 0.0
    inter = int(np.sum(mask_a & mask_b))
    return float((2.0 * inter) / denom)


def _contour_depth(hier: np.ndarray, idx: int) -> int:
    """Return RETR_TREE depth (even = outer stroke boundary, odd = enclosed hole)."""
    depth = 0
    parent = int(hier[idx][3])
    while parent != -1:
        depth += 1
        parent = int(hier[parent][3])
    return depth


def _is_ribbon_uturn(binary: np.ndarray, p_curr: np.ndarray, u1: np.ndarray, u2: np.ndarray,
                     n1: float, n2: float, cos_turn: float) -> bool:
    """
    Detect 180-degree endpoint U-turns where findContours traces back along the
    opposite wall of an open thin stroke (vs genuine sharp directional corners).
    """
    if cos_turn > -0.75:
        return False
    h, w = binary.shape
    probe = min(n1, n2, 22.0)
    a = p_curr - probe * u1
    b = p_curr + probe * u2
    mid = 0.5 * (a + b)
    mx = int(np.clip(round(float(mid[0])), 0, w - 1))
    my = int(np.clip(round(float(mid[1])), 0, h - 1))
    y1, y2 = max(0, my - 1), min(h, my + 2)
    x1, x2 = max(0, mx - 1), min(w, mx + 2)
    return bool(np.any(binary[y1:y2, x1:x2] > 0))


def _count_sharp_bends(contours: list, hierarchy, binary: np.ndarray = None) -> tuple:
    """
    Distinguish true holes (odd RETR_TREE depth) from nested solid islands (even depth >= 2),
    filter open-stroke endpoint U-turns, and measure genuine sharp directional bends.
    Returns (valid_components, enclosed_loops, sharp_bends, total_perim).
    """
    if not contours or hierarchy is None:
        return 0, 0, 0, 0.0

    hier = hierarchy[0]
    valid_components = 0
    enclosed_loops = 0
    sharp_bends = 0
    total_perim = 0.0

    for idx, cnt in enumerate(contours):
        perim = float(cv2.arcLength(cnt, True))
        area = float(cv2.contourArea(cnt))
        if perim < 20.0 and area < 9.0:
            continue

        depth = _contour_depth(hier, idx)
        if depth % 2 == 1:
            if area >= 16.0:
                enclosed_loops += 1
            continue

        valid_components += 1
        total_perim += perim
        eps = float(np.clip(0.015 * perim, 4.0, 14.0))
        approx = cv2.approxPolyDP(cnt, eps, True).reshape(-1, 2)
        m = len(approx)
        if m < 3:
            continue

        for k in range(m):
            p_prev = approx[(k - 1) % m].astype(np.float32)
            p_curr = approx[k].astype(np.float32)
            p_next = approx[(k + 1) % m].astype(np.float32)
            v1 = p_curr - p_prev
            v2 = p_next - p_curr
            n1 = float(np.linalg.norm(v1))
            n2 = float(np.linalg.norm(v2))
            if n1 < 8.0 or n2 < 8.0:
                continue
            cos_turn = float(np.dot(v1, v2) / (n1 * n2))
            if cos_turn < 0.50:  # exterior turn > 60 deg
                if binary is not None and _is_ribbon_uturn(binary, p_curr, v1 / n1, v2 / n2, n1, n2, cos_turn):
                    continue
                sharp_bends += 1

    return valid_components, enclosed_loops, sharp_bends, total_perim


def analyze_stroke_geometry(filename: str, canvas_filename: str = None, dwell_grid=None) -> dict:
    """
    100% Self-Hosted Mathematical, Fractal, and Rorschach Geometry Analyzer.
    Extracts stroke density, 3x3 spatial attention distribution (aligned with heatmap),
    Shannon spatial entropy, box-counting fractal dimension (Df), bilateral Dice symmetry,
    and loop/angularity topology.
    """
    img = cv2.imread(str(filename), cv2.IMREAD_UNCHANGED)
    if img is None or img.size == 0:
        return {
            "summary_label": "Abstract Gaze Trace",
            "description": "An abstract gaze-tracked drawing on a blank canvas."
        }

    binary, _, _ = extract_ink_mask(img)
    h, w = binary.shape
    stroke_mask = (binary > 0).astype(np.float32)

    ink_ratio = float(np.mean(stroke_mask))
    if ink_ratio < 0.03:
        density_desc = "a light, thin trail with lots of untouched paper around it"
    elif ink_ratio < 0.10:
        density_desc = "a moderate amount of ink, leaving clear breathing room"
    elif ink_ratio < 0.22:
        density_desc = "a dense, layered build-up of ink"
    else:
        density_desc = "ink covering much of the page, worked over again and again"

    zone_names = [
        ["upper-left", "upper-center", "upper-right"],
        ["mid-left", "center", "mid-right"],
        ["lower-left", "lower-center", "lower-right"],
    ]
    zone_matrix = compute_zone_matrix(scribble_filename=canvas_filename or filename, dwell_grid=dwell_grid)
    zone_scores = []
    raw_densities = []
    for r in range(3):
        for c in range(3):
            val = float(zone_matrix[r, c])
            raw_densities.append(val)
            zone_scores.append((val, zone_names[r][c]))

    spatial_entropy = compute_spatial_entropy(raw_densities)
    if spatial_entropy < 0.45:
        entropy_desc = "attention stayed tightly in one or two places"
    elif spatial_entropy < 0.78:
        entropy_desc = "attention settled on a few anchors and made purposeful trips between them"
    else:
        entropy_desc = "attention roamed over almost the whole page"

    zone_scores.sort(key=lambda x: x[0], reverse=True)
    primary_zone = zone_scores[0][1]
    secondary_zone = zone_scores[1][1]
    least_zone = zone_scores[-1][1]

    fractal_dim = compute_fractal_dimension(binary)
    sparse = ink_ratio < 0.06
    if fractal_dim < 1.18:
        fractal_desc = "the path is direct and simple, with little doubling back"
    elif fractal_dim < 1.38:
        fractal_desc = "the path branches and wavers a little, like something growing"
    elif sparse:
        fractal_desc = "although the trail is thin, it doubles back and crosses itself many times"
    else:
        fractal_desc = "the path folds over itself again and again into a thick, intricate web"

    dilated = cv2.dilate(binary, np.ones((5, 5), dtype=np.uint8), iterations=1) > 0
    left_half = dilated[:, :w // 2]
    right_half_flipped = np.fliplr(dilated[:, w - (w // 2):])
    horiz_sym = _dilated_dice(left_half, right_half_flipped)

    top_half = dilated[:h // 2, :]
    bottom_half_flipped = np.flipud(dilated[h - (h // 2):, :])
    vert_sym = _dilated_dice(top_half, bottom_half_flipped)

    if horiz_sym > 0.62 and vert_sym > 0.62:
        symmetry_desc = "strikingly symmetrical and centered, like a folded inkblot"
    elif horiz_sym > 0.50:
        symmetry_desc = "roughly mirrored left to right"
    elif vert_sym > 0.50:
        symmetry_desc = "roughly balanced between top and bottom"
    else:
        symmetry_desc = "asymmetrical, pulled more to one side than the other"

    contours, hierarchy = cv2.findContours(binary, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
    num_components, enclosed_loops, sharp_bends, total_perim = _count_sharp_bends(contours, hierarchy, binary=binary)

    bends_per_100px = (sharp_bends / max(total_perim / 100.0, 1.0)) if total_perim > 0 else 0.0
    is_angular = sharp_bends >= 3 and bends_per_100px >= 0.16
    line_quality = (
        "angular, with abrupt changes of direction"
        if is_angular
        else "fluid, sweeping, and organic in its transitions"
    )

    # Dominant gesture is measured on the uncropped canvas when available (page-relative direction)
    gesture_src = binary
    if canvas_filename and str(canvas_filename) != str(filename):
        try:
            canvas_img = cv2.imread(str(canvas_filename), cv2.IMREAD_UNCHANGED)
            if canvas_img is not None and canvas_img.size > 0:
                canvas_mask, _, _ = extract_ink_mask(canvas_img)
                if np.any(canvas_mask > 0):
                    gesture_src = canvas_mask
        except Exception:
            gesture_src = binary
    gesture = _compute_gesture(gesture_src)
    direction = gesture["direction"]
    elongated = gesture["elongation"] >= 2.2 and ink_ratio < 0.12
    compact = gesture["x_span"] < 0.42 and gesture["y_span"] < 0.42

    if enclosed_loops == 0:
        loops_desc = "the trail never closes on itself"
    elif enclosed_loops <= 3:
        loops_desc = f"it closes into {enclosed_loops} small pocket{'s' if enclosed_loops > 1 else ''}"
    elif enclosed_loops <= 15:
        loops_desc = "it crosses itself several times, forming a handful of small pockets"
    else:
        loops_desc = "it crosses itself many times, knotting into dozens of small pockets"

    if horiz_sym > 0.62 and vert_sym > 0.62:
        archetype_hint = "Rorschach Mirror"
    elif num_components > 8 and enclosed_loops < 10:
        archetype_hint = "Saccadic Constellation"
    elif elongated and direction == "falling_diagonal":
        archetype_hint = "Descending Sweep"
    elif elongated and direction == "rising_diagonal":
        archetype_hint = "Ascending Sweep"
    elif elongated and direction == "horizontal":
        archetype_hint = "Horizon Scan"
    elif elongated and direction == "vertical":
        archetype_hint = "Vertical Ascent"
    elif enclosed_loops >= 35 or (enclosed_loops >= 28 and fractal_dim >= 1.53):
        archetype_hint = "Fractal Labyrinth"
    elif horiz_sym > 0.50:
        archetype_hint = "Bilateral Echo"
    elif spatial_entropy < 0.52 and enclosed_loops >= 6:
        archetype_hint = "Focused Vortex"
    elif compact and enclosed_loops >= 4:
        archetype_hint = "Gathered Knot"
    elif spatial_entropy >= 0.65 and enclosed_loops >= 6:
        archetype_hint = "Zigzag Drift" if is_angular else "Panoramic Currents"
    elif enclosed_loops >= 12:
        archetype_hint = "Intertwined Orbits"
    elif enclosed_loops >= 2:
        archetype_hint = "Woven Loops"
    elif fractal_dim >= 1.32:
        archetype_hint = "Woven Tapestry"
    else:
        archetype_hint = "Continuous Horizon Trace"

    def _plain(zone: str) -> str:
        return zone.replace("-", " ")

    # Plain observational language (no metric names/adjectives the LLM would parrot verbatim)
    description = (
        "Observation notes about this visitor's gaze drawing "
        "(private notes for you: never quote, list, or name these terms):\n"
        f"- Overall gesture: {gesture['description']}.\n"
        f"- Where the eyes lingered: most attention in the {_plain(primary_zone)} and {_plain(secondary_zone)}; "
        f"the {_plain(least_zone)} was left almost untouched.\n"
        f"- Spread of attention: {entropy_desc}.\n"
        f"- Amount of ink: {density_desc}.\n"
        f"- Path: {fractal_desc}; {loops_desc}.\n"
        f"- Line character: {line_quality}.\n"
        f"- Balance: {symmetry_desc}."
    )

    return {
        "summary_label": archetype_hint,
        "description": description,
        "gesture_desc": gesture["description"],
        "direction": direction,
        "elongation": round(gesture["elongation"], 2),
        "ink_ratio": round(ink_ratio, 4),
        "fractal_dimension": round(fractal_dim, 3),
        "spatial_entropy": round(spatial_entropy, 3),
        "horiz_symmetry": round(horiz_sym, 3),
        "vert_symmetry": round(vert_sym, 3),
        "primary_zone": primary_zone,
        "secondary_zone": secondary_zone,
        "least_zone": least_zone,
        "enclosed_loops": enclosed_loops,
        "num_components": num_components,
        "sharp_bends": sharp_bends,
        "bends_per_100px": round(bends_per_100px, 3),
        "line_quality": "angular" if is_angular else "fluid",
    }


def _compute_gesture(binary: np.ndarray) -> dict:
    """
    Describe the dominant movement of the ink in plain language using a PCA of ink pixel
    coordinates normalized to the canvas (so diagonals are relative to the page, not pixels).
    """
    result = {"direction": "none", "elongation": 1.0, "x_span": 0.0, "y_span": 0.0,
              "description": "a few faint marks"}
    ys, xs = np.nonzero(binary > 0)
    if len(xs) < 20:
        return result
    h, w = binary.shape[:2]
    if len(xs) > 60000:
        idx = np.random.default_rng(0).choice(len(xs), 60000, replace=False)
        xs, ys = xs[idx], ys[idx]
    nx = xs.astype(np.float64) / max(1, w)
    ny = ys.astype(np.float64) / max(1, h)
    x_span = float(np.percentile(nx, 95) - np.percentile(nx, 5))
    y_span = float(np.percentile(ny, 95) - np.percentile(ny, 5))
    cov = np.cov(np.vstack([nx, ny]))
    evals, evecs = np.linalg.eigh(cov)
    l_small, l_big = max(float(evals[0]), 1e-9), max(float(evals[1]), 1e-9)
    elongation = float(np.sqrt(l_big / l_small))
    vx, vy = float(evecs[0, 1]), float(evecs[1, 1])
    angle = float(np.degrees(np.arctan2(vy, vx))) % 180.0  # image y grows downward

    cx, cy = float(np.mean(nx)), float(np.mean(ny))
    horiz = "left" if cx < 0.38 else ("right" if cx > 0.62 else "middle")
    vert = "upper" if cy < 0.38 else ("lower" if cy > 0.62 else "middle")
    if horiz == "middle" and vert == "middle":
        where = "the middle of the page"
    elif horiz == "middle":
        where = f"the {vert} half of the page"
    elif vert == "middle":
        where = f"the {horiz} side of the page"
    else:
        where = f"the {vert} {horiz} of the page"

    if elongation >= 2.2:
        if angle < 18 or angle > 162:
            direction = "horizontal"
            desc = "one long horizontal journey across the page, like scanning a horizon"
        elif 72 <= angle <= 108:
            direction = "vertical"
            desc = "a mostly vertical climb or fall through the page"
        elif angle < 90:
            direction = "falling_diagonal"
            desc = "a long slanting sweep from the upper left down toward the lower right"
        else:
            direction = "rising_diagonal"
            desc = "a long slanting sweep rising from the lower left toward the upper right"
    elif x_span < 0.42 and y_span < 0.42:
        direction = "compact"
        desc = f"a compact cluster gathered in {where}, with the rest of the page left open"
    elif x_span > 0.6 and y_span > 0.6:
        direction = "wide"
        desc = "a wandering path that reaches out over most of the page"
    else:
        direction = "loose"
        desc = f"a loose, roundish wandering centred on {where}"
    result.update({"direction": direction, "elongation": elongation, "x_span": x_span,
                   "y_span": y_span, "description": desc})
    return result


_GENERIC_VISION_LABELS = {
    "line", "lines", "drawing", "line drawing", "abstract drawing", "abstract line drawing",
    "abstract art", "abstract sketch", "sketch", "scribble", "scribbles", "black lines",
    "black and white", "black and white drawing", "black and white line drawing",
    "white background", "blank", "nothing", "unknown", "none", "ink", "inkblot",
    "rorschach", "rorschach inkblot", "abstract", "shape", "shapes", "doodle", "doodles",
    "strokes", "abstract lines", "tangled lines", "messy lines",
}

_GENERIC_MEDIA_WORDS = {
    "a", "an", "the", "and", "of", "in", "on", "with", "abstract", "black", "white",
    "line", "lines", "drawing", "sketch", "scribble", "scribbles", "ink", "inkblot",
    "rorschach", "image", "picture", "art", "artwork", "doodle", "strokes", "stroke",
    "simple", "random", "messy", "tangled", "minimalist", "background", "canvas", "shape", "shapes",
}

_SMALL_TITLE_WORDS = {"a", "an", "and", "as", "at", "by", "for", "from", "in", "into", "of", "on", "or", "the", "to", "with"}

# Known degenerate token priors of tiny captioning VLMs (e.g. moondream) on white line drawings
_DEGENERATE_VISION_LABELS = {"ike", "lke", "ikea", "iike", "i", "it", "ok", "yes", "no", "n/a", "na"}
# Plausible-but-overused labels accepted only when the stroke geometry actually supports them
_GEOMETRY_GATED_VISION_LABELS = {"urchin", "sea urchin", "urchins"}
_SHORT_LABEL_ALLOWLIST = {"owl", "eye", "cat", "dog", "fox", "bee", "sun", "web", "elk", "ant", "bat", "eel",
                          "ram", "yak", "cow", "pig", "hen", "jay", "emu", "fly", "key", "cup", "hat", "net",
                          "sea", "bay", "map", "kite", "ear", "arm", "leg", "lip", "toe", "egg"}
_URL_LIKE_RE = re.compile(r"https?://|www\.|\b[\w-]+\.(?:com|org|net|io|co|info|html?|php)\b", re.IGNORECASE)
_MIXED_ALNUM_RE = re.compile(r"\b(?=\w*\d)(?=\w*[a-zA-Z])\w{4,}\b")


def is_degenerate_vision_text(text: str) -> bool:
    """Detect garbage VLM output: URLs, underscore runs, letter/digit gibberish, or token loops."""
    s = str(text or "").strip()
    if not s:
        return True
    if _URL_LIKE_RE.search(s) or "__" in s or _MIXED_ALNUM_RE.search(s):
        return True
    letters = sum(ch.isalpha() for ch in s)
    if letters < max(2, int(0.6 * len(s.replace(" ", "")))):
        return True
    tokens = re.findall(r"[a-zA-Z]+", s.lower())
    if len(tokens) >= 6:
        counts = {}
        for t in tokens:
            counts[t] = counts.get(t, 0) + 1
        if max(counts.values()) >= max(4, len(tokens) // 3) and len(set(tokens)) <= len(tokens) // 2:
            return True
    return False

_VISION_PREAMBLE_PATTERNS = [
    r"^\s*(?:\d+\s*[\)\.\:\-]\s*)+",
    r"^\s*[\-\*\u2022]+\s*",
    r"^\s*(?:title|archetype|pareidolia|symbol|shape|object|creature|answer|label|image)\s*[:\-]\s*",
    r"^\s*(?:in\s+this\s+(?:image|drawing|sketch|inkblot)\s*,?\s*)",
    r"^\s*(?:this|the)\s+(?:abstract\s+)?(?:black[\-\s]and[\-\s]white\s+)?(?:line\s+)?(?:drawing|sketch|image|inkblot|lines?|strokes?|artwork|picture|composition)\s+(?:most\s+)?(?:closely\s+|vaguely\s+|strongly\s+)?(?:resembles?|looks?\s+like|depicts?|shows?|appears?\s+to\s+be|suggests?|reminds?\s+(?:me\s+)?of|is\s+similar\s+to|could\s+be|might\s+be|is)\s+",
    r"^\s*(?:it|they|this)\s+(?:most\s+)?(?:closely\s+|vaguely\s+|strongly\s+)?(?:resembles?|looks?\s+like|depicts?|shows?|appears?\s+to\s+be|suggests?|reminds?\s+(?:me\s+)?of|could\s+be|might\s+be|is)\s+",
    r"^\s*(?:i\s+(?:can\s+)?see|looks?\s+like|resembles?|depicts?|shows?)\s+",
    r"^\s*(?:an?\s+)?(?:abstract\s+)?(?:black[\-\s]and[\-\s]white\s+)?(?:line\s+)?(?:drawing|sketch|illustration|silhouette|outline|image|representation|depiction)\s+of\s+",
    r"^\s*(?:a|an|the)\s+",
]


def _title_case_archetype(text: str) -> str:
    words = text.split()
    out = []
    for idx, w in enumerate(words):
        low = w.lower()
        if idx > 0 and low in _SMALL_TITLE_WORDS:
            out.append(low)
        else:
            out.append(low.capitalize())
    return " ".join(out)


def clean_vision_archetype_label(raw_text: str, fallback: str = "") -> str:
    """
    Filter conversational VLM outputs (e.g. '1) The lines resemble a snake.') into a clean,
    concise gallery badge archetype (e.g. 'Snake'). Falls back cleanly if the model only
    returns generic boilerplate like 'abstract line drawing'.
    """
    if not raw_text or not str(raw_text).strip():
        return fallback

    # Pick first non-empty line, strip markdown formatting/quotes
    lines = [ln.strip() for ln in str(raw_text).splitlines() if ln.strip()]
    if not lines:
        return fallback
    s = lines[0]
    if is_degenerate_vision_text(s):
        return fallback
    s = re.sub(r"[`*_~]+", "", s).strip().strip("\"'“”‘’()[]")

    # Apply preamble stripping iteratively (e.g., "1) The lines resemble a ..." -> "...")
    for _ in range(4):
        prev = s
        for pat in _VISION_PREAMBLE_PATTERNS:
            s = re.sub(pat, "", s, flags=re.IGNORECASE).strip()
        s = s.strip("\"'“”‘’()[]")
        if s == prev:
            break

    # Take first sentence / clause before period, semicolon, question, exclamation, or parenthetical aside
    s = re.split(r"[\.!?;]|(?:\s+[—–-]\s+)|\(", s, maxsplit=1)[0].strip()
    # Remove trailing filler like "on a white background" or "made of lines"
    s = re.sub(
        r"\s+(?:on\s+a\s+white\s+background|drawn\s+with\s+.*|made\s+(?:of|from)\s+.*|in\s+black\s+and\s+white)$",
        "",
        s,
        flags=re.IGNORECASE,
    ).strip()
    s = re.sub(r"\s+", " ", s).strip(" ,:-•\"'“”‘’")

    # Re-strip leading article if sentence-splitting exposed one
    s = re.sub(r"^(?:a|an|the)\s+", "", s, flags=re.IGNORECASE).strip()

    if not s or len(s) < 2:
        return fallback
    low_s = s.lower()
    if low_s in _GENERIC_VISION_LABELS or all(w in _GENERIC_MEDIA_WORDS for w in low_s.split()):
        return fallback
    if is_degenerate_vision_text(s) or low_s in _DEGENERATE_VISION_LABELS:
        return fallback
    if " " not in low_s and len(low_s) <= 3 and low_s not in _SHORT_LABEL_ALLOWLIST:
        return fallback

    # Cap to a clean 4-word / 34-char gallery pill title
    words = s.split()
    if len(words) > 4:
        # If it still looks like a full conversational sentence rather than a short archetype, use fallback
        if any(w.lower() in ("is", "are", "was", "were", "that", "which", "because", "there") for w in words):
            return fallback
        words = words[:4]
        s = " ".join(words)

    if len(s) > 34:
        s = s[:34].rsplit(" ", 1)[0].strip() or s[:34].strip()

    if not s or s.lower() in _GENERIC_VISION_LABELS:
        return fallback
    return _title_case_archetype(s)


def _parse_custom_zero_shot_prompts(raw_text: str, default_pairs: list, prefix: str = "") -> list:
    """Parse newline/comma-separated custom concepts into (prompt_text, short_label) tuples."""
    if not raw_text or not str(raw_text).strip():
        return default_pairs
    items = [p.strip() for p in re.split(r"[\n,;]+", str(raw_text)) if p.strip()]
    if len(items) < 2:
        return default_pairs
    pairs = []
    for item in items[:24]:
        label = _title_case_archetype(item[:36])
        prompt = f"{prefix}{item}" if prefix and not item.lower().startswith(("a ", "an ", "the ")) else item
        pairs.append((prompt, label))
    return pairs


def _redact_error_secrets(text: str) -> str:
    """Strip URL userinfo and common secret query parameters from diagnostic errors."""
    safe = re.sub(r"(?i)([?&](?:key|api_key|token)=)[^&\s]+", r"\1***", str(text))
    safe = re.sub(r"(?i)(https?://)([^/@\s]+)@", r"\1***@", safe)
    return safe


_THINKING_MODEL_HINTS = THINKING_MODEL_HINTS
_THINK_TAG_RE = re.compile(r"<(?:think|thought)>.*?</(?:think|thought)>", re.DOTALL | re.IGNORECASE)
_GEMMA4_PX_PER_SOFT_TOKEN = 48 * 48  # 2304 px/token -> 280 tokens ~= 645K px


def _is_thinking_model_name(model_name: str) -> bool:
    return is_thinking_model_name(model_name)


def _strip_think_tags(text: str) -> str:
    if not text:
        return ""
    return _THINK_TAG_RE.sub("", str(text)).strip()


def _encode_image_for_vision(filename: str, token_budget: int = 280) -> str:
    """
    Encode drawing PNG to base64, optionally downscaling (aspect-ratio preserved)
    to match the configured Gemma 4 soft-token pixel budget (token_budget * 2304 px).
    Never upscales, and falls back to raw file bytes if image decoding fails.
    """
    raw_bytes = Path(filename).read_bytes()
    try:
        budget = int(token_budget or 0)
    except (TypeError, ValueError):
        budget = 0
    if budget > 0:
        max_pixels = budget * _GEMMA4_PX_PER_SOFT_TOKEN
        try:
            img = cv2.imread(str(filename), cv2.IMREAD_UNCHANGED)
            if img is not None and img.size > 0:
                h, w = img.shape[:2]
                if h * w > max_pixels:
                    scale = (max_pixels / float(h * w)) ** 0.5
                    new_w = max(32, int(round(w * scale)))
                    new_h = max(32, int(round(h * scale)))
                    resized = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)
                    ok, buf = cv2.imencode(".png", resized)
                    if ok:
                        return base64.b64encode(buf.tobytes()).decode("utf-8")
        except Exception:
            pass
    return base64.b64encode(raw_bytes).decode("utf-8")


def run_ollama_vision_analysis(filename: str, deadline: float = None, cancel_event=None):
    """
    100% Self-Hosted Local Ollama Vision Analyzer (e.g. moondream, llava, gemma3, gemma4).
    Uses separate configurable pareidolia label & visual description prompts, Ollama options
    (temperature, num_predict, timeout, thinking mode, vision token budget), and clean badge filtering.
    """
    _set_last_ollama_vision_error("")
    try:
        settings = load_settings()
        ai_cfg = settings.get("ai", {})
        vision_cfg = ai_cfg.get("vision", {}) if isinstance(ai_cfg.get("vision"), dict) else {}
        vision_model = (ai_cfg.get("ollama_vision_model") or "moondream").strip()
        host = get_configured_ollama_host(settings)
        if not host.startswith(("http://", "https://")):
            _set_last_ollama_vision_error(f"Invalid Ollama host URL: {_redact_error_secrets(host)}")
            return None

        label_prompt = (vision_cfg.get("ollama_label_prompt") or "").strip() or DEFAULT_VISION_LABEL_PROMPT
        desc_prompt = (vision_cfg.get("ollama_description_prompt") or "").strip()
        include_desc = vision_cfg.get("ollama_include_description", True) is not False
        temperature = float(vision_cfg.get("ollama_temperature", 0.4))
        max_tokens = int(vision_cfg.get("ollama_max_tokens", 280))
        timeout_sec = int(vision_cfg.get("ollama_timeout", 60))
        thinking_mode = str(vision_cfg.get("ollama_thinking") or "auto").strip().lower()
        token_budget = int(vision_cfg.get("vision_token_budget", 280) or 280)
        clean_label = vision_cfg.get("clean_label", True) is not False

        img_b64 = _encode_image_for_vision(filename, token_budget=token_budget)

        is_thinking_model = _is_thinking_model_name(vision_model)
        if thinking_mode in ("auto", "off"):
            # Fast exhibition perception: disable thinking on thinking-capable VLMs unless explicitly requested,
            # and omit 'think' completely for non-thinking models (Ollama returns 400 if unsupported).
            think_arg = False if is_thinking_model else None
        elif thinking_mode in ("low", "medium", "high"):
            think_arg = thinking_mode
        else:
            think_arg = False if is_thinking_model else None

        thinking_active = think_arg in ("low", "medium", "high", True)

        def _step_timeout():
            if cancel_event is not None and cancel_event.is_set():
                raise requests.exceptions.ReadTimeout("Vision analysis cancelled")
            if deadline is not None:
                rem = deadline - time.monotonic()
                if rem <= 0.2:
                    raise requests.exceptions.ReadTimeout("Vision analysis deadline exceeded")
                return min(float(timeout_sec), rem)
            return timeout_sec

        def _call_ollama_vision(prompt_text: str, num_predict: int) -> str:
            payload = {
                "model": vision_model,
                "prompt": prompt_text,
                "images": [img_b64],
                "stream": False,
                "options": {
                    "temperature": temperature,
                    "num_predict": num_predict,
                },
            }
            if think_arg is not None:
                payload["think"] = think_arg

            resp = requests.post(f"{host}/api/generate", json=payload, timeout=_step_timeout())

            # Graceful fallback if a model only supports boolean think or does not support thinking at all
            if resp.status_code == 400 and "think" in payload:
                if isinstance(payload["think"], str):
                    payload["think"] = True
                    resp = requests.post(f"{host}/api/generate", json=payload, timeout=_step_timeout())
                if resp.status_code == 400 and "think" in payload:
                    payload.pop("think", None)
                    resp = requests.post(f"{host}/api/generate", json=payload, timeout=_step_timeout())

            resp.raise_for_status()
            data = resp.json()
            text_out = _strip_think_tags(data.get("response") or "")

            # Safety net: if thinking consumed the entire num_predict budget leaving empty response, retry with think=False
            if not text_out and data.get("thinking") and payload.get("think") is not False:
                print(
                    f"[analysis] Note: Ollama vision '{vision_model}' exhausted token budget ({num_predict}) "
                    "during thinking; retrying directly with think=False."
                )
                payload["think"] = False
                retry_resp = requests.post(f"{host}/api/generate", json=payload, timeout=_step_timeout())
                if retry_resp.ok:
                    text_out = _strip_think_tags(retry_resp.json().get("response") or "")

            return text_out

        # Do not clamp Pass 1 to 48/64 tokens when thinking is active or thinking tokens will starve the badge output
        pass1_tokens = max(max_tokens, 280) if thinking_active else min(max_tokens, 64)
        pass2_tokens = max(max_tokens, 280) if thinking_active else max_tokens

        raw_label_text = _call_ollama_vision(label_prompt, pass1_tokens)
        if not raw_label_text:
            _set_last_ollama_vision_error(f"Ollama vision model '{vision_model}' returned empty response")
            return None

        filtered_label = (
            clean_vision_archetype_label(raw_label_text, fallback="")
            if clean_label
            else raw_label_text.splitlines()[0].strip()[:45]
        )

        visual_desc = ""
        if include_desc and desc_prompt:
            try:
                visual_desc = _call_ollama_vision(desc_prompt, pass2_tokens)
            except Exception as desc_err:
                print(f"[analysis] Note: Ollama vision description pass skipped ({_redact_error_secrets(desc_err)}).")
        if visual_desc and is_degenerate_vision_text(visual_desc):
            print(f"[analysis] Note: discarded degenerate vision description from '{vision_model}'.")
            visual_desc = ""

        desc_lines = [f"What a small vision model ({vision_model}) saw in the image (may be imprecise):"]
        if filtered_label:
            desc_lines.append(f"- Pareidolia Archetype (it resembles): {filtered_label}")
        elif not clean_label and not is_degenerate_vision_text(raw_label_text) and len(raw_label_text) <= 120:
            desc_lines.append(f"- Pareidolia Impression: {raw_label_text.strip()}")
        if visual_desc:
            desc_lines.append(f"- Visual Character: {visual_desc}")
        if len(desc_lines) == 1:
            desc_lines = []

        return {
            "summary_label": filtered_label,
            "raw_label": raw_label_text,
            "vision_description": visual_desc,
            "description": "\n".join(desc_lines),
            "engine": f"ollama_vision:{vision_model}",
            "options": {
                "temperature": temperature,
                "max_tokens": max_tokens,
                "timeout": timeout_sec,
                "thinking": thinking_mode,
                "vision_token_budget": token_budget,
                "clean_label": clean_label,
                "include_description": include_desc,
            },
        }
    except Exception as e:
        safe_err = _redact_error_secrets(e)
        _set_last_ollama_vision_error(safe_err)
        print(f"[analysis] Note: Local Ollama Vision model unavailable ({safe_err}), using geometric profile.")
        return None


def _run_zero_shot_hf_model(filename: str, model_id: str, cache_dict: dict, is_siglip: bool = False):
    """Helper for self-hosted HuggingFace CLIP or SigLIP zero-shot abstract art evaluation."""
    if cache_dict["failed"]:
        return None
    try:
        import torch
        from PIL import Image
        from transformers import AutoModel, AutoProcessor

        settings = load_settings()
        vision_cfg = settings.get("ai", {}).get("vision", {}) if isinstance(settings.get("ai", {}).get("vision"), dict) else {}
        archetype_prompts = _parse_custom_zero_shot_prompts(
            vision_cfg.get("zero_shot_archetypes", ""),
            RORSCHACH_ARCHETYPE_PROMPTS,
            prefix="an abstract inkblot drawing resembling ",
        )
        tone_prompts = _parse_custom_zero_shot_prompts(
            vision_cfg.get("zero_shot_tones", ""),
            EMOTIONAL_TONE_PROMPTS,
            prefix="an abstract drawing evoking ",
        )

        if cache_dict["model"] is None:
            cache_dict["processor"] = AutoProcessor.from_pretrained(model_id)
            cache_dict["model"] = AutoModel.from_pretrained(model_id)
            cache_dict["model"].eval()

        processor = cache_dict["processor"]
        model = cache_dict["model"]
        with Image.open(filename) as im:
            pil_img = im.convert("RGB")

        def top_match(prompt_pairs):
            texts = [p[0] for p in prompt_pairs]
            pad_mode = "max_length" if is_siglip else True
            inputs = processor(text=texts, images=pil_img, return_tensors="pt", padding=pad_mode)
            with torch.no_grad():
                outputs = model(**inputs)
                logits = outputs.logits_per_image[0]
                probs = (torch.sigmoid(logits) if is_siglip else logits.softmax(dim=-1)).cpu().numpy()
            best_idx = int(np.argmax(probs))
            return prompt_pairs[best_idx][1], prompt_pairs[best_idx][0], float(probs[best_idx])

        form_label, form_desc, _ = top_match(ABSTRACT_FORM_PROMPTS)
        tone_label, tone_desc, _ = top_match(tone_prompts)
        rorschach_label, rorschach_desc, _ = top_match(archetype_prompts)
        tag = "SigLIP" if is_siglip else "CLIP"

        cache_dict["last_error"] = ""
        return {
            "summary_label": f"{rorschach_label} ({tone_label})",
            "raw_label": f"{rorschach_label} ({tone_label})",
            "description": (
                f"Self-Hosted {tag} Abstract Art & Rorschach Perception:\n"
                f"- Visual Form: Resembles {form_desc} ({form_label}).\n"
                f"- Emotional Resonance: Evokes {tone_desc} ({tone_label}).\n"
                f"- Rorschach Pareidolia: Suggests {rorschach_desc} ({rorschach_label})."
            ),
            "engine": "siglip" if is_siglip else "clip",
        }
    except (ImportError, ModuleNotFoundError) as e:
        cache_dict["failed"] = True
        cache_dict["last_error"] = str(e)
        print(f"[analysis] Note: {model_id} dependencies not available ({e}), using geometric & fractal analysis.")
        return None
    except Exception as e:
        cache_dict["last_error"] = str(e)
        print(f"[analysis] Note: {model_id} not available ({e}), using geometric & fractal analysis.")
        return None


def run_clip_abstract_analysis(filename: str):
    """Self-hosted OpenAI CLIP zero-shot abstract art & Rorschach analyzer."""
    return _run_zero_shot_hf_model(filename, "openai/clip-vit-base-patch32", _CLIP_CACHE, is_siglip=False)


def run_siglip_abstract_analysis(filename: str):
    """Self-hosted Google SigLIP zero-shot abstract art & Rorschach analyzer."""
    return _run_zero_shot_hf_model(filename, "google/siglip-base-patch16-224", _SIGLIP_CACHE, is_siglip=True)


def run_resnet_classification(filename: str):
    """Legacy ImageNet ResNet50 classification (cached in memory)."""
    if _RESNET_CACHE["failed"]:
        return None
    try:
        from tensorflow.keras.applications.resnet50 import ResNet50, decode_predictions
        from tensorflow.keras.preprocessing.image import img_to_array, load_img

        settings = load_settings()
        vision_cfg = settings.get("ai", {}).get("vision", {}) if isinstance(settings.get("ai", {}).get("vision"), dict) else {}
        top_k = max(1, min(5, int(vision_cfg.get("resnet_top_k", 1))))

        if _RESNET_CACHE["model"] is None:
            _RESNET_CACHE["model"] = ResNet50(weights="imagenet")

        image = load_img(str(filename), target_size=(224, 224))
        arr = img_to_array(image)
        arr = np.expand_dims(arr, axis=0)
        y_pred = _RESNET_CACHE["model"].predict(arr, verbose=0)
        decoded = decode_predictions(y_pred, top=top_k)[0]
        labels = [item[1].replace("_", " ").capitalize() for item in decoded]
        label = labels[0]
        _RESNET_CACHE["last_error"] = ""
        return {
            "summary_label": label,
            "raw_label": ", ".join(labels),
            "description": f"ImageNet Pareidolia Association: {', '.join(labels)}",
            "engine": "resnet50",
        }
    except (ImportError, ModuleNotFoundError) as e:
        _RESNET_CACHE["failed"] = True
        _RESNET_CACHE["last_error"] = str(e)
        print(f"[analysis] Note: TensorFlow ResNet50 dependencies unavailable ({e}).")
        return None
    except Exception as e:
        _RESNET_CACHE["last_error"] = str(e)
        print(f"[analysis] Note: TensorFlow ResNet50 unavailable ({e}).")
        return None


def list_local_ollama_models(settings: dict = None) -> tuple:
    """
    Return (ollama_running: bool, installed_models: list[str]) by querying the configured
    Ollama host's /api/tags, falling back to scanning ~/.ollama/models/manifests on disk.
    """
    cfg = settings or load_settings()
    host = fast_local_ollama_url(get_configured_ollama_host(cfg))
    if host.startswith(("http://", "https://")):
        try:
            resp = requests.get(f"{host}/api/tags", timeout=0.6)
            if resp.ok:
                names = [str(m.get("name", "")).strip() for m in resp.json().get("models", []) if m.get("name")]
                return True, sorted(set(names))
        except Exception:
            pass

    # Disk fallback when Ollama server isn't currently running
    disk_models = set()
    try:
        ollama_models_root = Path(os.environ.get("OLLAMA_MODELS") or (Path.home() / ".ollama" / "models"))
        manifests_dir = ollama_models_root / "manifests"
        if manifests_dir.exists():
            for manifest_file in manifests_dir.rglob("*"):
                if not manifest_file.is_file():
                    continue
                rel_parts = manifest_file.relative_to(manifests_dir).parts
                # Standard layout: registry.ollama.ai/library/<model>/<tag>
                if len(rel_parts) >= 3:
                    tag = rel_parts[-1]
                    model_name = rel_parts[-2]
                    if len(rel_parts) >= 4 and rel_parts[-3] != "library":
                        model_name = f"{rel_parts[-3]}/{model_name}"
                    disk_models.add(f"{model_name}:{tag}")
    except Exception:
        pass
    return False, sorted(disk_models)


def _ollama_model_installed(model_name: str, installed_models: list) -> bool:
    target = (model_name or "").strip().lower()
    if not target:
        return False
    for m in installed_models:
        ml = m.lower()
        if ml == target or ml == f"{target}:latest" or (":" not in target and ml.startswith(f"{target}:")):
            return True
    return False


def _is_vision_model_name(model_name: str) -> bool:
    return is_vision_model_name(model_name)


def get_ai_availability(settings: dict = None) -> dict:
    """
    Inspect locally available Python ML packages, pulled Ollama models, and configured API keys
    (never exposing secret values) so Admin UI can show live availability and block applying
    unavailable models/vision modes.
    """
    cfg = settings or load_settings()
    ai_cfg = cfg.get("ai", {})
    env = load_env_vars()

    has_torch = importlib.util.find_spec("torch") is not None
    has_transformers = importlib.util.find_spec("transformers") is not None
    has_tensorflow = importlib.util.find_spec("tensorflow") is not None

    ollama_running, ollama_models = list_local_ollama_models(cfg)
    ollama_vision_models = [m for m in ollama_models if _is_vision_model_name(m)]
    configured_ovm = (ai_cfg.get("ollama_vision_model") or "moondream").strip()
    ovm_installed = _ollama_model_installed(configured_ovm, ollama_models)

    env_keys = {
        "gemini": bool((env.get("GEMINI_API_KEY") or env.get("GOOGLE_API_KEY") or "").strip()),
        "openai": bool((env.get("OPENAI_API_KEY") or "").strip()),
        "groq": bool((env.get("GROQ_API_KEY") or "").strip()),
        "openrouter": bool((env.get("OPENROUTER_API_KEY") or "").strip()),
        "anthropic": bool((env.get("ANTHROPIC_API_KEY") or "").strip()),
        "buas": bool(
            ((env.get("ACCESS_TOKEN") or "").strip() and (env.get("USER_KEY") or "").strip())
            or (env.get("BUAS_API_KEY") or "").strip()
        ),
    }

    saved_keys = {
        role: bool((ai_cfg.get(role, {}).get("api_key") or "").strip())
        for role in ("analysis", "poem")
    }

    eligible_failovers = []
    if env_keys["gemini"] or any((ai_cfg.get(r, {}).get("provider") == "gemini" and saved_keys[r]) for r in ("analysis", "poem")):
        eligible_failovers.append("gemini")
    if env_keys["groq"]:
        eligible_failovers.append("groq")
    if env_keys["openrouter"]:
        eligible_failovers.append("openrouter")
    if env_keys["openai"]:
        eligible_failovers.append("openai")
    if env_keys["anthropic"] or any((ai_cfg.get(r, {}).get("provider") == "anthropic" and saved_keys[r]) for r in ("analysis", "poem")):
        eligible_failovers.append("anthropic")
    if env_keys["buas"]:
        eligible_failovers.append("buas")
    if ollama_running:
        eligible_failovers.append("ollama")

    missing_hf = [pkg for pkg, ok in (("torch", has_torch), ("transformers", has_transformers)) if not ok]
    hf_ready = has_torch and has_transformers

    native_vision = _analysis_llm_handles_vision_natively(ollama_running_hint=ollama_running)
    vision_modes = {
        "auto": {
            "available": True,
            "reason": (
                "Ready (uses installed local vision model if available, otherwise built-in Fractal & Rorschach Geometry)"
            ),
        },
        "stroke_metrics": {
            "available": True,
            "reason": "Ready (built-in OpenCV & NumPy geometric, fractal, symmetry, and entropy analyzer — always offline)",
        },
        "ollama_vision": {
            "available": ovm_installed,
            "reason": (
                f"Ready (local Ollama model '{configured_ovm}' found)"
                if ovm_installed
                else f"Ollama vision model '{configured_ovm}' is not installed on this machine. Run: ollama pull {configured_ovm}"
            ),
        },
        "siglip": {
            "available": hf_ready,
            "reason": (
                "Ready (torch + transformers installed; downloads google/siglip-base-patch16-224 on first run)"
                if hf_ready
                else f"Missing Python packages: {', '.join(missing_hf)}. Install with: pip install torch transformers"
            ),
        },
        "clip": {
            "available": hf_ready,
            "reason": (
                "Ready (torch + transformers installed; downloads openai/clip-vit-base-patch32 on first run)"
                if hf_ready
                else f"Missing Python packages: {', '.join(missing_hf)}. Install with: pip install torch transformers"
            ),
        },
        "resnet50": {
            "available": has_tensorflow,
            "reason": (
                "Ready (TensorFlow installed for legacy ImageNet ResNet50)"
                if has_tensorflow
                else "Missing Python package: tensorflow. Install with: pip install tensorflow"
            ),
        },
        "vision": {
            "available": native_vision,
            "reason": (
                "Ready (Analysis LLM natively supports image input)"
                if native_vision
                else "Analysis LLM is text-only; choose a vision-capable Analysis model (e.g. llava, moondream, gemini, claude, gpt-4o)"
            ),
        },
    }

    return {
        "packages": {
            "torch": has_torch,
            "transformers": has_transformers,
            "tensorflow": has_tensorflow,
        },
        "ollama": {
            "running": ollama_running,
            "host": get_configured_ollama_host(cfg),
            "models": ollama_models,
            "vision_models": ollama_vision_models,
        },
        "env_keys": env_keys,
        "saved_keys": saved_keys,
        "eligible_failovers": eligible_failovers,
        "vision_modes": vision_modes,
    }


def _is_ollama_vision_model_ready() -> bool:
    """Fast local check (<=0.6s) whether configured ollama_vision_model is pulled locally."""
    try:
        settings = load_settings()
        ai_cfg = settings.get("ai", {})
        vision_model = ai_cfg.get("ollama_vision_model") or "moondream"
        host = fast_local_ollama_url(get_configured_ollama_host(settings))
        if not host.startswith(("http://", "https://")):
            return False
        resp = requests.get(f"{host}/api/tags", timeout=0.6)
        if not resp.ok:
            return False
        names = [m.get("name", "") for m in resp.json().get("models", [])]
        return any(n == vision_model or n.startswith(f"{vision_model}:") for n in names)
    except Exception:
        return False


def _analysis_llm_handles_vision_natively(ollama_running_hint=None) -> bool:
    """Return True when the configured analysis LLM already receives the image directly."""
    try:
        from chat import LLMModel
        settings = load_settings()
        acfg = settings.get("ai", {}).get("analysis", {})
        prov = (acfg.get("provider") or "ollama").lower()
        mod = (acfg.get("model") or "").strip()
        if prov in ("gemini", "anthropic"):
            return True
        if prov == "openai_compatible":
            return LLMModel._openai_compatible_supports_vision(mod)
        if prov == "ollama":
            if ollama_running_hint is False:
                return _is_vision_model_name(mod)
            base_url = (acfg.get("base_url") or f"{get_configured_ollama_host(settings)}/v1").rstrip("/")
            if not base_url.endswith("/v1") and "11434" in base_url and "/api" not in base_url:
                base_url += "/v1"
            return LLMModel._ollama_supports_vision(base_url, mod)
    except Exception:
        pass
    return False


def analyze_drawing(
    filename: str,
    mode: str = "auto",
    canvas_filename: str = None,
    dwell_grid=None,
    deadline: float = None,
    cancel_event=None,
) -> dict:
    """
    Comprehensive self-hosted drawing analysis returning a short `label` (archetype),
    rich `prompt_context` for the Psychoanalysis LLM, and vision diagnostics for vision.log.
    """
    t0 = time.monotonic()
    settings = load_settings()
    vision_cfg = settings.get("ai", {}).get("vision", {}) if isinstance(settings.get("ai", {}).get("vision"), dict) else {}
    badge_style = str(vision_cfg.get("badge_style") or "vision_only").strip().lower()

    geom = analyze_stroke_geometry(filename, canvas_filename=canvas_filename, dwell_grid=dwell_grid)
    sections = [geom["description"]]
    geom_label = geom["summary_label"]
    summary_label = geom_label

    extra = None
    effective_mode = "stroke_metrics"
    fallback_reason = ""

    if mode == "ollama_vision":
        extra = run_ollama_vision_analysis(filename, deadline=deadline, cancel_event=cancel_event)
        if extra is None:
            fallback_reason = f"Ollama vision unavailable ({_get_last_ollama_vision_error() or 'model not pulled or server offline'}); fell back to stroke_metrics"
    elif mode == "clip":
        with _CACHE_LOCK:
            extra = run_clip_abstract_analysis(filename)
            if extra is None:
                fallback_reason = f"CLIP unavailable ({_CLIP_CACHE.get('last_error') or 'torch/transformers not installed'}); fell back to stroke_metrics"
    elif mode == "siglip":
        with _CACHE_LOCK:
            extra = run_siglip_abstract_analysis(filename)
            if extra is None:
                fallback_reason = f"SigLIP unavailable ({_SIGLIP_CACHE.get('last_error') or 'torch/transformers not installed'}); fell back to stroke_metrics"
    elif mode == "resnet50":
        with _CACHE_LOCK:
            extra = run_resnet_classification(filename)
            if extra is None:
                fallback_reason = f"ResNet50 unavailable ({_RESNET_CACHE.get('last_error') or 'tensorflow not installed'}); fell back to stroke_metrics"
    elif mode == "vision":
        if _analysis_llm_handles_vision_natively():
            effective_mode = "direct_vision_llm+stroke_metrics"
        else:
            fallback_reason = "Configured Analysis LLM is text-only; image skipped, using stroke_metrics"
    elif mode == "auto":
        with _CACHE_LOCK:
            if _SIGLIP_CACHE["model"] is not None:
                extra = run_siglip_abstract_analysis(filename)
            elif _CLIP_CACHE["model"] is not None:
                extra = run_clip_abstract_analysis(filename)
        if extra is None:
            if _analysis_llm_handles_vision_natively():
                effective_mode = "auto(direct_vision_llm+stroke_metrics)"
            elif _is_ollama_vision_model_ready():
                extra = run_ollama_vision_analysis(filename, deadline=deadline, cancel_event=cancel_event)
            else:
                effective_mode = "auto(stroke_metrics)"

    raw_vision_label = ""
    vision_options = {}
    if extra:
        extra = dict(extra)
        vision_label = (extra.get("summary_label") or "").strip()
        # Overused VLM priors (e.g. moondream's "urchin") only survive on compact, spiky drawings
        if vision_label.lower() in _GEOMETRY_GATED_VISION_LABELS and not (
            geom.get("direction") == "compact" and int(geom.get("sharp_bends") or 0) >= 12
        ):
            vision_label = ""
            extra["summary_label"] = ""
            extra["description"] = "\n".join(
                ln for ln in str(extra.get("description") or "").splitlines() if not ln.startswith("- Pareidolia")
            )
            if not any(ln.startswith("- ") for ln in extra["description"].splitlines()):
                extra["description"] = ""
        if extra.get("description"):
            sections.insert(0, extra["description"])
        effective_mode = extra.get("engine", mode)
        raw_vision_label = extra.get("raw_label", "")
        vision_options = extra.get("options", {})

        if badge_style == "geometry_only":
            summary_label = geom_label
        elif badge_style == "combined":
            if vision_label and vision_label.lower() != geom_label.lower():
                summary_label = f"{vision_label} · {geom_label}"
            else:
                summary_label = vision_label or geom_label
        else:  # "vision_only"
            summary_label = vision_label or geom_label

    if "direct_vision_llm" in effective_mode:
        sections.append(
            "You can also see the drawing itself. Trust what you see over these notes if they disagree. "
            "If its overall shape genuinely suggests one concrete everyday image, you may use that as your "
            "single metaphor; otherwise stay with the movements of the eyes."
        )

    return {
        "label": summary_label,
        "raw_vision_label": raw_vision_label,
        "badge_style": badge_style,
        "prompt_context": "\n\n".join(sections),
        "geometry": geom,
        "requested_mode": mode,
        "effective_mode": effective_mode,
        "fallback_reason": fallback_reason,
        "vision_output": extra["description"] if extra else "",
        "vision_options": vision_options,
        "elapsed_sec": round(time.monotonic() - t0, 2),
    }
