#!/usr/bin/env python3
"""
Latent Space - Monitoring Dashboard Backend

Serves the remote_ui.html and provides the API endpoints for
monitoring and remote control.
"""

import os
import subprocess
import json
from datetime import datetime
from pathlib import Path
from flask import Flask, jsonify, render_template, send_from_directory, request
from flask_cors import CORS
import paramiko
import re
import logging

# --- Configuration ---
app = Flask(__name__, template_folder="web_ui", static_folder="web_ui/static")
CORS(app)

# Setup logging
LOGS_DIR = Path("/home/art/LatentSpace/logs")
LOGS_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    filename=LOGS_DIR / "monitoring_server.log",
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Device configuration
DEVICES = {
    "jetson_a": {
        "ip": "192.168.1.10",
        "user": "art",
        "services": ["gaze-painter"]
    },
    "jetson_b": {
        "ip": "127.0.0.1",  # Localhost
        "user": "art",
        "services": ["analysis-watcher", "web-server", "nfs-kernel-server"]
    },
    "rpi4": {
        "ip": "192.168.1.12",
        "user": "art",
        "services": ["print-service", "cups"]
    }
}
SSH_KEY_PATH = "/home/art/.ssh/id_rsa_sync"
SHARED_DIR = Path("/home/art/LatentSpace/shared_output")
DOCS_DIR = SHARED_DIR / "docs"

# --- Helper Functions ---

def run_remote_command(ip, user, command, timeout=5):
    """Executes a command on a remote device via SSH"""
    if ip == "127.0.0.1":
        # Run locally
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout
            )
            return result.stdout, result.stderr, result.returncode
        except Exception as e:
            logging.error(f"Local command failed: {e}")
            return None, str(e), 1

    # Run remotely
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(
            ip,
            username=user,
            key_filename=SSH_KEY_PATH,
            timeout=timeout
        )
        stdin, stdout, stderr = ssh.exec_command(command, timeout=timeout)
        stdout_data = stdout.read().decode().strip()
        stderr_data = stderr.read().decode().strip()
        return_code = stdout.channel.recv_exit_status()
        return stdout_data, stderr_data, return_code
    except Exception as e:
        logging.error(f"SSH command to {ip} failed: {e}")
        return None, str(e), 1
    finally:
        ssh.close()

def parse_cpu_mem(output):
    """Parse 'top' command output"""
    try:
        # CPU
        cpu_idle = re.search(r"(\d+\.\d+)\s+%id", output)
        cpu_usage = 100.0 - float(cpu_idle.group(1)) if cpu_idle else 0.0

        # Memory
        mem_total = re.search(r"Mem\s*:\s*(\d+)\s+total", output)
        mem_used = re.search(r"Mem\s*:\s*(\d+)\s+total,\s*(\d+)\s+free,\s*(\d+)\s+used", output)
        if mem_used:
             total_kb = float(mem_used.group(1))
             used_kb = float(mem_used.group(3))
             mem_percent = (used_kb / total_kb) * 100
             mem_str = f"{used_kb/1024:.0f}M / {total_kb/1024:.0f}M"
        elif mem_total:
             # Fallback for different 'top' formats
             total_kb = float(mem_total.group(1))
             used = re.search(r"(\d+)\s+used", output)
             used_kb = float(used.group(1)) if used else 0
             mem_percent = (used_kb / total_kb) * 100
             mem_str = f"{used_kb/1024:.0f}M / {total_kb/1024:.0f}M"
        else:
             mem_percent = 0
             mem_str = "N/A"

        return f"{cpu_usage:.1f}%", mem_str, int(mem_percent)
    except Exception as e:
        logging.warning(f"Failed to parse top output: {e}")
        return "N/A", "N/A", 0

def parse_disk(output):
    """Parse 'df' command output"""
    try:
        match = re.search(r"(\d+)\s+(\d+)\s+(\d+)%", output)
        if match:
            used_gb = float(match.group(1)) / (1024*1024)
            total_gb = float(match.group(2)) / (1024*1024)
            percent = int(match.group(3))
            return f"{used_gb:.1f}G / {total_gb:.1f}G", percent
    except Exception as e:
        logging.warning(f"Failed to parse df output: {e}")
    return "N/A", 0

def get_service_status(ip, user, service):
    """Check if a systemd service is active"""
    stdout, stderr, code = run_remote_command(
        ip, user, f"systemctl is-active {service}"
    )
    if stdout:
        return "online" if stdout == "active" else "offline"
    return "error"

def get_device_logs(ip, user, service):
    """Get last 5 log entries for a service"""
    stdout, stderr, code = run_remote_command(
        ip, user, f"journalctl -u {service} -n 5 --no-pager"
    )
    if stdout:
        return stdout.split('\n')
    return [f"Error fetching logs: {stderr}"]

def get_analytics():
    """Calculate usage statistics"""
    stats = {
        "drawings": 0,
        "prints": 0,
        "cycleTime": "N/A",
        "apiSuccess": "N/A"  # Mocked
    }
    today = datetime.now().date()
    
    # Total Drawings (Today)
    try:
        count = 0
        for f in DOCS_DIR.glob("print_*.pdf"):
            file_date = datetime.fromtimestamp(f.stat().st_mtime).date()
            if file_date == today:
                count += 1
        stats["drawings"] = count
    except Exception as e:
        logging.warning(f"Could not count drawings: {e}")

    # Total Prints (Today)
    try:
        log_path = DOCS_DIR / "print_history.json"
        if log_path.exists():
            with open(log_path, 'r') as f:
                history = json.load(f)
            
            count = 0
            for print_entry in history.get("prints", []):
                print_date = datetime.fromisoformat(print_entry["timestamp"]).date()
                if print_date == today:
                    count += 1
            stats["prints"] = count
    except Exception as e:
        logging.warning(f"Could not count prints: {e}")
        
    return stats

