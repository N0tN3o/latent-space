#!/usr/bin/env python3
"""
Unified Latent Space Server (Zero External Web Framework Dependencies)
Uses Python's built-in ThreadingHTTPServer so it runs out-of-the-box in any environment
(Windows, macOS, Linux, Docker, or existing .venv without requiring Flask).

Serves:
  - / or /index.html          -> Main Gallery (index.html)
  - /draw or /draw.html       -> Browser Gaze Painter (web_ui/draw.html)
  - /kiosk or /kiosk.html     -> Web Print Kiosk (web_ui/kiosk.html)
  - /admin or /admin.html     -> Admin Command Center (web_ui/admin.html)
  - /shared_output/*          -> Drawings, heatmaps, PDFs, audio, content.json
  - /api/*                    -> Status, AI config, attributions, drawings, printing, logs, QR fallback
"""

import base64
from datetime import datetime, timedelta, timezone
from email.parser import BytesParser
from email.policy import default as email_default_policy
import hmac
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import mimetypes
import os
from pathlib import Path
import queue
import re
import secrets
import shutil
import signal
import ssl
import struct
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, unquote, urlparse
import urllib.request

for _stream_name in ("stdout", "stderr"):
    _stream = getattr(sys, _stream_name, None)
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(errors="replace")
        except Exception:
            pass

try:
    import psutil
    psutil.cpu_percent(interval=None)
except ImportError:
    psutil = None

