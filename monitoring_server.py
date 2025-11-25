#!/usr/bin/env python3
"""
Simplified Monitoring Server for Single Jetson Setup
Combines all monitoring and control in one place
Port: 8001
"""

import os
import sys
import json
import time
import psutil
import subprocess
from pathlib import Path
from datetime import datetime
from flask import Flask, jsonify, request, send_from_directory, send_file
from flask_cors import CORS

# Configuration for kasboek user
USER = "kasboek"
PROJECT_DIR = Path(f"/home/{USER}/LatentSpace")
SHARED_DIR = PROJECT_DIR / "shared_output"
DOCS_DIR = SHARED_DIR / "docs"
LOGS_DIR = PROJECT_DIR / "logs"
WEB_UI_DIR = PROJECT_DIR / "web_ui"

# Ensure directories exist
SHARED_DIR.mkdir(parents=True, exist_ok=True)
DOCS_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# Add analysis_watcher to path for AI config
sys.path.append(str(PROJECT_DIR / "analysis_watcher"))

# Try to import AI modules
try:
    from chat import LLMModel
    AI_AVAILABLE = True
except ImportError as e:
    print(f"Note: AI config disabled - modules not found ({e})")
    AI_AVAILABLE = False

# Flask setup
app = Flask(__name__, static_folder=str(WEB_UI_DIR))
CORS(app)

# --- Helper Functions ---

def get_service_status(service_name):
    """Check if a Python process is running"""
    try:
        # Check by process name
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                cmdline = ' '.join(proc.info.get('cmdline', []))
                if service_name in cmdline:
                    return "running"
            except:
                pass
        return "stopped"
    except:
        return "unknown"

def check_gdrive_mount():
    """Check if Google Drive is mounted"""
    try:
        result = subprocess.run(
            ["mountpoint", "-q", str(SHARED_DIR)],
            capture_output=True
        )
        return result.returncode == 0
    except:
        return False

def get_system_stats():
    """Get system performance stats"""
    try:
        return {
            "cpu_percent": psutil.cpu_percent(interval=1),
            "memory_percent": psutil.virtual_memory().percent,
            "disk_usage": psutil.disk_usage(str(SHARED_DIR)).percent if SHARED_DIR.exists() else 0,
            "temperature": get_jetson_temp()
        }
    except:
        return {"cpu_percent": 0, "memory_percent": 0, "disk_usage": 0}

def get_jetson_temp():
    """Get Jetson temperature if available"""
    try:
        # Try Jetson thermal zone
        thermal_file = Path("/sys/devices/virtual/thermal/thermal_zone0/temp")
        if thermal_file.exists():
            temp = int(thermal_file.read_text()) / 1000.0
            return f"{temp:.1f}°C"
    except:
        pass
    return "N/A"

# --- Routes ---

@app.route('/')
def index():
    """Serve admin dashboard"""
    admin_file = WEB_UI_DIR / "admin.html"
    if admin_file.exists():
        return send_from_directory(WEB_UI_DIR, 'admin.html')
    else:
        return """
        <html>
        <body style="font-family: Arial; padding: 20px;">
        <h1>Latent Space Admin</h1>
        <p>Admin interface file not found.</p>
        <h3>Quick Status:</h3>
        <ul>
            <li><a href="/api/status">System Status (JSON)</a></li>
            <li><a href="/api/files/prints">List Prints</a></li>
            <li><a href="http://localhost:8000">Gallery (Port 8000)</a></li>
        </ul>
        </body>
        </html>
        """

@app.route('/api/status')
def system_status():
    """Complete system status"""
    
    # Check all services
    services = {
        "gaze_painter": get_service_status("run_gaze_painter"),
        "analysis_watcher": get_service_status("run_analysis_watcher"),
        "web_gallery": get_service_status("8000"),
        "google_drive": "mounted" if check_gdrive_mount() else "unmounted"
    }
    
    # Count recent files
    try:
        today_files = len(list(DOCS_DIR.glob("print_*.pdf")))
        total_images = len(list(SHARED_DIR.glob("*.png")))
    except:
        today_files = 0
        total_images = 0
    
    return jsonify({
        "device": "Jetson (All-in-One)",
        "services": services,
        "system": get_system_stats(),
        "statistics": {
            "prints_today": today_files,
            "total_images": total_images,
            "google_drive": check_gdrive_mount()
        },
        "timestamp": datetime.now().isoformat()
    })

