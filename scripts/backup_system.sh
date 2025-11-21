#!/bin/bash
#
# Latent Space - Automatic Backup Script
# Runs via systemd timer on Jetson B
#

LOG_FILE="/home/art/LatentSpace/logs/backup.log"
BACKUP_DIR="/mnt/backup_drive/latent_space_backups" # <-- MUST BE MOUNTED
SSH_KEY="/home/art/.ssh/id_rsa_sync"
USER="art"
JETSON_A="192.168.1.10"
RPI4="192.168.1.12"

# Function to log messages
log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') - $1" | tee -a "$LOG_FILE"
}

log "====== Backup Started ======"

# Check if backup drive is mounted
if ! mount | grep -q "$BACKUP_DIR"; then
    log "[ERROR] Backup drive not mounted at $BACKUP_DIR. Aborting."
    exit 1
fi

# --- 1. Backup Shared Data (NFS Share) ---
log "Backing up shared_output..."
mkdir -p "$BACKUP_DIR/shared_output"
rsync -avz --delete \
    "/home/art/LatentSpace/shared_output/" \
    "$BACKUP_DIR/shared_output/" \
    >> "$LOG_FILE" 2>&1

# --- 2. Backup Jetson B Code (Local) ---
log "Backing up Jetson B code..."
mkdir -p "$BACKUP_DIR/code/jetson_b"
rsync -avz --delete \
    --exclude '.git' \
    --exclude '**/__pycache__' \
    --exclude '**/*.pyc' \
    --exclude 'venv' \
    "/home/art/LatentSpace/" \
    "$BACKUP_DIR/code/jetson_b/" \
    >> "$LOG_FILE" 2>&1

# --- 3. Backup Jetson A Code (Remote) ---
log "Backing up Jetson A code..."
mkdir -p "$BACKUP_DIR/code/jetson_a"
rsync -avz --delete \
    -e "ssh -i $SSH_KEY -o StrictHostKeyChecking=no" \
    --exclude '.git' \
    --exclude '**/__pycache__' \
    --exclude '**/*.pyc' \
    --exclude 'venv' \
    "$USER@$JETSON_A:/home/art/LatentSpace/" \
    "$BACKUP_DIR/code/jetson_a/" \
    >> "$LOG_FILE" 2>&1

# --- 4. Backup RPi4 Code (Remote) ---
log "Backing up RPi4 code..."
mkdir -p "$BACKUP_DIR/code/rpi4"
rsync -avz --delete \
    -e "ssh -i $SSH_KEY -o StrictHostKeyChecking=no" \
    --exclude '.git' \
    --exclude '**/__pycache__' \
    --exclude '**/*.pyc' \
    --exclude 'venv' \
    "$USER@$RPI4:/home/art/LatentSpace/" \
    "$BACKUP_DIR/code/rpi4/" \
    >> "$LOG_FILE" 2>&1

# --- 5. Prune old backups (Optional) ---
# This simple example just keeps one copy.
# For daily snapshots, you would rsync to a dated folder
# and use 'find' to delete folders older than 7 days.

# Prune old PDFs (older than 1 day)
find /home/art/LatentSpace/shared_output/docs -name "print_*.pdf" -mtime +1 -delete
log "Pruned old PDF prints."

log "====== Backup Finished ======"
exit 0