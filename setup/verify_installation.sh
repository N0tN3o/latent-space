#!/bin/bash
#
# Verification Script
# Checks network (ping all peers), services, and NFS
#

GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

pass() { echo -e "${GREEN}✓${NC} $1"; }
fail() { echo -e "${RED}✗${NC} $1"; }

TARGET_USER="kasboek"
PROJECT_DIR="/home/$TARGET_USER/LatentSpace"

echo "=== System Verification for $TARGET_USER ==="

# 1. Identify Device
if [ -d "$PROJECT_DIR/gaze-painter" ] && [ ! -d "$PROJECT_DIR/analysis_watcher" ]; then
    ROLE="JETSON_A"
    MY_IP="192.168.1.10"
elif [ -d "$PROJECT_DIR/analysis_watcher" ]; then
    ROLE="JETSON_B"
    MY_IP="192.168.1.11"
elif [ -d "$PROJECT_DIR/print_service" ]; then
    ROLE="RPI4"
    MY_IP="192.168.1.12"
else
    fail "Could not identify device role based on folder structure."
    exit 1
fi
echo "Identified Role: $ROLE"

# 2. Check IP
CURRENT_IP=$(hostname -I)
if [[ "$CURRENT_IP" == *"$MY_IP"* ]]; then
    pass "IP matches expected ($MY_IP)"
else
    fail "IP mismatch! Expected $MY_IP, found $CURRENT_IP"
fi

# 3. Network Ping (Check ALL other devices)
echo "--- Network Check ---"
PEERS=("192.168.1.10" "192.168.1.11" "192.168.1.12")
for IP in "${PEERS[@]}"; do
    if [[ "$CURRENT_IP" == *"$IP"* ]]; then
        continue # Skip self
    fi
    if ping -c 1 -W 1 "$IP" >/dev/null; then
        pass "Ping to $IP successful"
    else
        fail "Ping to $IP FAILED"
    fi
done

# 4. NFS Check
echo "--- Storage Check ---"
if [ "$ROLE" == "JETSON_B" ]; then
    if showmount -e localhost | grep -q "shared_output"; then
        pass "NFS Export active"
    else
        fail "NFS Export missing"
    fi
else
    # Clients
    if mount | grep -q "$PROJECT_DIR/shared_output"; then
        pass "NFS Mount active"
    else
        fail "NFS Not mounted"
        echo "Trying to mount now..."
        sudo mount -a
    fi
fi

# 5. Service Check
echo "--- Service Check ---"
check_svc() {
    if systemctl is-active --quiet "$1"; then
        pass "Service $1 is running"
    else
        fail "Service $1 is NOT running"
    fi
}

case "$ROLE" in
    JETSON_A)
        check_svc "gaze-painter"
        ;;
    JETSON_B)
        check_svc "analysis-watcher"
        check_svc "web-server"
        check_svc "monitoring-server"
        ;;
    RPI4)
        check_svc "print-service"
        check_svc "cups"
        ;;
esac

echo "=== Verification Complete ==="