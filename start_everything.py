#!/usr/bin/env python3
"""
Portable Latent Space Starter
Works on Windows, macOS, Linux, On-Prem, or Cloud — no Google Drive or rclone required!
"""

import argparse
import os
import platform
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_DIR))

from config_manager import SHARED_DIR, DOCS_DIR, LOGS_DIR, ensure_directories, load_settings


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
        self.running = True

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

    def start_server_and_pipeline(self):
        py = find_python_executable("analysis_watcher")
        server_script = PROJECT_DIR / "monitoring_server.py"
        self.run_command(
            [py, str(server_script)],
            f"Unified Server & Analysis Pipeline (Port {self.port})",
            cwd=PROJECT_DIR,
            env={"PORT": str(self.port)},
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
        print("\nStopping all services...")
        for name, proc in reversed(self.processes):
            if proc.poll() is None:
                print(f"  Stopping {name}...")
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        print("✓ All services stopped")

    def monitor_processes(self):
        while self.running:
            time.sleep(3)
            for name, proc in list(self.processes):
                if proc.poll() is not None:
                    print(f"\n⚠️  {name} exited with code {proc.poll()}")
                    self.processes.remove((name, proc))
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
