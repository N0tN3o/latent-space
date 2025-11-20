#!/usr/bin/env python3
"""
Latent Space - Unified Command Center
Port: 8001
Integrates: Monitoring, AI Configuration, File Management, Log Viewing
"""

import os
import sys
import json
import time
import shutil
import logging
from pathlib import Path
from datetime import datetime
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

# --- PATH SETUP ---
CURRENT_DIR = Path(__file__).parent.absolute()
SHARED_DIR = CURRENT_DIR / "shared_output"
DOCS_DIR = SHARED_DIR / "docs"
LOGS_DIR = CURRENT_DIR / "logs"
WEB_UI_DIR = CURRENT_DIR / "web_ui"

# Add analysis_watcher to path to import AI logic
sys.path.append(str(CURRENT_DIR / "analysis_watcher"))

# Robust Import for AI Logic
try:
    from chat import LLMModel
    AI_AVAILABLE = True
except ImportError as e:
    print(f"WARNING: AI modules not found ({e}). AI Config will be disabled.")
    AI_AVAILABLE = False

# --- FLASK SETUP ---
app = Flask(__name__, static_folder=str(WEB_UI_DIR))
CORS(app)

# --- HELPER FUNCTIONS ---

def get_service_status(service_name):
    """Check systemd service status"""
    try:
        # Simple check: 0 = active, else inactive
        res = os.system(f"systemctl is-active --quiet {service_name}")
        return "online" if res == 0 else "offline"
    except:
        return "error"

# --- ROUTES: DASHBOARD ---

@app.route('/')
def index():
    """Serve the Admin Dashboard"""
    return send_from_directory(WEB_UI_DIR, 'admin.html')

# --- ROUTES: SYSTEM MONITORING ---

@app.route('/api/monitoring')
def monitoring():
    """Return status of all system components"""
    # This is a simplified version of your original monitoring logic
    # You can expand this with the full remote SSH logic if needed
    status = {
        "jetson_b": {
            "status": "online",
            "cpu": "Running",
            "services": {
                "Analysis": get_service_status("analysis-watcher"),
                "Web": get_service_status("web-server"),
                "NFS": get_service_status("nfs-kernel-server")
            }
        },
        # For full deployment, add SSH checks for Jetson A / RPi4 here
        # (omitted for brevity, but your original code fits here)
    }
    return jsonify(status)

@app.route('/api/control/<action>/<service>', methods=['POST'])
def service_control(action, service):
    """Restart/Stop services"""
    # Security: Only allow specific services
    ALLOWED = ["analysis-watcher", "web-server", "print-service", "gaze-painter"]
    if service not in ALLOWED:
        return jsonify({"error": "Service not allowed"}), 403
        
    if action not in ["restart", "stop", "start"]:
        return jsonify({"error": "Invalid action"}), 400
    
    cmd = f"sudo systemctl {action} {service}"
    ret = os.system(cmd)
    
    return jsonify({"status": "success" if ret == 0 else "failed"})

# --- ROUTES: AI CONFIGURATION ---

@app.route('/api/ai/config', methods=['GET'])
def get_ai_config():
    """Get current configuration for a model"""
    if not AI_AVAILABLE:
        return jsonify({"status": "error", "message": "AI Module unavailable"}), 503

    model_type = request.args.get('type', 'analysis')
    
    # Keys from your original code
    key = '8a79f5ec-c5dd-448c-9611-99610792a04b' if model_type == 'analysis' else '959058ad-4417-49e3-9e71-252ee2fb033d'
    
    try:
        # Need to be in the dir for .env files usually
        cwd = os.getcwd()
        os.chdir(CURRENT_DIR / "analysis_watcher")
        
        model = LLMModel(key)
        model.initialize()
        
        config = {
            "name": getattr(model, "_name", ""),
            "description": getattr(model, "_description", ""),
            "temperature": getattr(model, "_temperature", 0.7),
            # Add other fields as needed
        }
        
        os.chdir(cwd) # Restore CWD
        return jsonify({"status": "success", "config": config})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/ai/config', methods=['POST'])
def update_ai_config():
    """Update AI configuration"""
    if not AI_AVAILABLE: return jsonify({"status": "error"}), 503
    
    data = request.json
    model_type = data.get('model_type', 'analysis')
    config_data = data.get('config', {})
    
    key = '8a79f5ec-c5dd-448c-9611-99610792a04b' if model_type == 'analysis' else '959058ad-4417-49e3-9e71-252ee2fb033d'
    
    try:
        cwd = os.getcwd()
        os.chdir(CURRENT_DIR / "analysis_watcher")
        
        model = LLMModel(key)
        model.initialize()
        response = model.update_assistant(**config_data)
        
        os.chdir(cwd)
        return jsonify(response)
    except Exception as e:
        return jsonify({"status": "Failed", "error": str(e)}), 500

# --- ROUTES: FILE MANAGEMENT ---

@app.route('/api/files/prints', methods=['GET'])
def list_prints():
    """List PDF prints sorted by date"""
    if not DOCS_DIR.exists(): return jsonify([])
    
    files = []
    for f in DOCS_DIR.glob("print_*.pdf"):
        files.append({
            "name": f.name,
            "size": f"{f.stat().st_size / 1024:.1f} KB",
            "timestamp": f.stat().st_mtime,
            "date": datetime.fromtimestamp(f.stat().st_mtime).strftime('%Y-%m-%d %H:%M')
        })
    
    # Sort by timestamp desc
    files.sort(key=lambda x: x['timestamp'], reverse=True)
    return jsonify(files)

@app.route('/api/files/delete', methods=['POST'])
def delete_files():
    """Delete prints"""
    data = request.json
    action = data.get('action')
    filename = data.get('filename')
    
    count = 0
    try:
        if action == 'specific' and filename:
            file_path = DOCS_DIR / filename
            if file_path.exists():
                file_path.unlink()
                count = 1
        elif action == 'all':
            # Logic: Delete ONLY files older than today (Safety first)
            today_int = int(datetime.now().strftime('%Y%m%d'))
            for f in DOCS_DIR.glob("print_*.pdf"):
                try:
                    # Parse print_20251120_...
                    f_date = int(f.name.split('_')[1])
                    if f_date < today_int:
                        f.unlink()
                        count += 1
                except: pass
                
        return jsonify({"status": "success", "deleted": count})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# --- ROUTES: LOGS ---

@app.route('/api/logs/<log_type>', methods=['GET'])
def get_logs(log_type):
    """Get content of log files"""
    filename = f"{log_type}.log"
    # Security: prevent directory traversal
    if "/" in log_type or "\\" in log_type:
        return "Invalid log filename", 400
        
    log_path = LOGS_DIR / filename
    if log_path.exists():
        # Return last 200 lines
        try:
            with open(log_path, 'r') as f:
                lines = f.readlines()
                return "".join(lines[-200:])
        except Exception as e:
            return str(e), 500
    return "Log file not found", 404

if __name__ == '__main__':
    # Run on all interfaces so it's accessible on network
    app.run(host='0.0.0.0', port=8001, debug=False)