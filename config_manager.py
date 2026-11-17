#!/usr/bin/env python3
"""
Centralized configuration and cross-platform path manager for Latent Space.
Removes hardcoded Linux user paths and Google Drive dependencies, and stores
AI provider settings, printing preferences, and Kasboek attribution settings.
"""

import json
import os
import re
import secrets
import threading
from datetime import datetime
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
ADMIN_PIN_FILE = CONFIG_DIR / "admin_pin.txt"
ENV_FILE = PROJECT_DIR / "analysis_watcher" / ".env"

ALLOWED_PAPER_FORMATS = {"A4", "A3", "A5", "Letter", "Legal"}
_PRINTER_NAME_RE = re.compile(r"^[A-Za-z0-9 _.\-:()/\\]{1,128}$")

_lock = threading.Lock()
_history_lock = threading.Lock()
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
        "vision_mode": "auto",  # "auto", "vision", "ollama_vision", "siglip", "clip", "stroke_metrics", "resnet50"
        "ollama_vision_model": "moondream",
        "analysis": {
            "provider": "ollama",  # "ollama", "gemini", "anthropic", "openai_compatible", "buas"
            "model": "llama3.2",
            "base_url": "http://localhost:11434/v1",
            "api_key": "",
            "fallback_provider": "auto",  # "auto", "none", "ollama", "gemini", "anthropic", "openai_compatible", "buas"
            "fallback_model": "",
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
            "fallback_provider": "auto",  # "auto", "none", "ollama", "gemini", "anthropic", "openai_compatible", "buas"
            "fallback_model": "",
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
    for d in [SHARED_DIR, DOCS_DIR, PRINTED_DIR, WEB_UI_DIR, LOGS_DIR, CONFIG_DIR]:
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass


def atomic_write_bytes(target: Path, data: bytes):
    """Write bytes atomically via a unique sibling temp file and os.replace."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp_path.write_bytes(data)
        os.replace(str(tmp_path), str(target))
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass


def atomic_write_text(target: Path, content: str, encoding: str = "utf-8"):
    """Write text atomically via a unique sibling temp file and os.replace."""
    atomic_write_bytes(target, content.encode(encoding))


def _sanitize_incoming_settings(data: dict, existing: dict = None) -> dict:
    """Strip masked/transient API key fields before persisting settings."""
    cleaned = deepcopy(data) if isinstance(data, dict) else {}
    ai = cleaned.get("ai")
    if isinstance(ai, dict):
        existing_ai = (existing or {}).get("ai", {})
        for role in ("analysis", "poem"):
            role_cfg = ai.get(role)
            if isinstance(role_cfg, dict):
                role_cfg.pop("has_api_key", None)
                if role_cfg.get("api_key") == "***":
                    prev_key = existing_ai.get(role, {}).get("api_key", "")
                    if prev_key:
                        role_cfg["api_key"] = prev_key
                    else:
                        role_cfg.pop("api_key", None)
    return cleaned


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


def _read_existing_settings_raw() -> dict:
    """Read existing settings.json cleanly without silently wiping corrupt JSON."""
    if not SETTINGS_FILE.exists():
        return deepcopy(DEFAULT_SETTINGS)
    raw = SETTINGS_FILE.read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("settings.json root is not a JSON object")
    return _deep_merge(DEFAULT_SETTINGS, data)


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
            merged = _read_existing_settings_raw()
            _memory_settings = deepcopy(merged)
            return merged
        except Exception as e:
            print(f"[config_manager] Warning: failed to parse {SETTINGS_FILE}: {e}. Using cached/defaults without overwriting.")
            return deepcopy(_memory_settings or DEFAULT_SETTINGS)


def validate_safe_url(url: str, field_name: str = "url") -> str:
    """Allow only empty strings, relative root paths ('/...'), or http(s):// URLs."""
    if url is None:
        return ""
    if not isinstance(url, str):
        raise ValueError(f"{field_name} must be a string")
    val = url.strip()
    if not val:
        return ""
    if any(ord(ch) < 32 for ch in val):
        raise ValueError(f"{field_name} contains invalid control characters")
    low = val.lower()
    if low.startswith(("http://", "https://")) or (val.startswith("/") and not val.startswith("//")):
        return val
    raise ValueError(f"{field_name} must start with http://, https://, or /")


def validate_attributions_data(data: dict) -> dict:
    """Validate and sanitize attribution settings before saving."""
    if not isinstance(data, dict):
        raise ValueError("attributions payload must be an object")
    cleaned = deepcopy(data)
    for url_field in ("logo_path", "website_url", "company_linkedin_url"):
        if url_field in cleaned:
            cleaned[url_field] = validate_safe_url(cleaned[url_field], url_field)
    if "founders" in cleaned:
        if not isinstance(cleaned["founders"], list):
            raise ValueError("founders must be a list")
        safe_founders = []
        for idx, f in enumerate(cleaned["founders"][:5]):
            if not isinstance(f, dict):
                raise ValueError(f"founders[{idx}] must be an object")
            safe_founders.append({
                "name": str(f.get("name") or "")[:120],
                "url": validate_safe_url(f.get("url", ""), f"founders[{idx}].url"),
            })
        cleaned["founders"] = safe_founders
    return cleaned


def validate_printer_name(name: str) -> str:
    """Validate printer name against allowed characters to prevent injection."""
    if not isinstance(name, str):
        raise ValueError("printer_name must be a string")
    pname = name.strip() or "default"
    if not _PRINTER_NAME_RE.match(pname):
        raise ValueError("printer_name contains disallowed characters or exceeds 128 chars")
    return pname


def validate_printing_data(data: dict) -> dict:
    """Validate printer configuration to prevent shell/command injection and invalid formats."""
    if not isinstance(data, dict):
        raise ValueError("printing payload must be an object")
    cleaned = deepcopy(data)
    if "auto_print" in cleaned:
        cleaned["auto_print"] = bool(cleaned["auto_print"])
    if "printer_name" in cleaned:
        cleaned["printer_name"] = validate_printer_name(cleaned["printer_name"])
    if "paper_format" in cleaned:
        fmt = str(cleaned["paper_format"] or "A4").strip()
        if fmt not in ALLOWED_PAPER_FORMATS:
            raise ValueError(f"paper_format must be one of {sorted(ALLOWED_PAPER_FORMATS)}")
        cleaned["paper_format"] = fmt
    return cleaned


def mask_settings_secrets(settings: dict) -> dict:
    """Return a deep copy of settings with API keys replaced by '***'."""
    masked = deepcopy(settings)
    for role in ("analysis", "poem"):
        role_cfg = masked.get("ai", {}).get(role)
        if isinstance(role_cfg, dict) and role_cfg.get("api_key"):
            role_cfg["has_api_key"] = True
            role_cfg["api_key"] = "***"
    return masked


def get_or_create_admin_password() -> tuple:
    """
    Resolve the admin password/PIN from env vars (LATENT_SPACE_ADMIN_PASSWORD,
    ADMIN_PASSWORD, ADMIN_PIN) or persist a generated 8-char passcode in config/admin_pin.txt.
    Returns (password, source).
    """
    env = load_env_vars()
    for var in ("LATENT_SPACE_ADMIN_PASSWORD", "ADMIN_PASSWORD", "ADMIN_PIN"):
        val = (env.get(var) or "").strip()
        if val:
            return val, f"env:{var}"

    ensure_directories()
    with _lock:
        if ADMIN_PIN_FILE.exists():
            try:
                existing = ADMIN_PIN_FILE.read_text(encoding="utf-8").strip()
                if existing:
                    return existing, str(ADMIN_PIN_FILE)
            except Exception:
                pass
        pin = secrets.token_hex(4).upper()
        atomic_write_text(ADMIN_PIN_FILE, pin + "\n", encoding="utf-8")
        try:
            os.chmod(ADMIN_PIN_FILE, 0o600)
        except Exception:
            pass
        return pin, str(ADMIN_PIN_FILE)


def save_settings_unlocked(settings: dict) -> dict:
    global _memory_settings
    ensure_directories()
    existing = _memory_settings
    if existing is None and SETTINGS_FILE.exists():
        try:
            existing = _read_existing_settings_raw()
        except Exception:
            existing = None
    base = _deep_merge(DEFAULT_SETTINGS, existing or {})
    sanitized = _sanitize_incoming_settings(settings, existing=base)
    if isinstance(sanitized.get("attributions"), dict):
        sanitized["attributions"] = validate_attributions_data(sanitized["attributions"])
    if isinstance(sanitized.get("printing"), dict):
        sanitized["printing"] = validate_printing_data(sanitized["printing"])
    merged = _deep_merge(base, sanitized)
    _memory_settings = deepcopy(merged)
    try:
        atomic_write_text(SETTINGS_FILE, json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"[config_manager] Note: kept settings in memory ({e})")
    return merged


def save_settings(settings: dict) -> dict:
    """Thread-safe atomic save of settings to config/settings.json."""
    with _lock:
        return save_settings_unlocked(settings)


def update_settings_section(section: str, data: dict) -> dict:
    """Update a top-level section (e.g. 'ai', 'attributions', 'printing', 'tts') in settings.json."""
    with _lock:
        if SETTINGS_FILE.exists():
            try:
                current = _read_existing_settings_raw()
            except Exception as e:
                raise RuntimeError(f"Refusing to overwrite unreadable settings.json: {e}") from e
        else:
            current = deepcopy(_memory_settings or DEFAULT_SETTINGS)

        clean_data = data
        if section == "ai" and isinstance(data, dict):
            clean_data = _sanitize_incoming_settings({"ai": data}, existing=current).get("ai", data)
        elif section == "attributions":
            clean_data = validate_attributions_data(data)
        elif section == "printing":
            clean_data = validate_printing_data(data)

        if isinstance(current.get(section), dict) and isinstance(clean_data, dict):
            current[section] = _deep_merge(current[section], clean_data)
        else:
            current[section] = clean_data
        return save_settings_unlocked(current)


def mark_file_printed(filename: str, extra_meta: dict = None):
    """Record a printed PDF atomically in shared_output/docs/print_history.json."""
    print_log = DOCS_DIR / "print_history.json"
    with _history_lock:
        history = {"printed": [], "prints": []}
        if print_log.exists():
            try:
                history = json.loads(print_log.read_text(encoding="utf-8"))
            except Exception:
                pass
        if filename not in history.get("printed", []):
            history.setdefault("printed", []).append(filename)
        entry = {
            "filename": filename,
            "timestamp": datetime.now().isoformat(),
        }
        if isinstance(extra_meta, dict):
            entry.update(extra_meta)
        history.setdefault("prints", []).append(entry)
        atomic_write_text(print_log, json.dumps(history, indent=2), encoding="utf-8")


def remove_from_print_history(filenames):
    """Prune deleted PDF filenames from print_history.json atomically."""
    if not filenames:
        return
    remove_set = set(filenames)
    print_log = DOCS_DIR / "print_history.json"
    with _history_lock:
        if not print_log.exists():
            return
        try:
            history = json.loads(print_log.read_text(encoding="utf-8"))
        except Exception:
            return
        history["printed"] = [f for f in history.get("printed", []) if f not in remove_set]
        history["prints"] = [p for p in history.get("prints", []) if p.get("filename") not in remove_set]
        atomic_write_text(print_log, json.dumps(history, indent=2), encoding="utf-8")
