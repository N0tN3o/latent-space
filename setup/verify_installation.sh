#!/bin/bash
#
# Installation Verification Script
# Run on each device after setup to verify configuration
#

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

pass() {
    echo -e "${GREEN}✓${NC} $1"
}

fail() {
    echo -e "${RED}✗${NC} $1"
}

warn() {
    echo -e "${YELLOW}⚠${NC} $1"
}

echo "=========================================="
echo "Installation Verification"
echo "=========================================="
echo ""

# Detect device type
if [ -d "~/art/LatentSpace/gaze-painter" ]; then
    DEVICE="jetson_a"
    echo "Device: Jetson A (Gaze Painter)"
elif [ -d "~/art/LatentSpace/analysis_watcher" ]; then
    DEVICE="jetson_b"
    echo "Device: Jetson B (Analysis Watcher)"
elif [ -d "~/art/LatentSpace/print_service" ]; then
    DEVICE="rpi4"
    echo "Device: Raspberry Pi 4 (Print Service)"
else
    fail "Unknown device or incomplete installation"
    exit 1
fi

echo ""
echo "Running checks..."
echo ""

# Check 1: Python environment
echo "1. Python Environment:"
if [ -d "~/art/LatentSpace/$DEVICE/venv" ] || \
   [ -d "~/art/LatentSpace/gaze-painter/venv" ] || \
   [ -d "~/art/LatentSpace/analysis_watcher/venv" ] || \
   [ -d "~/art/LatentSpace/print_service/venv" ]; then
    pass "Virtual environment exists"
else
    fail "Virtual environment not found"
fi

# Check 2: NFS mount
echo "2. NFS Configuration:"
if mount | grep -q "shared_output"; then
    pass "NFS mounted successfully"
    mount | grep shared_output
else
    if [ "$DEVICE" = "jetson_b" ]; then
        warn "NFS mount not expected on server (Jetson B)"
    else
        fail "NFS not mounted"
        echo "   Try: sudo mount -a"
    fi
fi

# Check 3: SSH keys
echo "3. SSH Keys:"
if [ -f "~/art/.ssh/id_rsa_sync" ]; then
    pass "Sync SSH key exists"
else
    fail "Sync SSH key not found"
fi

# Check 4: Network connectivity
echo "4. Network Connectivity:"
if ping -c 1 -W 2 192.168.1.11 > /dev/null 2>&1; then
    pass "Can reach Jetson B (192.168.1.11)"
else
    fail "Cannot reach Jetson B"
fi

if [ "$DEVICE" != "jetson_b" ]; then
    if ping -c 1 -W 2 192.168.1.10 > /dev/null 2>&1; then
        pass "Can reach Jetson A (192.168.1.10)"
    fi
fi

if [ "$DEVICE" = "jetson_b" ]; then
    if ping -c 1 -W 2 192.168.1.12 > /dev/null 2>&1; then
        pass "Can reach RPi4 (192.168.1.12)"
    fi
fi

# Check 5: Services
echo "5. Systemd Services:"
case "$DEVICE" in
    jetson_a)
        if systemctl is-enabled gaze-painter > /dev/null 2>&1; then
            pass "gaze-painter service enabled"
        else
            fail "gaze-painter service not enabled"
        fi
        
        if systemctl is-active gaze-painter > /dev/null 2>&1; then
            pass "gaze-painter service running"
        else
            warn "gaze-painter service not running (start after reboot)"
        fi
        ;;
    
    jetson_b)
        if systemctl is-enabled analysis-watcher > /dev/null 2>&1; then
            pass "analysis-watcher service enabled"
        else
            fail "analysis-watcher service not enabled"
        fi
        
        if systemctl is-enabled web-server > /dev/null 2>&1; then
            pass "web-server service enabled"
        else
            fail "web-server service not enabled"
        fi
        
        if systemctl is-enabled nfs-kernel-server > /dev/null 2>&1; then
            pass "NFS server enabled"
        else
            fail "NFS server not enabled"
        fi
        ;;
    
    rpi4)
        if systemctl is-enabled print-service > /dev/null 2>&1; then
            pass "print-service enabled"
        else
            fail "print-service not enabled"
        fi
        
        if systemctl is-enabled cups > /dev/null 2>&1; then
            pass "CUPS printing service enabled"
        else
            fail "CUPS not enabled"
        fi
        ;;
esac

# Check 6: Firewall
echo "6. Firewall:"
if iptables -L -n | grep -q "DROP"; then
    pass "Firewall rules active"
else
    warn "Firewall may not be configured"
fi

# Check 7: Logs directory
echo "7. Logs:"
if [ -d "~/art/LatentSpace/logs" ]; then
    pass "Logs directory exists"
    ls -lh ~/art/LatentSpace/logs/
else
    fail "Logs directory missing"
fi

# Check 8: Device-specific checks
echo "8. Device-Specific:"
case "$DEVICE" in
    jetson_a)
        # Check camera
        if [ -e /dev/video0 ]; then
            pass "Camera device /dev/video0 found"
        else
            fail "Camera not detected"
        fi
        
        # Check displays
        DISPLAY_COUNT=$(xrandr --listmonitors 2>/dev/null | grep -c "Monitor")
        if [ "$DISPLAY_COUNT" -ge 2 ]; then
            pass "Dual displays detected ($DISPLAY_COUNT monitors)"
        elif [ "$DISPLAY_COUNT" -eq 1 ]; then
            warn "Only 1 display detected (need 2)"
        else
            fail "No displays detected"
        fi
        ;;
    
    jetson_b)
        # Check NFS exports
        if showmount -e localhost | grep -q "shared_output"; then
            pass "NFS export active"
        else
            fail "NFS export not found"
        fi
        
        # Check if Playwright installed
        if [ -f "~/art/LatentSpace/analysis_watcher/venv/bin/playwright" ]; then
            pass "Playwright installed"
        else
            warn "Playwright may not be installed"
        fi
        ;;
    
    rpi4)
        # Check printer
        if lpstat -p 2>/dev/null | grep -q "printer"; then
            pass "Printer configured"
            lpstat -p
        else
            warn "No printer configured yet"
            echo "   Configure at: http://192.168.1.12:631"
        fi
        
        # Check touchscreen
        if [ -e /dev/input/event0 ]; then
            pass "Input device found (touchscreen)"
        else
            warn "Touchscreen may not be detected"
        fi
        ;;
esac

# Summary
echo ""
echo "=========================================="
echo "Verification Summary"
echo "=========================================="

if [ "$?" -eq 0 ]; then
    echo -e "${GREEN}Installation appears correct!${NC}"
    echo ""
    echo "Next steps:"
    case "$DEVICE" in
        jetson_a)
            echo "  1. Ensure camera is connected"
            echo "  2. Connect two displays"
            echo "  3. Reboot to start service"
            ;;
        jetson_b)
            echo "  1. Ensure Jetson A can mount NFS"
            echo "  2. Reboot to start services"
            ;;
        rpi4)
            echo "  1. Connect USB printer"
            echo "  2. Configure printer at http://192.168.1.12:631"
            echo "  3. Reboot to start kiosk"
            ;;
    esac
else
    echo -e "${YELLOW}Some issues detected - review failures above${NC}"
fi

echo ""