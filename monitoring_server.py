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
  - /api/*                    -> Status, AI config, attributions, drawings, printing, logs
"""

import base64
import cgi
import io
import json
import mimetypes
import os
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

try:
    import psutil
except ImportError:
    psutil = None

PROJECT_DIR = Path(__file__).resolve().parent
ANALYSIS_DIR = PROJECT_DIR / "analysis_watcher"
for p in (str(PROJECT_DIR), str(ANALYSIS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from config_manager import (
    SHARED_DIR,
    DOCS_DIR,
    LOGS_DIR,
    WEB_UI_DIR,
    load_settings,
    save_settings,
    update_settings_section,
    ensure_directories,
)

ensure_directories()

try:
    from chat import LLMModel
    from run_analysis_watcher import (
        run_analysis_pipeline,
        send_pdf_to_printer,
        mark_file_printed,
        watch_loop,
    )
    AI_AVAILABLE = True
except Exception as e:
    print(f"[monitoring_server] Warning: pipeline import issue: {e}")
    AI_AVAILABLE = False

_pipeline_lock = threading.Lock()
_pipeline_state = {
    "running": False,
    "last_run": None,
    "last_status": "idle",
    "last_error": None,
}
_watcher_thread = None
_watcher_stop = threading.Event()


def trigger_pipeline_async():
    """Run the analysis pipeline in a background thread if not already running."""
    if _pipeline_state["running"]:
        return False

    def _worker():
        with _pipeline_lock:
            _pipeline_state["running"] = True
            _pipeline_state["last_status"] = "running"
            _pipeline_state["last_error"] = None
            try:
                ok = run_analysis_pipeline()
                _pipeline_state["last_status"] = "success" if ok else "failed"
                _pipeline_state["last_run"] = datetime.now().isoformat()
            except Exception as exc:
                _pipeline_state["last_status"] = "error"
                _pipeline_state["last_error"] = str(exc)
            finally:
                _pipeline_state["running"] = False

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    return True


def start_background_watcher():
    """Start the file watcher in a daemon thread if not already running externally."""
    global _watcher_thread
    if _watcher_thread is not None and _watcher_thread.is_alive():
        return
    if get_service_status("run_analysis_watcher") == "running":
        return
    if AI_AVAILABLE:
        _watcher_stop.clear()
        _watcher_thread = threading.Thread(
            target=watch_loop,
            args=(_watcher_stop, trigger_pipeline_async),
            daemon=True,
        )
        _watcher_thread.start()


def get_service_status(service_name: str) -> str:
    if service_name == "run_analysis_watcher" and _watcher_thread and _watcher_thread.is_alive():
        return "running (in-process)"
    if psutil is None:
        return "ready"
    try:
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                cmdline = " ".join(proc.info.get("cmdline") or [])
                if service_name in cmdline:
                    return "running"
            except Exception:
                pass
        return "stopped"
    except Exception:
        return "unknown"


def get_system_stats() -> dict:
    if psutil is None:
        return {"cpu_percent": 0, "memory_percent": 0, "disk_usage": 0, "temperature": get_device_temp()}
    try:
        return {
            "cpu_percent": psutil.cpu_percent(interval=0.1),
            "memory_percent": psutil.virtual_memory().percent,
            "disk_usage": psutil.disk_usage(str(SHARED_DIR)).percent if SHARED_DIR.exists() else 0,
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
    print_log = DOCS_DIR / "print_history.json"
    if not print_log.exists():
        return set()
    try:
        data = json.loads(print_log.read_text(encoding="utf-8"))
        return set(data.get("printed", []))
    except Exception:
        return set()


def _sync_attributions_to_content_json(attr_data: dict):
    content_file = SHARED_DIR / "content.json"
    if content_file.exists():
        try:
            content = json.loads(content_file.read_text(encoding="utf-8"))
            content["attributions"] = attr_data
            content_file.write_text(json.dumps(content, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass


# --- Core API Business Logic ---

def _get_lan_ip() -> str:
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "localhost"


def _check_ollama_status() -> str:
    try:
        from start_everything import _is_ollama_running
        if _is_ollama_running():
            return "running"
    except Exception:
        pass
    return get_service_status("ollama")


def api_get_status() -> dict:
    settings = load_settings()
    ollama_state = _check_ollama_status()
    gaze_state = get_service_status("run_gaze_painter")
    watcher_state = get_service_status("run_analysis_watcher")
    services = {
        "web_server": "running",
        "analysis_watcher": watcher_state,
        "ollama_server": ollama_state,
        "desktop_gaze_painter": gaze_state,
        "browser_gaze_painter": "ready (/draw)",
        "web_print_kiosk": "ready (/kiosk)",
    }

    try:
        all_pdfs = list(DOCS_DIR.glob("print_*.pdf"))
        today_prefix = f"print_{datetime.now().strftime('%Y%m%d')}"
        today_files = sum(1 for f in all_pdfs if f.name.startswith(today_prefix))
        printed_count = len(_get_printed_set())
    except Exception:
        all_pdfs = []
        today_files = 0
        printed_count = 0

    host_name = os.environ.get("COMPUTERNAME") or (os.uname().nodename if hasattr(os, "uname") else "Local Host")
    port = int(os.environ.get("PORT", settings.get("server", {}).get("port", 8000)))
    lan_ip = _get_lan_ip()
    sys_stats = get_system_stats()
    return {
        "device": host_name,
        "lan_url": f"http://{lan_ip}:{port}",
        "services": services,
        "pipeline": _pipeline_state,
        "system": sys_stats,
        "ai_summary": {
            "vision_mode": settings.get("ai", {}).get("vision_mode", "auto"),
            "analysis_provider": settings.get("ai", {}).get("analysis", {}).get("provider", "ollama"),
            "analysis_model": settings.get("ai", {}).get("analysis", {}).get("model", ""),
            "poem_provider": settings.get("ai", {}).get("poem", {}).get("provider", "ollama"),
            "poem_model": settings.get("ai", {}).get("poem", {}).get("model", ""),
        },
        "printing": settings.get("printing", {}),
        "attributions": {
            "enabled": settings.get("attributions", {}).get("enabled", False),
            "show_logo": settings.get("attributions", {}).get("show_logo", True),
        },
        "statistics": {
            "prints_today": today_files,
            "total_prints": len(all_pdfs),
            "printed_count": printed_count,
            "drawings": today_files,
            "prints": printed_count,
            "cycleTime": _pipeline_state.get("last_status", "idle"),
            "apiSuccess": "100%",
        },
        "jetson_a": {
            "status": "online" if "running" in gaze_state else "warning",
            "metrics": {
                "service": gaze_state,
                "network": lan_ip,
                "camera": "Ready",
                "display": "Ready (/draw)",
                "CPU": f"{sys_stats.get('cpu_percent', 0)}%",
            },
            "memory": int(sys_stats.get("memory_percent", 0)),
        },
        "jetson_b": {
            "status": "online",
            "metrics": {
                "analysis": f"{settings.get('ai', {}).get('analysis', {}).get('model', '')} / {settings.get('ai', {}).get('poem', {}).get('model', '')}",
                "web": f"Port {port}",
                "NFS": f"Ollama: {ollama_state}",
                "network": lan_ip,
                "CPU": f"{sys_stats.get('cpu_percent', 0)}%",
            },
            "disk": int(sys_stats.get("disk_usage", 0)),
        },
        "rpi4": {
            "status": "online",
            "metrics": {
                "service": "Ready (/kiosk)",
                "printer": settings.get("printing", {}).get("printer_name", "default"),
                "queue": f"{len(all_pdfs)} PDFs",
                "network": lan_ip,
                "CPU": f"{sys_stats.get('cpu_percent', 0)}%",
            },
        },
        "timestamp": datetime.now().isoformat(),
    }


def api_list_prints() -> list:
    if not DOCS_DIR.exists():
        return []
    printed_set = _get_printed_set()
    files = []
    for f in sorted(DOCS_DIR.glob("print_*.pdf"), key=lambda x: x.stat().st_mtime, reverse=True):
        thumb_file = f.with_suffix(".png")
        meta_file = f.with_suffix(".json")
        meta = {}
        if meta_file.exists():
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        mtime = f.stat().st_mtime
        dt = datetime.fromtimestamp(mtime)
        files.append({
            "name": f.name,
            "pdf_url": f"/shared_output/docs/{f.name}",
            "thumb_url": f"/shared_output/docs/{thumb_file.name}" if thumb_file.exists() else "/shared_output/image.png",
            "size": f"{f.stat().st_size / 1024:.1f} KB",
            "timestamp": mtime,
            "date": dt.strftime("%Y-%m-%d %H:%M"),
            "display_date": dt.strftime("%B %d, %Y"),
            "display_time": dt.strftime("%I:%M %p"),
            "is_today": dt.date() == datetime.now().date(),
            "printed": f.name in printed_set,
            "archetype": meta.get("archetype", ""),
            "fractalDimension": meta.get("fractalDimension"),
            "spatialEntropy": meta.get("spatialEntropy"),
            "poem": meta.get("poem", ""),
            "psychoanalysis": meta.get("psychoanalysis", ""),
        })
    return files


def api_get_logs(log_type: str) -> str:
    if "/" in log_type or "\\" in log_type or ".." in log_type:
        return "Invalid log name"
    candidates = [
        LOGS_DIR / f"{log_type}.log",
        LOGS_DIR / f"{log_type}.csv",
        ANALYSIS_DIR / f"{log_type}.log",
        ANALYSIS_DIR / f"{log_type}.csv",
    ]
    if log_type == "analysis":
        candidates.insert(0, LOGS_DIR / "installation.log")
        candidates.insert(1, ANALYSIS_DIR / "installation.log")
    for log_file in candidates:
        if log_file.exists():
            try:
                lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines(True)
                return "".join(lines[-200:]) or "(Log file is empty)"
            except Exception as e:
                return str(e)
    return f"No entries yet for '{log_type}'."


class LatentSpaceRequestHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Quiet routine polling logs; print errors or mutations
        if len(args) >= 2 and str(args[1]).startswith(("4", "5")):
            super().log_message(fmt, *args)

    def _send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type,Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,HEAD,OPTIONS")

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._send_cors_headers()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_text(self, text: str, status=200, content_type="text/plain; charset=utf-8"):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self._send_cors_headers()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_file(self, file_path: Path):
        if not file_path.exists() or not file_path.is_file():
            self._send_text("File not found", status=404)
            return
        ctype, _ = mimetypes.guess_type(str(file_path))
        if not ctype:
            if file_path.suffix == ".otf":
                ctype = "font/otf"
            else:
                ctype = "application/octet-stream"
        data = file_path.read_bytes()
        stat = file_path.stat()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Last-Modified", self.date_time_string(stat.st_mtime))
        self._send_cors_headers()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def do_OPTIONS(self):
        self.send_response(204)
        self._send_cors_headers()
        self.end_headers()

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)

        # Static & UI routes
        if path in ("/", "/index.html"):
            return self._send_file(PROJECT_DIR / "index.html")
        if path in ("/admin", "/admin.html"):
            return self._send_file(WEB_UI_DIR / "admin.html")
        if path in ("/draw", "/draw.html"):
            return self._send_file(WEB_UI_DIR / "draw.html")
        if path in ("/kiosk", "/kiosk.html"):
            return self._send_file(WEB_UI_DIR / "kiosk.html")
        if path in ("/remote", "/remote_ui.html"):
            return self._send_file(WEB_UI_DIR / "remote_ui.html")
        if path == "/kasboek-logo.jpg":
            is_preview = (query.get("preview") or [""])[0] == "1"
            attr_cfg = load_settings().get("attributions", {})
            if not is_preview and not (attr_cfg.get("enabled", False) and attr_cfg.get("show_logo", True)):
                self.send_response(204)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
            return self._send_file(PROJECT_DIR / "kasboek-logo.jpg")
        if path == "/korolev-medium.otf":
            return self._send_file(PROJECT_DIR / "korolev-medium.otf")

        if path.startswith("/shared_output/"):
            rel = path[len("/shared_output/"):]
            target = (SHARED_DIR / rel).resolve()
            if not str(target).startswith(str(SHARED_DIR.resolve())):
                return self._send_text("Forbidden", status=403)
            return self._send_file(target)

        # API GET routes
        if path in ("/api/status", "/api/monitoring"):
            return self._send_json(api_get_status())

        if path == "/api/pipeline/status":
            return self._send_json(_pipeline_state)

        if path == "/api/settings":
            settings = load_settings()
            for role in ("analysis", "poem"):
                if settings.get("ai", {}).get(role, {}).get("api_key"):
                    settings["ai"][role]["has_api_key"] = True
                    settings["ai"][role]["api_key"] = "***"
            return self._send_json(settings)

        if path == "/api/settings/attributions":
            return self._send_json(load_settings().get("attributions", {}))

        if path == "/api/settings/printing":
            return self._send_json(load_settings().get("printing", {}))

        if path == "/api/settings/tts":
            return self._send_json(load_settings().get("tts", {}))

        if path == "/api/ai/config":
            model_type = (query.get("type") or ["analysis"])[0]
            try:
                model = LLMModel(model_type)
                model.initialize()
                return self._send_json({"status": "success", "config": model.get_config_dict()})
            except Exception as e:
                model = LLMModel(model_type)
                return self._send_json({"status": "success", "config": model.get_config_dict(), "warning": str(e)})

        if path == "/api/files/prints":
            return self._send_json(api_list_prints())

        if path.startswith("/api/logs/"):
            log_type = path[len("/api/logs/"):]
            return self._send_text(api_get_logs(log_type))

        return self._send_text("Not Found", status=404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)

        if path == "/api/drawings":
            target_path = SHARED_DIR / "image.png"
            tmp_path = SHARED_DIR / "image.tmp.png"
            ctype = self.headers.get("Content-Type", "")
            try:
                if "multipart/form-data" in ctype:
                    form = cgi.FieldStorage(
                        fp=self.rfile,
                        headers=self.headers,
                        environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": ctype},
                    )
                    if "file" not in form:
                        return self._send_json({"status": "error", "error": "Missing file field"}, 400)
                    file_item = form["file"]
                    tmp_path.write_bytes(file_item.file.read())
                else:
                    data = self._read_json_body()
                    img_data = data.get("image", "")
                    if not img_data:
                        return self._send_json({"status": "error", "error": "No image provided"}, 400)
                    if "," in img_data:
                        img_data = img_data.split(",", 1)[1]
                    tmp_path.write_bytes(base64.b64decode(img_data))

                os.replace(str(tmp_path), str(target_path))

                if (query.get("sync") or [""])[0] == "1" and AI_AVAILABLE:
                    ok = run_analysis_pipeline()
                    return self._send_json({"status": "success" if ok else "error", "pipeline": "completed"})
                else:
                    started = trigger_pipeline_async()
                    return self._send_json({
                        "status": "success",
                        "message": "Drawing saved and analysis pipeline triggered",
                        "pipeline_started": started,
                    })
            except Exception as e:
                return self._send_json({"status": "error", "error": str(e)}, 500)

        if path == "/api/pipeline/run":
            if not (SHARED_DIR / "image.png").exists():
                return self._send_json({"status": "error", "error": "No shared_output/image.png found"}, 404)
            started = trigger_pipeline_async()
            return self._send_json({"status": "success", "started": started, "pipeline": _pipeline_state})

        if path == "/api/settings":
            data = self._read_json_body()
            updated = save_settings(data)
            _sync_attributions_to_content_json(updated.get("attributions", {}))
            return self._send_json({"status": "success", "settings": updated})

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
            config_data = data.get("config", {})
            try:
                model = LLMModel(model_type)
                response = model.update_assistant(**config_data)
                return self._send_json(response)
            except Exception as e:
                return self._send_json({"status": "Error", "error": str(e)}, 500)

        if path == "/api/ai/test":
            data = self._read_json_body()
            model_type = data.get("model_type", "analysis")
            try:
                model = LLMModel(model_type)
                reply = model.chat("Respond with a single short sentence confirming you are online and ready for Latent Space.")
                return self._send_json({
                    "status": "success",
                    "provider": model._provider,
                    "model": model._llm,
                    "reply": reply,
                })
            except Exception as e:
                return self._send_json({"status": "error", "error": str(e)}, 500)

        if path == "/api/ollama/stop":
            try:
                from start_everything import stop_ollama_serving
                stop_ollama_serving()
                return self._send_json({"status": "success", "message": "Ollama models unloaded and server stopped"})
            except Exception as e:
                return self._send_json({"status": "error", "error": str(e)}, 500)

        if path == "/api/files/print":
            data = self._read_json_body()
            filename = data.get("filename", "")
            mark_only = bool(data.get("mark_only", False))
            if not filename or "/" in filename or "\\" in filename:
                return self._send_json({"status": "error", "error": "Invalid filename"}, 400)
            pdf_path = DOCS_DIR / filename
            if not pdf_path.exists():
                return self._send_json({"status": "error", "error": "PDF file not found"}, 404)
            if mark_only:
                mark_file_printed(filename)
                return self._send_json({"status": "success", "filename": filename, "mode": "browser"})
            settings = load_settings()
            printer_name = data.get("printer_name") or settings.get("printing", {}).get("printer_name", "default")
            res = send_pdf_to_printer(str(pdf_path), printer_name=printer_name)
            return self._send_json(res, 200 if res.get("status") == "success" else 500)

        if path == "/api/files/delete":
            data = self._read_json_body()
            action = data.get("action")
            filename = data.get("filename")
            count = 0
            try:
                if action == "specific" and filename and "/" not in filename and "\\" not in filename:
                    file_path = DOCS_DIR / filename
                    for suffix in (".pdf", ".png", ".json"):
                        p = file_path.with_suffix(suffix)
                        if p.exists():
                            p.unlink()
                    count = 1
                elif action == "all":
                    today_int = int(datetime.now().strftime("%Y%m%d"))
                    for f in DOCS_DIR.glob("print_*.pdf"):
                        try:
                            f_date = int(f.name.split("_")[1])
                            if f_date < today_int:
                                for suffix in (".pdf", ".png", ".json"):
                                    p = f.with_suffix(suffix)
                                    if p.exists():
                                        p.unlink()
                                count += 1
                        except Exception:
                            pass
                return self._send_json({"status": "success", "deleted": count})
            except Exception as e:
                return self._send_json({"error": str(e)}, 500)

        return self._send_json({"error": "Not Found"}, 404)


def run_server(host="0.0.0.0", port=8000):
    start_background_watcher()
    server = ThreadingHTTPServer((host, port), LatentSpaceRequestHandler)
    print("=" * 64)
    print("LATENT SPACE UNIFIED SERVER")
    print(f"Project Root:  {PROJECT_DIR}")
    print(f"Shared Output: {SHARED_DIR}")
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
        server.server_close()
        try:
            from start_everything import stop_ollama_serving
            stop_ollama_serving()
        except Exception:
            pass


if __name__ == "__main__":
    settings = load_settings()
    port = int(os.environ.get("PORT", settings.get("server", {}).get("port", 8000)))
    host = os.environ.get("HOST", settings.get("server", {}).get("host", "0.0.0.0"))
    run_server(host=host, port=port)
