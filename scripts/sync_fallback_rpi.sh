#!/bin/bash
# Rsync fallback for RPi4 - pulls PDFs from Jetson B
SHARED_DIR="/home/kasboek/LatentSpace/shared_output"
JETSON_B="kasboek@192.168.1.11"
SSH_KEY="/home/kasboek/.ssh/id_rsa_sync"

rsync -avz -e "ssh -i $SSH_KEY -o StrictHostKeyChecking=no" \
    "$JETSON_B:/home/kasboek/LatentSpace/shared_output/docs/" \
    "$SHARED_DIR/docs/" \
    2>/dev/null

exit 0