#!/usr/bin/env python3
"""
Centralized configuration and cross-platform path manager for Latent Space.
Removes hardcoded Linux user paths and Google Drive dependencies, and stores
AI provider settings, printing preferences, and Kasboek attribution settings.
"""

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
import json
import math
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time

PROJECT_DIR = Path(__file__).resolve().parent
WEB_UI_DIR = PROJECT_DIR / "web_ui"
ENV_FILE = PROJECT_DIR / "analysis_watcher" / ".env"

SHARED_DIR = Path(os.environ.get("LATENT_SPACE_SHARED_DIR", PROJECT_DIR / "shared_output")).resolve()
DOCS_DIR = SHARED_DIR / "docs"
LOGS_DIR = Path(os.environ.get("LATENT_SPACE_LOGS_DIR", PROJECT_DIR / "logs")).resolve()
CONFIG_DIR = Path(os.environ.get("LATENT_SPACE_CONFIG_DIR", PROJECT_DIR / "config")).resolve()
SETTINGS_FILE = CONFIG_DIR / "settings.json"
ADMIN_PIN_FILE = CONFIG_DIR / "admin_pin.txt"

ALLOWED_PAPER_FORMATS = {"A4", "A3", "A5", "Letter", "Legal"}
ALLOWED_VISION_MODES = {"auto", "vision", "ollama_vision", "siglip", "clip", "stroke_metrics", "resnet50"}
ALLOWED_AI_PROVIDERS = {"ollama", "gemini", "anthropic", "openai_compatible", "buas"}
ALLOWED_FALLBACK_PROVIDERS = {"auto", "none"} | ALLOWED_AI_PROVIDERS
ALLOWED_TTS_PROVIDERS = {"elevenlabs", "google_cloud", "none"}
_PRINTER_NAME_RE = re.compile(r"^[A-Za-z0-9 _.\-:()/\\]{1,128}$")

_lock = threading.Lock()
_history_lock = threading.Lock()
_content_lock = threading.Lock()
_env_cache_lock = threading.Lock()
_env_file_cache = {}  # path_str -> (mtime_ns, size, parsed_dict)
_memory_settings = None


def reload_paths_from_env():
    """Refresh runtime directories from LATENT_SPACE_* env vars (primarily for isolated tests)."""
    global SHARED_DIR, DOCS_DIR, LOGS_DIR, CONFIG_DIR, SETTINGS_FILE, ADMIN_PIN_FILE, _memory_settings
    with _lock:
        SHARED_DIR = Path(os.environ.get("LATENT_SPACE_SHARED_DIR", PROJECT_DIR / "shared_output")).resolve()
        DOCS_DIR = SHARED_DIR / "docs"
        LOGS_DIR = Path(os.environ.get("LATENT_SPACE_LOGS_DIR", PROJECT_DIR / "logs")).resolve()
        CONFIG_DIR = Path(os.environ.get("LATENT_SPACE_CONFIG_DIR", PROJECT_DIR / "config")).resolve()
        SETTINGS_FILE = CONFIG_DIR / "settings.json"
        ADMIN_PIN_FILE = CONFIG_DIR / "admin_pin.txt"
        _memory_settings = None
    with _env_cache_lock:
        _env_file_cache.clear()


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
            "purpose": "",
            "expertise": "",
            "capabilities": "",
            "restrictions": "",
            "additional": "",
            "temperature": 0.7,
            "assistant_key": "8a79f5ec-c5dd-448c-9611-99610792a04b",
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
            "purpose": "",
            "expertise": "",
            "capabilities": "",
            "restrictions": "",
            "additional": "",
            "temperature": 0.8,
            "assistant_key": "959058ad-4417-49e3-9e71-252ee2fb033d",
        },
    },
    "tts": {
        "enabled": False,
        "provider": "elevenlabs",  # "elevenlabs", "google_cloud", "none"
        "voice_id_1": "45QiOxkuEAY6ckHqLMe8",
        "voice_id_2": "tJHJUEHzOkMoPmJJ5jo2",
    },
    "printing": {
        "auto_print": False,
        "printer_name": "default",
        "paper_format": "A4",
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
            {"name": "Co-Founder 3", "url": "https://www.linkedin.com/"},
        ],
        "custom_footer_note": "",
    },
    "server": {
        "host": "0.0.0.0",
        "port": 8000,
        "remote_server_url": "",
    },
}


