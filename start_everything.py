#!/usr/bin/env python3
"""
Simplified Latent Space Installation Starter
For 1 Jetson (everything) + 1 RPi (printer)
No systemd, no bash - pure Python!
"""

import os
import sys
import time
import subprocess
import threading
import signal
from pathlib import Path
import platform

# Configuration
USER = "kasboek"
PROJECT_DIR = f"/home/{USER}/LatentSpace"
SHARED_OUTPUT = f"{PROJECT_DIR}/shared_output"

class LatentSpaceStarter:
    def __init__(self):
        self.device_type = self.detect_device_type()
        self.processes = []
        self.running = True
        
        # Handle shutdown gracefully
        signal.signal(signal.SIGINT, self.signal_handler)
        signal.signal(signal.SIGTERM, self.signal_handler)
        
        print("="*60)
        print("LATENT SPACE INSTALLATION")
        print(f"User: {USER}")
        print(f"Device: {self.device_type.upper()}")
        print(f"Project: {PROJECT_DIR}")
        print("="*60)
        print()
    
    def signal_handler(self, signum, frame):
        print("\n\nShutting down... Please wait...")
        self.running = False
        self.stop_all()
        sys.exit(0)
    
    def detect_device_type(self):
        """Auto-detect Jetson or RPi"""
        hostname = platform.node().lower()
        
        # Auto-detect based on hostname
        if 'jetson' in hostname:
            return 'jetson'
        elif 'raspberry' in hostname or 'rpi' in hostname or 'pi' in hostname:
            return 'rpi'
        
        # Check for Jetson-specific files
        if Path('/etc/nv_tegra_release').exists():
            return 'jetson'
        
        # Check for RPi-specific files
        if Path('/proc/device-tree/model').exists():
            try:
                with open('/proc/device-tree/model', 'r') as f:
                    if 'raspberry' in f.read().lower():
                        return 'rpi'
            except:
                pass
        
        # Manual selection
        print("Could not auto-detect device type.")
        print("Please select:")
        print("1. Jetson (Runs everything except printing)")
        print("2. Raspberry Pi (Print service only)")
        
        choice = input("Enter choice (1-2): ").strip()
        
        if choice == '1':
            return 'jetson'
        elif choice == '2':
            return 'rpi'
        else:
            print("Invalid choice. Exiting.")
            sys.exit(1)
    
    def run_command(self, cmd, name, cwd=None):
        """Run a command as a subprocess"""
        print(f"Starting {name}...")
        try:
            if cwd:
                process = subprocess.Popen(cmd, cwd=cwd, shell=isinstance(cmd, str))
            else:
                process = subprocess.Popen(cmd, shell=isinstance(cmd, str))
            
            self.processes.append((name, process))
            print(f"✓ {name} started (PID: {process.pid})")
            time.sleep(1)  # Small delay between starts
            return process
        except Exception as e:
            print(f"✗ Failed to start {name}: {e}")
            return None
    
    def check_rclone(self):
        """Check if rclone is installed and configured"""
        try:
            subprocess.run(["rclone", "version"], capture_output=True, check=True)
        except:
            print("ERROR: rclone not installed!")
            print("Install with: curl https://rclone.org/install.sh | sudo bash")
            return False
        
        # Check if gdrive remote exists
        try:
            result = subprocess.run(["rclone", "listremotes"], capture_output=True, text=True)
            if "latent_space:" not in result.stdout:
                print("ERROR: 'latent_space' remote not configured!")
                print("Run: rclone config")
                print("Create a remote named 'latent_space' for Google Drive")
                return False
        except:
            return False
        
        return True
    
    def start_gdrive_mount(self):
        """Mount Google Drive using rclone directly"""
        print("Mounting Google Drive...")
        
        # Create mount point
        Path(SHARED_OUTPUT).mkdir(parents=True, exist_ok=True)
        
        # Check if already mounted
        try:
            result = subprocess.run(["mountpoint", "-q", SHARED_OUTPUT], capture_output=True)
            if result.returncode == 0:
                print("✓ Google Drive already mounted")
                return True
        except:
            pass
        
        # Mount with rclone
        cmd = [
            "rclone", "mount",
            "latent_space:",
            SHARED_OUTPUT,
            "--vfs-cache-mode", "full",
            "--vfs-cache-max-size", "10G",
            "--vfs-cache-max-age", "24h",
            "--vfs-read-chunk-size", "32M",
            "--buffer-size", "32M",
            "--dir-cache-time", "5m",
            "--poll-interval", "15s",
            "--daemon"
        ]
        
        try:
            subprocess.run(cmd, check=True)
            time.sleep(3)  # Wait for mount to establish
            
            # Verify mount
            result = subprocess.run(["mountpoint", "-q", SHARED_OUTPUT], capture_output=True)
            if result.returncode == 0:
                print("✓ Google Drive mounted successfully")
                
                # Create required directories
                Path(f"{SHARED_OUTPUT}/docs").mkdir(exist_ok=True)
                Path(f"{SHARED_OUTPUT}/docs/printed").mkdir(exist_ok=True)
                
                return True
            else:
                print("✗ Mount verification failed")
                return False
                
        except Exception as e:
            print(f"✗ Mount failed: {e}")
            return False
    
    def start_jetson_services(self):
        """Start all services on the Jetson"""
        print("\n🚀 STARTING JETSON SERVICES")
        print("-" * 40)
        
        # 1. Mount Google Drive
        if not self.start_gdrive_mount():
            print("WARNING: Google Drive mount failed. Continuing anyway...")
        
        time.sleep(2)
        
        # 2. Start Analysis Watcher (processes images, calls AI)
        print("\n📊 Starting Analysis Pipeline...")
        analysis_script = f"{PROJECT_DIR}/analysis_watcher/run_analysis_watcher.py"
        if Path(analysis_script).exists():
            # First, ensure we're in the right directory for .env file
            os.chdir(f"{PROJECT_DIR}/analysis_watcher")
            cmd = [sys.executable, "run_analysis_watcher.py"]
            self.run_command(cmd, "Analysis Watcher", cwd=f"{PROJECT_DIR}/analysis_watcher")
        else:
            print(f"⚠ Analysis script not found: {analysis_script}")
        
        time.sleep(2)
        
        # 3. Start Gaze Painter (eye tracking and drawing)
        print("\n👁️ Starting Gaze Tracker...")
        gaze_script = f"{PROJECT_DIR}/gaze-painter/run_gaze_painter.py"
        if Path(gaze_script).exists():
            cmd = [sys.executable, gaze_script]
            self.run_command(cmd, "Gaze Painter", cwd=f"{PROJECT_DIR}/gaze-painter")
        else:
            print(f"⚠ Gaze script not found: {gaze_script}")
        
        time.sleep(2)
        
        # 4. Start Web Gallery (port 8000)
        print("\n🌐 Starting Web Services...")
        os.chdir(PROJECT_DIR)
        cmd = f"python3 -m http.server 8000 --bind 0.0.0.0"
        self.run_command(cmd, "Gallery Server (8000)")
        
        time.sleep(1)
        
        # 5. Start Admin Dashboard (port 8001)
        monitor_script = f"{PROJECT_DIR}/monitoring_server.py"
        if Path(monitor_script).exists():
            cmd = [sys.executable, monitor_script]
            self.run_command(cmd, "Admin Dashboard (8001)")
        
        # 6. Optional: Direct file server for shared_output (port 8002)
        os.chdir(SHARED_OUTPUT)
        cmd = f"python3 -m http.server 8002 --bind 0.0.0.0"
        self.run_command(cmd, "File Server (8002)")
        
        print("\n" + "="*60)
        print("✅ ALL JETSON SERVICES RUNNING!")
        print("="*60)
        print("\n📍 Access Points:")
        print(f"  Gallery:   http://{self.get_ip_address()}:8000")
        print(f"  Admin:     http://{self.get_ip_address()}:8001")
        print(f"  Files:     http://{self.get_ip_address()}:8002")
        print("\n💡 The system will now:")
        print("  1. Track eyes and create drawings")
        print("  2. Analyze drawings with AI")
        print("  3. Generate poems and heatmaps")
        print("  4. Save everything to Google Drive")
        print("  5. Serve web interfaces")
    
    def start_rpi_services(self):
        """Start print service on RPi"""
        print("\n🖨️ STARTING RASPBERRY PI SERVICES")
        print("-" * 40)
        
        # 1. Mount Google Drive
        if not self.start_gdrive_mount():
            print("WARNING: Google Drive mount failed. Continuing anyway...")
        
        time.sleep(2)
        
        # 2. Ensure CUPS is running
        print("\n🖨️ Starting Print System...")
        try:
            subprocess.run(["sudo", "systemctl", "start", "cups"], capture_output=True)
            print("✓ CUPS printer service started")
        except:
            print("⚠ Could not start CUPS (may need sudo or already running)")
        
        # 3. Start Print Kiosk UI
        print("\n📱 Starting Touch Interface...")
        print_script = f"{PROJECT_DIR}/print_service/print_kiosk_ui.py"
        if Path(print_script).exists():
            cmd = [sys.executable, print_script]
            self.run_command(cmd, "Print Kiosk UI", cwd=f"{PROJECT_DIR}/print_service")
        else:
            print(f"⚠ Print UI script not found: {print_script}")
        
        print("\n" + "="*60)
        print("✅ PRINT SERVICE RUNNING!")
        print("="*60)
        print("\n💡 The kiosk will:")
        print("  1. Show available artwork from Google Drive")
        print("  2. Allow visitors to print their drawings")
        print("  3. Track print history")
    
    def get_ip_address(self):
        """Get the device's IP address"""
        try:
            import socket
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except:
            return "localhost"
    
    def monitor_processes(self):
        """Monitor and optionally restart crashed processes"""
        last_check = time.time()
        
        while self.running:
            time.sleep(5)
            
            # Check each process
            for name, proc in self.processes:
                if proc.poll() is not None:  # Process terminated
                    exit_code = proc.poll()
                    print(f"\n⚠️  {name} stopped (exit code: {exit_code})")
                    
                    # Optional: Auto-restart critical services
                    if "Google Drive" not in name:  # Don't auto-restart mount
                        print(f"   Consider restarting with: python3 start_everything.py")
            
            # Periodic status update (every 60 seconds)
            if time.time() - last_check > 60:
                active = sum(1 for _, p in self.processes if p.poll() is None)
                print(f"\n📊 Status: {active}/{len(self.processes)} services running")
                last_check = time.time()
    
    def stop_all(self):
        """Stop all services cleanly"""
        print("\n🛑 Stopping all services...")
        
        # Stop processes in reverse order
        for name, proc in reversed(self.processes):
            if proc.poll() is None:  # Still running
                print(f"  Stopping {name}...")
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    print(f"  Force killing {name}...")
                    proc.kill()
        
        # Unmount Google Drive
        print("  Unmounting Google Drive...")
        try:
            subprocess.run(["fusermount", "-u", SHARED_OUTPUT], capture_output=True)
        except:
            pass
        
        print("✓ All services stopped")
    
    def run(self):
        """Main entry point"""
        # Check rclone
        if not self.check_rclone():
            print("\n⚠️  Please install and configure rclone first!")
            sys.exit(1)
        
        # Start appropriate services
        if self.device_type == 'jetson':
            self.start_jetson_services()
        else:  # rpi
            self.start_rpi_services()
        
        print("\n" + "="*60)
        print("Press Ctrl+C to stop all services")
        print("="*60 + "\n")
        
        # Monitor until interrupted
        try:
            self.monitor_processes()
        except KeyboardInterrupt:
            pass
        
        self.stop_all()
        print("\nGoodbye! 👋")


def main():
    """Main entry point with checks"""
    
    # Check Python version
    if sys.version_info < (3, 7):
        print("ERROR: Python 3.7+ required")
        sys.exit(1)
    
    # Check user
    import getpass
    current_user = getpass.getuser()
    if current_user != USER:
        print(f"⚠️  Running as '{current_user}' instead of '{USER}'")
        print("   Paths may need adjustment.")
        response = input("Continue? (y/n): ")
        if response.lower() != 'y':
            sys.exit(0)
    
    # Check if project directory exists
    if not Path(PROJECT_DIR).exists():
        print(f"ERROR: Project directory not found: {PROJECT_DIR}")
        print("Please ensure the LatentSpace folder is in the correct location.")
        sys.exit(1)
    
    # Start the system
    starter = LatentSpaceStarter()
    starter.run()


if __name__ == "__main__":
    main()