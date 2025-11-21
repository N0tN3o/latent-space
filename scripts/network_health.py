"""
Network Health Check & Error Display Module
For Latent Space Installation

Monitors NFS mount health and provides fallback mechanisms.
Displays error screens when network failures exceed timeout.

Usage:
    from network_health import NetworkHealth, NetworkStatus
    
    health = NetworkHealth(role="jetson_a")  # or "jetson_b", "rpi4"
    
    while running:
        status = health.check()
        if status == NetworkStatus.STOPPED:
            health.display_error()
            break

Author: Latent Space Team
Version: 1.0
"""

import os
import time
import subprocess
import logging
from datetime import datetime
from enum import Enum
from pathlib import Path

class NetworkStatus(Enum):
    """Network health status levels"""
    PRIMARY = "primary"      # NFS healthy, all systems go
    SECONDARY = "secondary"  # SSH fallback active
    DEGRADED = "degraded"    # Retrying connection
    STOPPED = "stopped"      # Hard stop, show error

class NetworkHealth:
    """
    Network health monitoring and fallback management.
    
    Checks NFS mount health and triggers SSH rsync fallback if needed.
    After timeout, displays error screen and stops operations.
    """
    
    def __init__(self, role, nfs_mount="~/art/LatentSpace/shared_output"):
        """
        Initialize network health checker
        
        Args:
            role (str): Device role - "jetson_a", "jetson_b", or "rpi4"
            nfs_mount (str): Path to NFS mount point
        """
        self.role = role
        self.nfs_mount = nfs_mount
        self.status = NetworkStatus.PRIMARY
        self.failure_start = None
        self.retry_count = 0
        
        # Configuration timeouts
        self.NFS_TIMEOUT = 30  # Seconds before trying SSH fallback
        self.TOTAL_TIMEOUT = 120  # Seconds before hard stop
        self.RETRY_INTERVAL = 5  # Seconds between retries
        
        # Target IPs for connectivity checks
        self.targets = {
            "jetson_a": "192.168.1.10",
            "jetson_b": "192.168.1.11",
            "rpi4": "192.168.1.12"
        }
        
        # Setup logging
        log_dir = Path("~/art/LatentSpace/logs")
        log_dir.mkdir(parents=True, exist_ok=True)
        
        logging.basicConfig(
            filename=log_dir / f'{role}_network.log',
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s'
        )
        self.logger = logging.getLogger(role)
        self.logger.info(f"NetworkHealth initialized for {role}")
    
    def check_nfs_mount(self):
        """
        Check if NFS mount is healthy and writable
        
        Returns:
            bool: True if NFS is mounted and writable, False otherwise
        """
        try:
            # Check if path is actually a mount point
            if not os.path.ismount(self.nfs_mount):
                self.logger.warning(f"NFS not mounted at {self.nfs_mount}")
                return False
            
            # Check if writable by creating test file
            test_file = Path(self.nfs_mount) / f".health_check_{self.role}"
            try:
                test_file.write_text(str(time.time()))
                test_file.unlink()  # Delete test file
                return True
            except (OSError, PermissionError) as e:
                self.logger.error(f"NFS mount not writable: {e}")
                return False
                
        except Exception as e:
            self.logger.error(f"NFS check failed: {e}")
            return False
    
    def check_ssh_connectivity(self, target_role):
        """
        Check if SSH to target device works
        
        Args:
            target_role (str): Target device role (e.g., "jetson_b")
            
        Returns:
            bool: True if SSH connection successful, False otherwise
        """
        target_ip = self.targets.get(target_role)
        if not target_ip:
            self.logger.warning(f"Unknown target role: {target_role}")
            return False
        
        try:
            # Quick SSH check with timeout
            cmd = [
                "ssh",
                "-q",  # Quiet mode
                "-o", "BatchMode=yes",  # No password prompt
                "-o", "ConnectTimeout=5",  # 5 second timeout
                "-o", "StrictHostKeyChecking=no",  # Don't prompt for host key
                f"art@{target_ip}",
                "exit"
            ]
            
            result = subprocess.run(
                cmd,
                timeout=10,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            
            if result.returncode == 0:
                return True
            else:
                self.logger.warning(f"SSH to {target_role} failed (exit code: {result.returncode})")
                return False
                
        except subprocess.TimeoutExpired:
            self.logger.error(f"SSH check to {target_role} timed out")
            return False
        except Exception as e:
            self.logger.error(f"SSH check to {target_role} failed: {e}")
            return False
    
    def check_network_gateway(self):
        """
        Check if WiFi router/gateway is reachable
        
        Returns:
            bool: True if gateway responds to ping, False otherwise
        """
        try:
            result = subprocess.run(
                ["ping", "-c", "1", "-W", "2", "192.168.1.1"],
                timeout=5,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            return result.returncode == 0
        except Exception as e:
            self.logger.error(f"Gateway check failed: {e}")
            return False
    
    def attempt_nfs_remount(self):
        """
        Try to remount NFS if connection was restored
        
        Returns:
            bool: True if remount successful, False otherwise
        """
        try:
            self.logger.info("Attempting NFS remount...")
            
            # Try mount -a (reads /etc/fstab)
            result = subprocess.run(
                ["sudo", "mount", "-a"],
                timeout=10,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            
            time.sleep(2)  # Give mount time to settle
            
            # Check if mount succeeded
            if self.check_nfs_mount():
                self.logger.info("NFS remount successful!")
                return True
            else:
                self.logger.warning("Remount command executed but mount still unhealthy")
                return False
                
        except subprocess.TimeoutExpired:
            self.logger.error("Remount command timed out")
            return False
        except Exception as e:
            self.logger.error(f"Remount failed: {e}")
            return False
    
    def trigger_rsync_fallback(self):
        """
        Trigger rsync fallback script
        
        Returns:
            bool: True if rsync executed successfully, False otherwise
        """
        sync_script = Path("~/art/LatentSpace/scripts/sync_fallback.sh")
        
        if not sync_script.exists():
            self.logger.error(f"Sync fallback script not found: {sync_script}")
            return False
        
        try:
            self.logger.info("Triggering rsync fallback...")
            result = subprocess.run(
                [str(sync_script)],
                timeout=30,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            
            if result.returncode == 0:
                self.logger.info("Rsync fallback completed successfully")
                return True
            else:
                self.logger.warning(f"Rsync fallback returned code {result.returncode}")
                return False
                
        except subprocess.TimeoutExpired:
            self.logger.error("Rsync fallback timed out")
            return False
        except Exception as e:
            self.logger.error(f"Rsync fallback failed: {e}")
            return False
    
    def check(self):
        """
        Main health check function - call this periodically
        
        Returns:
            NetworkStatus: Current network health status
        """
        # Check NFS first
        if self.check_nfs_mount():
            # NFS is healthy!
            if self.status != NetworkStatus.PRIMARY:
                self.logger.info("✓ NFS restored! Returning to PRIMARY mode")
            
            self.status = NetworkStatus.PRIMARY
            self.failure_start = None
            self.retry_count = 0
            return self.status
        
        # NFS failed - start tracking failure time
        if self.failure_start is None:
            self.failure_start = time.time()
            self.logger.warning("✗ NFS failure detected, starting fallback procedures")
        
        failure_duration = time.time() - self.failure_start
        
        # Phase 1: Within first 30 seconds - Try remounting
        if failure_duration < self.NFS_TIMEOUT:
            self.status = NetworkStatus.DEGRADED
            self.logger.info(f"⚠ DEGRADED mode: Retrying NFS ({failure_duration:.0f}s elapsed)")
            
            # Try remount every 10 seconds (every 2nd retry at 5s intervals)
            if self.retry_count % 2 == 0:
                if self.attempt_nfs_remount():
                    # Remount succeeded, re-check immediately
                    return self.check()
            
            self.retry_count += 1
            time.sleep(self.RETRY_INTERVAL)
            return self.status
        
        # Phase 2: 30s-120s - Use SSH fallback if available
        if failure_duration < self.TOTAL_TIMEOUT:
            # Determine target for fallback sync
            if self.role == "jetson_a":
                target = "jetson_b"  # Jetson A syncs to Jetson B
            elif self.role == "rpi4":
                target = "jetson_b"  # RPi syncs from Jetson B
            else:
                target = "jetson_a"  # Jetson B syncs from Jetson A
            
            # Check if SSH fallback is possible
            if self.check_ssh_connectivity(target):
                if self.status != NetworkStatus.SECONDARY:
                    self.logger.warning("⚠ Switched to SECONDARY mode (SSH fallback)")
                
                self.status = NetworkStatus.SECONDARY
                
                # Trigger rsync every 4th retry (every 20s)
                if self.retry_count % 4 == 0:
                    self.trigger_rsync_fallback()
                
                # Continue trying NFS remount in background
                if self.retry_count % 6 == 0:
                    if self.attempt_nfs_remount():
                        return self.check()
                
                self.retry_count += 1
                time.sleep(self.RETRY_INTERVAL)
                return self.status
            else:
                # SSH also failed, still in DEGRADED
                self.status = NetworkStatus.DEGRADED
                self.logger.warning(f"⚠ SSH fallback unavailable ({failure_duration:.0f}s elapsed)")
                time.sleep(self.RETRY_INTERVAL)
                return self.status
        
        # Phase 3: Over 2 minutes - Hard stop
        self.logger.critical("✗ Network failure timeout reached. STOPPING operations.")
        self.status = NetworkStatus.STOPPED
        return self.status
    
    def display_error(self):
        """Display appropriate error screen based on device role"""
        self.logger.critical(f"Displaying error screen for {self.role}")
        
        if self.role == "jetson_a":
            return self._display_error_jetson_a()
        elif self.role == "jetson_b":
            return self._display_error_jetson_b()
        elif self.role == "rpi4":
            return self._display_error_rpi4()
    
    def _display_error_jetson_a(self):
        """Error display for gaze painter (2 windows with OpenCV)"""
        try:
            import cv2
            import numpy as np
        except ImportError:
            self.logger.error("OpenCV not available, cannot display error screen")
            return False
        
        # Create error screens
        error_eyes = self._create_error_image(
            (400, 200),
            "Connection Lost",
            "Please contact staff"
        )
        
        error_canvas = self._create_error_image(
            (1280, 720),
            "System Offline",
            "We're experiencing technical difficulties.\nPlease try again later."
        )
        
        # Display on both windows
        cv2.namedWindow("Eyes", cv2.WINDOW_NORMAL)
        cv2.namedWindow("Gaze Painter", cv2.WINDOW_NORMAL)
        
        cv2.imshow("Eyes", error_eyes)
        cv2.imshow("Gaze Painter", error_canvas)
        
        self.logger.info("Error screens displayed on Jetson A")
        
        # Wait for manual intervention or recovery
        while True:
            key = cv2.waitKey(1000) & 0xFF
            
            # Allow 'q' key to exit
            if key == ord('q'):
                self.logger.info("Manual exit via 'q' key")
                break
            
            # Check if connection restored
            if self.check_nfs_mount():
                self.logger.info("✓ Connection restored! Exiting error mode.")
                cv2.destroyAllWindows()
                return True
        
        cv2.destroyAllWindows()
        return False
    
    def _display_error_jetson_b(self):
        """Error handling for analysis watcher (no display)"""
        # Jetson B doesn't have displays, just log and create marker
        self.logger.critical("Analysis watcher STOPPED. Waiting for manual restart.")
        
        # Create marker file that can be detected by monitoring tools
        marker_path = Path("~/art/LatentSpace/logs/SYSTEM_STOPPED.flag")
        with open(marker_path, 'w') as f:
            f.write(f"System stopped at {datetime.now()}\n")
            f.write(f"Reason: Network failure timeout ({self.TOTAL_TIMEOUT}s)\n")
            f.write(f"Role: {self.role}\n")
            f.write(f"NFS Mount: {self.nfs_mount}\n")
        
        self.logger.info(f"Created stop marker: {marker_path}")
        
        # Keep monitoring for recovery
        self.logger.info("Monitoring for network recovery...")
        
        while True:
            time.sleep(30)
            
            # Check if NFS is restored
            if self.check_nfs_mount():
                self.logger.info("✓ Network restored!")
                
                # Wait for manual flag removal before resuming
                if not marker_path.exists():
                    self.logger.info("Stop marker removed, ready to resume")
                    return True
                else:
                    self.logger.info("Waiting for manual flag removal to resume...")
    
    def _display_error_rpi4(self):
        """Error display for print kiosk (handled by PyQt5 UI)"""
        # Create marker file that PyQt5 UI will detect
        self.logger.critical("Print service STOPPED")
        
        marker_path = Path("~/art/LatentSpace/logs/PRINT_SERVICE_STOPPED.flag")
        with open(marker_path, 'w') as f:
            f.write(f"Print service stopped at {datetime.now()}\n")
            f.write(f"Reason: Cannot access shared documents\n")
            f.write(f"NFS Mount: {self.nfs_mount}\n")
        
        self.logger.info(f"Created print service stop marker: {marker_path}")
        
        # The PyQt5 UI will detect this flag and show error screen
        # This function just maintains the flag
        
        while True:
            time.sleep(30)
            
            if self.check_nfs_mount():
                self.logger.info("✓ Network restored for print service")
                
                # Remove marker to allow UI to resume
                if marker_path.exists():
                    marker_path.unlink()
                    self.logger.info("Removed stop marker, print service can resume")
                    return True
    
    def _create_error_image(self, size, title, message):
        """
        Helper to create error overlay image with OpenCV
        
        Args:
            size (tuple): Image size (width, height)
            title (str): Error title text
            message (str): Error message text (can be multi-line)
            
        Returns:
            numpy.ndarray: Error image
        """
        import cv2
        import numpy as np
        
        width, height = size
        
        # Create light gray background
        img = np.ones((height, width, 3), dtype=np.uint8) * 240
        
        # Draw dark semi-transparent overlay
        overlay = img.copy()
        cv2.rectangle(overlay, (0, 0), (width, height), (40, 40, 40), -1)
        cv2.addWeighted(overlay, 0.7, img, 0.3, 0, img)
        
        # Draw title
        title_font = cv2.FONT_HERSHEY_BOLD
        title_scale = min(1.5, width / 300)  # Scale based on width
        title_thickness = 3
        
        (tw, th), _ = cv2.getTextSize(title, title_font, title_scale, title_thickness)
        title_x = (width - tw) // 2
        title_y = height // 3
        
        cv2.putText(img, title, (title_x, title_y), title_font,
                    title_scale, (255, 80, 80), title_thickness)
        
        # Draw message (multi-line support)
        msg_font = cv2.FONT_HERSHEY_SIMPLEX
        msg_scale = min(0.8, width / 400)
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
        """
        Return BGR color code for status indicator (for OpenCV overlays)
        
        Returns:
            tuple: BGR color (B, G, R)
        """
        colors = {
            NetworkStatus.PRIMARY: (0, 255, 0),      # Green
            NetworkStatus.SECONDARY: (0, 255, 255),  # Yellow
            NetworkStatus.DEGRADED: (0, 165, 255),   # Orange
            NetworkStatus.STOPPED: (0, 0, 255)       # Red
        }
        return colors.get(self.status, (128, 128, 128))
    
    def get_status_string(self):
        """
        Get human-readable status string
        
        Returns:
            str: Status description
        """
        status_strings = {
            NetworkStatus.PRIMARY: "✓ All systems operational",
            NetworkStatus.SECONDARY: "⚠ Using backup connection",
            NetworkStatus.DEGRADED: "⚠ Reconnecting...",
            NetworkStatus.STOPPED: "✗ System offline"
        }
        return status_strings.get(self.status, "Unknown")


# ============= Convenience Functions =============

def create_network_health(role):
    """
    Factory function to create NetworkHealth instance
    
    Args:
        role (str): "jetson_a", "jetson_b", or "rpi4"
        
    Returns:
        NetworkHealth: Configured health checker
    """
    return NetworkHealth(role=role)


def check_network_health_once(role):
    """
    One-time network health check (useful for scripts)
    
    Args:
        role (str): "jetson_a", "jetson_b", or "rpi4"
        
    Returns:
        tuple: (status: NetworkStatus, message: str)
    """
    health = NetworkHealth(role=role)
    status = health.check()
    message = health.get_status_string()
    return status, message


# ============= Standalone Test =============

if __name__ == "__main__":
    import sys
    
    print("=" * 60)
    print("Network Health Check - Standalone Test")
    print("=" * 60)
    print()
    
    # Determine role from command line or default
    if len(sys.argv) > 1:
        role = sys.argv[1]
    else:
        print("Usage: python3 network_health.py <role>")
        print("       role: jetson_a, jetson_b, or rpi4")
        print()
        print("Running test as 'jetson_a' by default...")
        role = "jetson_a"
    
    # Create health checker
    health = NetworkHealth(role=role)
    
    print(f"Role: {role}")
    print(f"NFS Mount: {health.nfs_mount}")
    print(f"Testing connectivity...")
    print()
    
    # Run continuous check
    try:
        check_count = 0
        while True:
            status = health.check()
            check_count += 1
            
            timestamp = datetime.now().strftime('%H:%M:%S')
            color_code = {
                NetworkStatus.PRIMARY: "\033[92m",     # Green
                NetworkStatus.SECONDARY: "\033[93m",   # Yellow
                NetworkStatus.DEGRADED: "\033[91m",    # Red
                NetworkStatus.STOPPED: "\033[91m\033[1m"  # Bold Red
            }.get(status, "\033[0m")
            
            reset_code = "\033[0m"
            
            print(f"[{timestamp}] Check #{check_count}: {color_code}{health.get_status_string()}{reset_code}")
            
            if status == NetworkStatus.STOPPED:
                print()
                print("=" * 60)
                print("STOPPED state reached. Displaying error screen...")
                print("=" * 60)
                health.display_error()
                break
            
            time.sleep(5)
            
    except KeyboardInterrupt:
        print()
        print()
        print("Test interrupted by user")
        print(f"Final status: {health.get_status_string()}")