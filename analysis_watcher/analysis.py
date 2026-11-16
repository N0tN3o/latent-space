import os
import sys
from pathlib import Path
import cv2
import numpy as np

# Add project root so config_manager can be imported
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

_CLIP_CACHE = {"model": None, "processor": None, "failed": False}
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


def analyze_stroke_geometry(filename: str) -> dict:
    """
    Extract quantitative and compositional features from an abstract gaze drawing
    using OpenCV and NumPy (zero heavy ML dependencies).
    """
    img = cv2.imread(str(filename), cv2.IMREAD_GRAYSCALE)
    if img is None:
        return {
            "summary_label": "Abstract Gaze Trace",
            "description": "An abstract gaze-tracked drawing on a blank canvas."
        }

    # Invert so strokes are positive (255) and background is 0
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

    # 2. 3x3 Grid spatial attention analysis
    zone_names = [
        ["upper-left", "upper-center", "upper-right"],
        ["mid-left", "center", "mid-right"],
        ["lower-left", "lower-center", "lower-right"]
    ]
    h_step, w_step = max(1, h // 3), max(1, w // 3)
    zone_scores = []
    for r in range(3):
        for c in range(3):
            region = stroke_mask[r * h_step:(r + 1) * h_step, c * w_step:(c + 1) * w_step]
            zone_scores.append((float(np.mean(region)), zone_names[r][c]))

    zone_scores.sort(key=lambda x: x[0], reverse=True)
    primary_zone = zone_scores[0][1]
    secondary_zone = zone_scores[1][1]
    least_zone = zone_scores[-1][1]

    # 3. Symmetry (horizontal and vertical mirror correlation)
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

    # 4. Contours, enclosed loops, and angularity vs. curvature
    contours, hierarchy = cv2.findContours(binary, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
    num_components = len(contours)
    enclosed_loops = 0
    if hierarchy is not None:
        # Inner holes have a parent contour (hierarchy[0][i][3] != -1)
        enclosed_loops = int(np.sum(hierarchy[0][:, 3] != -1))

    sharp_corners = 0
    total_perimeter = 0.0
    for cnt in contours:
        perim = cv2.arcLength(cnt, True)
        if perim < 15:
            continue
        total_perimeter += perim
        approx = cv2.approxPolyDP(cnt, 0.03 * perim, True)
        sharp_corners += len(approx)

    if enclosed_loops >= 6:
        structure_desc = f"an intricate lattice of {enclosed_loops} enclosed loops and recurring self-intersecting orbits"
        archetype_hint = "Tangled Web / Labyrinth"
    elif enclosed_loops >= 2:
        structure_desc = f"intertwining paths forming {enclosed_loops} enclosed focal chambers"
        archetype_hint = "Intertwined Orbits"
    elif num_components > 8:
        structure_desc = "fragmented, staccato bursts of attention jumping rapidly across the canvas"
        archetype_hint = "Saccadic Constellation"
    else:
        structure_desc = "a continuous, unbroken stream-of-consciousness trajectory"
        archetype_hint = "Continuous Horizon Trace"

    if sharp_corners > 18:
        line_quality = "angular, darting, and electric with sudden directional shifts"
    else:
        line_quality = "fluid, sweeping, and organic in its transitions"

    description = (
        f"Visual & Gaze Composition Profile:\n"
        f"- Archetype: {archetype_hint}\n"
        f"- Stroke Density: The marks are {density_desc} ({ink_ratio * 100:.1f}% canvas coverage).\n"
        f"- Spatial Fixation: The visitor's gaze gravitated most strongly toward the {primary_zone} "
        f"and {secondary_zone} regions, while avoiding the {least_zone}.\n"
        f"- Balance & Symmetry: The composition is {symmetry_desc}.\n"
        f"- Movement & Structure: Characterized by {structure_desc}, with strokes that feel {line_quality}."
    )

    return {
        "summary_label": archetype_hint,
        "description": description,
        "ink_ratio": round(ink_ratio, 4),
        "primary_zone": primary_zone,
        "secondary_zone": secondary_zone,
        "enclosed_loops": enclosed_loops,
    }


def run_clip_abstract_analysis(filename: str):
    """
    Use Zero-Shot CLIP (if transformers + torch are installed) to evaluate the drawing
    against abstract art forms, emotional tones, and Rorschach pareidolia archetypes.
    Returns None if CLIP is not installed or fails.
    """
    if _CLIP_CACHE["failed"]:
        return None

    try:
        import torch
        from PIL import Image
        from transformers import CLIPProcessor, CLIPModel

        if _CLIP_CACHE["model"] is None:
            model_id = "openai/clip-vit-base-patch32"
            _CLIP_CACHE["processor"] = CLIPProcessor.from_pretrained(model_id)
            _CLIP_CACHE["model"] = CLIPModel.from_pretrained(model_id)
            _CLIP_CACHE["model"].eval()

        processor = _CLIP_CACHE["processor"]
        model = _CLIP_CACHE["model"]
        pil_img = Image.open(filename).convert("RGB")

        def top_match(prompt_pairs):
            texts = [p[0] for p in prompt_pairs]
            inputs = processor(text=texts, images=pil_img, return_tensors="pt", padding=True)
            with torch.no_grad():
                outputs = model(**inputs)
                probs = outputs.logits_per_image.softmax(dim=1)[0].cpu().numpy()
            best_idx = int(np.argmax(probs))
            return prompt_pairs[best_idx][1], prompt_pairs[best_idx][0], float(probs[best_idx])

        form_label, form_desc, _ = top_match(ABSTRACT_FORM_PROMPTS)
        tone_label, tone_desc, _ = top_match(EMOTIONAL_TONE_PROMPTS)
        rorschach_label, rorschach_desc, _ = top_match(RORSCHACH_ARCHETYPE_PROMPTS)

        return {
            "summary_label": f"{rorschach_label} ({tone_label})",
            "description": (
                f"Abstract Art & Rorschach Perception (CLIP Zero-Shot):\n"
                f"- Visual Form: Resembles {form_desc} ({form_label}).\n"
                f"- Emotional Resonance: Evokes {tone_desc} ({tone_label}).\n"
                f"- Rorschach Pareidolia: Suggests {rorschach_desc} ({rorschach_label})."
            )
        }
    except Exception as e:
        _CLIP_CACHE["failed"] = True
        print(f"[analysis] Note: CLIP zero-shot not available ({e}), using geometric & vision analysis.")
        return None


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
    Comprehensive drawing analysis returning both a short `label` (for logging/CSV)
    and a rich `prompt_context` (for the Psychoanalysis LLM).

    Modes:
      - "auto" / "vision": Computes stroke geometry + optional CLIP if requested
      - "clip": Computes CLIP zero-shot abstract art analysis + stroke geometry
      - "stroke_metrics": Computes fast OpenCV stroke geometry & Rorschach balance
      - "resnet50": Legacy ImageNet ResNet50 + stroke geometry
    """
    geom = analyze_stroke_geometry(filename)
    sections = [geom["description"]]
    summary_label = geom["summary_label"]

    if mode == "clip":
        clip_res = run_clip_abstract_analysis(filename)
        if clip_res:
            sections.insert(0, clip_res["description"])
            summary_label = clip_res["summary_label"]
    elif mode == "resnet50":
        resnet_res = run_resnet_classification(filename)
        if resnet_res:
            sections.insert(0, resnet_res["description"])
            summary_label = resnet_res["summary_label"]
    elif mode == "auto":
        # If CLIP is already loaded, include it; otherwise keep fast geometry + direct Vision LLM support
        if _CLIP_CACHE["model"] is not None:
            clip_res = run_clip_abstract_analysis(filename)
            if clip_res:
                sections.insert(0, clip_res["description"])
                summary_label = clip_res["summary_label"]

    return {
        "label": summary_label,
        "prompt_context": "\n\n".join(sections),
        "geometry": geom
    }


def run_classification(filename: str, mode: str = "auto") -> str:
    """
    Backwards-compatible entry point returning the prompt context for the LLM.
    """
    result = analyze_drawing(filename, mode=mode)
    return result["prompt_context"]
