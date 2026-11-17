#!/usr/bin/env python3
"""
Portable Latent Space Starter
Works on Windows, macOS, Linux, On-Prem, or Cloud — no Google Drive or rclone required!
"""

import argparse
import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_DIR))

from config_manager import SHARED_DIR, DOCS_DIR, LOGS_DIR, ensure_directories, load_settings


def _is_ollama_running(host_url: str = "http://localhost:11434") -> bool:
    try:
        req = urllib.request.Request(f"{host_url.rstrip('/')}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            return resp.status == 200
    except Exception:
        return False


def stop_ollama_serving(kill_external: bool = False, owned_proc: subprocess.Popen = None):
    """
    Unload loaded Ollama models from VRAM/RAM (keep_alive: 0).
    Only terminates an Ollama server process if we spawned it (`owned_proc`)
    or if explicitly requested by an operator (`kill_external=True`).
    """
    settings = load_settings()
    ai_cfg = settings.get("ai", {})
    base_url = (
        ai_cfg.get("analysis", {}).get("base_url")
        or ai_cfg.get("poem", {}).get("base_url")
        or "http://localhost:11434"
    )
    host = base_url.replace("/v1", "").rstrip("/")
    if not host.startswith("http"):
        host = "http://localhost:11434"

    # 1. Unload any active models via Ollama HTTP API (keep_alive: 0)
    models_to_unload = set()
    for role in ("analysis", "poem"):
        role_cfg = ai_cfg.get(role, {})
        if role_cfg.get("provider", "ollama") == "ollama" and role_cfg.get("model"):
            models_to_unload.add(role_cfg["model"])

    if _is_ollama_running(host):
        try:
            req = urllib.request.Request(f"{host}/api/ps", method="GET")
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                ps_data = json.loads(resp.read().decode("utf-8"))
                for m in ps_data.get("models", []):
                    if m.get("name"):
                        models_to_unload.add(m["name"])
        except Exception:
            pass

        for model_name in models_to_unload:
            try:
                payload = json.dumps({"model": model_name, "keep_alive": 0}).encode("utf-8")
                req = urllib.request.Request(
                    f"{host}/api/generate",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=3.0):
                    pass
                print(f"  ✓ Unloaded Ollama model: {model_name}")
            except Exception:
                pass

    # 2. Terminate owned Ollama process first, if any
    stopped_any = False
    if owned_proc is not None and owned_proc.poll() is None:
        try:
            owned_proc.terminate()
            owned_proc.wait(timeout=5)
            stopped_any = True
        except Exception:
            try:
                owned_proc.kill()
                stopped_any = True
            except Exception:
                pass

    # 3. Only terminate external Ollama processes if explicitly requested (e.g. Admin button)
    if kill_external:
        exact_names = {"ollama", "ollama.exe"}
        try:
            import psutil
            for proc in psutil.process_iter(["pid", "name"]):
                try:
                    pname = (proc.info.get("name") or "").lower()
                    if pname in exact_names:
                        proc.terminate()
                        stopped_any = True
                except Exception:
                    pass
        except ImportError:
            if platform.system() == "Windows":
                try:
                    res = subprocess.run(
                        ["taskkill", "/F", "/IM", "ollama.exe", "/T"],
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    if res.returncode == 0:
                        stopped_any = True
                except Exception:
                    pass
            else:
                try:
                    res = subprocess.run(["pkill", "-x", "ollama"], capture_output=True, text=True, timeout=5)
                    if res.returncode == 0:
                        stopped_any = True
                except Exception:
                    pass

    if stopped_any:
        print("  ✓ Stopped Ollama server")


def find_python_executable(subproject: str = None) -> str:
    """Resolve the best Python interpreter (preferring local .venv or subproject venv)."""
    candidates = []
    if subproject:
        sub_dir = PROJECT_DIR / subproject
        candidates.extend([
            sub_dir / "virtual_environment" / "Scripts" / "python.exe",
            sub_dir / "virtual_environment" / "bin" / "python",
            sub_dir / ".venv" / "Scripts" / "python.exe",
            sub_dir / ".venv" / "bin" / "python",
        ])
    candidates.extend([
        PROJECT_DIR / ".venv" / "Scripts" / "python.exe",
        PROJECT_DIR / ".venv" / "bin" / "python",
    ])
    for c in candidates:
        if c.exists():
            return str(c)
    return sys.executable


class LatentSpaceStarter:
    def __init__(self, mode: str = "server", port: int = 8000):
        ensure_directories()
        self.mode = mode
        self.port = port
        self.processes = []
        self.ollama_proc = None
        self.running = True
        self._stopped = False

        signal.signal(signal.SIGINT, self.signal_handler)
        signal.signal(signal.SIGTERM, self.signal_handler)

    def signal_handler(self, signum, frame):
        print("\n\nShutting down Latent Space... Please wait...")
        self.running = False
        self.stop_all()
        sys.exit(0)

    def get_ip_address(self) -> str:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "localhost"

    def run_command(self, cmd, name, cwd=None, env=None):
        print(f"Starting {name}...")
        try:
            proc_env = os.environ.copy()
            if env:
                proc_env.update(env)
            process = subprocess.Popen(cmd, cwd=str(cwd or PROJECT_DIR), env=proc_env)
            self.processes.append((name, process))
            print(f"  ✓ {name} started (PID: {process.pid})")
            time.sleep(1)
            return process
        except Exception as e:
            print(f"  ✗ Failed to start {name}: {e}")
            return None

    def ensure_ollama_if_configured(self):
        settings = load_settings()
        ai_cfg = settings.get("ai", {})
        uses_ollama = any(
            ai_cfg.get(role, {}).get("provider", "ollama") == "ollama"
            for role in ("analysis", "poem")
        ) or ai_cfg.get("vision_mode") == "ollama_vision"

        if not uses_ollama:
            return

        if _is_ollama_running():
            print("  ✓ Local Ollama server is online")
            return

        ollama_bin = shutil.which("ollama")
        if ollama_bin:
            self.ollama_proc = self.run_command([ollama_bin, "serve"], "Ollama Local AI Server")

    def start_server_and_pipeline(self):
        self.ensure_ollama_if_configured()
        py = find_python_executable("analysis_watcher")
        server_script = PROJECT_DIR / "monitoring_server.py"
        self.run_command(
            [py, str(server_script)],
            f"Unified Server & Analysis Pipeline (Port {self.port})",
            cwd=PROJECT_DIR,
            env={"PORT": str(self.port), "LATENT_SPACE_MANAGED": "1"},
        )

    def start_desktop_gaze_painter(self):
        py = find_python_executable("gaze_painter")
        gaze_script = PROJECT_DIR / "gaze_painter" / "run_gaze_painter.py"
        if gaze_script.exists():
            self.run_command([py, str(gaze_script)], "Desktop Gaze Painter (OpenCV)", cwd=gaze_script.parent)
        else:
            print(f"  ⚠ Gaze script not found: {gaze_script}")

    def start_desktop_print_kiosk(self):
        py = find_python_executable("print_service")
        kiosk_script = PROJECT_DIR / "print_service" / "print_kiosk_ui.py"
        if kiosk_script.exists():
            self.run_command([py, str(kiosk_script)], "Desktop Print Kiosk (PyQt6)", cwd=kiosk_script.parent)
        else:
            print(f"  ⚠ Print Kiosk script not found: {kiosk_script}")

    def stop_all(self):
        if self._stopped:
            return
        self._stopped = True
        print("\nStopping all services...")
        for name, proc in reversed(self.processes):
            if proc is self.ollama_proc:
                continue
            if proc.poll() is None:
                print(f"  Stopping {name}...")
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        if self.mode in ("server", "full"):
            stop_ollama_serving(kill_external=False, owned_proc=self.ollama_proc)
        print("✓ All services stopped")

    def monitor_processes(self):
        while self.running:
            time.sleep(2)
            for name, proc in list(self.processes):
                if proc.poll() is not None:
                    print(f"\n⚠️  {name} exited with code {proc.poll()}")
                    self.processes.remove((name, proc))
                    if self.mode == "full" and "Gaze Painter" in name:
                        print("Desktop Gaze Painter closed — shutting down remaining services...")
                        self.running = False
                        break
            if not self.processes:
                print("All child processes have exited.")
                break

    def run(self):
        ip = self.get_ip_address()
        print("=" * 64)
        print("LATENT SPACE INSTALLATION")
        print(f"OS / Platform: {platform.system()} ({platform.machine()})")
        print(f"Project Dir:   {PROJECT_DIR}")
        print(f"Shared Output: {SHARED_DIR}")
        print(f"Launch Mode:   {self.mode.upper()}")
        print("=" * 64)

        if self.mode in ("server", "full"):
            self.start_server_and_pipeline()

        if self.mode in ("full", "gaze"):
            self.start_desktop_gaze_painter()

        if self.mode == "kiosk":
            self.start_desktop_print_kiosk()

        if self.mode in ("server", "full"):
            print("\n" + "=" * 64)
            print("✅ LATENT SPACE WEB SUITE RUNNING!")
            print("=" * 64)
            print(f"  🖼️  Live Gallery:         http://localhost:{self.port}/")
            print(f"  👁️  Browser Gaze Draw:    http://localhost:{self.port}/draw")
            print(f"  🖨️  Web Print Kiosk:      http://localhost:{self.port}/kiosk")
            print(f"  🎛️  Admin Command Center: http://localhost:{self.port}/admin")
            if ip != "localhost":
                print(f"\n  📱 Access from Tablet / Phone / Other Devices on LAN:")
                print(f"      http://{ip}:{self.port}/kiosk  (Print Kiosk)")
                print(f"      http://{ip}:{self.port}/draw   (Gaze Painter)")
                print(f"      http://{ip}:{self.port}/admin  (Admin Panel)")
            print("=" * 64)

        print("\nPress Ctrl+C to stop all services.\n")
        try:
            self.monitor_processes()
        except KeyboardInterrupt:
            pass
        finally:
            self.stop_all()


def main():
    settings = load_settings()
    default_port = int(os.environ.get("PORT", settings.get("server", {}).get("port", 8000)))

    parser = argparse.ArgumentParser(description="Start the Latent Space installation.")
    parser.add_argument(
        "--mode",
        choices=["server", "full", "gaze", "kiosk"],
        default="server",
        help=(
            "'server' (default): Web Server + AI Pipeline + Browser Gaze Draw (/draw) + Web Print Kiosk (/kiosk); "
            "'full': Server + Desktop OpenCV Gaze Painter; "
            "'gaze': Desktop OpenCV Gaze Painter only; "
            "'kiosk': Desktop PyQt6 Print Kiosk only."
        ),
    )
    parser.add_argument("--port", type=int, default=default_port, help="Port for the unified web server (default 8000)")
    args = parser.parse_args()

    starter = LatentSpaceStarter(mode=args.mode, port=args.port)
    starter.run()


if __name__ == "__main__":
    main()