def ensure_directories():
    """Ensure all required runtime directories exist."""
    for d in [SHARED_DIR, DOCS_DIR, WEB_UI_DIR, LOGS_DIR, CONFIG_DIR]:
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass


@contextmanager
def _interprocess_file_lock(lock_path: Path, timeout_sec: float = 5.0):
    """Cross-platform advisory file lock for settings, print_history, and content.json."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = None
    locked = False
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        if platform.system() == "Windows":
            import msvcrt
            # Ensure lock file has at least 1 byte for byte-range locking
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\x00")
            deadline = time.monotonic() + timeout_sec
            while True:
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    locked = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"Timed out waiting for lock {lock_path}")
                    time.sleep(0.01)
        else:
            import fcntl
            deadline = time.monotonic() + timeout_sec
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"Timed out waiting for lock {lock_path}")
                    time.sleep(0.01)
        yield
    finally:
        if locked and fd is not None:
            try:
                if platform.system() == "Windows":
                    import msvcrt
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_UN)
            except Exception:
                pass
        if fd is not None:
            try:
                os.close(fd)
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


def _parse_env_line(raw_line: str):
    line = raw_line.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None
    if line.startswith("export "):
        line = line[7:].strip()
        if "=" not in line:
            return None
    k, v = line.split("=", 1)
    key = k.strip()
    if not key:
        return None
    val = v.strip()
    if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
        val = val[1:-1]
    else:
        # Strip inline comments in unquoted values (e.g., PORT=8000 # default)
        if "#" in val:
            val = val.split("#", 1)[0].strip()
    return key, val


def _read_env_file_cached(env_path: Path) -> dict:
    if not env_path.exists():
        return {}
    try:
        st = env_path.stat()
        key = str(env_path)
        sig = (st.st_mtime_ns, st.st_size)
        with _env_cache_lock:
            cached = _env_file_cache.get(key)
            if cached and cached[0] == sig[0] and cached[1] == sig[1]:
                return cached[2]
        parsed = {}
        for line in env_path.read_text(encoding="utf-8").splitlines():
            item = _parse_env_line(line)
            if item:
                k, v = item
                if v or k not in parsed:
                    parsed[k] = v
        with _env_cache_lock:
            _env_file_cache[key] = (sig[0], sig[1], parsed)
        return parsed
    except Exception:
        return {}


def load_env_vars() -> dict:
    """Load key=value pairs from os.environ, root .env, and analysis_watcher/.env without empty-value shadowing."""
    env = dict(os.environ)
    for env_path in (PROJECT_DIR / ".env", ENV_FILE):
        parsed = _read_env_file_cached(env_path)
        for k, v in parsed.items():
            # Non-empty values always win over blank placeholders copied from .env.example
            if k not in env or (not str(env[k]).strip() and str(v).strip()):
                env[k] = v
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
            with _interprocess_file_lock(CONFIG_DIR / ".settings.lock"):
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
    for bool_field in ("enabled", "show_logo"):
        if bool_field in cleaned and not isinstance(cleaned[bool_field], bool):
            cleaned[bool_field] = bool(cleaned[bool_field])
    for str_field in (
        "company_name", "intro_text", "learn_more_text",
        "website_label", "company_linkedin_label", "thanks_text", "custom_footer_note",
    ):
        if str_field in cleaned:
            if cleaned[str_field] is None:
                cleaned[str_field] = ""
            elif not isinstance(cleaned[str_field], str):
                raise ValueError(f"attributions.{str_field} must be a string")
            else:
                cleaned[str_field] = cleaned[str_field][:500]
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


def validate_ai_data(data: dict) -> dict:
    """Validate AI vision mode, provider, model, base_url, and temperature fields."""
    if not isinstance(data, dict):
        raise ValueError("ai payload must be an object")
    cleaned = deepcopy(data)
    if "vision_mode" in cleaned:
        vm = str(cleaned["vision_mode"] or "").strip().lower()
        if vm not in ALLOWED_VISION_MODES:
            raise ValueError(f"vision_mode must be one of {sorted(ALLOWED_VISION_MODES)}")
        cleaned["vision_mode"] = vm
    if "ollama_vision_model" in cleaned:
        if not isinstance(cleaned["ollama_vision_model"], str):
            raise ValueError("ollama_vision_model must be a string")
        cleaned["ollama_vision_model"] = cleaned["ollama_vision_model"].strip()[:120] or "moondream"

    for role in ("analysis", "poem"):
        if role not in cleaned:
            continue
        rc = cleaned[role]
        if not isinstance(rc, dict):
            raise ValueError(f"ai.{role} must be an object")
        if "provider" in rc:
            prov = str(rc["provider"] or "").strip().lower()
            if prov not in ALLOWED_AI_PROVIDERS:
                raise ValueError(f"ai.{role}.provider must be one of {sorted(ALLOWED_AI_PROVIDERS)}")
            rc["provider"] = prov
        if "fallback_provider" in rc:
            fb_prov = str(rc["fallback_provider"] or "").strip().lower()
            if fb_prov not in ALLOWED_FALLBACK_PROVIDERS:
                raise ValueError(f"ai.{role}.fallback_provider must be one of {sorted(ALLOWED_FALLBACK_PROVIDERS)}")
            rc["fallback_provider"] = fb_prov
        if "temperature" in rc:
            raw_temp = rc["temperature"]
            if isinstance(raw_temp, bool):
                raise ValueError(f"ai.{role}.temperature must be a number")
            try:
                t_val = float(raw_temp)
            except (TypeError, ValueError) as e:
                raise ValueError(f"ai.{role}.temperature must be numeric") from e
            if not math.isfinite(t_val) or t_val < 0.0 or t_val > 2.0:
                raise ValueError(f"ai.{role}.temperature must be between 0.0 and 2.0")
            rc["temperature"] = t_val
        if "base_url" in rc:
            rc["base_url"] = validate_safe_url(rc["base_url"], f"ai.{role}.base_url")
        for s_field in (
            "model", "fallback_model", "api_key", "name", "description",
            "purpose", "expertise", "capabilities", "restrictions", "additional", "assistant_key",
        ):
            if s_field in rc:
                if rc[s_field] is None:
                    rc[s_field] = ""
                elif not isinstance(rc[s_field], str):
                    raise ValueError(f"ai.{role}.{s_field} must be a string")
                elif s_field in ("model", "fallback_model", "name", "assistant_key"):
                    rc[s_field] = rc[s_field].strip()[:200]
    return cleaned


def validate_tts_data(data: dict) -> dict:
    """Validate TTS settings before persistence."""
    if not isinstance(data, dict):
        raise ValueError("tts payload must be an object")
    cleaned = deepcopy(data)
    if "enabled" in cleaned:
        cleaned["enabled"] = bool(cleaned["enabled"])
    if "provider" in cleaned:
        prov = str(cleaned["provider"] or "").strip().lower()
        if prov not in ALLOWED_TTS_PROVIDERS:
            raise ValueError(f"tts.provider must be one of {sorted(ALLOWED_TTS_PROVIDERS)}")
        cleaned["provider"] = prov
    for vfield in ("voice_id_1", "voice_id_2"):
        if vfield in cleaned:
            if not isinstance(cleaned[vfield], str):
                raise ValueError(f"tts.{vfield} must be a string")
            cleaned[vfield] = cleaned[vfield].strip()[:120]
    return cleaned


def validate_server_data(data: dict) -> dict:
    """Validate server configuration section before persistence."""
    if not isinstance(data, dict):
        raise ValueError("server payload must be an object")
    cleaned = deepcopy(data)
    if "host" in cleaned:
        if not isinstance(cleaned["host"], str) or not cleaned["host"].strip():
            raise ValueError("server.host must be a non-empty string")
        cleaned["host"] = cleaned["host"].strip()[:128]
    if "port" in cleaned:
        raw_port = cleaned["port"]
        if isinstance(raw_port, bool):
            raise ValueError("server.port must be an integer")
        try:
            port_num = int(raw_port)
        except (TypeError, ValueError) as e:
            raise ValueError("server.port must be an integer") from e
        if port_num < 1 or port_num > 65535:
            raise ValueError("server.port must be between 1 and 65535")
        cleaned["port"] = port_num
    if "remote_server_url" in cleaned:
        cleaned["remote_server_url"] = validate_safe_url(cleaned["remote_server_url"], "server.remote_server_url")
    return cleaned


def get_configured_ollama_host(settings: dict = None) -> str:
    """Resolve the active Ollama root URL (without /v1) only from Ollama-configured roles or OLLAMA_HOST."""
    cfg = settings or load_settings()
    ai_cfg = cfg.get("ai", {})
    for role in ("analysis", "poem"):
        rc = ai_cfg.get(role, {})
        if (rc.get("provider") or "").lower() == "ollama":
            raw = (rc.get("base_url") or "").strip()
            if raw.startswith(("http://", "https://")):
                root = raw.rstrip("/")
                if root.endswith("/v1"):
                    root = root[:-3]
                return root.rstrip("/")
    env = load_env_vars()
    env_host = (env.get("OLLAMA_HOST") or env.get("OLLAMA_BASE_URL") or "").strip()
    if env_host.startswith(("http://", "https://")):
        root = env_host.rstrip("/")
        if root.endswith("/v1"):
            root = root[:-3]
        return root.rstrip("/")
    return "http://localhost:11434"


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
    with _lock, _interprocess_file_lock(CONFIG_DIR / ".admin_pin.lock"):
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


def _validate_settings_sections(data: dict) -> dict:
    cleaned = deepcopy(data)
    if isinstance(cleaned.get("ai"), dict):
        cleaned["ai"] = validate_ai_data(cleaned["ai"])
    if isinstance(cleaned.get("tts"), dict):
        cleaned["tts"] = validate_tts_data(cleaned["tts"])
    if isinstance(cleaned.get("attributions"), dict):
        cleaned["attributions"] = validate_attributions_data(cleaned["attributions"])
    if isinstance(cleaned.get("printing"), dict):
        cleaned["printing"] = validate_printing_data(cleaned["printing"])
    if isinstance(cleaned.get("server"), dict):
        cleaned["server"] = validate_server_data(cleaned["server"])
    return cleaned


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
    sanitized = _validate_settings_sections(sanitized)
    merged = _deep_merge(base, sanitized)
    _memory_settings = deepcopy(merged)
    try:
        atomic_write_text(SETTINGS_FILE, json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")
        try:
            os.chmod(SETTINGS_FILE, 0o600)
        except Exception:
            pass
    except Exception as e:
        print(f"[config_manager] Note: kept settings in memory ({e})")
    return merged


def save_settings(settings: dict) -> dict:
    """Thread-safe and process-safe atomic save of settings to config/settings.json."""
    ensure_directories()
    with _lock, _interprocess_file_lock(CONFIG_DIR / ".settings.lock"):
        return save_settings_unlocked(settings)


def update_settings_section(section: str, data: dict) -> dict:
    """Update a top-level section (e.g. 'ai', 'attributions', 'printing', 'tts', 'server') in settings.json."""
    ensure_directories()
    with _lock, _interprocess_file_lock(CONFIG_DIR / ".settings.lock"):
        if SETTINGS_FILE.exists():
            try:
                current = _read_existing_settings_raw()
            except Exception as e:
                raise RuntimeError(f"Refusing to overwrite unreadable settings.json: {e}") from e
        else:
            current = deepcopy(_memory_settings or DEFAULT_SETTINGS)

        clean_data = data
        if section == "ai":
            clean_data = validate_ai_data(_sanitize_incoming_settings({"ai": data}, existing=current).get("ai", data))
        elif section == "attributions":
            clean_data = validate_attributions_data(data)
        elif section == "printing":
            clean_data = validate_printing_data(data)
        elif section == "tts":
            clean_data = validate_tts_data(data)
        elif section == "server":
            clean_data = validate_server_data(data)

        if isinstance(current.get(section), dict) and isinstance(clean_data, dict):
            current[section] = _deep_merge(current[section], clean_data)
        else:
            current[section] = clean_data
        return save_settings_unlocked(current)


def write_content_json(payload: dict):
    """Atomically write shared_output/content.json under thread + inter-process lock."""
    ensure_directories()
    content_file = SHARED_DIR / "content.json"
    with _content_lock, _interprocess_file_lock(SHARED_DIR / ".content.lock"):
        atomic_write_text(content_file, json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def update_content_json(mutator_fn):
    """Atomically read-modify-write shared_output/content.json if it exists."""
    ensure_directories()
    content_file = SHARED_DIR / "content.json"
    with _content_lock, _interprocess_file_lock(SHARED_DIR / ".content.lock"):
        if not content_file.exists():
            return None
        try:
            current = json.loads(content_file.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(current, dict):
            return None
        updated = mutator_fn(current)
        if isinstance(updated, dict):
            atomic_write_text(content_file, json.dumps(updated, indent=2, ensure_ascii=False), encoding="utf-8")
            return updated
        return None


def mark_file_printed(filename: str, extra_meta: dict = None):
    """Record a printed PDF atomically in shared_output/docs/print_history.json."""
    print_log = DOCS_DIR / "print_history.json"
    with _history_lock, _interprocess_file_lock(DOCS_DIR / ".print_history.lock"):
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
    with _history_lock, _interprocess_file_lock(DOCS_DIR / ".print_history.lock"):
        if not print_log.exists():
            return
        try:
            history = json.loads(print_log.read_text(encoding="utf-8"))
        except Exception:
            return
        history["printed"] = [f for f in history.get("printed", []) if f not in remove_set]
        history["prints"] = [p for p in history.get("prints", []) if p.get("filename") not in remove_set]
        atomic_write_text(print_log, json.dumps(history, indent=2), encoding="utf-8")


def _render_pdf_pages_to_pngs(pdf_path: Path, out_dir: Path) -> list:
    """Render PDF pages to PNG files via PyMuPDF or pdftoppm for reliable Windows printing."""
    pages = []
    try:
        import fitz
        doc = fitz.open(str(pdf_path))
        try:
            for idx, page in enumerate(doc):
                pix = page.get_pixmap(dpi=200)
                png_file = out_dir / f"page_{idx:03d}.png"
                pix.save(str(png_file))
                pages.append(png_file)
        finally:
            doc.close()
        if pages:
            return pages
    except Exception:
        pages = []

    if shutil.which("pdftoppm"):
        try:
            prefix = str(out_dir / "page")
            subprocess.run(["pdftoppm", "-png", "-r", "200", str(pdf_path), prefix], check=True, capture_output=True)
            pages = sorted(out_dir.glob("page-*.png"))
            if pages:
                return pages
        except Exception:
            pass
    return []


def _print_windows_via_dotnet(pdf_path: Path, target_printer: str, is_custom_printer: bool) -> bool:
    """Print rendered PDF pages via .NET System.Drawing.Printing (avoids WinError 1155 when Edge is default PDF viewer)."""
    with tempfile.TemporaryDirectory(prefix="ls_winprint_") as tmp_dir:
        pages = _render_pdf_pages_to_pngs(pdf_path, Path(tmp_dir))
        if not pages:
            return False
        win_env = os.environ.copy()
        win_env["LS_PRINT_IMG_DIR"] = tmp_dir
        win_env["LS_PRINTER_NAME"] = target_printer if is_custom_printer else ""
        ps_script = (
            "Add-Type -AssemblyName System.Drawing; "
            "$files = Get-ChildItem -LiteralPath $env:LS_PRINT_IMG_DIR -Filter '*.png' | Sort-Object Name; "
            "if (-not $files) { throw 'No rendered pages found' }; "
            "$idx = 0; "
            "$doc = New-Object System.Drawing.Printing.PrintDocument; "
            "if ($env:LS_PRINTER_NAME) { $doc.PrinterSettings.PrinterName = $env:LS_PRINTER_NAME }; "
            "$doc.add_PrintPage({ param($s, $e) "
            "$img = [System.Drawing.Image]::FromFile($files[$script:idx].FullName); "
            "try { $e.Graphics.DrawImage($img, $e.MarginBounds) } finally { $img.Dispose() }; "
            "$script:idx++; $e.HasMorePages = ($script:idx -lt $files.Count) "
            "}); "
            "$doc.Print()"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            check=True,
            capture_output=True,
            text=True,
            env=win_env,
            timeout=45,
        )
        return True


def send_pdf_to_printer(pdf_path: str, printer_name: str = "default", extra_meta: dict = None) -> dict:
    """
    Cross-platform print helper for Windows, macOS, and Linux.
    Honors configured printer_name and avoids WinError 1155 on Windows 10/11 when Edge/Chrome is default.
    """
    path = Path(pdf_path)
    if not path.exists():
        return {"status": "error", "error": f"File not found: {pdf_path}"}

    target_printer = validate_printer_name(printer_name or "default")
    is_custom_printer = bool(target_printer and target_printer.lower() != "default")

    sys_name = platform.system()
    try:
        if sys_name == "Windows":
            sumatra = shutil.which("SumatraPDF.exe") or shutil.which("SumatraPDF")
            printed = False
            if sumatra:
                cmd = [sumatra, "-print-to", target_printer] if is_custom_printer else [sumatra, "-print-to-default"]
                cmd.extend(["-silent", str(path)])
                try:
                    subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=30)
                    printed = True
                except Exception:
                    printed = False

            if not printed:
                printed = _print_windows_via_dotnet(path, target_printer, is_custom_printer)

            if not printed:
                win_env = os.environ.copy()
                win_env["LS_PRINT_FILE"] = str(path)
                if is_custom_printer:
                    win_env["LS_PRINTER_ARG"] = f'"{target_printer}"'
                    try:
                        subprocess.run(
                            [
                                "powershell", "-NoProfile", "-Command",
                                "Start-Process -FilePath $env:LS_PRINT_FILE -Verb PrintTo -ArgumentList $env:LS_PRINTER_ARG",
                            ],
                            check=True, capture_output=True, text=True, env=win_env, timeout=30,
                        )
                        printed = True
                    except Exception:
                        printed = False
                if not printed:
                    try:
                        os.startfile(str(path), "print")
                    except Exception:
                        subprocess.run(
                            [
                                "powershell", "-NoProfile", "-Command",
                                "Start-Process -FilePath $env:LS_PRINT_FILE -Verb Print",
                            ],
                            check=True, capture_output=True, text=True, env=win_env, timeout=30,
                        )
        else:
            cmd = ["lp"]
            if is_custom_printer:
                cmd.extend(["-d", target_printer])
            cmd.append(str(path))
            subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=30)

        mark_file_printed(path.name, extra_meta=extra_meta)
        return {"status": "success", "filename": path.name, "printer": target_printer or "default"}
    except Exception as e:
        return {"status": "error", "error": str(e)}
