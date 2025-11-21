"""
Centralized Network Health Check & Error Display Module
For Latent Space Installation

Usage:
    from network_health import NetworkHealth
    
    health = NetworkHealth(role="jetson_a")  # or "jetson_b", "rpi4"
    
    while running:
        status = health.check()
        if status == "STOPPED":
            health.display_error()
            break
"""

import os
import time
import subprocess
import logging
from datetime import datetime
from enum import Enum

class NetworkStatus(Enum):
    PRIMARY = "primary"      # NFS healthy
    SECONDARY = "secondary"  # SSH fallback active
    DEGRADED = "degraded"    # Retrying connection
    STOPPED = "stopped"      # Hard stop, show error

class NetworkHealth:
    def __init__(self, role, nfs_mount="~/art/shared_output"):
        """
        Initialize network health checker
        
        Args:
            role: "jetson_a", "jetson_b", or "rpi4"
            nfs_mount: Path to NFS mount point
        """
        self.role = role
        self.nfs_mount = nfs_mount
        self.status = NetworkStatus.PRIMARY
        self.failure_start = None
        self.retry_count = 0
        
        # Configuration
        self.NFS_TIMEOUT = 30  # Seconds before trying SSH
        self.TOTAL_TIMEOUT = 120  # Seconds before hard stop
        self.RETRY_INTERVAL = 5  # Seconds between retries
        
        # Target IPs for connectivity checks
        self.targets = {
            "jetson_a": "192.168.1.10",
            "jetson_b": "192.168.1.11",
            "rpi4": "192.168.1.12"
        }
        
        # Setup logging
        logging.basicConfig(
            filename=f'~/art/logs/{role}_network.log',
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s'
        )
        self.logger = logging.getLogger(role)
    
    def check_nfs_mount(self):
        """Check if NFS mount is healthy and writable"""
        try:
            # Check if mounted
            if not os.path.ismount(self.nfs_mount):
                self.logger.warning("NFS not mounted")
                return False
            
            # Check if writable
            test_file = os.path.join(self.nfs_mount, f".health_check_{self.role}")
            with open(test_file, 'w') as f:
                f.write(str(time.time()))
            os.remove(test_file)
            
            return True
        except Exception as e:
            self.logger.error(f"NFS check failed: {e}")
            return False
    
    def check_ssh_connectivity(self, target_role):
        """Check if SSH to target device works"""
        target_ip = self.targets.get(target_role)
        if not target_ip:
            return False
        
        try:
            # Quick SSH check with timeout
            cmd = f"ssh -q -o BatchMode=yes -o ConnectTimeout=5 art@{target_ip} exit"
            result = subprocess.run(cmd, shell=True, timeout=10, 
                                    stdout=subprocess.DEVNULL, 
                                    stderr=subprocess.DEVNULL)
            return result.returncode == 0
        except Exception as e:
            self.logger.error(f"SSH check to {target_role} failed: {e}")
            return False
    
    def check_network_gateway(self):
        """Check if WiFi router is reachable"""
        try:
            result = subprocess.run(
                "ping -c 1 -W 2 192.168.1.1",
                shell=True, timeout=5,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            return result.returncode == 0
        except Exception:
            return False
    
    def attempt_nfs_remount(self):
        """Try to remount NFS if connection restored"""
        try:
            self.logger.info("Attempting NFS remount...")
            subprocess.run("sudo mount -a", shell=True, timeout=10)
            time.sleep(2)
            return self.check_nfs_mount()
        except Exception as e:
            self.logger.error(f"Remount failed: {e}")
            return False
    
    def check(self):
        """
        Main health check function
        Returns current NetworkStatus
        """
        # Check NFS first
        if self.check_nfs_mount():
            if self.status != NetworkStatus.PRIMARY:
                self.logger.info("NFS restored! Returning to PRIMARY mode")
            self.status = NetworkStatus.PRIMARY
            self.failure_start = None
            self.retry_count = 0
            return self.status
        
        # NFS failed, start tracking failure time
        if self.failure_start is None:
            self.failure_start = time.time()
            self.logger.warning("NFS failure detected, starting fallback procedures")
        
        failure_duration = time.time() - self.failure_start
        
        # Within first 30 seconds: Try remounting, check SSH
        if failure_duration < self.NFS_TIMEOUT:
            self.status = NetworkStatus.DEGRADED
            self.logger.info(f"DEGRADED mode: Retrying NFS ({failure_duration:.0f}s)")
            
            # Try remount every 10 seconds
            if self.retry_count % 2 == 0:
                if self.attempt_nfs_remount():
                    return self.check()  # Re-check after remount
            
            self.retry_count += 1
            time.sleep(self.RETRY_INTERVAL)
            return self.status
        
        # 30s-120s: Use SSH fallback if available
        if failure_duration < self.TOTAL_TIMEOUT:
            # Check if SSH fallback is possible
            target = "jetson_b" if self.role != "jetson_b" else "jetson_a"
            if self.check_ssh_connectivity(target):
                if self.status != NetworkStatus.SECONDARY:
                    self.logger.warning("Switched to SECONDARY mode (SSH fallback)")
                self.status = NetworkStatus.SECONDARY
                
                # Continue retrying NFS in background
                if self.retry_count % 4 == 0:
                    if self.attempt_nfs_remount():
                        return self.check()
                
                self.retry_count += 1
                time.sleep(self.RETRY_INTERVAL)
                return self.status
            else:
                # SSH also failed, still in DEGRADED
                self.status = NetworkStatus.DEGRADED
                self.logger.warning(f"SSH fallback unavailable ({failure_duration:.0f}s)")
                time.sleep(self.RETRY_INTERVAL)
                return self.status
        
        # Over 2 minutes: Hard stop
        self.logger.critical("Network failure timeout reached. STOPPING operations.")
        self.status = NetworkStatus.STOPPED
        return self.status
    
    def display_error(self):
        """Display error screen based on device role"""
        if self.role == "jetson_a":
            self._display_error_jetson_a()
        elif self.role == "jetson_b":
            self._display_error_jetson_b()
        elif self.role == "rpi4":
            self._display_error_rpi4()
    
    def _display_error_jetson_a(self):
        """Error display for gaze painter (2 windows)"""
        import cv2
        import numpy as np
        
        # Create error screens
        error_eyes = self._create_error_image((400, 200), 
            "Connection Lost",
            "Please contact staff")
        
        error_canvas = self._create_error_image((1280, 720),
            "System Offline",
            "We're experiencing technical difficulties.\nPlease try again later.")
        
        # Display on both windows
        cv2.namedWindow("Eyes")
        cv2.namedWindow("Gaze Painter")
        
        cv2.imshow("Eyes", error_eyes)
        cv2.imshow("Gaze Painter", error_canvas)
        
        self.logger.info("Error screens displayed on Jetson A")
        
        # Wait for manual intervention (or 'q' key)
        while True:
            key = cv2.waitKey(1000) & 0xFF
            if key == ord('q'):
                break
            # Check if connection restored
            if self.check_nfs_mount():
                self.logger.info("Connection restored! Exiting error mode.")
                cv2.destroyAllWindows()
                return True
        
        cv2.destroyAllWindows()
        return False
    
    def _display_error_jetson_b(self):
        """Error handling for analysis watcher"""
        # Jetson B doesn't have displays, just log
        self.logger.critical("Analysis watcher STOPPED. Waiting for manual restart.")
        
        # Create a marker file that kui/ can detect
        marker_path = "~/art/logs/SYSTEM_STOPPED.flag"
        with open(marker_path, 'w') as f:
            f.write(f"System stopped at {datetime.now()}\n")
            f.write("Reason: Network failure timeout\n")
        
        # Keep monitoring for recovery
        self.logger.info("Monitoring for network recovery...")
        while True:
            time.sleep(30)
            if self.check_nfs_mount():
                self.logger.info("Network restored! Remove SYSTEM_STOPPED.flag to resume.")
                # Don't auto-resume, wait for manual flag removal
                if not os.path.exists(marker_path):
                    return True
    
    def _display_error_rpi4(self):
        """Error display for print kiosk (touch screen)"""
        # This will be implemented in the RPi print UI
        # For now, create a marker file
        self.logger.critical("Print service STOPPED")
        
        marker_path = "~/art/logs/PRINT_SERVICE_STOPPED.flag"
        with open(marker_path, 'w') as f:
            f.write(f"Print service stopped at {datetime.now()}\n")
            f.write("Reason: Cannot access shared documents\n")
        
        # The PyQt5 touch UI will detect this flag and show error screen
        # (Implementation in separate print_service.py)
        
    def _create_error_image(self, size, title, message):
        """Helper to create error overlay image"""
        import cv2
        import numpy as np
        
        width, height = size
        img = np.ones((height, width, 3), dtype=np.uint8) * 240  # Light gray bg
        
        # Draw dark overlay
        overlay = img.copy()
        cv2.rectangle(overlay, (0, 0), (width, height), (40, 40, 40), -1)
        cv2.addWeighted(overlay, 0.7, img, 0.3, 0, img)
        
        # Title
        title_font = cv2.FONT_HERSHEY_BOLD
        title_scale = 1.5
        title_thickness = 3
        (tw, th), _ = cv2.getTextSize(title, title_font, title_scale, title_thickness)
        title_x = (width - tw) // 2
        title_y = height // 3
        cv2.putText(img, title, (title_x, title_y), title_font, 
                    title_scale, (255, 80, 80), title_thickness)
        
        # Message (multi-line)
        msg_font = cv2.FONT_HERSHEY_SIMPLEX
        msg_scale = 0.8
        msg_thickness = 2
        lines = message.split('\n')
        y_offset = height // 2
        
        for line in lines:
            (mw, mh), _ = cv2.getTextSize(line, msg_font, msg_scale, msg_thickness)
            msg_x = (width - mw) // 2
            cv2.putText(img, line, (msg_x, y_offset), msg_font, 
                        msg_scale, (255, 255, 255), msg_thickness)
            y_offset += mh + 20
        
        return img
    
    def get_status_color(self):
        """Return color code for status indicator"""
        colors = {
            NetworkStatus.PRIMARY: (0, 255, 0),    # Green
            NetworkStatus.SECONDARY: (0, 255, 255), # Yellow
            NetworkStatus.DEGRADED: (0, 165, 255),  # Orange
            NetworkStatus.STOPPED: (0, 0, 255)      # Red
        }
        return colors.get(self.status, (128, 128, 128))


# ============= Integration Example =============

def integrate_into_gaze_painter():
    """
    Example of how to integrate into run_gaze_painter.py
    """
    
    # At the top of GazeTrackingPainter.__init__():
    from scripts.network_health import NetworkHealth, NetworkStatus
    
    self.network_health = NetworkHealth(role="jetson_a")
    
    # In the main run() loop, add health check:
    # while self.running:
    #     # ... existing code ...
    #     
    #     # Check network health every 5 seconds
    #     if frame_count % 150 == 0:  # ~5 seconds at 30fps
    #         status = self.network_health.check()
    #         
    #         if status == NetworkStatus.STOPPED:
    #             print("Network failure - stopping operations")
    #             self.network_health.display_error()
    #             self.running = False
    #             break
    #         
    #         elif status == NetworkStatus.SECONDARY:
    #             print("Warning: Using SSH fallback")
    #             # Could show a small warning indicator
    #     
    #     # ... rest of loop ...


def integrate_into_analysis_watcher():
    """
    Example of how to integrate into run_analysis_watcher.py
    """
    
    from scripts.network_health import NetworkHealth, NetworkStatus
    
    health = NetworkHealth(role="jetson_b")
    
    # In the file watch loop:
    # while True:
    #     # Check health before processing
    #     status = health.check()
    #     
    #     if status == NetworkStatus.STOPPED:
    #         print("Network failure - stopping watcher")
    #         health.display_error()
    #         break
    #     
    #     if os.path.exists(image_path):
    #         # ... run analysis ...
    #         pass
    #     
    #     time.sleep(1)


def integrate_into_rpi_print_service():
    """
    Example for RPi print kiosk (to be implemented)
    """
    
    from scripts.network_health import NetworkHealth, NetworkStatus
    
    health = NetworkHealth(role="rpi4", nfs_mount="~/art/shared_output")
    
    # In PyQt5 UI main loop:
    # def check_network_status(self):
    #     status = self.health.check()
    #     
    #     if status == NetworkStatus.STOPPED:
    #         self.show_error_screen()
    #         self.stop_monitoring()
    #     elif status == NetworkStatus.SECONDARY:
    #         self.show_warning_indicator()
    #     else:
    #         self.hide_warning_indicator()


# ============= Standalone Test =============

if __name__ == "__main__":
    print("Network Health Check - Standalone Test")
    print("=" * 50)
    
    # Simulate health check
    health = NetworkHealth(role="jetson_a")
    
    print(f"NFS Mount: {health.nfs_mount}")
    print(f"Testing connectivity...\n")
    
    while True:
        status = health.check()
        
        status_msg = {
            NetworkStatus.PRIMARY: "✓ PRIMARY - All systems operational",
            NetworkStatus.SECONDARY: "⚠ SECONDARY - Using SSH fallback",
            NetworkStatus.DEGRADED: "⚠ DEGRADED - Retrying connection...",
            NetworkStatus.STOPPED: "✗ STOPPED - System offline"
        }
        
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {status_msg[status]}")
        
        if status == NetworkStatus.STOPPED:
            print("\nDisplaying error screen...")
            health.display_error()
            break
        
        time.sleep(5)