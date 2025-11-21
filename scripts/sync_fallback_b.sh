#!/bin/bash
# Rsync fallback sync script for Jetson B
# Pulls images from Jetson A if NFS fails

SHARED_DIR="~/art/LatentSpace/shared_output"
JETSON_A="art@192.168.1.10"
SSH_KEY="~/art/.ssh/id_rsa_sync"

# Sync image.png from Jetson A
rsync -avz -e "ssh -i $SSH_KEY -o StrictHostKeyChecking=no" \
    "$JETSON_A:~/art/LatentSpace/shared_output/image.png" \
    "$SHARED_DIR/" \
    2>/dev/null

# Sync heatmap.png
rsync -avz -e "ssh -i $SSH_KEY -o StrictHostKeyChecking=no" \
    "$JETSON_A:~/art/LatentSpace/shared_output/heatmap.png" \
    "$SHARED_DIR/" \
    2>/dev/null

exit 0