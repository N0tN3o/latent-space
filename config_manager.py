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
import socket
import subprocess
import tempfile
import threading
import time
from urllib.parse import urlparse

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
ALLOWED_LANGUAGES = {"en", "hu"}
SUPPORTED_LANGUAGES = ("en", "hu")
ALLOWED_TRANSLATION_MODES = {"local_nmt", "ollama", "native_llm", "off"}
DEFAULT_LOCAL_NMT_MODEL = "Helsinki-NLP/opus-mt-tc-big-en-hu"
_HF_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,160}$")
_PRINTER_NAME_RE = re.compile(r"^[A-Za-z0-9 _.\-:()/\\]{1,128}$")
SETTINGS_SECTIONS = ("ai", "translation", "tts", "printing", "attributions", "server")
MAX_PROMPT_FIELD_CHARS = 8000
MAX_API_KEY_CHARS = 512
# QR capacity is 213 bytes; "/shared_output/docs/print_YYYYMMDD_HHMMSS_mmm.pdf" adds 49 bytes.
MAX_REMOTE_URL_BYTES = 160

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
    "write a compelling, poetic, second-person psychoanalytical interpretation of 70 to 110 words "
    "(strict maximum: 110 words total; you may use multiple short paragraphs as long as the total "
    "stays within the word limit) about what their subconscious gaze reveals—their inner tensions, "
    "focus, curiosity, and emotional state. Do not mention technical metrics or numbers directly; "
    "translate them into psychological insight. End with a complete concluding sentence."
)

DEFAULT_POEM_PROMPT = (
    "You are a minimalist haiku poet for the art installation 'Latent Space: Through the Eyes of the Algorithm'. "
    "Based on the psychoanalytical interpretation of a visitor's eye-tracked drawing, write a single, evocative haiku "
    "(exactly 3 short unrhymed lines, following a 5-7-5 syllable rhythm) that distills the mood, movement, and quiet "
    "essence of their gaze into concrete imagery. Do NOT tell a story, do NOT rhyme, and do NOT explain the psychology. "
    "Return ONLY the 3-line haiku with no title, no syllable counts, no quotes, and no commentary."
)

DEFAULT_VISION_LABEL_PROMPT = (
    "Look at this abstract inkblot line drawing like a Rorschach test. "
    "What single creature, object, or natural symbol does it resemble most? "
    "Reply with only 1 to 3 words."
)

DEFAULT_VISION_DESCRIPTION_PROMPT = (
    "Describe the visual shape, flow, tension, and mood of these abstract lines in two short sentences."
)

ALLOWED_VISION_BADGE_STYLES = {"vision_only", "combined", "geometry_only"}
ALLOWED_THINKING_MODES = {"auto", "off", "low", "medium", "high"}
ALLOWED_VISION_TOKEN_BUDGETS = (70, 140, 280, 560, 1120)
ALLOWED_VISION_KEYS = {
    "ollama_label_prompt",
    "ollama_description_prompt",
    "ollama_include_description",
    "ollama_temperature",
    "ollama_max_tokens",
    "ollama_timeout",
    "ollama_thinking",
    "vision_token_budget",
    "clean_label",
    "badge_style",
    "zero_shot_archetypes",
    "zero_shot_tones",
    "resnet_top_k",
}
ALLOWED_VV_ATTRIBUTION_KEYS = {
    "enabled",
    "show_logo",
    "logo_path",
    "community_name",
    "intro_text",
    "learn_more_text",
    "website_label",
    "website_url",
    "linkedin_label",
    "linkedin_url",
    "facebook_label",
    "facebook_url",
    "custom_note",
}
_OLLAMA_MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,119}$")

