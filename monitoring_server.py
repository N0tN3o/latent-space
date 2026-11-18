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
from datetime import datetime, timedelta
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
import secrets
import struct
import sys
import threading
import time
from urllib.parse import parse_qs, unquote, urlparse

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
    get_configured_ollama_host,
    get_lan_ip,
    get_or_create_admin_password,
    get_public_base_url,
    get_server_host,
    get_server_port,
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
    write_content_json,
)
from qr_utils import qr_svg

ensure_directories()

try:
    from chat import AIGenerationError, LLMModel
    from run_analysis_watcher import (
        PipelineStageError,
        cleanup_orphaned_temp_files,
        generate_run_id,
        run_analysis_pipeline,
        snapshot_drawing_input,
        watch_loop,
        write_live_image_and_mark_handled,
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
_lan_ip_cache = {"ip": "localhost", "ts": 0.0}
_lan_ip_lock = threading.Lock()
_prints_meta_cache = {}  # meta_path_str -> (mtime_ns, size, meta_dict)
_prints_cache_lock = threading.Lock()

MAX_JSON_BODY_BYTES = 1 * 1024 * 1024        # 1 MB for settings/auth/config JSON
MAX_DRAWING_UPLOAD_BYTES = 15 * 1024 * 1024  # 15 MB for drawing uploads
MAX_IMAGE_WIDTH = 4096
MAX_IMAGE_HEIGHT = 4096
MAX_IMAGE_PIXELS = 4096 * 4096
SYNC_PIPELINE_TIMEOUT_SEC = 155.0
REQUEST_SOCKET_TIMEOUT_SEC = 30.0
ADMIN_SESSION_COOKIE = "ls_admin_session"
ADMIN_SESSION_TTL_SEC = 12 * 3600
MAX_ADMIN_SESSIONS = 128
LOGIN_WINDOW_SEC = 300
LOGIN_MAX_FAILURES = 8
UPLOAD_WINDOW_SEC = 60
UPLOAD_MAX_PER_WINDOW = 15
MAX_TRACKED_IPS = 4096

_auth_lock = threading.Lock()
_admin_sessions = {}     # token -> expiry_monotonic
_login_failures = {}     # client_ip -> [monotonic_timestamps]
_upload_timestamps = {}  # client_ip -> [monotonic_timestamps]

ALLOWED_AI_CONFIG_KEYS = {
    "name", "description", "purpose", "expertise", "capabilities",
    "restrictions", "additional", "llm", "model", "temperature",
    "provider", "base_url", "api_key", "fallback_provider",
    "fallback_model", "vision_mode", "ollama_vision_model",
}


def _prune_sessions_unlocked(now: float):
    expired = [tok for tok, exp in _admin_sessions.items() if exp <= now]
    for tok in expired:
        _admin_sessions.pop(tok, None)
    if len(_admin_sessions) > MAX_ADMIN_SESSIONS:
        oldest = sorted(_admin_sessions.items(), key=lambda kv: kv[1])
        for tok, _ in oldest[: len(_admin_sessions) - MAX_ADMIN_SESSIONS]:
            _admin_sessions.pop(tok, None)


def _create_admin_session() -> str:
    now = time.monotonic()
    token = secrets.token_urlsafe(32)
    with _auth_lock:
        _prune_sessions_unlocked(now)
        _admin_sessions[token] = now + ADMIN_SESSION_TTL_SEC
    return token


def _validate_admin_session(token: str) -> bool:
    if not token:
        return False
    now = time.monotonic()
    with _auth_lock:
        _prune_sessions_unlocked(now)
        exp = _admin_sessions.get(token)
        if exp is None or exp <= now:
            _admin_sessions.pop(token, None)
            return False
        _admin_sessions[token] = now + ADMIN_SESSION_TTL_SEC
        return True


def _revoke_admin_session(token: str):
    if token:
        with _auth_lock:
            _admin_sessions.pop(token, None)


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
        _prune_ip_bucket_unlocked(_upload_timestamps, UPLOAD_WINDOW_SEC, now)
        recent = _upload_timestamps.get(client_ip, [])
        if len(recent) >= UPLOAD_MAX_PER_WINDOW:
            retry_after = max(1, int(UPLOAD_WINDOW_SEC - (now - recent[0])))
            return False, retry_after
        recent.append(now)
        _upload_timestamps[client_ip] = recent
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
        try:
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

            pipeline_kwargs = {
                "run_id": rid,
                "snapshot_path": snap_path,
                "raise_on_error": True,
                "dwell_grid": dwell_grid,
            }
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
            _pipeline_queue.task_done()
            with _pipeline_lock:
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
) -> tuple:
    """Snapshot drawing input to input_{run_id}.png and enqueue into bounded FIFO worker queue."""
    if not AI_AVAILABLE:
        raise RuntimeError("Analysis pipeline is unavailable")
    _ensure_pipeline_worker()
    rid = run_id or generate_run_id()

    with _pipeline_lock:
        if _pipeline_queue.qsize() >= MAX_PIPELINE_QUEUE_SIZE:
            raise QueueFullError("Analysis queue is full. Please try again in a moment.")

        if image_bytes is not None:
            snap = config_manager.SHARED_DIR / f"input_{rid}.png"
            atomic_write_bytes(snap, image_bytes)
        else:
            _, snap = snapshot_drawing_input(source_path or (config_manager.SHARED_DIR / "image.png"), run_id=rid)

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


