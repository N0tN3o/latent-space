#!/usr/bin/env python3
"""
Download the MediaPipe FaceLandmarker web bundle into web_ui/vendor/mediapipe/
so the Browser Gaze Painter (/draw) can run 100% offline without internet access.

Usage:
    python download_offline_assets.py
    python download_offline_assets.py --force
"""

import argparse
import os
from pathlib import Path
import sys
import urllib.request

PROJECT_DIR = Path(__file__).resolve().parent
TARGET_DIR = PROJECT_DIR / "web_ui" / "vendor" / "mediapipe"

MEDIAPIPE_VERSION = "0.10.14"
CDN_BASE = f"https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@{MEDIAPIPE_VERSION}"

ASSETS = {
    "face_landmarker.task": (
        "https://storage.googleapis.com/mediapipe-models/face_landmarker/"
        "face_landmarker/float16/1/face_landmarker.task"
    ),
    "vision_bundle.mjs": f"{CDN_BASE}/vision_bundle.mjs",
    "wasm/vision_wasm_internal.js": f"{CDN_BASE}/wasm/vision_wasm_internal.js",
    "wasm/vision_wasm_internal.wasm": f"{CDN_BASE}/wasm/vision_wasm_internal.wasm",
    "wasm/vision_wasm_nosimd_internal.js": f"{CDN_BASE}/wasm/vision_wasm_nosimd_internal.js",
    "wasm/vision_wasm_nosimd_internal.wasm": f"{CDN_BASE}/wasm/vision_wasm_nosimd_internal.wasm",
}


def download_assets(force: bool = False) -> int:
    (TARGET_DIR / "wasm").mkdir(parents=True, exist_ok=True)
    print("=" * 64)
    print("LATENT SPACE — OFFLINE MEDIAPIPE ASSET DOWNLOADER")
    print(f"Target Directory: {TARGET_DIR}")
    print("=" * 64)

    for rel_path, url in ASSETS.items():
        dest = TARGET_DIR / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and dest.stat().st_size > 0 and not force:
            size_kb = dest.stat().st_size / 1024.0
            print(f"  [skip] {rel_path} already exists ({size_kb:.1f} KB)")
            continue

        tmp_dest = dest.with_name(f".{dest.name}.{os.getpid()}.tmp")
        print(f"  [down] Downloading {rel_path} ...")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "LatentSpace-OfflineDownloader/1.0"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read()
            if not data:
                raise RuntimeError(f"Empty response from {url}")
            tmp_dest.write_bytes(data)
            os.replace(str(tmp_dest), str(dest))
            print(f"  [ ok ] Saved {rel_path} ({len(data) / 1024.0:.1f} KB)")
        except Exception as exc:
            if tmp_dest.exists():
                try:
                    tmp_dest.unlink()
                except Exception:
                    pass
            print(f"  [fail] Could not download {rel_path}: {exc}", file=sys.stderr)
            return 1

    print("=" * 64)
    print("All offline MediaPipe assets are ready!")
    print("Browser Gaze Painter (/draw) will now run 100% offline.")
    print("=" * 64)
    return 0


def download_translation_model(model_id: str = None) -> int:
    print("=" * 64)
    print("LATENT SPACE — OFFLINE TRANSLATION MODEL DOWNLOADER (CPU NMT)")
    print("=" * 64)
    try:
        sys.path.insert(0, str(PROJECT_DIR))
        sys.path.insert(0, str(PROJECT_DIR / "analysis_watcher"))
        from translator import preload_local_nmt_model
        res = preload_local_nmt_model(model_id=model_id)
        print(f"  [ ok ] Cached & verified '{res['model']}' on CPU in {res['elapsed_sec']}s (0 MB VRAM)")
        print("=" * 64)
        return 0
    except Exception as exc:
        print(f"  [fail] Could not download translation model: {exc}", file=sys.stderr)
        return 1


def main():
    parser = argparse.ArgumentParser(
        description="Download MediaPipe FaceLandmarker assets (and optional local EN->HU translation model) for 100% offline use."
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download MediaPipe files even if they already exist locally.",
    )
    parser.add_argument(
        "--with-translation",
        action="store_true",
        help="Also pre-download & cache the local CPU EN->HU translation model (Helsinki-NLP/opus-mt-tc-big-en-hu).",
    )
    parser.add_argument(
        "--translation-only",
        action="store_true",
        help="Download only the local CPU EN->HU translation model (skip MediaPipe assets).",
    )
    parser.add_argument(
        "--translation-model",
        default=None,
        help="Optional HuggingFace model ID override for local CPU NMT.",
    )
    args = parser.parse_args()
    rc = 0
    if not args.translation_only:
        rc = download_assets(force=args.force)
        if rc != 0:
            sys.exit(rc)
    if args.with_translation or args.translation_only:
        rc = download_translation_model(model_id=args.translation_model)
    sys.exit(rc)


if __name__ == "__main__":
    main()
