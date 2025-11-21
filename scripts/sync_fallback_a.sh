#!/bin/bash
# Rsync fallback for Jetson A
# Pushes files to Jetson B if NFS fails

SHARED_DIR="/home/art/LatentSpace/shared_output"
JETSON_B="art@192.168.1.11"
SSH_KEY="/home/art/.ssh/id_rsa_sync"

# Push image.png to Jetson B
rsync -avz -e "ssh -i $SSH_KEY -o StrictHostKeyChecking=no" \
    "$SHARED_DIR/image.png" \
    "$JETSON_B:/home/art/LatentSpace/shared_output/" \
    2>/dev/null

exit 0