@app.route('/api/monitoring')
def monitoring():
    """Monitoring endpoint for compatibility"""
    status = system_status()
    return status

@app.route('/api/service/<action>/<service>', methods=['POST'])
def control_service(action, service):
    """Control services (restart/stop/start)"""
    
    allowed_services = {
        "gaze": f"{PROJECT_DIR}/gaze-painter/run_gaze_painter.py",
        "analysis": f"{PROJECT_DIR}/analysis_watcher/run_analysis_watcher.py",
        "gdrive": "gdrive_mount"
    }
    
    if service not in allowed_services:
        return jsonify({"error": "Unknown service"}), 404
    
    if action == "restart":
        # Find and kill existing process
        script_name = allowed_services[service]
        for proc in psutil.process_iter(['pid', 'cmdline']):
            try:
                if script_name in ' '.join(proc.info.get('cmdline', [])):
                    proc.kill()
                    time.sleep(1)
                    break
            except:
                pass
        
        # Start new process
        if service == "gdrive":
            subprocess.Popen(["rclone", "mount", "latent_space:", 
                            str(SHARED_DIR), "--daemon"])
        else:
            subprocess.Popen([sys.executable, script_name])
        
        return jsonify({"status": "success", "action": action, "service": service})
    
    return jsonify({"error": "Action not implemented"}), 501

@app.route('/api/files/prints', methods=['GET'])
def list_prints():
    """List all PDF prints"""
    if not DOCS_DIR.exists():
        return jsonify([])
    
    files = []
    try:
        for f in sorted(DOCS_DIR.glob("print_*.pdf"), key=lambda x: x.stat().st_mtime, reverse=True):
            files.append({
                "name": f.name,
                "size": f"{f.stat().st_size / 1024:.1f} KB",
                "timestamp": f.stat().st_mtime,
                "date": datetime.fromtimestamp(f.stat().st_mtime).strftime('%Y-%m-%d %H:%M')
            })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    
    return jsonify(files)

@app.route('/api/files/delete', methods=['POST'])
def delete_files():
    """Delete print files"""
    data = request.json
    action = data.get('action')
    filename = data.get('filename')
    
    count = 0
    try:
        if action == 'specific' and filename:
            file_path = DOCS_DIR / filename
            if file_path.exists():
                file_path.unlink()
                os.sync()  # Force sync to Google Drive
                count = 1
                
        elif action == 'all':
            # Delete files older than today
            today_int = int(datetime.now().strftime('%Y%m%d'))
            for f in DOCS_DIR.glob("print_*.pdf"):
                try:
                    f_date = int(f.name.split('_')[1])
                    if f_date < today_int:
                        f.unlink()
                        count += 1
                except:
                    pass
            os.sync()
        
        return jsonify({"status": "success", "deleted": count})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/logs/<log_type>')
def get_logs(log_type):
    """Get log files"""
    if "/" in log_type or "\\" in log_type:
        return "Invalid log name", 400
    
    log_file = LOGS_DIR / f"{log_type}.log"
    
    if not log_file.exists():
        # Try alternative locations
        if log_type == "gaze":
            log_file = PROJECT_DIR / "gaze-painter" / "calibrations" / "gaze.log"
        elif log_type == "analysis":
            log_file = PROJECT_DIR / "analysis_watcher" / "installation.log"
    
    if log_file.exists():
        try:
            with open(log_file, 'r') as f:
                lines = f.readlines()
                return "".join(lines[-200:])  # Last 200 lines
        except Exception as e:
            return str(e), 500
    
    return "Log file not found", 404