DEFAULT_SETTINGS = {
    "ai": {
        "vision_mode": "auto",  # "auto", "vision", "ollama_vision", "siglip", "clip", "stroke_metrics", "resnet50"
        "ollama_vision_model": "moondream",
        "vision": {
            "ollama_label_prompt": DEFAULT_VISION_LABEL_PROMPT,
            "ollama_description_prompt": DEFAULT_VISION_DESCRIPTION_PROMPT,
            "ollama_include_description": True,
            "ollama_temperature": 0.4,
            "ollama_max_tokens": 280,
            "ollama_timeout": 60,
            "ollama_thinking": "auto",  # "auto" | "off" | "low" | "medium" | "high"
            "vision_token_budget": 280,  # Gemma 4 visual soft-token tier: 70 | 140 | 280 | 560 | 1120
            "clean_label": True,
            "badge_style": "vision_only",  # "vision_only" | "combined" | "geometry_only"
            "zero_shot_archetypes": "",
            "zero_shot_tones": "",
            "resnet_top_k": 1,
        },
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
            "max_words": 110,
            "thinking_mode": "auto",  # "auto" | "off" | "low" | "medium" | "high"
            "max_tokens": 0,  # 0 = model default; 64..4096 caps thinking + output tokens
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
            "thinking_mode": "auto",  # "auto" | "off" | "low" | "medium" | "high"
            "max_tokens": 0,  # 0 = model default; 64..4096 caps thinking + output tokens
            "assistant_key": "959058ad-4417-49e3-9e71-252ee2fb033d",
        },
    },
    "translation": {
        "enabled": True,
        "mode": "local_nmt",  # "local_nmt" | "ollama" | "native_llm" | "off"
        "target_languages": ["hu"],
        "local_nmt_model": DEFAULT_LOCAL_NMT_MODEL,
        "local_nmt_threads": 6,
        "local_nmt_preload": False,
        "ollama_model": "translategemma:4b",
        "ollama_base_url": "",
        "ollama_temperature": 0.2,
        "ollama_timeout": 45,
        "default_print_language": "en",
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
        "logo_path": "/assets/kasboek/kasboek-logo.jpg",
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
        "veszprem_vibe": {
            "enabled": False,
            "show_logo": True,
            "logo_path": "/assets/veszprem-vibe/vv-skyline-on-paper.svg",
            "community_name": "Veszprém Vibe",
            "intro_text": "Created for the Veszprém Vibe community.",
            "learn_more_text": "Connect with us:",
            "website_label": "veszpremvibe.hu",
            "website_url": "https://veszpremvibe.hu",
            "linkedin_label": "LinkedIn",
            "linkedin_url": "",
            "facebook_label": "Facebook",
            "facebook_url": "",
            "custom_note": "",
        },
    },
    "server": {
        "host": "0.0.0.0",
        "port": 8000,
        "https_enabled": True,
        "https_port": 8443,
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


def _try_lock_fd(fd: int) -> bool:
    if platform.system() == "Windows":
        import msvcrt
        # msvcrt.locking takes a mandatory byte-range lock that may extend past EOF,
        # so no padding write is needed (a write into a region locked by another
        # process would raise PermissionError instead of reporting "busy").
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def _unlock_fd(fd: int):
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


def try_acquire_nonblocking_lock(lock_path: Path):
    """Attempt a non-blocking inter-process lock; returns an open fd on success or None if held."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = None
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        if _try_lock_fd(fd):
            return fd
        os.close(fd)
        return None
    except Exception:
        if fd is not None:
            try:
                os.close(fd)
            except Exception:
                pass
        return None


def release_nonblocking_lock(fd):
    """Release an advisory lock fd acquired via try_acquire_nonblocking_lock()."""
    if fd is None:
        return
    _unlock_fd(fd)
    try:
        os.close(fd)
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
        deadline = time.monotonic() + timeout_sec
        while True:
            if _try_lock_fd(fd):
                locked = True
                break
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out waiting for lock {lock_path}")
            time.sleep(0.01)
        yield
    finally:
        if locked and fd is not None:
            _unlock_fd(fd)
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
    """Strip masked/transient API key fields and prevent stale keys crossing changed endpoints."""
    cleaned = deepcopy(data) if isinstance(data, dict) else {}
    ai = cleaned.get("ai")
    if isinstance(ai, dict):
        existing_ai = (existing or {}).get("ai", {})
        for role in ("analysis", "poem"):
            role_cfg = ai.get(role)
            if isinstance(role_cfg, dict):
                role_cfg.pop("has_api_key", None)
                prev_cfg = existing_ai.get(role, {}) if isinstance(existing_ai, dict) else {}
                prev_prov = (prev_cfg.get("provider") or "ollama").strip().lower()
                next_prov = (role_cfg.get("provider") or prev_prov).strip().lower()
                prev_url = (prev_cfg.get("base_url") or "").strip().rstrip("/")
                raw_next_url = role_cfg.get("base_url") if "base_url" in role_cfg else prev_url
                next_url = (raw_next_url or "").strip().rstrip("/")
                endpoint_changed = (next_prov != prev_prov) or (
                    next_prov in ("openai_compatible", "ollama")
                    and "base_url" in role_cfg
                    and next_url != prev_url
                )
                raw_key = role_cfg.get("api_key")
                if raw_key == "***":
                    prev_key = prev_cfg.get("api_key", "")
                    if prev_key and not endpoint_changed:
                        role_cfg["api_key"] = prev_key
                    elif endpoint_changed:
                        role_cfg["api_key"] = ""
                    else:
                        role_cfg.pop("api_key", None)
                elif "api_key" not in role_cfg and endpoint_changed:
                    role_cfg["api_key"] = ""
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
        # Strip inline comments in unquoted values (e.g., PORT=8000 # default), but only when
        # '#' is preceded by whitespace so secrets like "Pass#2026" are preserved.
        val = re.split(r"\s+#", val, maxsplit=1)[0].strip()
        if val.startswith("#"):
            val = ""
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


def validate_safe_url(url: str, field_name: str = "url", allow_relative: bool = True) -> str:
    """Allow only empty strings, relative root paths ('/...', if allow_relative), or http(s):// URLs."""
    if url is None:
        return ""
    if not isinstance(url, str):
        raise ValueError(f"{field_name} must be a string")
    val = url.strip()
    if not val:
        return ""
    if any(ord(ch) <= 32 or ch in ('"', "'", "<", ">") for ch in val):
        raise ValueError(f"{field_name} contains invalid whitespace, quote, or control characters")
    low = val.lower()
    if low.startswith(("http://", "https://")):
        if not urlparse(val).hostname:
            raise ValueError(f"{field_name} is missing a host")
        return val
    if allow_relative and val.startswith("/") and not val.startswith("//"):
        return val
    if not allow_relative:
        raise ValueError(f"{field_name} must be an absolute http:// or https:// URL")
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
    if "logo_path" in cleaned:
        cleaned["logo_path"] = validate_safe_url(cleaned["logo_path"], "logo_path", allow_relative=True)
    for url_field in ("website_url", "company_linkedin_url"):
        if url_field in cleaned:
            cleaned[url_field] = validate_safe_url(cleaned[url_field], url_field, allow_relative=False)
    if "founders" in cleaned:
        if not isinstance(cleaned["founders"], list):
            raise ValueError("founders must be a list")
        safe_founders = []
        for idx, f in enumerate(cleaned["founders"][:5]):
            if not isinstance(f, dict):
                raise ValueError(f"founders[{idx}] must be an object")
            safe_founders.append({
                "name": str(f.get("name") or "")[:120],
                "url": validate_safe_url(f.get("url", ""), f"founders[{idx}].url", allow_relative=False),
            })
        cleaned["founders"] = safe_founders
    if "veszprem_vibe" in cleaned:
        vv = cleaned["veszprem_vibe"]
        if not isinstance(vv, dict):
            raise ValueError("attributions.veszprem_vibe must be an object")
        unknown_vv = set(vv.keys()) - ALLOWED_VV_ATTRIBUTION_KEYS
        if unknown_vv:
            raise ValueError(f"Unknown attributions.veszprem_vibe keys: {sorted(unknown_vv)}")
        vv_clean = deepcopy(vv)
        for bool_field in ("enabled", "show_logo"):
            if bool_field in vv_clean and not isinstance(vv_clean[bool_field], bool):
                vv_clean[bool_field] = bool(vv_clean[bool_field])
        for str_field in (
            "community_name", "intro_text", "learn_more_text",
            "website_label", "linkedin_label", "facebook_label", "custom_note",
        ):
            if str_field in vv_clean:
                if vv_clean[str_field] is None:
                    vv_clean[str_field] = ""
                elif not isinstance(vv_clean[str_field], str):
                    raise ValueError(f"attributions.veszprem_vibe.{str_field} must be a string")
                else:
                    vv_clean[str_field] = vv_clean[str_field][:500]
        if "logo_path" in vv_clean:
            vv_clean["logo_path"] = validate_safe_url(vv_clean["logo_path"], "veszprem_vibe.logo_path", allow_relative=True)
        for url_field in ("website_url", "linkedin_url", "facebook_url"):
            if url_field in vv_clean:
                vv_clean[url_field] = validate_safe_url(vv_clean[url_field], f"veszprem_vibe.{url_field}", allow_relative=False)
        cleaned["veszprem_vibe"] = vv_clean
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
        ovm = cleaned["ollama_vision_model"].strip() or "moondream"
        if not _OLLAMA_MODEL_NAME_RE.match(ovm):
            raise ValueError("ollama_vision_model contains disallowed characters or exceeds 120 chars")
        cleaned["ollama_vision_model"] = ovm

    if "vision" in cleaned:
        vc = cleaned["vision"]
        if not isinstance(vc, dict):
            raise ValueError("ai.vision must be an object")
        unknown_v = set(vc.keys()) - ALLOWED_VISION_KEYS
        if unknown_v:
            raise ValueError(f"Unknown ai.vision keys: {sorted(unknown_v)}")
        vc_clean = deepcopy(vc)
        for s_field in ("ollama_label_prompt", "ollama_description_prompt", "zero_shot_archetypes", "zero_shot_tones"):
            if s_field in vc_clean:
                if vc_clean[s_field] is None:
                    vc_clean[s_field] = ""
                elif not isinstance(vc_clean[s_field], str):
                    raise ValueError(f"ai.vision.{s_field} must be a string")
                elif len(vc_clean[s_field]) > MAX_PROMPT_FIELD_CHARS:
                    raise ValueError(f"ai.vision.{s_field} exceeds {MAX_PROMPT_FIELD_CHARS} characters")
        for b_field in ("ollama_include_description", "clean_label"):
            if b_field in vc_clean and not isinstance(vc_clean[b_field], bool):
                vc_clean[b_field] = bool(vc_clean[b_field])
        if "badge_style" in vc_clean:
            bs = str(vc_clean["badge_style"] or "vision_only").strip().lower()
            if bs not in ALLOWED_VISION_BADGE_STYLES:
                raise ValueError(f"ai.vision.badge_style must be one of {sorted(ALLOWED_VISION_BADGE_STYLES)}")
            vc_clean["badge_style"] = bs
        if "ollama_temperature" in vc_clean:
            raw_vt = vc_clean["ollama_temperature"]
            if isinstance(raw_vt, bool):
                raise ValueError("ai.vision.ollama_temperature must be numeric")
            try:
                vt = float(raw_vt)
            except (TypeError, ValueError) as e:
                raise ValueError("ai.vision.ollama_temperature must be numeric") from e
            if not math.isfinite(vt) or vt < 0.0 or vt > 2.0:
                raise ValueError("ai.vision.ollama_temperature must be between 0.0 and 2.0")
            vc_clean["ollama_temperature"] = vt
        if "ollama_max_tokens" in vc_clean:
            raw_mt = vc_clean["ollama_max_tokens"]
            if isinstance(raw_mt, bool):
                raise ValueError("ai.vision.ollama_max_tokens must be an integer")
            try:
                mt = int(raw_mt)
            except (TypeError, ValueError) as e:
                raise ValueError("ai.vision.ollama_max_tokens must be an integer") from e
            if mt < 16 or mt > 2048:
                raise ValueError("ai.vision.ollama_max_tokens must be between 16 and 2048")
            vc_clean["ollama_max_tokens"] = mt
        if "ollama_thinking" in vc_clean:
            ot = str(vc_clean["ollama_thinking"] or "auto").strip().lower()
            if ot not in ALLOWED_THINKING_MODES:
                raise ValueError(f"ai.vision.ollama_thinking must be one of {sorted(ALLOWED_THINKING_MODES)}")
            vc_clean["ollama_thinking"] = ot
        if "vision_token_budget" in vc_clean:
            raw_vtb = vc_clean["vision_token_budget"]
            if isinstance(raw_vtb, bool):
                raise ValueError("ai.vision.vision_token_budget must be an integer")
            try:
                vtb = int(raw_vtb)
            except (TypeError, ValueError) as e:
                raise ValueError("ai.vision.vision_token_budget must be an integer") from e
            if vtb not in ALLOWED_VISION_TOKEN_BUDGETS:
                raise ValueError(
                    f"ai.vision.vision_token_budget must be one of {list(ALLOWED_VISION_TOKEN_BUDGETS)}"
                )
            vc_clean["vision_token_budget"] = vtb
        if "ollama_timeout" in vc_clean:
            raw_to = vc_clean["ollama_timeout"]
            if isinstance(raw_to, bool):
                raise ValueError("ai.vision.ollama_timeout must be an integer")
            try:
                to_val = int(raw_to)
            except (TypeError, ValueError) as e:
                raise ValueError("ai.vision.ollama_timeout must be an integer") from e
            if to_val < 5 or to_val > 180:
                raise ValueError("ai.vision.ollama_timeout must be between 5 and 180 seconds")
            vc_clean["ollama_timeout"] = to_val
        if "resnet_top_k" in vc_clean:
            raw_k = vc_clean["resnet_top_k"]
            if isinstance(raw_k, bool):
                raise ValueError("ai.vision.resnet_top_k must be an integer")
            try:
                kv = int(raw_k)
            except (TypeError, ValueError) as e:
                raise ValueError("ai.vision.resnet_top_k must be an integer") from e
            if kv < 1 or kv > 5:
                raise ValueError("ai.vision.resnet_top_k must be between 1 and 5")
            vc_clean["resnet_top_k"] = kv
        cleaned["vision"] = vc_clean

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
        if "max_words" in rc:
            raw_mw = rc["max_words"]
            if isinstance(raw_mw, bool):
                raise ValueError(f"ai.{role}.max_words must be an integer")
            try:
                mw_val = int(raw_mw)
            except (TypeError, ValueError) as e:
                raise ValueError(f"ai.{role}.max_words must be an integer") from e
            if mw_val < 0 or mw_val > 500:
                raise ValueError(f"ai.{role}.max_words must be between 0 and 500")
            rc["max_words"] = mw_val
        if "thinking_mode" in rc:
            tm = str(rc["thinking_mode"] or "auto").strip().lower()
            if tm not in ALLOWED_THINKING_MODES:
                raise ValueError(f"ai.{role}.thinking_mode must be one of {sorted(ALLOWED_THINKING_MODES)}")
            rc["thinking_mode"] = tm
        if "max_tokens" in rc:
            raw_mtok = rc["max_tokens"]
            if isinstance(raw_mtok, bool):
                raise ValueError(f"ai.{role}.max_tokens must be an integer")
            try:
                mtok_val = int(raw_mtok)
            except (TypeError, ValueError) as e:
                raise ValueError(f"ai.{role}.max_tokens must be an integer") from e
            if mtok_val != 0 and (mtok_val < 64 or mtok_val > 4096):
                raise ValueError(f"ai.{role}.max_tokens must be 0 (auto) or between 64 and 4096")
            rc["max_tokens"] = mtok_val
        if "base_url" in rc:
            rc["base_url"] = validate_safe_url(rc["base_url"], f"ai.{role}.base_url", allow_relative=False)
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
                elif s_field == "api_key":
                    if len(rc[s_field]) > MAX_API_KEY_CHARS:
                        raise ValueError(f"ai.{role}.api_key exceeds {MAX_API_KEY_CHARS} characters")
                    rc[s_field] = rc[s_field].strip()
                elif len(rc[s_field]) > MAX_PROMPT_FIELD_CHARS:
                    raise ValueError(f"ai.{role}.{s_field} exceeds {MAX_PROMPT_FIELD_CHARS} characters")
    return cleaned


def validate_translation_data(data: dict) -> dict:
    """Validate multilingual translation engine settings before persistence."""
    if not isinstance(data, dict):
        raise ValueError("translation payload must be an object")
    cleaned = deepcopy(data)
    allowed_keys = {
        "enabled",
        "mode",
        "target_languages",
        "local_nmt_model",
        "local_nmt_threads",
        "local_nmt_preload",
        "ollama_model",
        "ollama_base_url",
        "ollama_temperature",
        "ollama_timeout",
        "default_print_language",
    }
    unknown = set(cleaned.keys()) - allowed_keys
    if unknown:
        raise ValueError(f"Unknown translation keys: {sorted(unknown)}")
    for b_field in ("enabled", "local_nmt_preload"):
        if b_field in cleaned and not isinstance(cleaned[b_field], bool):
            cleaned[b_field] = bool(cleaned[b_field])
    if "mode" in cleaned:
        mode = str(cleaned["mode"] or "local_nmt").strip().lower()
        if mode not in ALLOWED_TRANSLATION_MODES:
            raise ValueError(f"translation.mode must be one of {sorted(ALLOWED_TRANSLATION_MODES)}")
        cleaned["mode"] = mode
    if "target_languages" in cleaned:
        raw_langs = cleaned["target_languages"]
        if not isinstance(raw_langs, list):
            raise ValueError("translation.target_languages must be a list of language codes")
        out_langs = []
        for item in raw_langs:
            code = str(item or "").strip().lower()
            if code not in ALLOWED_LANGUAGES or code == "en":
                raise ValueError(f"Unsupported target language '{item}'; allowed: {sorted(ALLOWED_LANGUAGES - {'en'})}")
            if code not in out_langs:
                out_langs.append(code)
        cleaned["target_languages"] = out_langs
    if "local_nmt_model" in cleaned:
        if not isinstance(cleaned["local_nmt_model"], str):
            raise ValueError("translation.local_nmt_model must be a string")
        m_id = cleaned["local_nmt_model"].strip() or DEFAULT_LOCAL_NMT_MODEL
        if ".." in m_id or not _HF_MODEL_ID_RE.match(m_id):
            raise ValueError("translation.local_nmt_model contains disallowed characters")
        cleaned["local_nmt_model"] = m_id
    if "local_nmt_threads" in cleaned:
        raw_thr = cleaned["local_nmt_threads"]
        if isinstance(raw_thr, bool):
            raise ValueError("translation.local_nmt_threads must be an integer")
        try:
            thr = int(raw_thr)
        except (TypeError, ValueError) as e:
            raise ValueError("translation.local_nmt_threads must be an integer") from e
        if thr < 1 or thr > 16:
            raise ValueError("translation.local_nmt_threads must be between 1 and 16")
        cleaned["local_nmt_threads"] = thr
    if "ollama_model" in cleaned:
        if not isinstance(cleaned["ollama_model"], str):
            raise ValueError("translation.ollama_model must be a string")
        om = cleaned["ollama_model"].strip() or "translategemma:4b"
        if not _OLLAMA_MODEL_NAME_RE.match(om):
            raise ValueError("translation.ollama_model contains disallowed characters or exceeds 120 chars")
        cleaned["ollama_model"] = om
    if "ollama_base_url" in cleaned:
        cleaned["ollama_base_url"] = validate_safe_url(
            cleaned["ollama_base_url"], "translation.ollama_base_url", allow_relative=False
        )
    if "ollama_temperature" in cleaned:
        raw_t = cleaned["ollama_temperature"]
        if isinstance(raw_t, bool):
            raise ValueError("translation.ollama_temperature must be numeric")
        try:
            tv = float(raw_t)
        except (TypeError, ValueError) as e:
            raise ValueError("translation.ollama_temperature must be numeric") from e
        if not math.isfinite(tv) or tv < 0.0 or tv > 2.0:
            raise ValueError("translation.ollama_temperature must be between 0.0 and 2.0")
        cleaned["ollama_temperature"] = tv
    if "ollama_timeout" in cleaned:
        raw_to = cleaned["ollama_timeout"]
        if isinstance(raw_to, bool):
            raise ValueError("translation.ollama_timeout must be an integer")
        try:
            to_val = int(raw_to)
        except (TypeError, ValueError) as e:
            raise ValueError("translation.ollama_timeout must be an integer") from e
        if to_val < 5 or to_val > 180:
            raise ValueError("translation.ollama_timeout must be between 5 and 180 seconds")
        cleaned["ollama_timeout"] = to_val
    if "default_print_language" in cleaned:
        pl = str(cleaned["default_print_language"] or "en").strip().lower()
        if pl not in ALLOWED_LANGUAGES:
            raise ValueError(f"translation.default_print_language must be one of {sorted(ALLOWED_LANGUAGES)}")
        cleaned["default_print_language"] = pl
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
    if "https_enabled" in cleaned:
        cleaned["https_enabled"] = bool(cleaned["https_enabled"])
    if "https_port" in cleaned:
        raw_hport = cleaned["https_port"]
        if isinstance(raw_hport, bool):
            raise ValueError("server.https_port must be an integer")
        try:
            hport_num = int(raw_hport)
        except (TypeError, ValueError) as e:
            raise ValueError("server.https_port must be an integer") from e
        if hport_num < 0 or hport_num > 65535:
            raise ValueError("server.https_port must be between 0 and 65535")
        cleaned["https_port"] = hport_num
    if "remote_server_url" in cleaned:
        remote = validate_safe_url(cleaned["remote_server_url"], "server.remote_server_url", allow_relative=False)
        remote = remote.rstrip("/")
        if len(remote.encode("utf-8")) > MAX_REMOTE_URL_BYTES:
            raise ValueError(
                f"server.remote_server_url must be at most {MAX_REMOTE_URL_BYTES} bytes "
                "so printed QR codes still fit"
            )
        cleaned["remote_server_url"] = remote
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


def get_server_port(settings: dict = None) -> int:
    """Resolve server HTTP port from env/.env PORT, then settings.server.port, defaulting to 8000."""
    env = load_env_vars()
    raw_env_port = (env.get("PORT") or "").strip()
    if raw_env_port:
        try:
            p = int(raw_env_port)
            if 1 <= p <= 65535:
                return p
        except (TypeError, ValueError):
            pass
    cfg = settings or load_settings()
    raw_cfg_port = cfg.get("server", {}).get("port", 8000)
    try:
        p = int(raw_cfg_port)
        if 1 <= p <= 65535:
            return p
    except (TypeError, ValueError):
        pass
    return 8000


def get_server_host(settings: dict = None) -> str:
    """Resolve server bind host from env/.env HOST, then settings.server.host, defaulting to 0.0.0.0."""
    env = load_env_vars()
    raw_env_host = (env.get("HOST") or "").strip()
    if raw_env_host:
        return raw_env_host
    cfg = settings or load_settings()
    raw_cfg_host = (cfg.get("server", {}).get("host") or "").strip()
    return raw_cfg_host or "0.0.0.0"


def get_https_enabled(settings: dict = None) -> bool:
    """Resolve whether the companion offline LAN HTTPS listener should start (default True)."""
    env = load_env_vars()
    raw = (env.get("HTTPS_ENABLED") or env.get("LATENT_SPACE_HTTPS") or "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    cfg = settings or load_settings()
    return bool(cfg.get("server", {}).get("https_enabled", True))


def get_https_port(settings: dict = None) -> int:
    """Resolve companion HTTPS port from env/.env HTTPS_PORT, then settings.server.https_port (default 8443; 0 disables)."""
    env = load_env_vars()
    raw_env_port = (env.get("HTTPS_PORT") or "").strip()
    if raw_env_port:
        try:
            p = int(raw_env_port)
            if 0 <= p <= 65535:
                return p
        except (TypeError, ValueError):
            pass
    cfg = settings or load_settings()
    raw_cfg_port = cfg.get("server", {}).get("https_port", 8443)
    try:
        p = int(raw_cfg_port)
        if 0 <= p <= 65535:
            return p
    except (TypeError, ValueError):
        pass
    return 8443


def get_all_local_ipv4s() -> list:
    """Collect 127.0.0.1 plus all detected local LAN IPv4 addresses for TLS certificate SAN entries."""
    ips = {"127.0.0.1"}
    primary = get_lan_ip()
    if primary and primary != "localhost":
        ips.add(primary)
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addr = info[4][0]
            if addr and not addr.startswith("169.254."):
                ips.add(addr)
    except Exception:
        pass
    return sorted(ips)


def _find_openssl_binary() -> str:
    """Locate openssl executable, including standard Git-for-Windows paths if not on PATH."""
    found = shutil.which("openssl")
    if found:
        return found
    if platform.system() == "Windows":
        for candidate in (
            r"C:\Program Files\Git\mingw64\bin\openssl.exe",
            r"C:\Program Files\Git\usr\bin\openssl.exe",
            r"C:\Program Files (x86)\Git\mingw64\bin\openssl.exe",
            r"C:\Program Files\OpenSSL-Win64\bin\openssl.exe",
        ):
            if Path(candidate).is_file():
                return candidate
    return ""


def _chmod_private(path: Path):
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass


def ensure_lan_tls_cert(cert_dir: Path = None, extra_ips: list = None) -> tuple:
    """
    Ensure a valid local CA (`ca.crt`) and SAN-equipped server certificate (`server.crt`, `server.key`)
    exist for offline LAN HTTPS (phone/tablet camera access on iOS Safari & Android Chrome).
    Automatically regenerates the leaf cert if the host LAN IP changes or approaches expiry.
    Returns (cert_path, key_path, ca_cert_path) or (None, None, None) if TLS tooling is unavailable.
    """
    target_dir = Path(cert_dir) if cert_dir else (CONFIG_DIR / "certs")
    target_dir.mkdir(parents=True, exist_ok=True)
    ca_key = target_dir / "ca.key"
    ca_crt = target_dir / "ca.crt"
    srv_key = target_dir / "server.key"
    srv_crt = target_dir / "server.crt"
    meta_file = target_dir / "cert_meta.json"

    ips = set(get_all_local_ipv4s())
    for ip in (extra_ips or []):
        ip_s = str(ip or "").strip()
        if ip_s and re.match(r"^\d{1,3}(?:\.\d{1,3}){3}$", ip_s):
            ips.add(ip_s)
    sorted_ips = sorted(ips)

    raw_host = ""
    try:
        raw_host = socket.gethostname().strip()
    except Exception:
        raw_host = ""
    dns_names = ["localhost"]
    if raw_host and re.match(r"^[A-Za-z0-9._-]+$", raw_host) and raw_host.lower() != "localhost":
        dns_names.append(raw_host)
        if "." not in raw_host:
            dns_names.append(f"{raw_host}.local")

    # Fast reuse check if certs exist, cover all current LAN IPs, and are under 300 days old
    if ca_crt.exists() and srv_crt.exists() and srv_key.exists() and meta_file.exists():
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            age_sec = time.time() - float(meta.get("generated_at", 0))
            covered_ips = set(meta.get("ips") or [])
            if age_sec < 300 * 86400 and set(sorted_ips).issubset(covered_ips):
                return srv_crt, srv_key, ca_crt
        except Exception:
            pass

    openssl_bin = _find_openssl_binary()
    if not openssl_bin:
        print("[tls] Note: openssl executable not found; offline LAN HTTPS listener skipped.")
        return None, None, None

    run_kwargs = {"capture_output": True, "text": True, "timeout": 30, "check": True}
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        run_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    with tempfile.TemporaryDirectory(prefix="ls_tls_") as tmp_s:
        tmp = Path(tmp_s)
        try:
            # 1. Create a persistent 10-year local Root CA once (can optionally be trusted on exhibition tablets)
            if not (ca_key.exists() and ca_crt.exists()):
                ca_cnf = tmp / "ca.cnf"
                ca_cnf.write_text(
                    "[req]\n"
                    "distinguished_name = dn\n"
                    "x509_extensions = v3_ca\n"
                    "prompt = no\n"
                    "[dn]\n"
                    "CN = Latent Space Local Exhibition CA\n"
                    "O = Latent Space Installation\n"
                    "[v3_ca]\n"
                    "subjectKeyIdentifier = hash\n"
                    "authorityKeyIdentifier = keyid:always,issuer\n"
                    "basicConstraints = critical, CA:TRUE, pathlen:0\n"
                    "keyUsage = critical, keyCertSign, cRLSign\n",
                    encoding="utf-8",
                )
                tmp_ca_key = tmp / "ca.key"
                tmp_ca_crt = tmp / "ca.crt"
                subprocess.run(
                    [
                        openssl_bin, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-keyout", str(tmp_ca_key),
                        "-out", str(tmp_ca_crt),
                        "-days", "3650",
                        "-sha256",
                        "-config", str(ca_cnf),
                    ],
                    **run_kwargs,
                )
                atomic_write_bytes(ca_key, tmp_ca_key.read_bytes())
                _chmod_private(ca_key)
                atomic_write_bytes(ca_crt, tmp_ca_crt.read_bytes())

            # 2. Generate 365-day leaf server certificate with SANs for localhost + all current LAN IPs
            san_entries = [f"DNS:{d}" for d in dns_names] + [f"IP:{ip}" for ip in sorted_ips]
            san_line = ", ".join(san_entries)

            req_cnf = tmp / "req.cnf"
            req_cnf.write_text(
                "[req]\n"
                "distinguished_name = dn\n"
                "prompt = no\n"
                "[dn]\n"
                f"CN = {sorted_ips[-1] if len(sorted_ips) > 1 else 'localhost'}\n"
                "O = Latent Space Installation\n",
                encoding="utf-8",
            )
            ext_cnf = tmp / "ext.cnf"
            ext_cnf.write_text(
                "authorityKeyIdentifier = keyid,issuer\n"
                "basicConstraints = critical, CA:FALSE\n"
                "keyUsage = critical, digitalSignature, keyEncipherment\n"
                "extendedKeyUsage = serverAuth\n"
                f"subjectAltName = {san_line}\n",
                encoding="utf-8",
            )

            tmp_srv_key = tmp / "server.key"
            tmp_srv_csr = tmp / "server.csr"
            tmp_srv_crt = tmp / "server.crt"
            tmp_srl = tmp / "ca.srl"

            subprocess.run(
                [
                    openssl_bin, "req", "-new", "-newkey", "rsa:2048", "-nodes",
                    "-keyout", str(tmp_srv_key),
                    "-out", str(tmp_srv_csr),
                    "-config", str(req_cnf),
                ],
                **run_kwargs,
            )
            subprocess.run(
                [
                    openssl_bin, "x509", "-req",
                    "-in", str(tmp_srv_csr),
                    "-CA", str(ca_crt),
                    "-CAkey", str(ca_key),
                    "-CAcreateserial",
                    "-CAserial", str(tmp_srl),
                    "-out", str(tmp_srv_crt),
                    "-days", "365",
                    "-sha256",
                    "-extfile", str(ext_cnf),
                ],
                **run_kwargs,
            )

            atomic_write_bytes(srv_key, tmp_srv_key.read_bytes())
            _chmod_private(srv_key)
            fullchain = tmp_srv_crt.read_bytes().strip() + b"\n" + ca_crt.read_bytes().strip() + b"\n"
            atomic_write_bytes(srv_crt, fullchain)
            atomic_write_text(
                meta_file,
                json.dumps(
                    {
                        "ips": sorted_ips,
                        "dns": dns_names,
                        "generated_at": time.time(),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            return srv_crt, srv_key, ca_crt
        except Exception as exc:
            err_detail = getattr(exc, "stderr", "") or str(exc)
            print(f"[tls] Warning: could not generate LAN HTTPS certificate ({err_detail.strip()}); continuing with HTTP only.")
            return None, None, None


def get_lan_ip() -> str:
    """Detect local LAN IPv4 address, falling back to hostname resolution on air-gapped networks."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
        finally:
            s.close()
    except Exception:
        pass
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
        fallback = ""
        for info in infos:
            addr = info[4][0]
            if not addr or addr.startswith("127."):
                continue
            if not addr.startswith("169.254."):
                return addr
            if not fallback:
                fallback = addr
        if fallback:
            return fallback
    except Exception:
        pass
    return "localhost"


def get_public_base_url(settings: dict = None, lan_ip: str = None) -> str:
    """Resolve the canonical public/QR base URL (remote_server_url if configured, else LAN IP + port)."""
    cfg = settings or load_settings()
    remote_url = (cfg.get("server", {}).get("remote_server_url") or "").strip().rstrip("/")
    if remote_url.startswith(("http://", "https://")):
        return remote_url
    host_ip = lan_ip or get_lan_ip()
    return f"http://{host_ip}:{get_server_port(cfg)}"


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
    if not isinstance(data, dict):
        raise ValueError("settings payload must be an object")
    cleaned = deepcopy(data)
    # Unknown top-level keys are not validated here (they are rejected at the API layer);
    # keys already present on disk are preserved by the merge in save_settings_unlocked.
    for section in SETTINGS_SECTIONS:
        if section in cleaned and not isinstance(cleaned[section], dict):
            raise ValueError(f"settings.{section} must be an object")
    if "ai" in cleaned:
        cleaned["ai"] = validate_ai_data(cleaned["ai"])
    if "translation" in cleaned:
        cleaned["translation"] = validate_translation_data(cleaned["translation"])
    if "tts" in cleaned:
        cleaned["tts"] = validate_tts_data(cleaned["tts"])
    if "attributions" in cleaned:
        cleaned["attributions"] = validate_attributions_data(cleaned["attributions"])
    if "printing" in cleaned:
        cleaned["printing"] = validate_printing_data(cleaned["printing"])
    if "server" in cleaned:
        cleaned["server"] = validate_server_data(cleaned["server"])
    return cleaned


def save_settings_unlocked(settings: dict) -> dict:
    global _memory_settings
    ensure_directories()
    if SETTINGS_FILE.exists():
        # Always merge onto the on-disk state (another process may have written it) and
        # never silently replace an unreadable file with defaults.
        try:
            existing = _read_existing_settings_raw()
        except Exception as e:
            raise RuntimeError(f"Refusing to overwrite unreadable settings.json: {e}") from e
    else:
        existing = _memory_settings
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
        elif section == "translation":
            clean_data = validate_translation_data(data)
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
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_script],
                check=True,
                capture_output=True,
                text=True,
                env=win_env,
                timeout=45,
            )
            return True
        except Exception:
            return False


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
