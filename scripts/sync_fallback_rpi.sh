#!/bin/bash
# Rsync fallback for RPi4 - pulls PDFs from Jetson B
SHARED_DIR="~/art/LatentSpace/shared_output"
JETSON_B="art@192.168.1.11"
SSH_KEY="~/art/.ssh/id_rsa_sync"

rsync -avz -e "ssh -i $SSH_KEY -o StrictHostKeyChecking=no" \
    "$JETSON_B:~/art/LatentSpace/shared_output/docs/" \
    "$SHARED_DIR/docs/" \
    2>/dev/null

exit 0