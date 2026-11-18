import base64
from pathlib import Path
import sys
import threading
import cv2
import numpy as np
import requests

# Add project root and current dir so config_manager and heatmap can be imported
CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent
for p in (str(PROJECT_ROOT), str(CURRENT_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from config_manager import get_configured_ollama_host, load_settings
from heatmap import compute_zone_matrix
from preprocess_image import extract_ink_mask

_CACHE_LOCK = threading.Lock()
_CLIP_CACHE = {"model": None, "processor": None, "failed": False}
_SIGLIP_CACHE = {"model": None, "processor": None, "failed": False}
_RESNET_CACHE = {"model": None, "failed": False}

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
        density_desc = "sparse, minimalist, and fleeting"
    elif ink_ratio < 0.10:
        density_desc = "balanced and deliberate"
    elif ink_ratio < 0.22:
        density_desc = "dense, layered, and expressive"
    else:
        density_desc = "heavily saturated, intense, and consuming"

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
        entropy_desc = "tunnel-vision fixation concentrated tightly in specific zones"
    elif spatial_entropy < 0.78:
        entropy_desc = "selective anchoring with purposeful excursions across the canvas"
    else:
        entropy_desc = "wide-ranging, panoramic exploration across nearly all regions"

    zone_scores.sort(key=lambda x: x[0], reverse=True)
    primary_zone = zone_scores[0][1]
    secondary_zone = zone_scores[1][1]
    least_zone = zone_scores[-1][1]

    fractal_dim = compute_fractal_dimension(binary)
    if fractal_dim < 1.18:
        fractal_desc = f"low-complexity linear trajectory (D={fractal_dim:.2f}), reflecting direct, unknotted movement"
    elif fractal_dim < 1.38:
        fractal_desc = f"organic intermediate fractal complexity (D={fractal_dim:.2f}), echoing natural branching patterns"
    else:
        fractal_desc = f"high space-filling fractal intricacy (D={fractal_dim:.2f}), akin to dense abstract expressionist webs"

    dilated = cv2.dilate(binary, np.ones((5, 5), dtype=np.uint8), iterations=1) > 0
    left_half = dilated[:, :w // 2]
    right_half_flipped = np.fliplr(dilated[:, w - (w // 2):])
    horiz_sym = _dilated_dice(left_half, right_half_flipped)

    top_half = dilated[:h // 2, :]
    bottom_half_flipped = np.flipud(dilated[h - (h // 2):, :])
    vert_sym = _dilated_dice(top_half, bottom_half_flipped)

    if horiz_sym > 0.62 and vert_sym > 0.62:
        symmetry_desc = "strikingly symmetrical and centered, like a classic Rorschach inkblot"
    elif horiz_sym > 0.50:
        symmetry_desc = "horizontally mirrored with bilateral balance"
    elif vert_sym > 0.50:
        symmetry_desc = "vertically balanced between upper and lower realms"
    else:
        symmetry_desc = "decidedly asymmetrical, driven by directional drift and spontaneous tension"

    contours, hierarchy = cv2.findContours(binary, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
    num_components, enclosed_loops, sharp_bends, total_perim = _count_sharp_bends(contours, hierarchy, binary=binary)

    if enclosed_loops >= 6:
        structure_desc = f"an intricate lattice of {enclosed_loops} enclosed loops and recurring self-intersecting orbits"
        archetype_hint = "Fractal Labyrinth"
    elif enclosed_loops >= 2:
        structure_desc = f"intertwining paths forming {enclosed_loops} enclosed focal chambers"
        archetype_hint = "Intertwined Orbits"
    elif num_components > 8:
        structure_desc = "fragmented, staccato bursts of attention jumping rapidly across the canvas"
        archetype_hint = "Saccadic Constellation"
    elif fractal_dim >= 1.32:
        structure_desc = "a densely woven tapestry of searching movements"
        archetype_hint = "Woven Tapestry"
    else:
        structure_desc = "a continuous, unbroken stream-of-consciousness trajectory"
        archetype_hint = "Continuous Horizon Trace"

    bends_per_100px = (sharp_bends / max(total_perim / 100.0, 1.0)) if total_perim > 0 else 0.0
    line_quality = (
        "angular, darting, and electric with sudden directional shifts"
        if (sharp_bends >= 3 and bends_per_100px >= 0.16)
        else "fluid, sweeping, and organic in its transitions"
    )

    description = (
        f"Visual & Gaze Composition Profile:\n"
        f"- Archetype: {archetype_hint}\n"
        f"- Stroke Density: The marks are {density_desc}.\n"
        f"- Spatial Fixation & Entropy: Characterized by {entropy_desc}. "
        f"The visitor's gaze gravitated most strongly toward the {primary_zone} and {secondary_zone} regions, "
        f"while avoiding the {least_zone}.\n"
        f"- Fractal Complexity: Exhibits {fractal_desc}.\n"
        f"- Balance & Rorschach Symmetry: The composition is {symmetry_desc}.\n"
        f"- Movement & Topology: Characterized by {structure_desc}, with strokes that feel {line_quality}."
    )

    return {
        "summary_label": archetype_hint,
        "description": description,
        "ink_ratio": round(ink_ratio, 4),
        "fractal_dimension": round(fractal_dim, 3),
        "spatial_entropy": round(spatial_entropy, 3),
        "horiz_symmetry": round(horiz_sym, 3),
        "vert_symmetry": round(vert_sym, 3),
        "primary_zone": primary_zone,
        "secondary_zone": secondary_zone,
        "enclosed_loops": enclosed_loops,
    }


def run_ollama_vision_analysis(filename: str):
    """
    100% Self-Hosted Local Ollama Vision Analyzer (e.g. moondream, llava, llama3.2-vision).
    Inspects the abstract gaze drawing locally via Ollama and describes its pareidolia,
    emotional movement, and visual forms.
    """
    try:
        settings = load_settings()
        ai_cfg = settings.get("ai", {})
        vision_model = ai_cfg.get("ollama_vision_model") or "moondream"
        host = get_configured_ollama_host(settings)
        if not host.startswith(("http://", "https://")):
            return None

        img_b64 = base64.b64encode(Path(filename).read_bytes()).decode("utf-8")
        prompt = (
            "This is an abstract black-and-white line drawing created by human eye movements (gaze tracking). "
            "Treat it like a Rorschach inkblot or abstract expressionist sketch: "
            "1) What pareidolia shape, symbol, or archetype do these lines vaguely resemble? (Give a 2-4 word title on the first line). "
            "2) Describe the visual character of the lines, loops, tension, and negative space in 2 sentences."
        )
        resp = requests.post(
            f"{host}/api/generate",
            json={
                "model": vision_model,
                "prompt": prompt,
                "images": [img_b64],
                "stream": False,
            },
            timeout=30,
        )
        resp.raise_for_status()
        text = resp.json().get("response", "").strip()
        if not text:
            return None
        first_line = text.splitlines()[0].strip().strip("*#:- ")[:45]
        return {
            "summary_label": first_line or "Ollama Vision Reading",
            "description": f"Self-Hosted Vision Model ({vision_model}) Perception:\n{text}",
        }
    except Exception as e:
        print(f"[analysis] Note: Local Ollama Vision model unavailable ({e}), using geometric profile.")
        return None


def _run_zero_shot_hf_model(filename: str, model_id: str, cache_dict: dict, is_siglip: bool = False):
    """Helper for self-hosted HuggingFace CLIP or SigLIP zero-shot abstract art evaluation."""
    if cache_dict["failed"]:
        return None
    try:
        import torch
        from PIL import Image
        from transformers import AutoModel, AutoProcessor

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
        tone_label, tone_desc, _ = top_match(EMOTIONAL_TONE_PROMPTS)
        rorschach_label, rorschach_desc, _ = top_match(RORSCHACH_ARCHETYPE_PROMPTS)
        tag = "SigLIP" if is_siglip else "CLIP"

        return {
            "summary_label": f"{rorschach_label} ({tone_label})",
            "description": (
                f"Self-Hosted {tag} Abstract Art & Rorschach Perception:\n"
                f"- Visual Form: Resembles {form_desc} ({form_label}).\n"
                f"- Emotional Resonance: Evokes {tone_desc} ({tone_label}).\n"
                f"- Rorschach Pareidolia: Suggests {rorschach_desc} ({rorschach_label})."
            ),
        }
    except Exception as e:
        cache_dict["failed"] = True
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

        if _RESNET_CACHE["model"] is None:
            _RESNET_CACHE["model"] = ResNet50(weights="imagenet")

        image = load_img(str(filename), target_size=(224, 224))
        arr = img_to_array(image)
        arr = np.expand_dims(arr, axis=0)
        y_pred = _RESNET_CACHE["model"].predict(arr, verbose=0)
        label = decode_predictions(y_pred, top=1)[0][0][1]
        label = label.replace("_", " ").capitalize()
        return {
            "summary_label": label,
            "description": f"ImageNet Pareidolia Association: {label}",
        }
    except Exception as e:
        _RESNET_CACHE["failed"] = True
        print(f"[analysis] Note: TensorFlow ResNet50 unavailable ({e}).")
        return None


def _is_ollama_vision_model_ready() -> bool:
    """Fast local check (<=0.6s) whether configured ollama_vision_model is pulled locally."""
    try:
        settings = load_settings()
        ai_cfg = settings.get("ai", {})
        vision_model = ai_cfg.get("ollama_vision_model") or "moondream"
        host = get_configured_ollama_host(settings)
        if not host.startswith(("http://", "https://")):
            return False
        resp = requests.get(f"{host}/api/tags", timeout=0.6)
        if not resp.ok:
            return False
        names = [m.get("name", "") for m in resp.json().get("models", [])]
        return any(n == vision_model or n.startswith(f"{vision_model}:") for n in names)
    except Exception:
        return False


_OPENAI_COMPAT_VISION_HINTS = (
    "gpt-4o", "gpt-4-turbo", "gpt-4.1", "gpt-4.5", "chatgpt-4o",
    "vision", "pixtral", "llava", "qwen2-vl", "qwen2.5-vl", "qwen2.5vl",
    "llama-3.2-11b-vision", "llama-3.2-90b-vision", "llama-4-",
)


def _analysis_llm_handles_vision_natively() -> bool:
    """Return True when the configured analysis LLM already receives the image directly."""
    try:
        from chat import LLMModel
        settings = load_settings()
        acfg = settings.get("ai", {}).get("analysis", {})
        prov = (acfg.get("provider") or "ollama").lower()
        mod = (acfg.get("model") or "").lower()
        if prov in ("gemini", "anthropic"):
            return True
        if prov == "openai_compatible":
            return (
                any(h in mod for h in _OPENAI_COMPAT_VISION_HINTS)
                or mod.startswith(("o1", "o3", "o4"))
            )
        if prov == "ollama":
            base_url = (acfg.get("base_url") or "http://localhost:11434/v1").rstrip("/")
            if not base_url.endswith("/v1") and "11434" in base_url and "/api" not in base_url:
                base_url += "/v1"
            return LLMModel._ollama_supports_vision(base_url, acfg.get("model") or "")
    except Exception:
        pass
    return False


def analyze_drawing(filename: str, mode: str = "auto", canvas_filename: str = None, dwell_grid=None) -> dict:
    """
    Comprehensive self-hosted drawing analysis returning a short `label` (archetype)
    and a rich `prompt_context` for the Psychoanalysis LLM.
    """
    geom = analyze_stroke_geometry(filename, canvas_filename=canvas_filename, dwell_grid=dwell_grid)
    sections = [geom["description"]]
    summary_label = geom["summary_label"]

    extra = None
    if mode == "ollama_vision":
        extra = run_ollama_vision_analysis(filename)
    elif mode == "clip":
        with _CACHE_LOCK:
            extra = run_clip_abstract_analysis(filename)
    elif mode == "siglip":
        with _CACHE_LOCK:
            extra = run_siglip_abstract_analysis(filename)
    elif mode == "resnet50":
        with _CACHE_LOCK:
            extra = run_resnet_classification(filename)
    elif mode == "auto":
        with _CACHE_LOCK:
            if _SIGLIP_CACHE["model"] is not None:
                extra = run_siglip_abstract_analysis(filename)
            elif _CLIP_CACHE["model"] is not None:
                extra = run_clip_abstract_analysis(filename)
        if extra is None and not _analysis_llm_handles_vision_natively() and _is_ollama_vision_model_ready():
            extra = run_ollama_vision_analysis(filename)

    if extra:
        sections.insert(0, extra["description"])
        summary_label = extra["summary_label"]

    return {
        "label": summary_label,
        "prompt_context": "\n\n".join(sections),
        "geometry": geom,
    }