@app.route('/api/gdrive/status')
def gdrive_status():
    """Google Drive mount status"""
    mounted = check_gdrive_mount()
    
    status = {
        "mounted": mounted,
        "mount_point": str(SHARED_DIR)
    }
    
    if mounted:
        try:
            files = list(SHARED_DIR.iterdir())
            status["accessible"] = True
            status["file_count"] = len(files)
            
            # Check disk usage
            usage = psutil.disk_usage(str(SHARED_DIR))
            status["disk_usage"] = {
                "total_gb": round(usage.total / (1024**3), 1),
                "used_gb": round(usage.used / (1024**3), 1),
                "free_gb": round(usage.free / (1024**3), 1),
                "percent": usage.percent
            }
        except Exception as e:
            status["accessible"] = False
            status["error"] = str(e)
    
    return jsonify(status)

@app.route('/api/gdrive/remount', methods=['POST'])
def remount_gdrive():
    """Remount Google Drive"""
    try:
        # Unmount first
        subprocess.run(["fusermount", "-u", str(SHARED_DIR)], capture_output=True)
        time.sleep(1)
        
        # Mount again
        cmd = [
            "rclone", "mount",
            "latent_space:",
            str(SHARED_DIR),
            "--vfs-cache-mode", "full",
            "--daemon"
        ]
        
        subprocess.run(cmd)
        time.sleep(3)
        
        if check_gdrive_mount():
            return jsonify({"status": "success"})
        else:
            return jsonify({"error": "Mount failed"}), 500
            
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# Serve shared output files
@app.route('/shared_output/<path:filename>')
def serve_shared_file(filename):
    """Serve files from shared output"""
    file_path = SHARED_DIR / filename
    if file_path.exists():
        return send_file(str(file_path))
    return "File not found", 404

# AI Configuration routes (if available)
if AI_AVAILABLE:
    @app.route('/api/ai/config', methods=['GET'])
    def get_ai_config():
        """Get AI model configuration"""
        model_type = request.args.get('type', 'analysis')
        key = '8a79f5ec-c5dd-448c-9611-99610792a04b' if model_type == 'analysis' else '959058ad-4417-49e3-9e71-252ee2fb033d'
        
        try:
            os.chdir(PROJECT_DIR / "analysis_watcher")
            model = LLMModel(key)
            model.initialize()
            
            config = {
                "name": getattr(model, "_name", ""),
                "description": getattr(model, "_description", ""),
                "temperature": getattr(model, "_temperature", 0.7)
            }
            
            return jsonify({"status": "success", "config": config})
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    
    @app.route('/api/ai/config', methods=['POST'])
    def update_ai_config():
        """Update AI model configuration"""
        data = request.json
        model_type = data.get('model_type', 'analysis')
        config_data = data.get('config', {})
        
        key = '8a79f5ec-c5dd-448c-9611-99610792a04b' if model_type == 'analysis' else '959058ad-4417-49e3-9e71-252ee2fb033d'
        
        try:
            os.chdir(PROJECT_DIR / "analysis_watcher")
            model = LLMModel(key)
            model.initialize()
            response = model.update_assistant(**config_data)
            return jsonify(response)
        except Exception as e:
            return jsonify({"error": str(e)}), 500


if __name__ == '__main__':
    print("="*60)
    print("LATENT SPACE MONITORING SERVER")
    print(f"Project: {PROJECT_DIR}")
    print(f"Shared: {SHARED_DIR}")
    print("="*60)
    
    # Check Google Drive mount
    if not check_gdrive_mount():
        print("⚠️  Google Drive not mounted!")
        print("   Run: rclone mount latent_space:", SHARED_DIR, "--daemon")
    else:
        print("✓ Google Drive mounted")
    
    print(f"\n🌐 Starting server on port 8001...")
    print(f"   Admin Dashboard: http://localhost:8001")
    print()
    
    app.run(host='0.0.0.0', port=8001, debug=False)