PROJECT_DIR = Path(__file__).resolve().parent
ANALYSIS_DIR = PROJECT_DIR / "analysis_watcher"
for p in (str(PROJECT_DIR), str(ANALYSIS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import config_manager
from config_manager import (
    atomic_write_bytes,
    ensure_directories,
    ensure_lan_tls_cert,
    fast_local_ollama_url,
    get_ca_cert_fingerprint,
    get_configured_ollama_host,
    get_daily_quota_usage,
    get_https_enabled,
    get_https_port,
    get_lan_ip,
    get_or_create_admin_password,
    get_public_base_url,
    get_server_host,
    get_server_port,
    is_thinking_model_name,
    load_settings,
    mark_file_printed,
    mask_settings_secrets,
    release_nonblocking_lock,
    remove_from_print_history,
    save_settings,
    send_pdf_to_printer,
    try_acquire_nonblocking_lock,
    update_content_json,
    update_settings_section,
    validate_printer_name,
    validate_translation_data,
    write_content_json,
)
from qr_utils import qr_svg

ensure_directories()

_SAFE_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_]{4,64}$")
PIPELINE_RUN_DEADLINE_SEC = 150.0

try:
    from analysis import analyze_drawing, get_ai_availability
    from chat import AIGenerationError, LLMModel
    from run_analysis_watcher import (
        PIPELINE_RUN_DEADLINE_SEC,
        PipelineStageError,
        cleanup_orphaned_temp_files,
        ensure_localized_print_pdf,
        generate_run_id,
        run_analysis_pipeline,
        snapshot_drawing_input,
        watch_loop,
        write_live_image_and_mark_handled,
    )
    from translator import (
        get_translation_availability,
        get_translation_config,
        preload_local_nmt_model,
        reset_local_nmt_cache,
        translate_reading,
    )
    AI_AVAILABLE = True
except Exception as e:
    print(f"[monitoring_server] Warning: pipeline import issue: {e}")
    AI_AVAILABLE = False

_pipeline_lock = threading.Lock()
_pipeline_queue = queue.Queue()
_worker_thread = None
_run_statuses = {}  # run_id -> {"run_id", "state", "status", "error_code", "error", "updated_at"}
MAX_RUN_HISTORY = 100
MAX_PIPELINE_QUEUE_SIZE = 20
_pipeline_state = {
    "state": "IDLE",  # "IDLE" | "QUEUED" | "RUNNING" | "ERROR"
    "running": False,
    "active_run_id": None,
    "queue_length": 0,
    "last_run": None,
    "last_status": "idle",
    "last_error_code": None,
    "last_error": None,
}


def _record_run_status_unlocked(rid: str, state: str, status: str, error_code: str = None, error: str = None):
    _run_statuses[rid] = {
        "run_id": rid,
        "state": state,
        "status": status,
        "error_code": error_code,
        "error": error,
        "updated_at": datetime.now().isoformat(),
    }
    while len(_run_statuses) > MAX_RUN_HISTORY:
        oldest_key = next(iter(_run_statuses))
        _run_statuses.pop(oldest_key, None)


_watcher_thread = None
_watcher_stop = threading.Event()
_watcher_lock_fd = None
_ollama_cache = {"status": "unknown", "ts": 0.0}
_ollama_cache_lock = threading.Lock()
_ollama_probe_lock = threading.Lock()
_ollama_runtime_cache = {"data": None, "ts": 0.0, "host": ""}
_ollama_runtime_lock = threading.Lock()
_ollama_tags_cache = {"sizes": {}, "ts": 0.0, "host": ""}
_ollama_tags_lock = threading.Lock()
_gpu_stats_cache = {"data": None, "ts": 0.0}
_gpu_stats_lock = threading.Lock()
_lan_ip_cache = {"ip": "localhost", "ts": 0.0}
_lan_ip_lock = threading.Lock()
_prints_meta_cache = {}  # meta_path_str -> (mtime_ns, size, meta_dict)
_prints_cache_lock = threading.Lock()

MAX_JSON_BODY_BYTES = 1 * 1024 * 1024        # 1 MB for settings/auth/config JSON
MAX_DRAWING_UPLOAD_BYTES = 15 * 1024 * 1024  # 15 MB for drawing uploads
MAX_IMAGE_WIDTH = 4096
MAX_IMAGE_HEIGHT = 4096
MAX_IMAGE_PIXELS = 4096 * 4096
SYNC_PIPELINE_TIMEOUT_SEC = 185.0
REQUEST_SOCKET_TIMEOUT_SEC = 30.0
ADMIN_SESSION_COOKIE = "ls_admin_session"
ADMIN_SESSION_TTL_SEC = 12 * 3600
ADMIN_SESSION_IDLE_TTL_SEC = 30 * 60
MAX_ADMIN_SESSIONS = 128
LOGIN_WINDOW_SEC = 300
LOGIN_MAX_FAILURES = 8
UPLOAD_WINDOW_SEC = 60
UPLOAD_MAX_PER_WINDOW = 4
GLOBAL_UPLOAD_MAX_PER_WINDOW = 20
UPLOAD_TOKEN_TTL_SEC = 900
MAX_UPLOAD_TOKENS = 512
MAX_TRACKED_IPS = 4096

_auth_lock = threading.Lock()
_admin_sessions = {}     # token -> {"abs_exp": float, "idle_exp": float} (or float for legacy callers)
_login_failures = {}     # client_ip -> [monotonic_timestamps]
_upload_timestamps = {}  # client_ip -> [monotonic_timestamps]
_global_upload_timestamps = []  # [monotonic_timestamps]
_upload_tokens = {}      # token -> expiry_monotonic
_active_client_ips = {}  # run_id -> client_ip
_queued_run_ids = []     # ordered list of queued run_ids

ALLOWED_AI_CONFIG_KEYS = {
    "name", "description", "purpose", "expertise", "capabilities",
    "restrictions", "additional", "llm", "model", "temperature", "max_words",
    "thinking_mode", "max_tokens",
    "provider", "base_url", "api_key", "fallback_provider",
    "fallback_model", "vision_mode", "ollama_vision_model",
}


def _session_expiries(entry) -> tuple:
    if isinstance(entry, dict):
        abs_exp = float(entry.get("abs_exp", 0.0))
        idle_exp = float(entry.get("idle_exp", abs_exp))
        return abs_exp, idle_exp
    val = float(entry or 0.0)
    return val, val


def _prune_sessions_unlocked(now: float):
    expired = []
    for tok, entry in _admin_sessions.items():
        abs_exp, idle_exp = _session_expiries(entry)
        if abs_exp <= now or idle_exp <= now:
            expired.append(tok)
    for tok in expired:
        _admin_sessions.pop(tok, None)
    if len(_admin_sessions) > MAX_ADMIN_SESSIONS:
        oldest = sorted(_admin_sessions.items(), key=lambda kv: _session_expiries(kv[1])[1])
        for tok, _ in oldest[: len(_admin_sessions) - MAX_ADMIN_SESSIONS]:
            _admin_sessions.pop(tok, None)


def _create_admin_session() -> str:
    now = time.monotonic()
    token = secrets.token_urlsafe(32)
    with _auth_lock:
        _prune_sessions_unlocked(now)
        _admin_sessions[token] = {
            "abs_exp": now + ADMIN_SESSION_TTL_SEC,
            "idle_exp": now + ADMIN_SESSION_IDLE_TTL_SEC,
        }
    return token


def _validate_admin_session(token: str, touch: bool = True) -> bool:
    if not token:
        return False
    now = time.monotonic()
    with _auth_lock:
        _prune_sessions_unlocked(now)
        entry = _admin_sessions.get(token)
        if entry is None:
            return False
        abs_exp, idle_exp = _session_expiries(entry)
        if abs_exp <= now or idle_exp <= now:
            _admin_sessions.pop(token, None)
            return False
        if touch:
            new_idle = min(abs_exp, now + ADMIN_SESSION_IDLE_TTL_SEC)
            if isinstance(entry, dict):
                entry["idle_exp"] = new_idle
            else:
                _admin_sessions[token] = {"abs_exp": abs_exp, "idle_exp": new_idle}
        return True


def _revoke_admin_session(token: str):
    if token:
        with _auth_lock:
            _admin_sessions.pop(token, None)


def _create_upload_token() -> str:
    now = time.monotonic()
    tok = secrets.token_urlsafe(24)
    with _auth_lock:
        expired = [k for k, exp in _upload_tokens.items() if exp <= now]
        for k in expired:
            _upload_tokens.pop(k, None)
        if len(_upload_tokens) >= MAX_UPLOAD_TOKENS:
            oldest = sorted(_upload_tokens.items(), key=lambda kv: kv[1])
            for k, _ in oldest[: len(_upload_tokens) - MAX_UPLOAD_TOKENS + 1]:
                _upload_tokens.pop(k, None)
        _upload_tokens[tok] = now + UPLOAD_TOKEN_TTL_SEC
    return tok


def _validate_upload_token(token: str, consume: bool = False) -> bool:
    if not token:
        return False
    now = time.monotonic()
    with _auth_lock:
        exp = _upload_tokens.get(token)
        if exp is None or exp <= now:
            _upload_tokens.pop(token, None)
            return False
        if consume:
            _upload_tokens.pop(token, None)
        return True


def _prune_ip_bucket_unlocked(bucket: dict, window_sec: float, now: float):
    stale_ips = []
    for ip, stamps in bucket.items():
        fresh = [ts for ts in stamps if (now - ts) < window_sec]
        if fresh:
            bucket[ip] = fresh
        else:
            stale_ips.append(ip)
    for ip in stale_ips:
        bucket.pop(ip, None)
    while len(bucket) > MAX_TRACKED_IPS:
        oldest_ip = min(bucket.items(), key=lambda kv: kv[1][-1])[0]
        bucket.pop(oldest_ip, None)


def _check_login_rate_limit(client_ip: str) -> tuple:
    now = time.monotonic()
    with _auth_lock:
        _prune_ip_bucket_unlocked(_login_failures, LOGIN_WINDOW_SEC, now)
        recent = _login_failures.get(client_ip, [])
        if len(recent) >= LOGIN_MAX_FAILURES:
            retry_after = max(1, int(LOGIN_WINDOW_SEC - (now - recent[0])))
            return False, retry_after
        return True, 0


def _record_login_failure(client_ip: str):
    now = time.monotonic()
    with _auth_lock:
        _prune_ip_bucket_unlocked(_login_failures, LOGIN_WINDOW_SEC, now)
        recent = _login_failures.get(client_ip, [])
        recent.append(now)
        _login_failures[client_ip] = recent


def _clear_login_failures(client_ip: str):
    with _auth_lock:
        _login_failures.pop(client_ip, None)


def _check_and_record_upload_rate_limit(client_ip: str) -> tuple:
    now = time.monotonic()
    with _auth_lock:
        if not _upload_timestamps:
            _global_upload_timestamps.clear()
        _prune_ip_bucket_unlocked(_upload_timestamps, UPLOAD_WINDOW_SEC, now)
        _global_upload_timestamps[:] = [ts for ts in _global_upload_timestamps if (now - ts) < UPLOAD_WINDOW_SEC]
        recent = _upload_timestamps.get(client_ip, [])
        if len(recent) >= UPLOAD_MAX_PER_WINDOW:
            retry_after = max(1, int(UPLOAD_WINDOW_SEC - (now - recent[0])))
            return False, retry_after
        if len(_global_upload_timestamps) >= GLOBAL_UPLOAD_MAX_PER_WINDOW:
            retry_after = max(1, int(UPLOAD_WINDOW_SEC - (now - _global_upload_timestamps[0])))
            return False, retry_after
        recent.append(now)
        _upload_timestamps[client_ip] = recent
        _global_upload_timestamps.append(now)
        return True, 0


def _is_valid_image_bytes(data: bytes) -> bool:
    """Verify uploaded drawing bytes have a valid image header and safe dimensions (anti-decompression bomb)."""
    if not data or len(data) < 12:
        return False
    is_magic_ok = (
        data.startswith(b"\x89PNG\r\n\x1a\n")
        or data.startswith(b"\xff\xd8\xff")
        or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")
        or data.startswith(b"BM")
    )
    if not is_magic_ok:
        return False

    try:
        from PIL import Image
        # Image.open() is lazy and only parses the header, so the dimension check below runs
        # before any pixel decoding. Pillow's process-global MAX_IMAGE_PIXELS is deliberately
        # not mutated here: doing so per request is racy across ThreadingHTTPServer threads.
        with Image.open(io.BytesIO(data)) as im:
            w, h = im.size
            if w <= 0 or h <= 0 or w > MAX_IMAGE_WIDTH or h > MAX_IMAGE_HEIGHT or (w * h) > MAX_IMAGE_PIXELS:
                return False
            im.verify()
        return True
    except ImportError:
        # Minimal fallback header dimension check when Pillow is unavailable
        if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24 and data[12:16] == b"IHDR":
            w, h = struct.unpack(">II", data[16:24])
            return 0 < w <= MAX_IMAGE_WIDTH and 0 < h <= MAX_IMAGE_HEIGHT and (w * h) <= MAX_IMAGE_PIXELS
        return True
    except Exception:
        return False


def _pipeline_worker_loop():
    """Single dedicated FIFO worker thread processing every queued drawing sequentially."""
    while True:
        item = _pipeline_queue.get()
        rid, snap_path, done_event, result_holder, dwell_grid, cancel_event = item
        watchdog = None
        try:
            with _pipeline_lock:
                if rid in _queued_run_ids:
                    _queued_run_ids.remove(rid)
            if cancel_event is not None and cancel_event.is_set():
                if snap_path and Path(snap_path).name.startswith("input_"):
                    try:
                        Path(snap_path).unlink(missing_ok=True)
                    except Exception:
                        pass
                with _pipeline_lock:
                    _pipeline_state["last_run"] = datetime.now().isoformat()
                    _pipeline_state["last_status"] = "error"
                    _pipeline_state["last_error_code"] = "ERR_PIPELINE_TIMEOUT"
                    _pipeline_state["last_error"] = "Queued job cancelled after caller timeout"
                    _record_run_status_unlocked(
                        rid, state="ERROR", status="error",
                        error_code="ERR_PIPELINE_TIMEOUT", error="Cancelled after caller timeout",
                    )
                if result_holder is not None:
                    result_holder["ok"] = False
                    result_holder["run_id"] = rid
                    result_holder["error_code"] = "ERR_PIPELINE_TIMEOUT"
                    result_holder["error"] = "Cancelled after caller timeout"
                continue

            with _pipeline_lock:
                _pipeline_state["state"] = "RUNNING"
                _pipeline_state["running"] = True
                _pipeline_state["active_run_id"] = rid
                _pipeline_state["queue_length"] = _pipeline_queue.qsize()
                _pipeline_state["last_status"] = "running"
                _pipeline_state["last_error_code"] = None
                _pipeline_state["last_error"] = None
                _record_run_status_unlocked(rid, state="RUNNING", status="running")

            eff_cancel = cancel_event if cancel_event is not None else threading.Event()
            run_deadline = time.monotonic() + float(PIPELINE_RUN_DEADLINE_SEC)
            watchdog = threading.Timer(float(PIPELINE_RUN_DEADLINE_SEC), eff_cancel.set)
            watchdog.daemon = True
            watchdog.start()

            pipeline_kwargs = {
                "run_id": rid,
                "snapshot_path": snap_path,
                "raise_on_error": True,
                "dwell_grid": dwell_grid,
            }
            try:
                ok = run_analysis_pipeline(
                    **pipeline_kwargs,
                    cancel_event=eff_cancel,
                    deadline=run_deadline,
                )
            except TypeError:
                if cancel_event is not None:
                    pipeline_kwargs["cancel_event"] = cancel_event
                ok = run_analysis_pipeline(**pipeline_kwargs)
            with _pipeline_lock:
                _pipeline_state["last_run"] = datetime.now().isoformat()
                if not ok:
                    _pipeline_state["last_status"] = "error"
                    _pipeline_state["last_error_code"] = "ERR_PIPELINE_FAILED"
                    _pipeline_state["last_error"] = "Pipeline returned failure"
                    _record_run_status_unlocked(
                        rid, state="ERROR", status="error",
                        error_code="ERR_PIPELINE_FAILED", error="Pipeline returned failure",
                    )
                else:
                    _pipeline_state["last_status"] = "success"
                    _record_run_status_unlocked(rid, state="COMPLETED", status="success")
            if result_holder is not None:
                result_holder["ok"] = bool(ok)
                result_holder["run_id"] = rid
                if not ok:
                    result_holder["error_code"] = "ERR_PIPELINE_FAILED"
                    result_holder["error"] = "Pipeline returned failure"

        except (PipelineStageError, AIGenerationError) as err:
            with _pipeline_lock:
                _pipeline_state["last_status"] = "error"
                _pipeline_state["last_error_code"] = err.code
                _pipeline_state["last_error"] = str(err)
                _pipeline_state["last_run"] = datetime.now().isoformat()
                _record_run_status_unlocked(rid, state="ERROR", status="error", error_code=err.code, error=str(err))
            if result_holder is not None:
                result_holder["ok"] = False
                result_holder["run_id"] = rid
                result_holder["error_code"] = err.code
                result_holder["error"] = str(err)

        except Exception as exc:
            with _pipeline_lock:
                _pipeline_state["last_status"] = "error"
                _pipeline_state["last_error_code"] = "ERR_PIPELINE_FAILED"
                _pipeline_state["last_error"] = str(exc)
                _pipeline_state["last_run"] = datetime.now().isoformat()
                _record_run_status_unlocked(rid, state="ERROR", status="error", error_code="ERR_PIPELINE_FAILED", error=str(exc))
            if result_holder is not None:
                result_holder["ok"] = False
                result_holder["run_id"] = rid
                result_holder["error_code"] = "ERR_PIPELINE_FAILED"
                result_holder["error"] = str(exc)

        finally:
            if watchdog is not None:
                try:
                    watchdog.cancel()
                except Exception:
                    pass
            _pipeline_queue.task_done()
            with _pipeline_lock:
                _active_client_ips.pop(rid, None)
                if rid in _queued_run_ids:
                    _queued_run_ids.remove(rid)
                _pipeline_state["running"] = False
                _pipeline_state["active_run_id"] = None
                qlen = _pipeline_queue.qsize()
                _pipeline_state["queue_length"] = qlen
                if qlen > 0:
                    _pipeline_state["state"] = "QUEUED"
                elif _pipeline_state["last_status"] in ("error", "failed"):
                    _pipeline_state["state"] = "ERROR"
                else:
                    _pipeline_state["state"] = "IDLE"
            if done_event is not None:
                done_event.set()


def _ensure_pipeline_worker():
    global _worker_thread
    with _pipeline_lock:
        if _worker_thread is None or not _worker_thread.is_alive():
            _worker_thread = threading.Thread(target=_pipeline_worker_loop, daemon=True)
            _worker_thread.start()


class QueueFullError(RuntimeError):
    """Raised when the drawing analysis queue has reached MAX_PIPELINE_QUEUE_SIZE."""


def enqueue_pipeline_job(
    source_path: Path = None,
    image_bytes: bytes = None,
    run_id: str = None,
    done_event: threading.Event = None,
    result_holder: dict = None,
    dwell_grid=None,
    cancel_event: threading.Event = None,
    client_ip: str = None,
) -> tuple:
    """Snapshot drawing input to input_{run_id}.png and enqueue into bounded FIFO worker queue."""
    if not AI_AVAILABLE:
        raise RuntimeError("Analysis pipeline is unavailable")
    _ensure_pipeline_worker()
    rid = run_id or generate_run_id()

    with _pipeline_lock:
        if _pipeline_queue.qsize() >= MAX_PIPELINE_QUEUE_SIZE:
            raise QueueFullError("Analysis queue is full. Please try again in a moment.")
        if client_ip and client_ip in _active_client_ips.values():
            raise QueueFullError("A drawing from your device is already queued or being analyzed.")

        if image_bytes is not None:
            snap = config_manager.SHARED_DIR / f"input_{rid}.png"
            atomic_write_bytes(snap, image_bytes)
        else:
            _, snap = snapshot_drawing_input(source_path or (config_manager.SHARED_DIR / "image.png"), run_id=rid)

        if client_ip:
            _active_client_ips[rid] = client_ip
        if rid not in _queued_run_ids:
            _queued_run_ids.append(rid)
        _record_run_status_unlocked(rid, state="QUEUED", status="queued")
        _pipeline_queue.put((rid, str(snap), done_event, result_holder, dwell_grid, cancel_event))
        qlen = _pipeline_queue.qsize()
        _pipeline_state["queue_length"] = qlen
        if not _pipeline_state["running"]:
            _pipeline_state["state"] = "QUEUED"
    return rid, qlen


def start_background_watcher():
    """Start the file watcher in a daemon thread if not already running externally."""
    global _watcher_thread, _watcher_lock_fd
    _ensure_pipeline_worker()
    if _watcher_thread is not None and _watcher_thread.is_alive():
        return
    if get_service_status("run_analysis_watcher") == "running":
        return
    if _watcher_lock_fd is None:
        _watcher_lock_fd = try_acquire_nonblocking_lock(config_manager.LOGS_DIR / ".watcher.lock")
        if _watcher_lock_fd is None:
            return
    if AI_AVAILABLE:
        _watcher_stop.clear()
        _watcher_thread = threading.Thread(
            target=watch_loop,
            args=(_watcher_stop, lambda img_p, dwell_grid=None: enqueue_pipeline_job(source_path=img_p, dwell_grid=dwell_grid)),
            daemon=True,
        )
        _watcher_thread.start()


def _snapshot_process_cmdlines() -> list:
    if psutil is None:
        return []
    cmdlines = []
    my_pid = os.getpid()
    try:
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                if proc.info.get("pid") == my_pid:
                    continue
                pname = (proc.info.get("name") or "").lower()
                argv = proc.info.get("cmdline") or []
                cmdlines.append((pname, argv))
            except Exception:
                pass
    except Exception:
        pass
    return cmdlines


def get_service_status(service_name: str, cmdlines: list = None) -> str:
    if service_name == "run_analysis_watcher" and _watcher_thread and _watcher_thread.is_alive():
        return "running (in-process)"
    if psutil is None and cmdlines is None:
        return "ready"
    try:
        entries = cmdlines if cmdlines is not None else _snapshot_process_cmdlines()
        target_script = f"{service_name}.py"
        for pname, argv in entries:
            if service_name == "ollama":
                exe_name = Path(argv[0]).name.lower() if argv else ""
                if pname in ("ollama", "ollama.exe") or exe_name in ("ollama", "ollama.exe"):
                    return "running"
            elif argv and "python" in pname:
                if any(Path(arg).name == target_script for arg in argv[1:] if arg and not arg.startswith("-")):
                    return "running"
        return "stopped"
    except Exception:
        return "unknown"


def _fast_local_ollama_url(host: str) -> str:
    """Replace 'http://localhost:' with 'http://127.0.0.1:' to avoid Windows 2s IPv6 ::1 refusal stalls."""
    return fast_local_ollama_url(host)


def _get_memory_percent_stdlib():
    """Return host RAM usage percent without requiring psutil (Windows ctypes / Linux /proc/meminfo)."""
    try:
        if os.name == "nt":
            import ctypes

            class _MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = _MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return float(stat.dwMemoryLoad)
        else:
            meminfo = Path("/proc/meminfo")
            if meminfo.exists():
                vals = {}
                for ln in meminfo.read_text(encoding="utf-8", errors="ignore").splitlines():
                    parts = ln.split(":")
                    if len(parts) == 2:
                        num = parts[1].strip().split()[0]
                        if num.isdigit():
                            vals[parts[0].strip()] = int(num)
                total = vals.get("MemTotal", 0)
                avail = vals.get("MemAvailable", vals.get("MemFree", 0))
                if total > 0:
                    return round(100.0 * (total - avail) / total, 1)
    except Exception:
        pass
    return None


def get_system_stats() -> dict:
    sd = config_manager.SHARED_DIR
    disk_pct = 0.0
    try:
        target = sd if sd.exists() else PROJECT_DIR
        du = shutil.disk_usage(str(target))
        if du.total > 0:
            disk_pct = round(100.0 * du.used / du.total, 1)
    except Exception:
        disk_pct = 0.0

    if psutil is None:
        return {
            "cpu_percent": None,
            "memory_percent": _get_memory_percent_stdlib(),
            "disk_usage": disk_pct,
            "temperature": get_device_temp(),
        }
    try:
        return {
            "cpu_percent": psutil.cpu_percent(interval=None),
            "memory_percent": psutil.virtual_memory().percent,
            "disk_usage": disk_pct,
            "temperature": get_device_temp(),
        }
    except Exception:
        return {"cpu_percent": None, "memory_percent": _get_memory_percent_stdlib(), "disk_usage": disk_pct, "temperature": "N/A"}


def get_device_temp() -> str:
    try:
        thermal_file = Path("/sys/devices/virtual/thermal/thermal_zone0/temp")
        if thermal_file.exists():
            temp = int(thermal_file.read_text()) / 1000.0
            return f"{temp:.1f}°C"
    except Exception:
        pass
    return "N/A"


def get_gpu_stats(force: bool = False) -> dict:
    """Query NVIDIA GPU utilization, VRAM usage, and temperature via nvidia-smi (3s cached)."""
    now = time.monotonic()
    with _gpu_stats_lock:
        if not force and _gpu_stats_cache["data"] is not None and (now - _gpu_stats_cache["ts"]) < 3.0:
            return dict(_gpu_stats_cache["data"])

    result = {"available": False}
    nvidia_smi = shutil.which("nvidia-smi")
    if nvidia_smi:
        try:
            run_kwargs = {
                "capture_output": True,
                "text": True,
                "timeout": 1.5,
            }
            if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
                run_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            proc = subprocess.run(
                [
                    nvidia_smi,
                    "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                    "--format=csv,noheader,nounits",
                ],
                **run_kwargs,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                first_line = proc.stdout.strip().splitlines()[0]
                parts = [p.strip() for p in first_line.split(",")]
                if len(parts) >= 5:
                    name = parts[0]
                    util = float(parts[1]) if parts[1] not in ("", "N/A", "[N/A]") else 0.0
                    used_mb = float(parts[2])
                    total_mb = float(parts[3])
                    temp_c = float(parts[4]) if parts[4] not in ("", "N/A", "[N/A]") else None
                    vram_pct = round((used_mb / total_mb) * 100.0, 1) if total_mb > 0 else 0.0
                    result = {
                        "available": True,
                        "name": name,
                        "utilization_percent": round(util, 1),
                        "vram_used_mb": int(round(used_mb)),
                        "vram_total_mb": int(round(total_mb)),
                        "vram_used_gb": round(used_mb / 1024.0, 2),
                        "vram_total_gb": round(total_mb / 1024.0, 2),
                        "vram_percent": vram_pct,
                        "temperature_c": round(temp_c, 1) if temp_c is not None else None,
                    }
        except Exception:
            result = {"available": False}

    with _gpu_stats_lock:
        _gpu_stats_cache["data"] = dict(result)
        _gpu_stats_cache["ts"] = time.monotonic()
    return result


_ISO_FRAC_RE = re.compile(r"(\.\d{6})\d+")


def _parse_iso_expires_in_sec(expires_raw: str):
    """Parse an Ollama RFC3339/ISO8601 expires_at timestamp into seconds remaining (Python 3.10 safe)."""
    if not expires_raw or not isinstance(expires_raw, str):
        return None
    s = expires_raw.strip()
    if not s or s.startswith("0001-"):
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        s = _ISO_FRAC_RE.sub(r"\1", s)
        dt = datetime.fromisoformat(s)
        if dt.year >= 2200:
            return None  # keep_alive=-1 (pinned indefinitely)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now_utc = datetime.now(timezone.utc)
        return max(0, int((dt - now_utc).total_seconds()))
    except Exception:
        return None


def get_ollama_runtime_status(settings: dict = None, force: bool = False) -> dict:
    """Query Ollama's GET /api/ps endpoint (3s cached) for live loaded models, VRAM/RAM split, and ctx."""
    cfg = settings if isinstance(settings, dict) else load_settings()
    host = _fast_local_ollama_url(get_configured_ollama_host(cfg))
    now = time.monotonic()
    with _ollama_runtime_lock:
        if (
            not force
            and _ollama_runtime_cache["data"] is not None
            and _ollama_runtime_cache["host"] == host
            and (now - _ollama_runtime_cache["ts"]) < 3.0
        ):
            return dict(_ollama_runtime_cache["data"])

    result = {
        "running": False,
        "host": host,
        "loaded_models": [],
        "total_size_gb": 0.0,
        "total_vram_gb": 0.0,
        "total_ram_gb": 0.0,
    }
    if host.startswith(("http://", "https://")):
        try:
            req = urllib.request.Request(f"{host}/api/ps", method="GET")
            with urllib.request.urlopen(req, timeout=0.8) as resp:
                if resp.status == 200:
                    payload = json.loads(resp.read().decode("utf-8"))
                    raw_models = payload.get("models") if isinstance(payload, dict) else []
                    models_out = []
                    total_bytes = 0
                    total_vram_bytes = 0
                    for m in (raw_models or []):
                        if not isinstance(m, dict):
                            continue
                        name = str(m.get("name") or m.get("model") or "").strip()
                        if not name:
                            continue
                        size_b = max(0, int(m.get("size") or 0))
                        vram_b = max(0, int(m.get("size_vram") or 0))
                        ram_b = max(0, size_b - vram_b)
                        gpu_pct = int(round((vram_b / size_b) * 100.0)) if size_b > 0 else 0
                        gpu_pct = max(0, min(100, gpu_pct))
                        cpu_pct = max(0, 100 - gpu_pct) if size_b > 0 else 0
                        details = m.get("details") if isinstance(m.get("details"), dict) else {}
                        exp_raw = str(m.get("expires_at") or "")
                        total_bytes += size_b
                        total_vram_bytes += vram_b
                        models_out.append({
                            "name": name,
                            "size_bytes": size_b,
                            "size_vram_bytes": vram_b,
                            "size_gb": round(size_b / (1024 ** 3), 2),
                            "vram_gb": round(vram_b / (1024 ** 3), 2),
                            "ram_gb": round(ram_b / (1024 ** 3), 2),
                            "gpu_percent": gpu_pct,
                            "cpu_percent": cpu_pct,
                            "context_length": int(m.get("context_length") or 0),
                            "expires_at": exp_raw,
                            "expires_in_sec": _parse_iso_expires_in_sec(exp_raw),
                            "quantization": str(details.get("quantization_level") or ""),
                            "parameter_size": str(details.get("parameter_size") or ""),
                        })
                    total_ram_bytes = max(0, total_bytes - total_vram_bytes)
                    result = {
                        "running": True,
                        "host": host,
                        "loaded_models": models_out,
                        "total_size_gb": round(total_bytes / (1024 ** 3), 2),
                        "total_vram_gb": round(total_vram_bytes / (1024 ** 3), 2),
                        "total_ram_gb": round(total_ram_bytes / (1024 ** 3), 2),
                    }
        except Exception:
            pass

    with _ollama_runtime_lock:
        _ollama_runtime_cache["data"] = dict(result)
        _ollama_runtime_cache["host"] = host
        _ollama_runtime_cache["ts"] = time.monotonic()
    return result


def unload_ollama_model(model_name: str, settings: dict = None) -> dict:
    """Unload a single loaded Ollama model from VRAM/RAM (keep_alive: 0) without stopping Ollama."""
    clean_name = str(model_name or "").strip()
    if not clean_name or not config_manager._OLLAMA_MODEL_NAME_RE.match(clean_name):
        raise ValueError("Invalid Ollama model name")
    cfg = settings if isinstance(settings, dict) else load_settings()
    host = get_configured_ollama_host(cfg).rstrip("/")
    if not host.startswith(("http://", "https://")):
        raise RuntimeError("Invalid Ollama host URL")
    body = json.dumps({"model": clean_name, "keep_alive": 0}).encode("utf-8")
    req = urllib.request.Request(
        f"{host}/api/generate",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=6.0) as resp:
        resp.read()
    with _ollama_runtime_lock:
        _ollama_runtime_cache["data"] = None
        _ollama_runtime_cache["ts"] = 0.0
    return {"status": "success", "model": clean_name, "message": f"Unloaded '{clean_name}' from Ollama memory"}


def get_ollama_model_sizes(settings: dict = None, force: bool = False, ollama_running: bool = None) -> dict:
    """Query Ollama /api/tags (10s cached) for installed model sizes in GB, with local manifest disk fallback."""
    cfg = settings if isinstance(settings, dict) else load_settings()
    host = _fast_local_ollama_url(get_configured_ollama_host(cfg))
    now = time.monotonic()
    with _ollama_tags_lock:
        if (
            not force
            and _ollama_tags_cache["host"] == host
            and (now - _ollama_tags_cache["ts"]) < 10.0
            and _ollama_tags_cache["sizes"]
        ):
            return dict(_ollama_tags_cache["sizes"])

    sizes = {}
    if ollama_running is not False and host.startswith(("http://", "https://")):
        try:
            req = urllib.request.Request(f"{host}/api/tags", method="GET")
            with urllib.request.urlopen(req, timeout=0.6) as resp:
                if resp.status == 200:
                    payload = json.loads(resp.read().decode("utf-8"))
                    for m in (payload.get("models") or []):
                        name = str(m.get("name") or "").strip().lower()
                        size_b = int(m.get("size") or 0)
                        if name and size_b > 0:
                            gb = round(size_b / (1024 ** 3), 2)
                            sizes[name] = gb
                            if name.endswith(":latest"):
                                sizes[name[:-7]] = gb
        except Exception:
            pass

    if not sizes:
        try:
            ollama_root = Path(os.environ.get("OLLAMA_MODELS") or (Path.home() / ".ollama" / "models"))
            manifests_dir = ollama_root / "manifests"
            if manifests_dir.exists():
                for mf in manifests_dir.rglob("*"):
                    if not mf.is_file():
                        continue
                    rel_parts = mf.relative_to(manifests_dir).parts
                    if len(rel_parts) < 3:
                        continue
                    tag = rel_parts[-1]
                    mname = rel_parts[-2]
                    if len(rel_parts) >= 4 and rel_parts[-3] != "library":
                        mname = f"{rel_parts[-3]}/{mname}"
                    full = f"{mname}:{tag}".lower()
                    try:
                        mjson = json.loads(mf.read_text(encoding="utf-8"))
                        total_b = int((mjson.get("config") or {}).get("size") or 0)
                        for layer in (mjson.get("layers") or []):
                            total_b += int(layer.get("size") or 0)
                        if total_b > 0:
                            gb = round(total_b / (1024 ** 3), 2)
                            sizes[full] = gb
                            if full.endswith(":latest"):
                                sizes[full[:-7]] = gb
                    except Exception:
                        pass
        except Exception:
            pass

    with _ollama_tags_lock:
        _ollama_tags_cache["sizes"] = dict(sizes)
        _ollama_tags_cache["host"] = host
        _ollama_tags_cache["ts"] = time.monotonic()
    return sizes


def prewarm_ollama_model(model_name: str = None, settings: dict = None, keep_alive: str = "30m") -> dict:
    """Pre-load an Ollama model into VRAM with zero generated tokens so visitors avoid cold-load delays."""
    cfg = settings if isinstance(settings, dict) else load_settings()
    target = str(model_name or "").strip()
    if not target:
        acfg = (cfg.get("ai") or {}).get("analysis") or {}
        if (acfg.get("provider") or "ollama").lower() == "ollama":
            target = str(acfg.get("model") or "").strip()
    if not target or not config_manager._OLLAMA_MODEL_NAME_RE.match(target):
        raise ValueError("Invalid or unconfigured Ollama model name")
    host = get_configured_ollama_host(cfg).rstrip("/")
    if not host.startswith(("http://", "https://")):
        raise RuntimeError("Invalid Ollama host URL")

    t0 = time.monotonic()
    body = json.dumps({"model": target, "prompt": "", "keep_alive": keep_alive}).encode("utf-8")
    req = urllib.request.Request(
        f"{host}/api/generate",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120.0) as resp:
        resp.read()
    elapsed = round(time.monotonic() - t0, 2)
    with _ollama_runtime_lock:
        _ollama_runtime_cache["data"] = None
        _ollama_runtime_cache["ts"] = 0.0
    rt = get_ollama_runtime_status(cfg, force=True)
    return {
        "status": "success",
        "model": target,
        "elapsed_sec": elapsed,
        "runtime": rt,
        "message": f"Pre-warmed '{target}' in {elapsed}s",
    }


def _match_model_name(a: str, b: str) -> bool:
    al = (a or "").strip().lower()
    bl = (b or "").strip().lower()
    if not al or not bl:
        return False
    return (
        al == bl
        or al == f"{bl}:latest"
        or bl == f"{al}:latest"
    )


def _lookup_model_size_gb(model_name: str, model_sizes: dict) -> float:
    low = (model_name or "").strip().lower()
    if not low or not isinstance(model_sizes, dict):
        return 0.0
    if low in model_sizes:
        return float(model_sizes[low])
    if f"{low}:latest" in model_sizes:
        return float(model_sizes[f"{low}:latest"])
    for k, v in model_sizes.items():
        if ":" not in low and k.startswith(f"{low}:"):
            return float(v)
    return 0.0


def _is_thinking_model_hint(model_name: str) -> bool:
    low = (model_name or "").strip().lower()
    if not low:
        return False
    hints = (
        "gemma4", "qwen3", "deepseek-r1", "qwq", "gpt-oss", "magistral",
        "cogito", "openthinker", "phi4-reasoning", "exaone-deep", "marco-o1",
    )
    return any(h in low for h in hints)


def _is_vision_model_hint(model_name: str) -> bool:
    low = (model_name or "").strip().lower()
    if not low or low.startswith("gemma3:1b"):
        return False
    hints = (
        "vision", "llava", "moondream", "bakllava", "minicpm-v",
        "qwen2-vl", "qwen2.5-vl", "qwen2.5vl", "qwen3-vl", "qwen3vl", "qwen3.5",
        "pixtral", "internvl", "mistral-small3.1", "mistral-small3.2",
        "gemma3", "gemma4", "llama4", "llama-4",
    )
    return any(h in low for h in hints)


def compute_smart_diagnostics(
    settings: dict = None,
    gpu_stats: dict = None,
    ollama_runtime: dict = None,
    model_sizes: dict = None,
) -> dict:
    """
    Smart pre-flight & live runtime advisor detecting common local-GPU exhibition pitfalls:
      - Ollama offline while configured for active stages
      - Multi-model VRAM thrashing / model swaps per drawing (e.g. gemma4:e4b + llama3.1:8b on 8 GB VRAM)
      - Active CPU/RAM offload spill reported by Ollama /api/ps
      - Tight single-model VRAM margin (>78% of GPU VRAM)
      - Unbounded thinking on boolean/thinking models (e.g. gemma4:e4b with max_tokens=0)
      - Primary Ollama model currently cold (not loaded in /api/ps)
    Returns structured diagnostics with actionable 1-click UI fix descriptors.
    """
    cfg = settings if isinstance(settings, dict) else load_settings()
    gpu = gpu_stats if isinstance(gpu_stats, dict) else get_gpu_stats()
    ol_rt = ollama_runtime if isinstance(ollama_runtime, dict) else get_ollama_runtime_status(cfg)
    sizes = model_sizes if isinstance(model_sizes, dict) else get_ollama_model_sizes(cfg)

    ai_cfg = cfg.get("ai") or {}
    analysis_cfg = ai_cfg.get("analysis") or {}
    poem_cfg = ai_cfg.get("poem") or {}
    tr_cfg = cfg.get("translation") or {}

    vision_mode = str(ai_cfg.get("vision_mode") or "auto").strip().lower()
    ollama_vision_model = str(ai_cfg.get("ollama_vision_model") or "moondream").strip()
    a_prov = str(analysis_cfg.get("provider") or "ollama").strip().lower()
    a_mod = str(analysis_cfg.get("model") or "").strip()
    p_prov = str(poem_cfg.get("provider") or "ollama").strip().lower()
    p_mod = str(poem_cfg.get("model") or "").strip()
    tr_enabled = bool(tr_cfg.get("enabled", True))
    tr_mode = str(tr_cfg.get("mode") or "local_nmt").strip().lower()
    tr_ollama_mod = str(tr_cfg.get("ollama_model") or "translategemma:4b").strip()

    # Determine ordered per-drawing Ollama stage sequence to accurately count model swaps
    ollama_stage_seq = []
    analysis_handles_vision = (
        a_prov in ("gemini", "anthropic")
        or (a_prov == "ollama" and _is_vision_model_hint(a_mod))
        or (a_prov == "openai_compatible" and _is_vision_model_hint(a_mod))
    )
    if vision_mode == "ollama_vision" or (vision_mode == "auto" and not analysis_handles_vision and ollama_vision_model):
        if ollama_vision_model:
            ollama_stage_seq.append(("vision", ollama_vision_model))
    if a_prov == "ollama" and a_mod:
        ollama_stage_seq.append(("analysis", a_mod))
    if p_prov == "ollama" and p_mod:
        ollama_stage_seq.append(("poem", p_mod))
    if tr_enabled and tr_mode == "ollama" and tr_ollama_mod:
        ollama_stage_seq.append(("translation", tr_ollama_mod))
    elif tr_enabled and tr_mode == "native_llm":
        if a_prov == "ollama" and a_mod:
            ollama_stage_seq.append(("translation_analysis", a_mod))
        if p_prov == "ollama" and p_mod:
            ollama_stage_seq.append(("translation_poem", p_mod))

    distinct_models = []
    for _, mname in ollama_stage_seq:
        if not any(_match_model_name(mname, ex) for ex in distinct_models):
            distinct_models.append(mname)

    swaps_per_drawing = 0
    for idx in range(1, len(ollama_stage_seq)):
        if not _match_model_name(ollama_stage_seq[idx - 1][1], ollama_stage_seq[idx][1]):
            swaps_per_drawing += 1

    vram_total_gb = float(gpu.get("vram_total_gb") or 0.0) if gpu.get("available") else 0.0
    model_size_map = {m: _lookup_model_size_gb(m, sizes) for m in distinct_models}
    combined_gb = round(sum(model_size_map.values()), 2)
    largest_gb = max(model_size_map.values()) if model_size_map else 0.0
    loaded_models = ol_rt.get("loaded_models") if isinstance(ol_rt.get("loaded_models"), list) else []

    issues = []

    # 1. Ollama offline when required by active stages
    if ollama_stage_seq and not ol_rt.get("running"):
        issues.append({
            "id": "ollama_offline",
            "severity": "critical",
            "title": "Ollama server is offline",
            "detail": f"Active pipeline stages ({', '.join(distinct_models)}) require local Ollama, but the server is not reachable.",
            "recommendation": "Start Ollama via 'python start_everything.py' or 'ollama serve' before accepting visitors.",
            "metrics": {"models": distinct_models},
            "actions": [],
        })

    # 2. Multi-model VRAM swap / thrashing detection
    if len(distinct_models) >= 2:
        exceeds_vram = (
            (vram_total_gb > 0 and combined_gb > 0 and combined_gb > (vram_total_gb * 0.85))
            or (vram_total_gb > 0 and combined_gb == 0 and vram_total_gb <= 12.5)
            or (vram_total_gb == 0 and combined_gb > 7.5)
        )
        if exceeds_vram:
            actions = []
            if a_prov == "ollama" and p_prov == "ollama" and a_mod and not _match_model_name(a_mod, p_mod):
                actions.append({
                    "id": "sync_poem_to_analysis",
                    "label": f"Use {a_mod} for Poem too",
                    "params": {"model": a_mod},
                })
            if tr_enabled and tr_mode == "ollama":
                actions.append({
                    "id": "switch_translation_local_nmt",
                    "label": "Use 0-VRAM CPU Translation (NMT)",
                    "params": {},
                })
            size_desc = ", ".join(
                f"{m} ({model_size_map[m]} GB)" if model_size_map.get(m) else m
                for m in distinct_models
            )
            vram_str = f"{vram_total_gb:.1f} GB GPU VRAM" if vram_total_gb > 0 else "available VRAM"
            issues.append({
                "id": "vram_model_swap",
                "severity": "critical" if swaps_per_drawing >= 2 else "warning",
                "title": f"VRAM model thrashing ({swaps_per_drawing} cold swaps/drawing)",
                "detail": (
                    f"Pipeline loads {size_desc} (total {combined_gb:.1f} GB vs {vram_str}). "
                    f"Ollama will evict and reload models {swaps_per_drawing}× per drawing, adding 20–90s+ of stall time."
                ),
                "recommendation": "Keep one Ollama model shared across Analysis & Poem, and use zero-VRAM CPU NMT (or Native LLM with that same single model) for Hungarian.",
                "metrics": {
                    "distinct_models": distinct_models,
                    "combined_gb": combined_gb,
                    "vram_total_gb": vram_total_gb,
                    "swaps_per_drawing": swaps_per_drawing,
                },
                "actions": actions,
            })

    # 3. Live CPU/RAM offload spill from /api/ps
    spilling = [m for m in loaded_models if int(m.get("cpu_percent") or 0) > 0]
    if spilling:
        top = spilling[0]
        issues.append({
            "id": "vram_cpu_spill_live",
            "severity": "warning",
            "title": f"Active CPU offload on {top.get('name')} ({top.get('gpu_percent')}% GPU / {top.get('cpu_percent')}% CPU)",
            "detail": (
                f"{top.get('ram_gb')} GB of {top.get('name')} spilled into system RAM instead of GPU VRAM, "
                "which significantly slows image encoding and token generation."
            ),
            "recommendation": "Launch via start_everything.py (auto-enables OLLAMA_FLASH_ATTENTION=1 & OLLAMA_KV_CACHE_TYPE=q8_0), unload unused models, or switch to gemma3:4b.",
            "metrics": {
                "model": top.get("name"),
                "gpu_percent": top.get("gpu_percent"),
                "cpu_percent": top.get("cpu_percent"),
                "ram_gb": top.get("ram_gb"),
            },
            "actions": [],
        })
    elif vram_total_gb > 0 and largest_gb > (vram_total_gb * 0.78):
        # 4. Tight single-model VRAM margin
        big_model = max(model_size_map.items(), key=lambda kv: kv[1])[0]
        issues.append({
            "id": "vram_tight_fit",
            "severity": "warning",
            "title": f"Tight VRAM headroom for {big_model} ({largest_gb:.1f} GB on {vram_total_gb:.1f} GB GPU)",
            "detail": (
                f"Model weights ({largest_gb:.1f} GB) leave little room for KV cache and vision projector on a {vram_total_gb:.1f} GB GPU. "
                "Cold loads may take 20–45s and can spill to CPU RAM without Q8 KV cache."
            ),
            "recommendation": "Run via start_everything.py (enables Flash Attention + Q8_0 KV cache + 30m keep-alive) and pre-warm before visitors arrive.",
            "metrics": {"model": big_model, "model_gb": largest_gb, "vram_total_gb": vram_total_gb},
            "actions": (
                [{"id": "prewarm_ollama", "label": f"Pre-warm {big_model}", "params": {"model": big_model}}]
                if ol_rt.get("running")
                else []
            ),
        })

    # 5. Unbounded thinking risk on Ollama thinking models
    unbounded_roles = []
    for role_name, rcfg in (("analysis", analysis_cfg), ("poem", poem_cfg)):
        if (rcfg.get("provider") or "ollama").lower() != "ollama":
            continue
        rmod = str(rcfg.get("model") or "").strip()
        tmode = str(rcfg.get("thinking_mode") or "auto").strip().lower()
        try:
            mtok = int(rcfg.get("max_tokens") or 0)
        except (TypeError, ValueError):
            mtok = 0
        if _is_thinking_model_hint(rmod) and tmode != "off" and mtok == 0:
            unbounded_roles.append((role_name, rmod, tmode))
    if unbounded_roles:
        desc = ", ".join(f"{r} ({m}, thinking={tm})" for r, m, tm in unbounded_roles)
        issues.append({
            "id": "thinking_unbounded",
            "severity": "warning",
            "title": "Uncapped thinking mode on local model",
            "detail": (
                f"{desc} has Max Tokens = 0 (uncapped). Boolean-thinking models like gemma4:e4b can spend "
                "hundreds of tokens reasoning before answering (auto safety cap applies at runtime, or set an explicit cap)."
            ),
            "recommendation": "Cap Max Tokens to 560 for brief thinking, or set Thinking Mode to Off for fastest exhibition responses.",
            "metrics": {"roles": [r for r, _, _ in unbounded_roles]},
            "actions": [
                {"id": "cap_thinking_tokens", "label": "Cap Max Tokens (560)", "params": {"roles": [r for r, _, _ in unbounded_roles], "max_tokens": 560}},
                {"id": "disable_thinking", "label": "Set Thinking Off (Fastest)", "params": {"roles": [r for r, _, _ in unbounded_roles]}},
            ],
        })

    # 6. Cold primary Ollama model (running, but not yet loaded in VRAM)
    if ol_rt.get("running") and a_prov == "ollama" and a_mod:
        is_loaded = any(_match_model_name(m.get("name", ""), a_mod) for m in loaded_models)
        if not is_loaded:
            issues.append({
                "id": "ollama_cold_model",
                "severity": "info",
                "title": f"Primary model '{a_mod}' is cold (not in VRAM)",
                "detail": "Ollama is running with no active instance of your Analysis model. The first visitor will wait an extra 15–45s while it loads from disk.",
                "recommendation": "Pre-warm the model into VRAM now so the first exhibition drawing responds immediately.",
                "metrics": {"model": a_mod},
                "actions": [
                    {"id": "prewarm_ollama", "label": f"Pre-warm {a_mod} Now", "params": {"model": a_mod}},
                ],
            })

    overall = "ok"
    if any(i["severity"] == "critical" for i in issues):
        overall = "critical"
    elif any(i["severity"] == "warning" for i in issues):
        overall = "warning"
    elif issues:
        overall = "info"

    return {
        "status": overall,
        "summary": {
            "distinct_ollama_models": distinct_models,
            "model_sizes_gb": model_size_map,
            "combined_ollama_gb": combined_gb,
            "swaps_per_drawing": swaps_per_drawing,
            "vram_total_gb": vram_total_gb,
        },
        "issues": issues,
    }


def _read_last_stage_timings() -> dict:
    """Read last completed run stage timings from shared_output/content.json if present."""
    content_file = config_manager.SHARED_DIR / "content.json"
    if not content_file.exists():
        return {}
    try:
        data = json.loads(content_file.read_text(encoding="utf-8"))
        timings = data.get("stageTimings")
        if isinstance(timings, dict) and timings:
            out = dict(timings)
            if data.get("runId"):
                out["run_id"] = data["runId"]
            if data.get("analysisProvider"):
                out["analysis_provider"] = data["analysisProvider"]
            if data.get("translationProvider"):
                out["translation_provider"] = data["translationProvider"]
            return out
    except Exception:
        pass
    return {}


def _get_printed_set() -> set:
    print_log = config_manager.DOCS_DIR / "print_history.json"
    if not print_log.exists():
        return set()
    try:
        data = json.loads(print_log.read_text(encoding="utf-8"))
        return set(data.get("printed", []))
    except Exception:
        return set()


def _sync_attributions_to_content_json(attr_data: dict):
    def _apply(current: dict) -> dict:
        current["attributions"] = attr_data
        return current
    update_content_json(_apply)
    # Invalidate cached localized PDFs so subsequent /api/pdf/<run_id>/<lang> renders include the updated attribution footer
    try:
        lang_root = config_manager.DOCS_DIR / "lang"
        if lang_root.exists():
            for cached_pdf in lang_root.rglob("print_*.pdf"):
                try:
                    cached_pdf.unlink(missing_ok=True)
                except OSError:
                    pass
    except Exception:
        pass


def _evict_deleted_prints_and_sync_gallery(deleted_names: list):
    """Evict deleted metadata caches and keep content.json from pointing to removed files."""
    if not deleted_names:
        return
    docs_dir = config_manager.DOCS_DIR
    shared_dir = config_manager.SHARED_DIR
    deleted_set = set(deleted_names)
    deleted_stems = {Path(n).stem for n in deleted_names}
    deleted_rids = {s[len("print_"):] for s in deleted_stems if s.startswith("print_")}

    with _prints_cache_lock:
        for stem in deleted_stems:
            _prints_meta_cache.pop(str(docs_dir / f"{stem}.json"), None)

    def _sync_deleted(current: dict):
        cur_pdf_name = Path(current.get("printPDF") or "").name
        cur_scribble_stem = Path(current.get("scribblePath") or "").stem
        cur_rid = str(current.get("runId") or "")
        if cur_pdf_name not in deleted_set and cur_scribble_stem not in deleted_stems and cur_rid not in deleted_rids:
            return None

        settings = load_settings()
        candidates = []
        if docs_dir.exists():
            for pdf in docs_dir.glob("print_*.pdf"):
                try:
                    candidates.append((pdf.stat().st_mtime, pdf))
                except Exception:
                    continue
        candidates.sort(key=lambda item: (item[0], item[1].name), reverse=True)

        for _, pdf in candidates:
            meta = _read_cached_meta(pdf.with_suffix(".json"))
            if not meta:
                continue
            payload = dict(meta)
            payload["status"] = "ok"
            payload["pdfRendering"] = False
            payload["attributions"] = settings.get("attributions", {})
            for rel_key in ("scribblePath", "heatmapPath", "poemAudio", "printPDF"):
                rel_val = payload.get(rel_key) or ""
                if rel_val and not (shared_dir / rel_val.lstrip("/")).exists():
                    payload[rel_key] = ""
            return payload

        return {
            "status": "empty",
            "runId": "",
            "scribblePath": "",
            "heatmapPath": "",
            "psychoanalysis": "",
            "poem": "",
            "poemAudio": "",
            "printPDF": "",
            "pdfRendering": False,
            "attributions": settings.get("attributions", {}),
            "timestamp": datetime.now().isoformat(),
        }

    update_content_json(_sync_deleted)


# --- Core API Business Logic ---

def _get_lan_ip() -> str:
    now = time.monotonic()
    with _lan_ip_lock:
        if (now - _lan_ip_cache["ts"]) < 30.0:
            return _lan_ip_cache["ip"]
    ip = get_lan_ip()
    with _lan_ip_lock:
        _lan_ip_cache["ip"] = ip
        _lan_ip_cache["ts"] = time.monotonic()
    return ip


def _check_ollama_status(cmdlines: list = None, force: bool = False) -> str:
    now = time.monotonic()
    with _ollama_cache_lock:
        if not force and (now - _ollama_cache["ts"]) < 3.0 and _ollama_cache["status"] != "unknown":
            return _ollama_cache["status"]

    with _ollama_probe_lock:
        now = time.monotonic()
        with _ollama_cache_lock:
            if not force and (now - _ollama_cache["ts"]) < 3.0 and _ollama_cache["status"] != "unknown":
                return _ollama_cache["status"]

        status = "stopped"
        try:
            from start_everything import _is_ollama_running
            if _is_ollama_running(get_configured_ollama_host()):
                status = "running"
        except Exception:
            pass
        if status == "stopped":
            proc_status = get_service_status("ollama", cmdlines=cmdlines)
            if proc_status == "running":
                status = "running"

        with _ollama_cache_lock:
            _ollama_cache["status"] = status
            _ollama_cache["ts"] = time.monotonic()
        return status


def api_get_status() -> dict:
    settings = load_settings()
    cmdlines = _snapshot_process_cmdlines()
    ollama_runtime = get_ollama_runtime_status(settings)
    if ollama_runtime.get("running"):
        ollama_state = "running"
    else:
        proc_ol = get_service_status("ollama", cmdlines=cmdlines)
        ollama_state = "running" if proc_ol == "running" else "stopped"
    gaze_state = get_service_status("run_gaze_painter", cmdlines=cmdlines)
    watcher_state = get_service_status("run_analysis_watcher", cmdlines=cmdlines)
    services = {
        "web_server": "running",
        "analysis_watcher": watcher_state,
        "ollama_server": ollama_state,
        "desktop_gaze_painter": gaze_state,
        "browser_gaze_painter": "ready (/draw)",
        "web_print_kiosk": "ready (/kiosk)",
    }

    try:
        all_pdfs = list(config_manager.DOCS_DIR.glob("print_*.pdf"))
        today_date = datetime.now().date()
        today_files = sum(1 for f in all_pdfs if _resolve_print_datetime(f).date() == today_date)
        printed_count = len(_get_printed_set())
    except Exception:
        all_pdfs = []
        today_files = 0
        printed_count = 0

    host_name = os.environ.get("COMPUTERNAME") or (os.uname().nodename if hasattr(os, "uname") else "Local Host")
    port = get_server_port(settings)
    lan_ip = _get_lan_ip()
    sys_stats = get_system_stats()
    gpu_stats = get_gpu_stats()
    if sys_stats.get("temperature") == "N/A" and gpu_stats.get("available") and gpu_stats.get("temperature_c") is not None:
        sys_stats["temperature"] = f"{gpu_stats['temperature_c']:.0f}°C (GPU)"
    model_sizes = get_ollama_model_sizes(settings, ollama_running=bool(ollama_runtime.get("running")))
    diagnostics = compute_smart_diagnostics(
        settings=settings,
        gpu_stats=gpu_stats,
        ollama_runtime=ollama_runtime,
        model_sizes=model_sizes,
    )
    with _pipeline_lock:
        pipe_snapshot = dict(_pipeline_state)
        pipe_snapshot["queue_length"] = _pipeline_queue.qsize()

    tr_cfg = settings.get("translation", {})
    tr_summary = {
        "enabled": bool(tr_cfg.get("enabled", True)),
        "mode": tr_cfg.get("mode", "local_nmt"),
        "local_nmt_model": tr_cfg.get("local_nmt_model", config_manager.DEFAULT_LOCAL_NMT_MODEL),
        "ollama_model": tr_cfg.get("ollama_model", "translategemma:4b"),
        "default_print_language": tr_cfg.get("default_print_language", "en"),
        "target_languages": tr_cfg.get("target_languages", ["hu"]),
    }

    return {
        "device": host_name,
        "lan_url": f"http://{lan_ip}:{port}",
        "https_url": f"https://{lan_ip}:{_active_https_port}" if _active_https_port else "",
        "https_port": _active_https_port,
        "public_url": get_public_base_url(settings, lan_ip=lan_ip),
        "services": services,
        "pipeline": pipe_snapshot,
        "system": sys_stats,
        "gpu": gpu_stats,
        "ollama_runtime": ollama_runtime,
        "diagnostics": diagnostics,
        "last_timings": _read_last_stage_timings(),
        "ai_summary": {
            "vision_mode": settings.get("ai", {}).get("vision_mode", "auto"),
            "ollama_vision_model": settings.get("ai", {}).get("ollama_vision_model", "moondream"),
            "analysis_provider": settings.get("ai", {}).get("analysis", {}).get("provider", "ollama"),
            "analysis_model": settings.get("ai", {}).get("analysis", {}).get("model", ""),
            "poem_provider": settings.get("ai", {}).get("poem", {}).get("provider", "ollama"),
            "poem_model": settings.get("ai", {}).get("poem", {}).get("model", ""),
        },
        "translation": tr_summary,
        "tts": settings.get("tts", {}),
        "printing": settings.get("printing", {}),
        "attributions": {
            "enabled": settings.get("attributions", {}).get("enabled", False),
            "show_logo": settings.get("attributions", {}).get("show_logo", True),
            "veszprem_vibe_enabled": settings.get("attributions", {}).get("veszprem_vibe", {}).get("enabled", False),
        },
        "statistics": {
            "prints_today": today_files,
            "total_prints": len(all_pdfs),
            "printed_count": printed_count,
        },
        "timestamp": datetime.now().isoformat(),
    }


def _read_cached_meta(meta_file: Path) -> dict:
    try:
        st = meta_file.stat()
    except FileNotFoundError:
        return {}
    except Exception:
        return {}
    key = str(meta_file)
    sig = (st.st_mtime_ns, st.st_size)
    with _prints_cache_lock:
        cached = _prints_meta_cache.get(key)
        if cached and cached[0] == sig[0] and cached[1] == sig[1]:
            return cached[2]
    try:
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        if not isinstance(meta, dict):
            meta = {}
    except Exception:
        meta = {}
    with _prints_cache_lock:
        _prints_meta_cache[key] = (sig[0], sig[1], meta)
    return meta


def _resolve_print_datetime(pdf_file: Path, mtime: float = None, meta: dict = None) -> datetime:
    """Resolve print creation datetime consistently across status, kiosk list, and delete."""
    parts = pdf_file.stem.split("_")
    if len(parts) >= 3 and len(parts[1]) == 8 and len(parts[2]) == 6:
        try:
            return datetime.strptime(f"{parts[1]}_{parts[2]}", "%Y%m%d_%H%M%S")
        except ValueError:
            pass
    if isinstance(meta, dict) and meta.get("timestamp"):
        try:
            return datetime.fromisoformat(str(meta["timestamp"]).replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            pass
    if mtime is not None:
        return datetime.fromtimestamp(mtime)
    try:
        return datetime.fromtimestamp(pdf_file.stat().st_mtime)
    except Exception:
        return datetime.now()


def prune_expired_prints(retention_days: int = None) -> int:
    """Prune archived print artifacts older than retention_days when retention_days > 0."""
    if retention_days is None:
        try:
            retention_days = int(load_settings().get("printing", {}).get("retention_days", 0) or 0)
        except (TypeError, ValueError):
            retention_days = 0
    if retention_days <= 0:
        return 0
    docs_dir = config_manager.DOCS_DIR
    shared_dir = config_manager.SHARED_DIR
    if not docs_dir.exists():
        return 0
    cutoff = datetime.now() - timedelta(days=int(retention_days))
    deleted_names = []
    for pdf_file in docs_dir.glob("print_*.pdf"):
        try:
            if _resolve_print_datetime(pdf_file) < cutoff:
                stem = pdf_file.stem
                try:
                    pdf_file.unlink()
                except OSError:
                    continue
                deleted_names.append(pdf_file.name)
                for suffix in (".png", ".json", ".heatmap.png", ".page.png"):
                    try:
                        pdf_file.with_name(f"{stem}{suffix}").unlink(missing_ok=True)
                    except OSError:
                        pass
                for lc in config_manager.SUPPORTED_LANGUAGES:
                    try:
                        (docs_dir / "lang" / lc / f"{stem}.pdf").unlink(missing_ok=True)
                    except OSError:
                        pass
                if stem.startswith("print_"):
                    rid = stem[len("print_"):]
                    try:
                        (shared_dir / f"output_{rid}.mp3").unlink(missing_ok=True)
                    except OSError:
                        pass
        except Exception:
            pass
    if deleted_names:
        remove_from_print_history(deleted_names)
        _evict_deleted_prints_and_sync_gallery(deleted_names)
    return len(deleted_names)


def api_list_prints(limit: int = None, offset: int = 0, summary_only: bool = False) -> list:
    docs_dir = config_manager.DOCS_DIR
    if not docs_dir.exists():
        return []
    prune_expired_prints()
    printed_set = _get_printed_set()
    entries = []
    for f in docs_dir.glob("print_*.pdf"):
        try:
            st = f.stat()
            entries.append((st.st_mtime, st.st_size, f))
        except FileNotFoundError:
            continue
        except Exception:
            continue

    entries.sort(key=lambda item: item[0], reverse=True)
    if offset and offset > 0:
        entries = entries[offset:]
    if limit is not None and limit > 0:
        entries = entries[:limit]
    files = []
    today_date = datetime.now().date()
    yesterday_date = today_date - timedelta(days=1)
    for mtime, size_bytes, f in entries:
        thumb_file = f.with_suffix(".png")
        heatmap_file = f.with_name(f"{f.stem}.heatmap.png")
        meta = _read_cached_meta(f.with_suffix(".json"))
        dt = _resolve_print_datetime(f, mtime=mtime, meta=meta)
        run_id = meta.get("runId", f.stem.replace("print_", ""))
        translations = meta.get("translations") if isinstance(meta.get("translations"), dict) else {}
        langs = meta.get("languages")
        if not isinstance(langs, list) or not langs:
            langs = list(translations.keys()) if translations else ["en"]
        entry = {
            "name": f.name,
            "run_id": run_id,
            "pdf_url": f"/shared_output/docs/{f.name}",
            "pdf_urls": {lc: f"/api/pdf/{run_id}/{lc}" for lc in config_manager.SUPPORTED_LANGUAGES},
            "thumb_url": f"/shared_output/docs/{thumb_file.name}" if thumb_file.exists() else "",
            "heatmap_url": f"/shared_output/docs/{heatmap_file.name}" if heatmap_file.exists() else "",
            "size": f"{size_bytes / 1024:.1f} KB",
            "timestamp": mtime,
            "date": dt.strftime("%Y-%m-%d %H:%M"),
            "display_date": dt.strftime("%B %d, %Y"),
            "display_time": dt.strftime("%I:%M %p"),
            "is_today": dt.date() == today_date,
            "is_yesterday": dt.date() == yesterday_date,
            "printed": f.name in printed_set,
            "archetype": meta.get("archetype", ""),
            "fractalDimension": meta.get("fractalDimension"),
            "spatialEntropy": meta.get("spatialEntropy"),
            "poem": meta.get("poem", ""),
            "languages": langs,
            "print_language": meta.get("printLanguage", "en"),
        }
        if not summary_only:
            entry["psychoanalysis"] = meta.get("psychoanalysis", "")
            entry["translations"] = translations
        files.append(entry)
    return files


def api_get_logs(log_type: str) -> str:
    if not log_type or "\x00" in log_type or "/" in log_type or "\\" in log_type or ".." in log_type:
        return "Invalid log name"
    logs_dir = config_manager.LOGS_DIR
    candidates = [
        logs_dir / f"{log_type}.log",
        logs_dir / f"{log_type}.csv",
        ANALYSIS_DIR / f"{log_type}.log",
        ANALYSIS_DIR / f"{log_type}.csv",
    ]
    if log_type == "analysis":
        candidates.insert(0, logs_dir / "installation.log")
        candidates.insert(1, ANALYSIS_DIR / "installation.log")
    for log_file in candidates:
        if log_file.exists():
            try:
                lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines(True)
                return "".join(lines[-200:]) or "(Log file is empty)"
            except Exception as e:
                return str(e)
    return f"No entries yet for '{log_type}'."


class InvalidJSONError(ValueError):
    """Raised when an HTTP request body is malformed."""


class PayloadTooLargeError(ValueError):
    """Raised when request body exceeds the configured limit."""


_active_https_port = 0
_active_ca_cert_path = None


class LockedThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class LockedThreadingHTTPSServer(LockedThreadingHTTPServer):
    """
    Threading HTTPS server that performs the TLS handshake inside each per-connection
    worker thread so aborted self-signed probes from mobile browsers never block accept().
    """

    def __init__(self, server_address, RequestHandlerClass, ssl_context: ssl.SSLContext):
        self.ssl_context = ssl_context
        super().__init__(server_address, RequestHandlerClass)

    def process_request_thread(self, request, client_address):
        try:
            request.settimeout(10.0)
            tls_sock = self.ssl_context.wrap_socket(request, server_side=True)
        except (ssl.SSLError, OSError):
            try:
                request.close()
            except Exception:
                pass
            return
        super().process_request_thread(tls_sock, client_address)


class LatentSpaceRequestHandler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        try:
            self.request.settimeout(REQUEST_SOCKET_TIMEOUT_SEC)
        except Exception:
            pass

    def log_message(self, fmt, *args):
        if len(args) >= 2 and str(args[1]).startswith(("4", "5")):
            super().log_message(fmt, *args)

    def _is_trusted_proxy(self) -> bool:
        return os.environ.get("LATENT_SPACE_TRUST_PROXY") == "1" or any(
            os.environ.get(var) for var in ("RENDER", "SPACE_ID", "RAILWAY_ENVIRONMENT", "FLY_APP_NAME")
        )

    def _is_https_request(self) -> bool:
        if isinstance(getattr(self, "request", None), ssl.SSLSocket) or getattr(getattr(self, "request", None), "_is_tls", False):
            return True
        if self._is_trusted_proxy():
            xfp = (self.headers.get("X-Forwarded-Proto") or "").split(",", 1)[0].strip().lower()
            if xfp == "https":
                return True
        return False

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Permissions-Policy", "camera=(self), microphone=(), geolocation=()")
        if self._is_https_request():
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        if not getattr(self, "_custom_csp_sent", False):
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self' 'unsafe-inline' 'wasm-unsafe-eval'; "
                "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
                "media-src 'self' data: blob:; connect-src 'self'; font-src 'self' data:; "
                "worker-src 'self' blob:; frame-ancestors 'self'",
            )
        super().end_headers()

    def _get_client_ip(self) -> str:
        peer = self.client_address[0] if self.client_address else "unknown"
        if self._is_trusted_proxy():
            if os.environ.get("FLY_APP_NAME"):
                fly_vals = [v.strip() for v in (self.headers.get_all("Fly-Client-IP") or []) if v.strip()]
                if fly_vals:
                    return fly_vals[-1]
            if os.environ.get("LATENT_SPACE_TRUST_CF") == "1":
                cf_vals = [v.strip() for v in (self.headers.get_all("CF-Connecting-IP") or []) if v.strip()]
                if cf_vals:
                    return cf_vals[-1]
            xff_all = self.headers.get_all("X-Forwarded-For") or []
            if xff_all:
                parts = [p.strip() for hdr in xff_all for p in hdr.split(",") if p.strip()]
                if parts:
                    return parts[-1]
        return peer

    def _get_session_cookie_token(self) -> str:
        raw_cookie = self.headers.get("Cookie", "")
        if not raw_cookie:
            return ""
        try:
            cookie = SimpleCookie()
            cookie.load(raw_cookie)
            morsel = cookie.get(ADMIN_SESSION_COOKIE)
            return morsel.value if morsel else ""
        except Exception:
            return ""

    def _check_admin_auth_state(self, touch: bool = True) -> tuple:
        expected_pw, _ = get_or_create_admin_password()
        auth_header = (self.headers.get("Authorization") or "").strip()
        bearer_token = ""
        if auth_header.lower().startswith("bearer "):
            bearer_token = auth_header[7:].strip()
        header_token = (self.headers.get("X-Admin-Token") or "").strip() or bearer_token
        if header_token:
            if _validate_admin_session(header_token, touch=touch):
                return True, 0
            client_ip = self._get_client_ip()
            allowed, retry_after = _check_login_rate_limit(client_ip)
            if not allowed:
                return False, retry_after
            if hmac.compare_digest(header_token, expected_pw):
                _clear_login_failures(client_ip)
                return True, 0
            _record_login_failure(client_ip)
            return False, 0
        cookie_token = self._get_session_cookie_token()
        if cookie_token and _validate_admin_session(cookie_token, touch=touch):
            return True, 0
        return False, 0

    def _is_admin_authenticated(self, touch: bool = True) -> bool:
        ok, _ = self._check_admin_auth_state(touch=touch)
        return ok

    def _require_admin(self, touch: bool = True) -> bool:
        ok, retry_after = self._check_admin_auth_state(touch=touch)
        if ok:
            return True
        if retry_after > 0:
            self._send_json(
                {
                    "status": "error",
                    "error": f"Too many authentication attempts. Retry in {retry_after}s.",
                    "error_code": "ERR_RATE_LIMIT",
                },
                status=429,
                extra_headers=[("Retry-After", str(retry_after))],
            )
            return False
        self._send_json(
            {"status": "error", "error": "Admin authentication required", "error_code": "ERR_UNAUTHORIZED"},
            status=401,
        )
        return False

    def _send_json(self, data, status=200, extra_headers: list = None):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if extra_headers:
                for k, v in extra_headers:
                    self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, ssl.SSLError):
            pass

    def _send_text(self, text: str, status=200, content_type="text/plain; charset=utf-8"):
        body = text.encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, ssl.SSLError):
            pass

    def _send_file(self, file_path: Path, cacheable: bool = False):
        if not file_path.exists() or not file_path.is_file():
            self._send_text("File not found", status=404)
            return
        try:
            stat = file_path.stat()
        except FileNotFoundError:
            self._send_text("File not found", status=404)
            return

        file_size = stat.st_size
        etag = f'W/"{stat.st_mtime_ns}-{file_size}"'
        if cacheable and self.headers.get("If-None-Match") == etag:
            try:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", "public, max-age=300")
                self.end_headers()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            return

        suffix = file_path.suffix.lower()
        if suffix in (".mjs", ".js"):
            ctype = "text/javascript; charset=utf-8"
        elif suffix == ".wasm":
            ctype = "application/wasm"
        elif suffix == ".svg":
            ctype = "image/svg+xml"
        else:
            ctype, _ = mimetypes.guess_type(str(file_path))
            if not ctype:
                if suffix == ".otf":
                    ctype = "font/otf"
                else:
                    ctype = "application/octet-stream"

        range_hdr = (self.headers.get("Range") or "").strip()
        start_byte = 0
        end_byte = max(0, file_size - 1)
        is_partial = False

        if range_hdr.startswith("bytes=") and file_size > 0:
            spec = range_hdr[len("bytes="):].split(",", 1)[0].strip()
            if "-" in spec:
                s_part, e_part = spec.split("-", 1)
                try:
                    if s_part == "":
                        suffix_len = int(e_part)
                        if suffix_len > 0:
                            start_byte = max(0, file_size - suffix_len)
                            end_byte = file_size - 1
                            is_partial = True
                    else:
                        start_byte = int(s_part)
                        end_byte = int(e_part) if e_part else (file_size - 1)
                        end_byte = min(end_byte, file_size - 1)
                        if 0 <= start_byte <= end_byte < file_size:
                            is_partial = True
                        else:
                            self.send_response(416)
                            self.send_header("Content-Range", f"bytes */{file_size}")
                            self.send_header("Content-Length", "0")
                            self.end_headers()
                            return
                except ValueError:
                    is_partial = False

        content_len = (end_byte - start_byte + 1) if (is_partial or file_size > 0) else 0
        try:
            self.send_response(206 if is_partial else 200)
            self.send_header("Content-Type", ctype)
            if suffix == ".svg":
                self._custom_csp_sent = True
                self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; sandbox")
            self.send_header("Accept-Ranges", "bytes")
            if is_partial:
                self.send_header("Content-Range", f"bytes {start_byte}-{end_byte}/{file_size}")
            self.send_header("Content-Length", str(content_len))
            self.send_header("ETag", etag)
            if cacheable:
                self.send_header("Cache-Control", "public, max-age=300")
            else:
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Last-Modified", self.date_time_string(stat.st_mtime))
            self.end_headers()
            if self.command != "HEAD" and content_len > 0:
                with file_path.open("rb") as f:
                    if start_byte > 0:
                        f.seek(start_byte)
                    remaining = content_len
                    while remaining > 0:
                        chunk = f.read(min(65536, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, ssl.SSLError):
            pass

    def _read_chunked_body(self, max_bytes: int) -> bytes:
        chunks = []
        total = 0
        while True:
            size_line = self.rfile.readline(128)
            if not size_line:
                raise InvalidJSONError("Incomplete chunked request body")
            size_token = size_line.split(b";", 1)[0].strip()
            try:
                chunk_size = int(size_token, 16)
            except ValueError as e:
                raise InvalidJSONError("Malformed chunk size header") from e
            if chunk_size < 0:
                raise InvalidJSONError("Negative chunk size")
            if chunk_size == 0:
                while True:
                    trailer = self.rfile.readline(256)
                    if not trailer or trailer in (b"\r\n", b"\n"):
                        break
                break
            total += chunk_size
            if total > max_bytes:
                raise PayloadTooLargeError(f"Request body exceeds {max_bytes} byte limit")
            data = self.rfile.read(chunk_size)
            if len(data) != chunk_size:
                raise InvalidJSONError("Incomplete chunked body payload")
            chunks.append(data)
            crlf = self.rfile.read(2)
            if crlf != b"\r\n":
                raise InvalidJSONError("Malformed chunk terminator")
        return b"".join(chunks)

    def _read_raw_body(self, max_bytes: int = MAX_JSON_BODY_BYTES) -> bytes:
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te:
            return self._read_chunked_body(max_bytes=max_bytes)

        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError as e:
            raise InvalidJSONError("Invalid Content-Length header") from e
        if length < 0:
            raise InvalidJSONError("Negative Content-Length header")
        if length > max_bytes:
            raise PayloadTooLargeError(f"Request body exceeds {max_bytes} byte limit")
        if length == 0:
            return b""
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise InvalidJSONError("Incomplete request body")
        return raw

    def _read_json_body(self, max_bytes: int = MAX_JSON_BODY_BYTES) -> dict:
        raw = self._read_raw_body(max_bytes=max_bytes)
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise InvalidJSONError(f"Malformed JSON body: {e}") from e
        if not isinstance(data, dict):
            raise InvalidJSONError("JSON request body must be an object")
        return data

    def _extract_multipart_file(self, ctype: str, raw_body: bytes, field_name: str = "file") -> bytes:
        header_bytes = f"Content-Type: {ctype}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8")
        msg = BytesParser(policy=email_default_policy).parsebytes(header_bytes + raw_body)
        if msg.is_multipart():
            for part in msg.iter_parts():
                if part.get_param("name", header="content-disposition") == field_name:
                    payload = part.get_payload(decode=True)
                    if payload is not None:
                        return payload
        return b""

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)
        if "\x00" in path:
            return self._send_text("Bad Request", status=400)

        # Static & UI routes
        if path in ("/", "/index.html"):
            return self._send_file(PROJECT_DIR / "index.html")
        if path in ("/admin", "/admin.html"):
            if (
                _active_https_port > 0
                and not self._is_https_request()
                and self._get_client_ip() not in ("127.0.0.1", "::1", "localhost")
            ):
                host_hdr = (self.headers.get("Host") or "").split(":", 1)[0].strip() or _get_lan_ip()
                target_url = f"https://{host_hdr}:{_active_https_port}/admin"
                self.send_response(307)
                self.send_header("Location", target_url)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
            return self._send_file(config_manager.WEB_UI_DIR / "admin.html")
        if path in ("/draw", "/draw.html"):
            return self._send_file(config_manager.WEB_UI_DIR / "draw.html")
        if path in ("/kiosk", "/kiosk.html"):
            return self._send_file(config_manager.WEB_UI_DIR / "kiosk.html")
        if path in ("/i18n.js", "/web_ui/i18n.js"):
            return self._send_file(config_manager.WEB_UI_DIR / "i18n.js")
        if path.lower() in ("/kasboek-logo.jpg", "/assets/kasboek/kasboek-logo.jpg"):
            is_preview = (query.get("preview") or [""])[0] == "1"
            attr_cfg = load_settings().get("attributions", {})
            if not is_preview and not (attr_cfg.get("enabled", False) and attr_cfg.get("show_logo", True)):
                self.send_response(204)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
            kasboek_logo = PROJECT_DIR / "assets" / "kasboek" / "kasboek-logo.jpg"
            if not kasboek_logo.exists():
                kasboek_logo = PROJECT_DIR / "kasboek-logo.jpg"
            return self._send_file(kasboek_logo, cacheable=True)
        if path == "/korolev-medium.otf":
            return self._send_file(PROJECT_DIR / "korolev-medium.otf", cacheable=True)

        if path.lower().startswith("/assets/"):
            rel = path[len("/assets/"):]
            rel_parts = Path(rel).parts
            assets_root = (PROJECT_DIR / "assets").resolve()
            if not rel or any(part.startswith(".") for part in rel_parts):
                return self._send_text("Forbidden", status=403)
            target = (assets_root / rel).resolve()
            if not target.is_relative_to(assets_root):
                return self._send_text("Forbidden", status=403)
            # Enforce Kasboek attribution hiding even on case-insensitive filesystems (Windows/macOS)
            kasboek_dir = (assets_root / "kasboek").resolve()
            if os.path.normcase(str(target)).startswith(os.path.normcase(str(kasboek_dir)) + os.sep):
                is_preview = (query.get("preview") or [""])[0] == "1"
                attr_cfg = load_settings().get("attributions", {})
                if not is_preview and not (attr_cfg.get("enabled", False) and attr_cfg.get("show_logo", True)):
                    self.send_response(204)
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    return
            return self._send_file(target, cacheable=True)

        if path.startswith("/vendor/"):
            rel = path[len("/vendor/"):]
            rel_parts = Path(rel).parts
            vendor_root = (config_manager.WEB_UI_DIR / "vendor").resolve()
            if not rel or any(part.startswith(".") for part in rel_parts):
                return self._send_text("Forbidden", status=403)
            target = (vendor_root / rel).resolve()
            if not target.is_relative_to(vendor_root):
                return self._send_text("Forbidden", status=403)
            return self._send_file(target, cacheable=True)

        if path.startswith("/shared_output/"):
            rel = path[len("/shared_output/"):]
            rel_parts = Path(rel).parts
            if not rel or any(part.startswith(".") for part in rel_parts):
                return self._send_text("Forbidden", status=403)
            leaf_low = rel_parts[-1].lower()
            if leaf_low == "print_history.json" or leaf_low.startswith(("input_", "image_processed_", "heatmap_", "dwell_grid")):
                return self._send_text("Forbidden", status=403)
            shared_root = config_manager.SHARED_DIR.resolve()
            target = (shared_root / rel).resolve()
            if not target.is_relative_to(shared_root):
                return self._send_text("Forbidden", status=403)
            is_archived_doc = (
                len(rel_parts) == 2
                and rel_parts[0] == "docs"
                and rel_parts[1].startswith("print_")
                and rel_parts[1].endswith((".png", ".pdf"))
            )
            return self._send_file(target, cacheable=is_archived_doc)

        if path == "/api/qr.svg":
            target_url = (query.get("url") or [""])[0].strip()
            if not target_url or len(target_url.encode("utf-8")) > 213:
                return self._send_text("Invalid QR url parameter", status=400)
            try:
                svg = qr_svg(target_url, cell_size=3, quiet=4)
                return self._send_text(svg, status=200, content_type="image/svg+xml; charset=utf-8")
            except Exception as e:
                return self._send_text(str(e), status=400)

        # Public auth & gallery metadata routes
        if path == "/api/auth/status":
            return self._send_json({"authenticated": self._is_admin_authenticated(touch=False)})

        if path == "/api/drawings/token":
            return self._send_json({
                "status": "success",
                "upload_token": _create_upload_token(),
                "expires_in": UPLOAD_TOKEN_TTL_SEC,
            })

        if path.startswith("/api/pdf/"):
            raw_tail = path[len("/api/pdf/"):]
            if "\\" in raw_tail:
                return self._send_text("Bad Request", status=400)
            rest = raw_tail.strip("/")
            parts = rest.split("/")
            if len(parts) != 2:
                return self._send_text("Bad Request", status=400)
            req_rid, req_lang = parts[0].strip(), parts[1].strip().lower()
            if req_lang not in config_manager.ALLOWED_LANGUAGES:
                return self._send_text("Unsupported language", status=400)
            if not _SAFE_RUN_ID_RE.match(req_rid):
                if not AI_AVAILABLE:
                    return self._send_text("Bad Request", status=400)
                return self._send_text("Print PDF not found", status=404)
            if not AI_AVAILABLE:
                docs_root = config_manager.DOCS_DIR.resolve()
                fallback_pdf = (docs_root / f"print_{req_rid}.pdf").resolve()
                if not fallback_pdf.is_relative_to(docs_root):
                    return self._send_text("Bad Request", status=400)
                if fallback_pdf.exists():
                    return self._send_file(fallback_pdf, cacheable=True)
                return self._send_text("Not Found", status=404)
            pdf_target = ensure_localized_print_pdf(req_rid, req_lang)
            if pdf_target is None or not Path(pdf_target).exists():
                return self._send_text("Print PDF not found", status=404)
            return self._send_file(Path(pdf_target), cacheable=True)

        if path == "/api/tls/ca.crt":
            ca_file = _active_ca_cert_path
            if not ca_file or not Path(ca_file).is_file():
                candidate = config_manager.CONFIG_DIR / "certs" / "ca.crt"
                ca_file = candidate if candidate.is_file() else None
            if not ca_file:
                return self._send_text("Local CA certificate not generated", status=404)
            try:
                raw_pem = Path(ca_file).read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/x-x509-ca-cert")
                self.send_header("Content-Disposition", 'attachment; filename="latent-space-local-ca.crt"')
                self.send_header("Content-Length", str(len(raw_pem)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(raw_pem)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, ssl.SSLError):
                pass
            return

        if path == "/api/public/info":
            settings = load_settings()
            port = get_server_port(settings)
            lan_ip = _get_lan_ip()
            tr_cfg = settings.get("translation", {})
            req_tok = bool(
                settings.get("server", {}).get("require_upload_token", False)
                or os.environ.get("LATENT_SPACE_REQUIRE_UPLOAD_TOKEN") == "1"
            )
            return self._send_json({
                "lan_url": get_public_base_url(settings, lan_ip=lan_ip),
                "local_lan_url": f"http://{lan_ip}:{port}",
                "https_url": f"https://{lan_ip}:{_active_https_port}" if _active_https_port else "",
                "https_port": _active_https_port,
                "ca_fingerprint": get_ca_cert_fingerprint(_active_ca_cert_path),
                "require_upload_token": req_tok,
                "supported_languages": list(config_manager.SUPPORTED_LANGUAGES),
                "translation_enabled": bool(tr_cfg.get("enabled", True) and tr_cfg.get("mode", "local_nmt") != "off"),
            })

        if path == "/api/pipeline/status":
            req_run_id = (query.get("run_id") or [""])[0]
            is_admin = self._is_admin_authenticated(touch=False)
            with _pipeline_lock:
                snap = dict(_pipeline_state)
                snap["queue_length"] = _pipeline_queue.qsize()
                if req_run_id:
                    run_entry = _run_statuses.get(req_run_id)
                    if run_entry:
                        run_copy = dict(run_entry)
                        if req_run_id in _queued_run_ids:
                            run_copy["queue_position"] = _queued_run_ids.index(req_run_id) + 1
                        elif run_copy.get("state") == "RUNNING":
                            run_copy["queue_position"] = 0
                        snap["run"] = run_copy
                    else:
                        snap["run"] = None
            if not is_admin:
                def _public_err_msg(code):
                    if not code:
                        return None
                    if code == "ERR_DRAWING_BLANK":
                        return "Drawing appears blank or has too few strokes."
                    if code == "ERR_PIPELINE_TIMEOUT":
                        return "Analysis timed out."
                    return "Pipeline stage failed."
                if snap.get("last_error"):
                    snap["last_error"] = _public_err_msg(snap.get("last_error_code"))
                if isinstance(snap.get("run"), dict) and snap["run"].get("error"):
                    snap["run"]["error"] = _public_err_msg(snap["run"].get("error_code"))
            return self._send_json(snap)

        if path == "/api/settings/attributions":
            return self._send_json(load_settings().get("attributions", {}))

        if path == "/api/settings/printing":
            printing_cfg = load_settings().get("printing", {})
            if self._is_admin_authenticated(touch=False):
                return self._send_json(printing_cfg)
            return self._send_json({
                "auto_print": bool(printing_cfg.get("auto_print", False)),
                "paper_format": printing_cfg.get("paper_format", "A4"),
            })

        if path == "/api/files/prints":
            try:
                limit = int((query.get("limit") or ["0"])[0])
            except (TypeError, ValueError):
                limit = 0
            try:
                offset = max(0, int((query.get("offset") or ["0"])[0]))
            except (TypeError, ValueError):
                offset = 0
            summary_only = (query.get("summary") or [""])[0] == "1"
            prints_list = api_list_prints(
                limit=limit if limit > 0 else None,
                offset=offset,
                summary_only=summary_only,
            )
            top_ts = prints_list[0]["timestamp"] if prints_list else 0
            printed_cnt = sum(1 for p in prints_list if p.get("printed"))
            etag = f'W/"prints-{len(prints_list)}-{printed_cnt}-{top_ts}-{offset}-{limit}-{int(summary_only)}"'
            if self.headers.get("If-None-Match") == etag:
                try:
                    self.send_response(304)
                    self.send_header("ETag", etag)
                    self.end_headers()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, ssl.SSLError):
                    pass
                return
            return self._send_json(prints_list, extra_headers=[("ETag", etag)])

        # Admin-protected GET routes
        if path in ("/api/status", "/api/monitoring"):
            if not self._require_admin(touch=False):
                return
            return self._send_json(api_get_status())

        if path == "/api/settings":
            if not self._require_admin():
                return
            return self._send_json(mask_settings_secrets(load_settings()))

        if path == "/api/settings/tts":
            if not self._require_admin():
                return
            return self._send_json(load_settings().get("tts", {}))

        if path == "/api/settings/translation":
            if not self._require_admin():
                return
            return self._send_json(load_settings().get("translation", {}))

        if path == "/api/ai/translation/status":
            if not self._require_admin():
                return
            if not AI_AVAILABLE:
                return self._send_json({"status": "error", "error": "Translation module unavailable"}, 503)
            return self._send_json(get_translation_availability())

        if path == "/api/ai/availability":
            if not self._require_admin():
                return
            if not AI_AVAILABLE:
                return self._send_json({"status": "error", "error": "Analysis module unavailable"}, 503)
            return self._send_json(get_ai_availability())

        if path == "/api/ai/vision":
            if not self._require_admin():
                return
            ai_cfg = load_settings().get("ai", {})
            return self._send_json({
                "status": "success",
                "vision_mode": ai_cfg.get("vision_mode", "auto"),
                "ollama_vision_model": ai_cfg.get("ollama_vision_model", "moondream"),
                "vision": ai_cfg.get("vision", {}),
            })

        if path == "/api/ai/config":
            if not self._require_admin():
                return
            model_type = (query.get("type") or ["analysis"])[0]
            if model_type not in ("analysis", "poem"):
                return self._send_json({"status": "error", "error": "type must be 'analysis' or 'poem'"}, 400)
            try:
                model = LLMModel(model_type)
                model.initialize()
                return self._send_json({"status": "success", "config": model.get_config_dict()})
            except Exception as e:
                model = LLMModel(model_type)
                return self._send_json({"status": "success", "config": model.get_config_dict(), "warning": str(e)})

        if path.startswith("/api/logs/"):
            if not self._require_admin():
                return
            log_type = path[len("/api/logs/"):]
            return self._send_text(api_get_logs(log_type))

        return self._send_text("Not Found", status=404)

    def do_POST(self):
        try:
            self._handle_post()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, ssl.SSLError):
            pass
        except PayloadTooLargeError as e:
            self._send_json({"status": "error", "error": str(e)}, status=413)
        except QueueFullError as e:
            self._send_json({"status": "error", "error_code": "ERR_QUEUE_FULL", "error": str(e)}, status=429)
        except (InvalidJSONError, ValueError, TypeError) as e:
            self._send_json({"status": "error", "error": str(e)}, status=400)
        except Exception as e:
            print(f"[monitoring_server] Unexpected error on POST {self.path}: {e}")
            err_msg = str(e) if self._is_admin_authenticated(touch=False) else "Internal server error"
            self._send_json({"status": "error", "error": err_msg}, status=500)

    def _handle_post(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)
        if "\x00" in path:
            return self._send_json({"status": "error", "error": "Bad Request"}, status=400)

        if path == "/api/auth/login":
            client_ip = self._get_client_ip()
            allowed, retry_after = _check_login_rate_limit(client_ip)
            if not allowed:
                return self._send_json(
                    {"status": "error", "error": f"Too many login attempts. Retry in {retry_after}s."},
                    status=429,
                    extra_headers=[("Retry-After", str(retry_after))],
                )
            data = self._read_json_body()
            supplied = str(data.get("password") or "")
            expected_pw, _ = get_or_create_admin_password()
            if not supplied or not hmac.compare_digest(supplied, expected_pw):
                _record_login_failure(client_ip)
                return self._send_json({"status": "error", "error": "Invalid admin password"}, status=401)
            _clear_login_failures(client_ip)
            session_tok = _create_admin_session()
            secure_attr = "; Secure" if self._is_https_request() else ""
            cookie_val = (
                f"{ADMIN_SESSION_COOKIE}={session_tok}; HttpOnly; SameSite=Strict; Path=/; "
                f"Max-Age={ADMIN_SESSION_TTL_SEC}{secure_attr}"
            )
            return self._send_json(
                {"status": "success", "authenticated": True},
                status=200,
                extra_headers=[("Set-Cookie", cookie_val)],
            )

        if path == "/api/auth/logout":
            cookie_tok = self._get_session_cookie_token()
            _revoke_admin_session(cookie_tok)
            secure_attr = "; Secure" if self._is_https_request() else ""
            expired_cookie = f"{ADMIN_SESSION_COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0{secure_attr}"
            return self._send_json(
                {"status": "success", "authenticated": False},
                status=200,
                extra_headers=[("Set-Cookie", expired_cookie)],
            )

        if path == "/api/drawings/token":
            return self._send_json({
                "status": "success",
                "upload_token": _create_upload_token(),
                "expires_in": UPLOAD_TOKEN_TTL_SEC,
            })

        if path == "/api/drawings":
            settings = load_settings()
            require_tok = bool(
                settings.get("server", {}).get("require_upload_token", False)
                or os.environ.get("LATENT_SPACE_REQUIRE_UPLOAD_TOKEN") == "1"
            )
            header_upload_tok = (self.headers.get("X-Upload-Token") or "").strip()
            if require_tok and not self._is_admin_authenticated(touch=False):
                if not _validate_upload_token(header_upload_tok, consume=True):
                    return self._send_json(
                        {
                            "status": "error",
                            "error_code": "ERR_UPLOAD_TOKEN_REQUIRED",
                            "error": "Valid upload token required to submit drawings.",
                        },
                        status=401,
                    )

            client_ip = self._get_client_ip()
            allowed, retry_after = _check_and_record_upload_rate_limit(client_ip)
            if not allowed:
                return self._send_json(
                    {"status": "error", "error_code": "ERR_RATE_LIMIT", "error": f"Too many uploads. Retry in {retry_after}s."},
                    status=429,
                    extra_headers=[("Retry-After", str(retry_after))],
                )

            target_path = config_manager.SHARED_DIR / "image.png"
            ctype = self.headers.get("Content-Type", "")
            dwell_grid = None
            if "multipart/form-data" in ctype.lower():
                raw_body = self._read_raw_body(max_bytes=MAX_DRAWING_UPLOAD_BYTES)
                img_bytes = self._extract_multipart_file(ctype, raw_body, "file")
                if not img_bytes:
                    return self._send_json({"status": "error", "error": "Missing file field"}, 400)
                dwell_raw = self._extract_multipart_file(ctype, raw_body, "dwell_grid")
                if dwell_raw:
                    try:
                        dwell_grid = json.loads(dwell_raw.decode("utf-8"))
                    except Exception:
                        dwell_grid = None
            else:
                data = self._read_json_body(max_bytes=MAX_DRAWING_UPLOAD_BYTES)
                img_data = data.get("image", "")
                dwell_grid = data.get("dwell_grid")
                if not img_data or not isinstance(img_data, str):
                    return self._send_json({"status": "error", "error": "No image provided"}, 400)
                if "," in img_data:
                    img_data = img_data.split(",", 1)[1]
                try:
                    img_bytes = base64.b64decode(img_data, validate=True)
                except Exception as dec_err:
                    return self._send_json({"status": "error", "error": f"Invalid base64 image: {dec_err}"}, 400)

            if not _is_valid_image_bytes(img_bytes):
                return self._send_json(
                    {"status": "error", "error": "Uploaded data is not a valid image or exceeds max 4096x4096 bounds"},
                    400,
                )

            if not AI_AVAILABLE:
                atomic_write_bytes(target_path, img_bytes)
                return self._send_json({"status": "error", "error": "Analysis pipeline unavailable"}, 503)

            is_sync = (query.get("sync") or [""])[0] == "1"
            done_event = threading.Event() if is_sync else None
            cancel_event = threading.Event() if is_sync else None
            result_holder = {} if is_sync else None

            try:
                rid, qlen = enqueue_pipeline_job(
                    image_bytes=img_bytes,
                    done_event=done_event,
                    result_holder=result_holder,
                    dwell_grid=dwell_grid,
                    cancel_event=cancel_event,
                    client_ip=client_ip,
                )
            except TypeError:
                rid, qlen = enqueue_pipeline_job(
                    image_bytes=img_bytes,
                    done_event=done_event,
                    result_holder=result_holder,
                    dwell_grid=dwell_grid,
                    cancel_event=cancel_event,
                )
            write_live_image_and_mark_handled(target_path, img_bytes)

            if is_sync:
                if not done_event.wait(timeout=SYNC_PIPELINE_TIMEOUT_SEC):
                    cancel_event.set()
                    return self._send_json(
                        {
                            "status": "error",
                            "run_id": rid,
                            "error_code": "ERR_PIPELINE_TIMEOUT",
                            "error": "Analysis timed out waiting for completion",
                        },
                        status=504,
                    )
                ok = result_holder.get("ok", False)
                return self._send_json(
                    {
                        "status": "success" if ok else "error",
                        "run_id": rid,
                        "pipeline": "completed",
                        "error_code": result_holder.get("error_code"),
                        "error": result_holder.get("error"),
                    },
                    status=200 if ok else 500,
                )

            return self._send_json({
                "status": "success",
                "run_id": rid,
                "queue_length": qlen,
                "message": "Drawing saved and queued for analysis",
                "pipeline_started": True,
            })

        # All remaining POST routes require Admin authentication
        if not self._require_admin():
            return

        if path == "/api/pipeline/run":
            live_img = config_manager.SHARED_DIR / "image.png"
            if not live_img.exists():
                return self._send_json({"status": "error", "error": "No shared_output/image.png found"}, 404)
            rid, qlen = enqueue_pipeline_job(source_path=live_img)
            with _pipeline_lock:
                pipe_snap = dict(_pipeline_state)
            return self._send_json({
                "status": "success",
                "started": True,
                "run_id": rid,
                "queue_length": qlen,
                "pipeline": pipe_snap,
            })

        if path == "/api/settings":
            data = self._read_json_body()
            unknown_sections = set(data.keys()) - set(config_manager.SETTINGS_SECTIONS)
            if unknown_sections:
                raise ValueError(f"Unknown settings sections: {sorted(unknown_sections)}")
            updated = save_settings(data)
            _sync_attributions_to_content_json(updated.get("attributions", {}))
            return self._send_json({"status": "success", "settings": mask_settings_secrets(updated)})

        if path == "/api/settings/attributions":
            data = self._read_json_body()
            updated = update_settings_section("attributions", data)
            attr = updated.get("attributions", {})
            _sync_attributions_to_content_json(attr)
            return self._send_json({"status": "success", "attributions": attr})

        if path == "/api/settings/printing":
            data = self._read_json_body()
            updated = update_settings_section("printing", data)
            return self._send_json({"status": "success", "printing": updated.get("printing", {})})

        if path == "/api/settings/tts":
            data = self._read_json_body()
            updated = update_settings_section("tts", data)
            return self._send_json({"status": "success", "tts": updated.get("tts", {})})

        if path == "/api/settings/translation":
            data = self._read_json_body()
            prev = load_settings().get("translation", {})
            updated = update_settings_section("translation", data)
            new_tr = updated.get("translation", {})
            if AI_AVAILABLE and (
                new_tr.get("local_nmt_model") != prev.get("local_nmt_model")
                or new_tr.get("local_nmt_threads") != prev.get("local_nmt_threads")
            ):
                reset_local_nmt_cache()
            return self._send_json({"status": "success", "translation": new_tr})

        if path == "/api/ai/translation/preload":
            if not AI_AVAILABLE:
                return self._send_json({"status": "error", "error": "Translation module unavailable"}, 503)
            data = self._read_json_body()
            unknown_keys = set(data.keys()) - {"local_nmt_model", "local_nmt_threads"}
            if unknown_keys:
                return self._send_json({"status": "error", "error": f"Unknown preload keys: {sorted(unknown_keys)}"}, 400)
            val_payload = {}
            if "local_nmt_model" in data and data["local_nmt_model"] is not None:
                val_payload["local_nmt_model"] = data["local_nmt_model"]
            if "local_nmt_threads" in data and data["local_nmt_threads"] is not None:
                val_payload["local_nmt_threads"] = data["local_nmt_threads"]
            if val_payload:
                validate_translation_data(val_payload)
            model_id = data.get("local_nmt_model")
            threads = data.get("local_nmt_threads")
            try:
                res = preload_local_nmt_model(model_id=model_id, num_threads=threads)
                return self._send_json(res)
            except ValueError as ve:
                return self._send_json({"status": "error", "error": str(ve)}, 400)
            except Exception as e:
                return self._send_json({"status": "error", "error": str(e)}, 500)

        if path == "/api/ai/translation/test":
            if not AI_AVAILABLE:
                return self._send_json({"status": "error", "error": "Translation module unavailable"}, 503)
            data = self._read_json_body()
            target_lang = str(data.get("target_lang") or "hu").strip().lower()
            if target_lang not in config_manager.ALLOWED_LANGUAGES or target_lang == "en":
                return self._send_json({"status": "error", "error": "target_lang must be 'hu'"}, 400)
            sample_arch = str(data.get("archetype") or "Urchin · Woven Loops")[:120]
            sample_analysis = str(
                data.get("psychoanalysis")
                or "Your gaze traces restless orbits across the canvas, balancing quiet curiosity with an urge to break free."
            )[:1200]
            sample_poem = str(
                data.get("poem")
                or "Restless dark ink drifts,\nQuiet loops bend toward the light,\nStillness holds the edge."
            )[:1200]
            try:
                res = translate_reading(
                    archetype=sample_arch,
                    psychoanalysis=sample_analysis,
                    poem=sample_poem,
                    target_lang=target_lang,
                )
                if not res.get("ok"):
                    return self._send_json({"status": "error", "error": res.get("error") or "Translation disabled"}, 400)
                return self._send_json({
                    "status": "success",
                    "mode": res.get("mode"),
                    "provider": res.get("provider"),
                    "elapsed_sec": res.get("elapsed_sec"),
                    "target_lang": target_lang,
                    "translated": {
                        "archetype": res.get("archetype", ""),
                        "psychoanalysis": res.get("psychoanalysis", ""),
                        "poem": res.get("poem", ""),
                    },
                })
            except Exception as e:
                return self._send_json({"status": "error", "error": str(e)}, 500)

        if path == "/api/ai/vision":
            data = self._read_json_body()
            unknown_top = set(data.keys()) - {"vision_mode", "ollama_vision_model", "vision"}
            if unknown_top:
                return self._send_json(
                    {"status": "error", "error": f"Unknown vision payload keys: {sorted(unknown_top)}"},
                    400,
                )
            payload = {}
            if "vision_mode" in data:
                payload["vision_mode"] = data["vision_mode"]
            if "ollama_vision_model" in data:
                payload["ollama_vision_model"] = data["ollama_vision_model"]
            if "vision" in data:
                if not isinstance(data["vision"], dict):
                    return self._send_json({"status": "error", "error": "vision must be an object"}, 400)
                payload["vision"] = data["vision"]
            if not payload:
                return self._send_json({"status": "error", "error": "No vision fields provided"}, 400)
            updated = update_settings_section("ai", payload)
            ai_out = updated.get("ai", {})
            return self._send_json({
                "status": "success",
                "vision_mode": ai_out.get("vision_mode", "auto"),
                "ollama_vision_model": ai_out.get("ollama_vision_model", "moondream"),
                "vision": ai_out.get("vision", {}),
            })

        if path == "/api/ai/vision/test":
            if not AI_AVAILABLE:
                return self._send_json({"status": "error", "error": "Analysis module unavailable"}, 503)
            settings = load_settings()
            vision_mode = settings.get("ai", {}).get("vision_mode", "auto")
            sample_img = config_manager.SHARED_DIR / "image_processed.png"
            tmp_sample = None
            if not sample_img.exists():
                docs = sorted(
                    (p for p in config_manager.DOCS_DIR.glob("print_*.png") if not p.name.endswith(".heatmap.png")),
                    key=lambda p: p.stat().st_mtime if p.exists() else 0,
                    reverse=True,
                )
                if docs:
                    sample_img = docs[0]
            if not sample_img.exists():
                import cv2
                import numpy as np
                tmp_sample = config_manager.SHARED_DIR / f".tmp_vision_test_{os.getpid()}_{threading.get_ident()}.png"
                canvas = np.ones((256, 256), dtype=np.uint8) * 255
                cv2.ellipse(canvas, (128, 128), (75, 45), 15, 0, 360, 0, 4)
                cv2.line(canvas, (45, 190), (210, 65), 0, 4)
                cv2.imwrite(str(tmp_sample), canvas)
                sample_img = tmp_sample
            try:
                res = analyze_drawing(str(sample_img), mode=vision_mode)
                return self._send_json({
                    "status": "success",
                    "sample_image": sample_img.name,
                    "requested_mode": res.get("requested_mode", vision_mode),
                    "effective_mode": res.get("effective_mode", vision_mode),
                    "fallback_reason": res.get("fallback_reason", ""),
                    "archetype": res.get("label", ""),
                    "raw_vision_label": res.get("raw_vision_label", ""),
                    "badge_style": res.get("badge_style", "vision_only"),
                    "elapsed_sec": res.get("elapsed_sec", 0),
                    "vision_output": res.get("vision_output", ""),
                })
            finally:
                if tmp_sample is not None and tmp_sample.exists():
                    try:
                        tmp_sample.unlink()
                    except Exception:
                        pass

        if path == "/api/ai/config":
            data = self._read_json_body()
            model_type = data.get("model_type", "analysis")
            if model_type not in ("analysis", "poem"):
                return self._send_json({"status": "error", "error": "model_type must be 'analysis' or 'poem'"}, 400)
            config_data = data.get("config", {})
            if not isinstance(config_data, dict):
                return self._send_json({"status": "error", "error": "config must be an object"}, 400)
            unknown_keys = set(config_data.keys()) - ALLOWED_AI_CONFIG_KEYS
            if unknown_keys:
                return self._send_json(
                    {"status": "error", "error": f"Unknown AI config keys: {sorted(unknown_keys)}"},
                    400,
                )
            clean_cfg = dict(config_data)
            if "model" in clean_cfg and "llm" not in clean_cfg:
                clean_cfg["llm"] = clean_cfg.pop("model")
            else:
                clean_cfg.pop("model", None)
            if not AI_AVAILABLE:
                return self._send_json({"status": "error", "error": "AI pipeline module unavailable"}, 503)
            model = LLMModel(model_type)
            response = model.update_assistant(**clean_cfg)
            return self._send_json(response)

        if path == "/api/ai/test":
            data = self._read_json_body()
            model_type = data.get("model_type", "analysis")
            if model_type not in ("analysis", "poem"):
                return self._send_json({"status": "error", "error": "model_type must be 'analysis' or 'poem'"}, 400)
            if not AI_AVAILABLE:
                return self._send_json({"status": "error", "error": "AI pipeline module unavailable"}, 503)
            try:
                model = LLMModel(model_type)
                reply = model.chat(
                    "Respond with a single short sentence confirming you are online and ready for Latent Space.",
                    allow_failover=False,
                )
                return self._send_json({
                    "status": "success",
                    "provider": model._provider,
                    "model": model._last_used_model or model._llm,
                    "reply": reply,
                })
            except AIGenerationError as e:
                return self._send_json({"status": "error", "error_code": e.code, "error": str(e)}, 500)

        if path == "/api/ollama/stop":
            from start_everything import stop_ollama_serving
            stop_ollama_serving(kill_external=True)
            with _ollama_cache_lock:
                _ollama_cache["status"] = "stopped"
                _ollama_cache["ts"] = time.monotonic()
            with _ollama_runtime_lock:
                _ollama_runtime_cache["data"] = None
                _ollama_runtime_cache["ts"] = 0.0
            return self._send_json({"status": "success", "message": "Ollama models unloaded and server stopped"})

        if path == "/api/ollama/unload":
            data = self._read_json_body()
            model_name = data.get("model", "")
            try:
                res = unload_ollama_model(model_name)
                return self._send_json(res)
            except ValueError as ve:
                return self._send_json({"status": "error", "error": str(ve)}, 400)
            except Exception as e:
                return self._send_json({"status": "error", "error": str(e)}, 500)

        if path == "/api/ollama/prewarm":
            data = self._read_json_body()
            model_name = data.get("model", "")
            try:
                res = prewarm_ollama_model(model_name=model_name or None)
                return self._send_json(res)
            except ValueError as ve:
                return self._send_json({"status": "error", "error": str(ve)}, 400)
            except Exception as e:
                return self._send_json({"status": "error", "error": str(e)}, 500)

        if path == "/api/files/print":
            data = self._read_json_body()
            filename = data.get("filename", "")
            mark_only = bool(data.get("mark_only", False))
            req_lang = str(data.get("lang") or "").strip().lower()
            if req_lang and req_lang not in config_manager.ALLOWED_LANGUAGES:
                return self._send_json({"status": "error", "error": "Unsupported print language"}, 400)
            if (
                not isinstance(filename, str)
                or not filename.startswith("print_")
                or not filename.endswith(".pdf")
                or "/" in filename or "\\" in filename or ".." in filename
            ):
                return self._send_json({"status": "error", "error": "Invalid filename"}, 400)
            pdf_path = config_manager.DOCS_DIR / filename
            if not pdf_path.exists():
                return self._send_json({"status": "error", "error": "PDF file not found"}, 404)
            if mark_only:
                mark_file_printed(filename, extra_meta={"lang": req_lang} if req_lang else None)
                return self._send_json({"status": "success", "filename": filename, "mode": "browser"})
            target_pdf = pdf_path
            if req_lang and AI_AVAILABLE:
                rid = pdf_path.stem[len("print_"):]
                loc_pdf = ensure_localized_print_pdf(rid, req_lang)
                if loc_pdf and Path(loc_pdf).exists():
                    target_pdf = Path(loc_pdf)
            settings = load_settings()
            raw_printer = data.get("printer_name") or settings.get("printing", {}).get("printer_name", "default")
            printer_name = validate_printer_name(raw_printer)
            res = send_pdf_to_printer(
                str(target_pdf),
                printer_name=printer_name,
                extra_meta={"lang": req_lang} if req_lang else None,
            )
            return self._send_json(res, 200 if res.get("status") == "success" else 500)

        if path == "/api/files/delete":
            data = self._read_json_body()
            action = data.get("action")
            filename = data.get("filename")
            if action not in ("specific", "all"):
                return self._send_json({"status": "error", "error": "action must be 'specific' or 'all'"}, 400)

            deleted_names = []
            docs_dir = config_manager.DOCS_DIR
            shared_dir = config_manager.SHARED_DIR

            def _delete_print_artifacts(pdf_file: Path) -> bool:
                stem = pdf_file.stem
                removed_any = False
                if pdf_file.exists():
                    pdf_file.unlink()
                    removed_any = True
                for suffix in (".png", ".json", ".heatmap.png", ".page.png"):
                    p = pdf_file.with_name(f"{stem}{suffix}")
                    if p.exists():
                        try:
                            p.unlink()
                            removed_any = True
                        except OSError as side_err:
                            print(f"[monitoring_server] Warning: could not delete sidecar {p.name}: {side_err}")
                for lc in config_manager.SUPPORTED_LANGUAGES:
                    loc_pdf = docs_dir / "lang" / lc / f"{stem}.pdf"
                    if loc_pdf.exists():
                        try:
                            loc_pdf.unlink()
                            removed_any = True
                        except OSError as loc_err:
                            print(f"[monitoring_server] Warning: could not delete localized PDF {loc_pdf}: {loc_err}")
                if stem.startswith("print_"):
                    rid = stem[len("print_"):]
                    audio_p = shared_dir / f"output_{rid}.mp3"
                    if audio_p.exists():
                        try:
                            audio_p.unlink()
                        except OSError as aud_err:
                            print(f"[monitoring_server] Warning: could not delete audio {audio_p.name}: {aud_err}")
                return removed_any

            if action == "specific":
                if (
                    not isinstance(filename, str)
                    or not filename.startswith("print_")
                    or not filename.endswith(".pdf")
                    or "/" in filename or "\\" in filename or ".." in filename
                ):
                    return self._send_json({"status": "error", "error": "Invalid print PDF filename"}, 400)
                target_pdf = docs_dir / filename
                if not target_pdf.exists():
                    return self._send_json({"status": "error", "error": "Print PDF not found"}, 404)
                try:
                    if _delete_print_artifacts(target_pdf):
                        deleted_names.append(target_pdf.name)
                except OSError as lock_err:
                    return self._send_json(
                        {"status": "error", "error": f"PDF file is currently in use and could not be deleted: {lock_err}"},
                        status=409,
                    )
            elif action == "all":
                today_date = datetime.now().date()
                for f in docs_dir.glob("print_*.pdf"):
                    try:
                        if _resolve_print_datetime(f).date() < today_date and _delete_print_artifacts(f):
                            deleted_names.append(f.name)
                    except Exception:
                        pass

            if deleted_names:
                remove_from_print_history(deleted_names)
                _evict_deleted_prints_and_sync_gallery(deleted_names)
            return self._send_json({"status": "success", "deleted": len(deleted_names)})

        return self._send_json({"error": "Not Found"}, 404)


