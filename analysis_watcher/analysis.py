import base64
import os
import sys
from pathlib import Path
import cv2
import numpy as np
import requests

# Add project root so config_manager can be imported
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config_manager import load_settings

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
    of the visitor's gaze strokes (the same mathematical technique used to analyze
    Jackson Pollock's abstract expressionist paintings).
    """
    Z = binary_mask > 0
    if not np.any(Z):
        return 1.0

    p = min(Z.shape)
    n = int(np.floor(np.log2(p)))
    if n < 3:
        return 1.1

    sizes = 2 ** np.arange(n - 1, 1, -1)
    counts = []
    valid_sizes = []

    for size in sizes:
        h_trim = (Z.shape[0] // size) * size
        w_trim = (Z.shape[1] // size) * size
        if h_trim == 0 or w_trim == 0:
            continue
        trimmed = Z[:h_trim, :w_trim]
        reshaped = trimmed.reshape(h_trim // size, size, w_trim // size, size)
        box_has_ink = np.any(reshaped, axis=(1, 3))
        cnt = int(np.sum(box_has_ink))
        if cnt > 0:
            counts.append(cnt)
            valid_sizes.append(size)

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


def analyze_stroke_geometry(filename: str) -> dict:
    """
    100% Self-Hosted Mathematical, Fractal, and Rorschach Geometry Analyzer.
    Extracts stroke density, 3x3 spatial attention distribution, Shannon spatial entropy,
    box-counting fractal dimension (Df), bilateral Rorschach symmetry, and loop topology.
    """
    img = cv2.imread(str(filename), cv2.IMREAD_GRAYSCALE)
    if img is None:
        return {
            "summary_label": "Abstract Gaze Trace",
            "description": "An abstract gaze-tracked drawing on a blank canvas."
        }

    _, binary = cv2.threshold(img, 220, 255, cv2.THRESH_BINARY_INV)
    h, w = binary.shape
    stroke_mask = (binary > 0).astype(np.float32)

    # 1. Overall ink coverage / density
    ink_ratio = float(np.mean(stroke_mask))
    if ink_ratio < 0.03:
        density_desc = "sparse, minimalist, and fleeting"
    elif ink_ratio < 0.10:
        density_desc = "balanced and deliberate"
    elif ink_ratio < 0.22:
        density_desc = "dense, layered, and expressive"
    else:
        density_desc = "heavily saturated, intense, and consuming"

    # 2. 3x3 Grid spatial attention & Shannon entropy
    zone_names = [
        ["upper-left", "upper-center", "upper-right"],
        ["mid-left", "center", "mid-right"],
        ["lower-left", "lower-center", "lower-right"]
    ]
    h_step, w_step = max(1, h // 3), max(1, w // 3)
    zone_scores = []
    raw_densities = []
    for r in range(3):
        for c in range(3):
            region = stroke_mask[r * h_step:(r + 1) * h_step, c * w_step:(c + 1) * w_step]
            val = float(np.mean(region))
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

    # 3. Fractal Dimension (Box-Counting)
    fractal_dim = compute_fractal_dimension(binary)
    if fractal_dim < 1.18:
        fractal_desc = f"low-complexity linear trajectory (D={fractal_dim:.2f}), reflecting direct, unknotted movement"
    elif fractal_dim < 1.38:
        fractal_desc = f"organic intermediate fractal complexity (D={fractal_dim:.2f}), echoing natural branching patterns"
    else:
        fractal_desc = f"high space-filling fractal intricacy (D={fractal_dim:.2f}), akin to dense abstract expressionist webs"

    # 4. Bilateral Rorschach Symmetry
    left_half = stroke_mask[:, :w // 2]
    right_half_flipped = np.fliplr(stroke_mask[:, w - (w // 2):])
    horiz_sym = 1.0 - float(np.mean(np.abs(left_half - right_half_flipped))) / max(ink_ratio * 2.0, 0.05)
    horiz_sym = float(np.clip(horiz_sym, 0.0, 1.0))

    top_half = stroke_mask[:h // 2, :]
    bottom_half_flipped = np.flipud(stroke_mask[h - (h // 2):, :])
    vert_sym = 1.0 - float(np.mean(np.abs(top_half - bottom_half_flipped))) / max(ink_ratio * 2.0, 0.05)
    vert_sym = float(np.clip(vert_sym, 0.0, 1.0))

    if horiz_sym > 0.65 and vert_sym > 0.65:
        symmetry_desc = "strikingly symmetrical and centered, like a classic Rorschach inkblot"
    elif horiz_sym > 0.55:
        symmetry_desc = "horizontally mirrored with bilateral balance"
    elif vert_sym > 0.55:
        symmetry_desc = "vertically balanced between upper and lower realms"
    else:
        symmetry_desc = "decidedly asymmetrical, driven by directional drift and spontaneous tension"

    # 5. Contours, enclosed loops, and angularity vs. curvature
    contours, hierarchy = cv2.findContours(binary, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
    num_components = len(contours)
    enclosed_loops = int(np.sum(hierarchy[0][:, 3] != -1)) if hierarchy is not None else 0

    sharp_corners = 0
    for cnt in contours:
        perim = cv2.arcLength(cnt, True)
        if perim < 15:
            continue
        approx = cv2.approxPolyDP(cnt, 0.03 * perim, True)
        sharp_corners += len(approx)

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

    line_quality = (
        "angular, darting, and electric with sudden directional shifts"
        if sharp_corners > 18
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
        base_url = ai_cfg.get("analysis", {}).get("base_url", "http://localhost:11434")
        host = base_url.replace("/v1", "").rstrip("/")

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
        from transformers import AutoProcessor, AutoModel

        if cache_dict["model"] is None:
            cache_dict["processor"] = AutoProcessor.from_pretrained(model_id)
            cache_dict["model"] = AutoModel.from_pretrained(model_id)
            cache_dict["model"].eval()

        processor = cache_dict["processor"]
        model = cache_dict["model"]
        pil_img = Image.open(filename).convert("RGB")

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
        from tensorflow.keras.preprocessing.image import load_img, img_to_array
        from tensorflow.keras.applications.resnet50 import ResNet50, decode_predictions

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
            "description": f"ImageNet Pareidolia Association: {label}"
        }
    except Exception as e:
        _RESNET_CACHE["failed"] = True
        print(f"[analysis] Note: TensorFlow ResNet50 unavailable ({e}).")
        return None


def analyze_drawing(filename: str, mode: str = "auto") -> dict:
    """
    Comprehensive self-hosted drawing analysis returning a short `label` (archetype)
    and a rich `prompt_context` for the Psychoanalysis LLM.

    Supported modes:
      - "auto" / "vision": Fractal + Entropy + Rorschach Geometry (+ direct image to Vision LLM)
      - "ollama_vision": Self-hosted Ollama Vision model (moondream / llava) + Fractal/Rorschach Geometry
      - "clip": Self-hosted OpenAI CLIP zero-shot abstract art & Rorschach + Fractal/Rorschach Geometry
      - "siglip": Self-hosted Google SigLIP zero-shot abstract art & Rorschach + Fractal/Rorschach Geometry
      - "stroke_metrics": Fast local Fractal + Entropy + Rorschach Geometry only
      - "resnet50": Legacy ImageNet ResNet50 + Fractal/Rorschach Geometry
    """
    geom = analyze_stroke_geometry(filename)
    sections = [geom["description"]]
    summary_label = geom["summary_label"]

    extra = None
    if mode == "ollama_vision":
        extra = run_ollama_vision_analysis(filename)
    elif mode == "clip":
        extra = run_clip_abstract_analysis(filename)
    elif mode == "siglip":
        extra = run_siglip_abstract_analysis(filename)
    elif mode == "resnet50":
        extra = run_resnet_classification(filename)
    elif mode == "auto":
        if _SIGLIP_CACHE["model"] is not None:
            extra = run_siglip_abstract_analysis(filename)
        elif _CLIP_CACHE["model"] is not None:
            extra = run_clip_abstract_analysis(filename)

    if extra:
        sections.insert(0, extra["description"])
        summary_label = extra["summary_label"]

    return {
        "label": summary_label,
        "prompt_context": "\n\n".join(sections),
        "geometry": geom,
    }


def run_classification(filename: str, mode: str = "auto") -> str:
    """Backwards-compatible entry point returning the prompt context for the LLM."""
    result = analyze_drawing(filename, mode=mode)
    return result["prompt_context"]