# --- API Endpoints ---

@app.route('/')
def index():
    """Serve the main HTML dashboard"""
    return render_template('remote_ui.html')

@app.route('/<path:filename>')
def serve_ui_files(filename):
    """Serve supporting files for the UI (if any)"""
    return send_from_directory(app.template_folder, filename)

@app.route('/api/monitoring')
def get_monitoring_data():
    """Main API endpoint to fetch status from all devices"""
    data = {
        "jetson_a": {"status": "offline", "metrics": {}},
        "jetson_b": {"status": "offline", "metrics": {}},
        "rpi4": {"status": "offline", "metrics": {}},
        "statistics": get_analytics()
    }

    # --- Check Jetson A ---
    dev = DEVICES["jetson_a"]
    stdout, stderr, code = run_remote_command(
        dev["ip"], dev["user"], "top -b -n 1 | head -n 5"
    )
    if code == 0:
        data["jetson_a"]["status"] = "online"
        cpu, mem_str, mem_pct = parse_cpu_mem(stdout)
        data["jetson_a"]["metrics"]["CPU"] = cpu
        data["jetson_a"]["metrics"]["Memory"] = mem_str
        data["jetson_a"]["memory"] = mem_pct
        data["jetson_a"]["metrics"]["Service"] = get_service_status(
            dev["ip"], dev["user"], dev["services"][0]
        )
        data["jetson_a"]["logs"] = get_device_logs(
            dev["ip"], dev["user"], dev["services"][0]
        )
    else:
        data["jetson_a"]["status"] = "error"
        data["jetson_a"]["logs"] = [f"Device unreachable: {stderr}"]

    # --- Check Jetson B (Self) ---
    dev = DEVICES["jetson_b"]
    stdout, stderr, code = run_remote_command(
        dev["ip"], dev["user"], "top -b -n 1 | head -n 5"
    )
    if code == 0:
        data["jetson_b"]["status"] = "online"
        cpu, _, _ = parse_cpu_mem(stdout)
        data["jetson_b"]["metrics"]["CPU"] = cpu
        
        # Disk usage for shared_output
        stdout_disk, _, _ = run_remote_command(
            dev["ip"], dev["user"], f"df -k {SHARED_DIR} | tail -n 1"
        )
        disk_str, disk_pct = parse_disk(stdout_disk)
        data["jetson_b"]["metrics"]["Disk"] = disk_str
        data["jetson_b"]["disk"] = disk_pct

        data["jetson_b"]["metrics"]["Analysis"] = get_service_status(
            dev["ip"], dev["user"], "analysis-watcher"
        )
        data["jetson_b"]["metrics"]["Web"] = get_service_status(
            dev["ip"], dev["user"], "web-server"
        )
        data["jetson_b"]["metrics"]["NFS"] = get_service_status(
            dev["ip"], dev["user"], "nfs-kernel-server"
        )
        data["jetson_b"]["logs"] = get_device_logs(
            dev["ip"], dev["user"], "analysis-watcher"
        )
    else:
        data["jetson_b"]["status"] = "error"
        data["jetson_b"]["logs"] = [f"Local check failed: {stderr}"]

    # --- Check RPi4 ---
    dev = DEVICES["rpi4"]
    stdout, stderr, code = run_remote_command(
        dev["ip"], dev["user"], "top -b -n 1 | head -n 5"
    )
    if code == 0:
        data["rpi4"]["status"] = "online"
        cpu, _, _ = parse_cpu_mem(stdout)
        data["rpi4"]["metrics"]["CPU"] = cpu
        data["rpi4"]["metrics"]["Service"] = get_service_status(
            dev["ip"], dev["user"], dev["services"][0]
        )
        data["rpi4"]["metrics"]["Printer"] = get_service_status(
            dev["ip"], dev["user"], dev["services"][1]
        )
        # Check print queue
        stdout_q, _, _ = run_remote_command(
            dev["ip"], dev["user"], "lpq | grep -c '^[0-9]'"
        )
        data["rpi4"]["metrics"]["Queue"] = f"{stdout_q} jobs"
        data["rpi4"]["logs"] = get_device_logs(
            dev["ip"], dev["user"], dev["services"][0]
        )
    else:
        data["rpi4"]["status"] = "error"
        data["rpi4"]["logs"] = [f"Device unreachable: {stderr}"]

    return jsonify(data)

@app.route('/api/<action>/<device>', methods=['POST'])
def control_service(action, device):
    """Handle restart/stop commands"""
    if device not in DEVICES:
        return jsonify({"status": "error", "message": "Unknown device"}), 404
    
    if action not in ["restart", "stop"]:
        return jsonify({"status": "error", "message": "Invalid action"}), 400

    dev = DEVICES[device]
    results = {}
    
    for service in dev["services"]:
        # Don't let user stop the web server itself
        if service == "web-server" and action == "stop":
            results[service] = "Stop disabled for web-server"
            continue
            
        logging.info(f"Executing {action} on {service} for {device}")
        
        # Sudo is required, configured via /etc/sudoers.d/
        command = f"sudo /bin/systemctl {action} {service}"
        
        stdout, stderr, code = run_remote_command(dev["ip"], dev["user"], command)
        
        if code == 0:
            results[service] = f"Command '{action}' sent successfully"
        else:
            results[service] = f"Failed: {stderr}"
            
    return jsonify({"status": "ok", "results": results})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000, debug=True)