def get_system_stats() -> dict:
    if psutil is None:
        return {"cpu_percent": 0, "memory_percent": 0, "disk_usage": 0, "temperature": get_device_temp()}
    try:
        sd = config_manager.SHARED_DIR
        return {
            "cpu_percent": psutil.cpu_percent(interval=None),
            "memory_percent": psutil.virtual_memory().percent,
            "disk_usage": psutil.disk_usage(str(sd)).percent if sd.exists() else 0,
            "temperature": get_device_temp(),
        }
    except Exception:
        return {"cpu_percent": 0, "memory_percent": 0, "disk_usage": 0, "temperature": "N/A"}


def get_device_temp() -> str:
    try:
        thermal_file = Path("/sys/devices/virtual/thermal/thermal_zone0/temp")
        if thermal_file.exists():
            temp = int(thermal_file.read_text()) / 1000.0
            return f"{temp:.1f}°C"
    except Exception:
        pass
    return "N/A"


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
    ollama_state = _check_ollama_status(cmdlines=cmdlines)
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
    with _pipeline_lock:
        pipe_snapshot = dict(_pipeline_state)
        pipe_snapshot["queue_length"] = _pipeline_queue.qsize()

    return {
        "device": host_name,
        "lan_url": f"http://{lan_ip}:{port}",
        "public_url": get_public_base_url(settings, lan_ip=lan_ip),
        "services": services,
        "pipeline": pipe_snapshot,
        "system": sys_stats,
        "ai_summary": {
            "vision_mode": settings.get("ai", {}).get("vision_mode", "auto"),
            "ollama_vision_model": settings.get("ai", {}).get("ollama_vision_model", "moondream"),
            "analysis_provider": settings.get("ai", {}).get("analysis", {}).get("provider", "ollama"),
            "analysis_model": settings.get("ai", {}).get("analysis", {}).get("model", ""),
            "poem_provider": settings.get("ai", {}).get("poem", {}).get("provider", "ollama"),
            "poem_model": settings.get("ai", {}).get("poem", {}).get("model", ""),
        },
        "tts": settings.get("tts", {}),
        "printing": settings.get("printing", {}),
        "attributions": {
            "enabled": settings.get("attributions", {}).get("enabled", False),
            "show_logo": settings.get("attributions", {}).get("show_logo", True),
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


def api_list_prints() -> list:
    docs_dir = config_manager.DOCS_DIR
    if not docs_dir.exists():
        return []
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
    files = []
    today_date = datetime.now().date()
    yesterday_date = today_date - timedelta(days=1)
    for mtime, size_bytes, f in entries:
        thumb_file = f.with_suffix(".png")
        heatmap_file = f.with_name(f"{f.stem}.heatmap.png")
        meta = _read_cached_meta(f.with_suffix(".json"))
        dt = _resolve_print_datetime(f, mtime=mtime, meta=meta)
        files.append({
            "name": f.name,
            "run_id": meta.get("runId", f.stem.replace("print_", "")),
            "pdf_url": f"/shared_output/docs/{f.name}",
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
            "psychoanalysis": meta.get("psychoanalysis", ""),
        })
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


class LockedThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


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

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "same-origin")
        super().end_headers()

    def _get_client_ip(self) -> str:
        peer = self.client_address[0] if self.client_address else "unknown"
        trust_proxy = os.environ.get("LATENT_SPACE_TRUST_PROXY") == "1" or any(
            os.environ.get(var) for var in ("RENDER", "SPACE_ID", "RAILWAY_ENVIRONMENT", "FLY_APP_NAME")
        )
        if trust_proxy:
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

    def _check_admin_auth_state(self) -> tuple:
        expected_pw, _ = get_or_create_admin_password()
        auth_header = (self.headers.get("Authorization") or "").strip()
        bearer_token = ""
        if auth_header.lower().startswith("bearer "):
            bearer_token = auth_header[7:].strip()
        header_token = (self.headers.get("X-Admin-Token") or "").strip() or bearer_token
        if header_token:
            if _validate_admin_session(header_token):
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
        if cookie_token and _validate_admin_session(cookie_token):
            return True, 0
        return False, 0

    def _is_admin_authenticated(self) -> bool:
        ok, _ = self._check_admin_auth_state()
        return ok

    def _require_admin(self) -> bool:
        ok, retry_after = self._check_admin_auth_state()
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
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
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
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
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

        ctype, _ = mimetypes.guess_type(str(file_path))
        if not ctype:
            if file_path.suffix == ".otf":
                ctype = "font/otf"
            elif file_path.suffix == ".svg":
                ctype = "image/svg+xml"
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
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
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
            return self._send_file(config_manager.WEB_UI_DIR / "admin.html")
        if path in ("/draw", "/draw.html"):
            return self._send_file(config_manager.WEB_UI_DIR / "draw.html")
        if path in ("/kiosk", "/kiosk.html"):
            return self._send_file(config_manager.WEB_UI_DIR / "kiosk.html")
        if path == "/kasboek-logo.jpg":
            is_preview = (query.get("preview") or [""])[0] == "1"
            attr_cfg = load_settings().get("attributions", {})
            if not is_preview and not (attr_cfg.get("enabled", False) and attr_cfg.get("show_logo", True)):
                self.send_response(204)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
            return self._send_file(PROJECT_DIR / "kasboek-logo.jpg", cacheable=True)
        if path == "/korolev-medium.otf":
            return self._send_file(PROJECT_DIR / "korolev-medium.otf", cacheable=True)

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
            return self._send_json({"authenticated": self._is_admin_authenticated()})

        if path == "/api/public/info":
            settings = load_settings()
            port = get_server_port(settings)
            lan_ip = _get_lan_ip()
            return self._send_json({
                "lan_url": get_public_base_url(settings, lan_ip=lan_ip),
                "local_lan_url": f"http://{lan_ip}:{port}",
            })

        if path == "/api/pipeline/status":
            req_run_id = (query.get("run_id") or [""])[0]
            with _pipeline_lock:
                snap = dict(_pipeline_state)
                snap["queue_length"] = _pipeline_queue.qsize()
                if req_run_id:
                    snap["run"] = _run_statuses.get(req_run_id)
            return self._send_json(snap)

        if path == "/api/settings/attributions":
            return self._send_json(load_settings().get("attributions", {}))

        if path == "/api/settings/printing":
            printing_cfg = load_settings().get("printing", {})
            if self._is_admin_authenticated():
                return self._send_json(printing_cfg)
            return self._send_json({
                "auto_print": bool(printing_cfg.get("auto_print", False)),
                "paper_format": printing_cfg.get("paper_format", "A4"),
            })

        if path == "/api/files/prints":
            return self._send_json(api_list_prints())

        # Admin-protected GET routes
        if path in ("/api/status", "/api/monitoring"):
            if not self._require_admin():
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
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except PayloadTooLargeError as e:
            self._send_json({"status": "error", "error": str(e)}, status=413)
        except QueueFullError as e:
            self._send_json({"status": "error", "error_code": "ERR_QUEUE_FULL", "error": str(e)}, status=429)
        except (InvalidJSONError, ValueError, TypeError) as e:
            self._send_json({"status": "error", "error": str(e)}, status=400)
        except Exception as e:
            self._send_json({"status": "error", "error": str(e)}, status=500)

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
            cookie_val = (
                f"{ADMIN_SESSION_COOKIE}={session_tok}; HttpOnly; SameSite=Strict; Path=/; "
                f"Max-Age={ADMIN_SESSION_TTL_SEC}"
            )
            return self._send_json(
                {"status": "success", "authenticated": True},
                status=200,
                extra_headers=[("Set-Cookie", cookie_val)],
            )

        if path == "/api/auth/logout":
            cookie_tok = self._get_session_cookie_token()
            _revoke_admin_session(cookie_tok)
            expired_cookie = f"{ADMIN_SESSION_COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"
            return self._send_json(
                {"status": "success", "authenticated": False},
                status=200,
                extra_headers=[("Set-Cookie", expired_cookie)],
            )

        if path == "/api/drawings":
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
            return self._send_json({"status": "success", "message": "Ollama models unloaded and server stopped"})

        if path == "/api/files/print":
            data = self._read_json_body()
            filename = data.get("filename", "")
            mark_only = bool(data.get("mark_only", False))
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
                mark_file_printed(filename)
                return self._send_json({"status": "success", "filename": filename, "mode": "browser"})
            settings = load_settings()
            raw_printer = data.get("printer_name") or settings.get("printing", {}).get("printer_name", "default")
            printer_name = validate_printer_name(raw_printer)
            res = send_pdf_to_printer(str(pdf_path), printer_name=printer_name)
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
                for suffix in (".pdf", ".png", ".json", ".heatmap.png"):
                    p = pdf_file.with_name(f"{stem}{suffix}")
                    if p.exists():
                        p.unlink()
                        removed_any = True
                if stem.startswith("print_"):
                    rid = stem[len("print_"):]
                    audio_p = shared_dir / f"output_{rid}.mp3"
                    if audio_p.exists():
                        audio_p.unlink()
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
                if _delete_print_artifacts(target_pdf):
                    deleted_names.append(target_pdf.name)
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


def run_server(host="0.0.0.0", port=8000):
    if AI_AVAILABLE:
        cleanup_orphaned_temp_files()
    start_background_watcher()
    admin_pw, auth_src = get_or_create_admin_password()
    server = LockedThreadingHTTPServer((host, port), LatentSpaceRequestHandler)
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
    print("=" * 64)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server...")
        _watcher_stop.set()
        release_nonblocking_lock(_watcher_lock_fd)
        server.server_close()
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
    run_server(host=host, port=port)
