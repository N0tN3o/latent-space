#!/bin/bash
#
# Unified Sync Fallback Script
# Detects device role and executes appropriate sync
#

SCRIPTS_DIR="/home/art/LatentSpace/scripts"
HOSTNAME=$(hostname)

# Detect role by checking which directories exist
if [ -d "/home/art/LatentSpace/gaze-painter" ] || [[ "$HOSTNAME" == *"jetson-a"* ]]; then
    ROLE="jetson_a"
elif [ -d "/home/art/LatentSpace/analysis_watcher" ] || [[ "$HOSTNAME" == *"jetson-b"* ]]; then
    ROLE="jetson_b"
elif [ -d "/home/art/LatentSpace/print_service" ] || [[ "$HOSTNAME" == *"rpi"* ]]; then
    ROLE="rpi4"
else
    echo "ERROR: Cannot determine device role"
    exit 1
fi

# Execute role-specific sync
case $ROLE in
    jetson_a)
        bash "$SCRIPTS_DIR/sync_fallback_a.sh"
        ;;
    jetson_b)
        bash "$SCRIPTS_DIR/sync_fallback_b.sh"
        ;;
    rpi4)
        bash "$SCRIPTS_DIR/sync_fallback_rpi.sh"
        ;;
    *)
        echo "ERROR: Unknown role: $ROLE"
        exit 1
        ;;
esac

exit $?