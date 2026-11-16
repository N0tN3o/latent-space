#!/usr/bin/env python3
"""
Centralized configuration and cross-platform path manager for Latent Space.
Removes hardcoded Linux user paths and Google Drive dependencies, and stores
AI provider settings, printing preferences, and Kasboek attribution settings.
"""

import json
import os
import threading
from pathlib import Path
from copy import deepcopy

PROJECT_DIR = Path(__file__).resolve().parent
SHARED_DIR = Path(os.environ.get("LATENT_SPACE_SHARED_DIR", PROJECT_DIR / "shared_output")).resolve()
DOCS_DIR = SHARED_DIR / "docs"
PRINTED_DIR = DOCS_DIR / "printed"
LOGS_DIR = Path(os.environ.get("LATENT_SPACE_LOGS_DIR", PROJECT_DIR / "logs")).resolve()
WEB_UI_DIR = PROJECT_DIR / "web_ui"
CONFIG_DIR = Path(os.environ.get("LATENT_SPACE_CONFIG_DIR", PROJECT_DIR / "config")).resolve()
SETTINGS_FILE = CONFIG_DIR / "settings.json"
ENV_FILE = PROJECT_DIR / "analysis_watcher" / ".env"

_lock = threading.Lock()
_memory_settings = None

DEFAULT_ANALYSIS_PROMPT = (
    "You are an insightful psychoanalyst and art critic for the interactive installation "
    "'Latent Space: Through the Eyes of the Algorithm'. Visitors create abstract drawings "
    "purely through their eye movements (gaze tracking). "
    "Given the visual perception profile of their gaze drawing (including stroke dynamics, "
    "spatial focus, pareidolia/Rorschach archetypes, and emotional resonance) or the image itself, "
    "write a compelling, poetic, second-person psychoanalytical interpretation (around 70-110 words) "
    "about what their subconscious gaze reveals—their inner tensions, focus, curiosity, and emotional state. "
    "Do not mention technical metrics or numbers directly; translate them into psychological insight."
)

DEFAULT_POEM_PROMPT = (
    "You are a contemplative poet for the art installation 'Latent Space: Through the Eyes of the Algorithm'. "
    "Based on the psychoanalytical interpretation of a visitor's eye-tracked drawing, write an evocative, "
    "lyrical 4-to-8 line poem that captures the mood, movement, and inner landscape of their gaze. "
    "Return ONLY the poem text without titles, markdown code blocks, or introductory commentary."
)

DEFAULT_SETTINGS = {
    "ai": {
        "vision_mode": "auto",  # "auto", "vision", "clip", "stroke_metrics", "resnet50"
        "analysis": {
            "provider": "ollama",  # "ollama", "gemini", "anthropic", "openai_compatible", "buas"
            "model": "llama3.2",
            "base_url": "http://localhost:11434/v1",
            "api_key": "",
            "name": "Latent Space Psychoanalyst",
            "description": DEFAULT_ANALYSIS_PROMPT,
            "temperature": 0.7,
            "assistant_key": "8a79f5ec-c5dd-448c-9611-99610792a04b"
        },
        "poem": {
            "provider": "ollama",  # "ollama", "gemini", "anthropic", "openai_compatible", "buas"
            "model": "llama3.2",
            "base_url": "http://localhost:11434/v1",
            "api_key": "",
            "name": "Latent Space Poet",
            "description": DEFAULT_POEM_PROMPT,
            "temperature": 0.8,
            "assistant_key": "959058ad-4417-49e3-9e71-252ee2fb033d"
        }
    },
    "tts": {
        "enabled": False,
        "provider": "elevenlabs",  # "elevenlabs", "google_cloud", "none"
        "voice_id_1": "45QiOxkuEAY6ckHqLMe8",
        "voice_id_2": "tJHJUEHzOkMoPmJJ5jo2"
    },
    "printing": {
        "auto_print": False,
        "printer_name": "default",
        "paper_format": "A4"
    },
    "attributions": {
        "enabled": False,
        "show_logo": True,
        "logo_path": "/kasboek-logo.jpg",
        "company_name": "Kasboek",
        "intro_text": "Made possible by Kasboek.",
        "learn_more_text": "To learn more, visit",
        "website_label": "Kasboek Website",
        "website_url": "https://kasboek.nl",
        "company_linkedin_label": "Kasboek LinkedIn",
        "company_linkedin_url": "https://www.linkedin.com/company/kasboek",
        "thanks_text": "Thank you for letting us use this:",
        "founders": [
            {"name": "Co-Founder / CEO 1", "url": "https://www.linkedin.com/"},
            {"name": "Co-Founder 2", "url": "https://www.linkedin.com/"},
            {"name": "Co-Founder 3", "url": "https://www.linkedin.com/"}
        ],
        "custom_footer_note": ""
    },
    "server": {
        "host": "0.0.0.0",
        "port": 8000,
        "remote_server_url": ""
    }
}


def ensure_directories():
    """Ensure all required runtime directories exist."""
    global LOGS_DIR, CONFIG_DIR, SETTINGS_FILE
    for d in [SHARED_DIR, DOCS_DIR, PRINTED_DIR, WEB_UI_DIR]:
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        LOGS_DIR = PROJECT_DIR / "analysis_watcher"
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        CONFIG_DIR = SHARED_DIR
        SETTINGS_FILE = CONFIG_DIR / "settings.json"


def _deep_merge(base: dict, override: dict) -> dict:
    result = deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def load_env_vars() -> dict:
    """Load key=value pairs from analysis_watcher/.env or root .env + os.environ."""
    env = dict(os.environ)
    for env_path in [PROJECT_DIR / ".env", ENV_FILE]:
        if env_path.exists():
            try:
                for line in env_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
            except Exception:
                pass
    return env


def load_settings() -> dict:
    """Load settings from config/settings.json merged with defaults."""
    global _memory_settings
    ensure_directories()
    with _lock:
        if _memory_settings is not None and not SETTINGS_FILE.exists():
            return deepcopy(_memory_settings)
        if not SETTINGS_FILE.exists():
            save_settings_unlocked(DEFAULT_SETTINGS)
            return deepcopy(DEFAULT_SETTINGS)
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            merged = _deep_merge(DEFAULT_SETTINGS, data)
            _memory_settings = deepcopy(merged)
            return merged
        except Exception as e:
            print(f"[config_manager] Warning: failed to parse {SETTINGS_FILE}: {e}. Using defaults.")
            return deepcopy(_memory_settings or DEFAULT_SETTINGS)


def save_settings_unlocked(settings: dict) -> dict:
    global _memory_settings
    ensure_directories()
    merged = _deep_merge(DEFAULT_SETTINGS, settings)
    _memory_settings = deepcopy(merged)
    try:
        SETTINGS_FILE.write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"[config_manager] Note: kept settings in memory ({e})")
    return merged


def save_settings(settings: dict) -> dict:
    """Thread-safe save of settings to config/settings.json."""
    with _lock:
        return save_settings_unlocked(settings)


def update_settings_section(section: str, data: dict) -> dict:
    """Update a top-level section (e.g. 'ai', 'attributions', 'printing', 'tts') in settings.json."""
    with _lock:
        current = deepcopy(DEFAULT_SETTINGS)
        if SETTINGS_FILE.exists():
            try:
                current = _deep_merge(current, json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
            except Exception:
                pass
        if isinstance(current.get(section), dict) and isinstance(data, dict):
            current[section] = _deep_merge(current[section], data)
        else:
            current[section] = data
        return save_settings_unlocked(current)