def _build_https_ssl_context(cert_path: Path, key_path: Path) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    if hasattr(ssl, "TLSVersion"):
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    return ctx


def run_server(host="0.0.0.0", port=8000, https_port=None):
    global _active_https_port, _active_ca_cert_path
    if AI_AVAILABLE:
        cleanup_orphaned_temp_files()
        try:
            tcfg = get_translation_config()
            if tcfg["enabled"] and tcfg["mode"] == "local_nmt" and tcfg["local_nmt_preload"]:
                def _bg_preload():
                    try:
                        res = preload_local_nmt_model()
                        print(f"[translator] Preloaded CPU NMT '{res['model']}' in {res['elapsed_sec']}s (0 MB VRAM)")
                    except Exception as pre_err:
                        print(f"[translator] Optional preload skipped: {pre_err}")
                threading.Thread(target=_bg_preload, daemon=True, name="ls_nmt_preload").start()
        except Exception:
            pass
        if os.environ.get("LATENT_SPACE_SKIP_PREWARM", "").strip() != "1":
            try:
                boot_cfg = load_settings()
                boot_acfg = (boot_cfg.get("ai") or {}).get("analysis") or {}
                if (boot_acfg.get("provider") or "ollama").lower() == "ollama" and boot_acfg.get("model"):
                    def _bg_ollama_prewarm():
                        time.sleep(2.0)
                        try:
                            rt = get_ollama_runtime_status(boot_cfg, force=True)
                            if not rt.get("running"):
                                return
                            target_m = str(boot_acfg.get("model") or "").strip()
                            if any(_match_model_name(m.get("name", ""), target_m) for m in rt.get("loaded_models") or []):
                                return
                            res = prewarm_ollama_model(model_name=target_m, settings=boot_cfg)
                            print(f"[ollama] Pre-warmed '{res['model']}' into VRAM in {res['elapsed_sec']}s")
                        except Exception as pw_err:
                            print(f"[ollama] Background pre-warm note: {pw_err}")
                    threading.Thread(target=_bg_ollama_prewarm, daemon=True, name="ls_ollama_prewarm").start()
            except Exception:
                pass
    start_background_watcher()
    admin_pw, auth_src = get_or_create_admin_password()
    server = LockedThreadingHTTPServer((host, port), LatentSpaceRequestHandler)

    # Start companion offline LAN HTTPS listener (default :8443) so phones/tablets can use camera eye-tracking
    boot_settings = load_settings()
    if https_port is None:
        resolved_https_port = get_https_port(boot_settings) if get_https_enabled(boot_settings) else 0
    else:
        resolved_https_port = int(https_port or 0)

    https_server = None
    _active_https_port = 0
    _active_ca_cert_path = None
    if resolved_https_port > 0 and resolved_https_port != port:
        cert_path, key_path, ca_path = ensure_lan_tls_cert()
        if cert_path and key_path:
            try:
                ssl_ctx = _build_https_ssl_context(cert_path, key_path)
                https_server = LockedThreadingHTTPSServer((host, resolved_https_port), LatentSpaceRequestHandler, ssl_ctx)
                _active_https_port = resolved_https_port
                _active_ca_cert_path = ca_path
                threading.Thread(
                    target=https_server.serve_forever,
                    daemon=True,
                    name="ls_https_server",
                ).start()
            except Exception as tls_err:
                print(f"[tls] Warning: could not bind HTTPS listener on port {resolved_https_port} ({tls_err}); HTTP :{port} remains active.")
                if https_server is not None:
                    try:
                        https_server.server_close()
                    except Exception:
                        pass
                https_server = None
                _active_https_port = 0

    lan_ip = _get_lan_ip()
    print("=" * 64)
    print("LATENT SPACE UNIFIED SERVER")
    print(f"Project Root:  {PROJECT_DIR}")
    print(f"Shared Output: {config_manager.SHARED_DIR}")
    if auth_src.startswith("env:"):
        print(f"Admin Auth:    Configured via {auth_src}")
    else:
        print(f"Admin Auth:    Passcode '{admin_pw}' (saved in {auth_src})")
    print("=" * 64)
    print(f"  Gallery:            http://localhost:{port}/")
    print(f"  Browser Gaze Draw:  http://localhost:{port}/draw")
    print(f"  Web Print Kiosk:    http://localhost:{port}/kiosk")
    print(f"  Admin Dashboard:    http://localhost:{port}/admin")
    if _active_https_port:
        target_host = lan_ip if lan_ip != "localhost" else "localhost"
        ca_fp = get_ca_cert_fingerprint(_active_ca_cert_path)
        print("-" * 64)
        print(f"  Phone / Tablet HTTPS (Camera Enabled — Offline LAN):")
        print(f"    Gaze Draw:        https://{target_host}:{_active_https_port}/draw")
        print(f"    Print Kiosk:      https://{target_host}:{_active_https_port}/kiosk")
        print(f"    Gallery:          https://{target_host}:{_active_https_port}/")
        print(f"    Admin Dashboard:  https://{target_host}:{_active_https_port}/admin")
        print(f"    Name-Scoped CA:   https://{target_host}:{_active_https_port}/api/tls/ca.crt")
        if ca_fp:
            print(f"    CA SHA-256:       {ca_fp}")
    print("=" * 64)

    def _handle_sigterm(_signum, _frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    try:
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, _handle_sigterm)
    except Exception:
        pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print("\nShutting down server...")
        _watcher_stop.set()
        release_nonblocking_lock(_watcher_lock_fd)
        server.server_close()
        if https_server is not None:
            try:
                https_server.shutdown()
                https_server.server_close()
            except Exception:
                pass
        _active_https_port = 0
        if os.environ.get("LATENT_SPACE_MANAGED") != "1":
            try:
                from start_everything import stop_ollama_serving
                stop_ollama_serving(kill_external=False)
            except Exception:
                pass


if __name__ == "__main__":
    settings = load_settings()
    port = get_server_port(settings)
    host = get_server_host(settings)
    hport = get_https_port(settings) if get_https_enabled(settings) else 0
    run_server(host=host, port=port, https_port=hport)
