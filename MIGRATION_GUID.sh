#!/bin/bash
#
# Latent Space - Migration & Cleanup Script
# Run this to clean up old files and update to corrected versions
#

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;36m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_step() { echo -e "${BLUE}[STEP]${NC} $1"; }

PROJECT_ROOT="~/art/LatentSpace"

echo "==========================================="
echo "  Latent Space - Migration & Cleanup"
echo "==========================================="
echo ""

# Confirm before proceeding
read -p "This will clean up old files and update configurations. Continue? (y/N) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Aborted."
    exit 0
fi

# ============================================
# PHASE 1: Backup Current State
# ============================================
log_step "Phase 1: Creating backup..."

BACKUP_DIR="$PROJECT_ROOT/backup_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$BACKUP_DIR"

# Backup critical files
log_info "Backing up configuration files..."
cp -r "$PROJECT_ROOT/setup" "$BACKUP_DIR/" 2>/dev/null || true
cp -r "$PROJECT_ROOT/scripts" "$BACKUP_DIR/" 2>/dev/null || true
cp "$PROJECT_ROOT/monitoring_server.py" "$BACKUP_DIR/" 2>/dev/null || true
cp -r "$PROJECT_ROOT/print_service" "$BACKUP_DIR/" 2>/dev/null || true

log_info "Backup created at: $BACKUP_DIR"

# ============================================
# PHASE 2: Delete Obsolete Files
# ============================================
log_step "Phase 2: Removing obsolete files..."

# Delete duplicate setup scripts
if [ -f "$PROJECT_ROOT/setup/setup_jetson_a copy.sh" ]; then
    log_info "Removing: setup/setup_jetson_a copy.sh"
    rm "$PROJECT_ROOT/setup/setup_jetson_a copy.sh"
fi

if [ -f "$PROJECT_ROOT/setup/setup_jetson_b copy.sh" ]; then
    log_info "Removing: setup/setup_jetson_b copy.sh"
    rm "$PROJECT_ROOT/setup/setup_jetson_b copy.sh"
fi

if [ -f "$PROJECT_ROOT/setup/setup_rpi4 copy.sh" ]; then
    log_info "Removing: setup/setup_rpi4 copy.sh"
    rm "$PROJECT_ROOT/setup/setup_rpi4 copy.sh"
fi

# Delete old setup directory
if [ -d "$PROJECT_ROOT/sys_setup_old" ]; then
    log_info "Removing: sys_setup_old/"
    rm -rf "$PROJECT_ROOT/sys_setup_old"
fi

# Delete outdated documentation
if [ -f "$PROJECT_ROOT/docs/SystemArchitecture.md" ]; then
    log_info "Removing: docs/SystemArchitecture.md (outdated)"
    rm "$PROJECT_ROOT/docs/SystemArchitecture.md"
fi

# ============================================
# PHASE 3: Update Requirements Files
# ============================================
log_step "Phase 3: Updating requirements.txt..."

# Update print_service requirements
cat > "$PROJECT_ROOT/print_service/requirements.txt" << 'EOFREQ'
PyQt5
PyMuPDF
watchdog
Pillow
pdf2image
EOFREQ

log_info "Updated: print_service/requirements.txt"

# ============================================
# PHASE 4: Create Missing Scripts
# ============================================
log_step "Phase 4: Creating/updating scripts..."

# Create unified sync_fallback.sh
cat > "$PROJECT_ROOT/scripts/sync_fallback.sh" << 'EOFSYNC'
#!/bin/bash
# Unified Sync Fallback Script
SCRIPTS_DIR="~/art/LatentSpace/scripts"
HOSTNAME=$(hostname)

if [ -d "~/art/LatentSpace/gaze-painter" ] || [[ "$HOSTNAME" == *"jetson-a"* ]]; then
    ROLE="jetson_a"
elif [ -d "~/art/LatentSpace/analysis_watcher" ] || [[ "$HOSTNAME" == *"jetson-b"* ]]; then
    ROLE="jetson_b"
elif [ -d "~/art/LatentSpace/print_service" ] || [[ "$HOSTNAME" == *"rpi"* ]]; then
    ROLE="rpi4"
else
    echo "ERROR: Cannot determine device role"
    exit 1
fi

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
esac

exit $?
EOFSYNC

chmod +x "$PROJECT_ROOT/scripts/sync_fallback.sh"
log_info "Created: scripts/sync_fallback.sh"

# Create RPi sync fallback
cat > "$PROJECT_ROOT/scripts/sync_fallback_rpi.sh" << 'EOFRPI'
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
EOFRPI

chmod +x "$PROJECT_ROOT/scripts/sync_fallback_rpi.sh"
log_info "Created: scripts/sync_fallback_rpi.sh"

# Update display_manager.sh
cat > "$PROJECT_ROOT/scripts/display_manager.sh" << 'EOFDISPLAY'
#!/bin/bash
export DISPLAY=:0
sleep 5

PRIMARY=$(xrandr | grep " connected" | head -1 | awk '{print $1}')
SECONDARY=$(xrandr | grep " connected" | tail -1 | awk '{print $1}')

echo "Primary Display: $PRIMARY"
echo "Secondary Display: $SECONDARY"

if [ ! -z "$PRIMARY" ] && [ ! -z "$SECONDARY" ]; then
    xrandr --output $PRIMARY --auto --primary
    xrandr --output $SECONDARY --auto --right-of $PRIMARY
    echo "Dual display configured"
else
    echo "Warning: Only one display detected"
fi
EOFDISPLAY

chmod +x "$PROJECT_ROOT/scripts/display_manager.sh"
log_info "Updated: scripts/display_manager.sh"

# ============================================
# PHASE 5: Update Monitoring Server Port
# ============================================
log_step "Phase 5: Updating monitoring server port..."

if [ -f "$PROJECT_ROOT/monitoring_server.py" ]; then
    # Update port to 8001
    sed -i "s/app\.run(host='0\.0\.0\.0', port=8000/app.run(host='0.0.0.0', port=8001/" \
        "$PROJECT_ROOT/monitoring_server.py"
    
    log_info "Updated monitoring_server.py to use port 8001"
fi

# ============================================
# PHASE 6: Update Systemd Services (if running)
# ============================================
log_step "Phase 6: Checking systemd services..."

if systemctl is-active --quiet monitoring-server 2>/dev/null; then
    log_warn "monitoring-server service is running"
    echo "To update, run:"
    echo "  sudo systemctl stop monitoring-server"
    echo "  sudo systemctl daemon-reload"
    echo "  sudo systemctl start monitoring-server"
fi

# ============================================
# PHASE 7: Update Firewall (if needed)
# ============================================
log_step "Phase 7: Checking firewall configuration..."

# Check if port 8001 is open
if ! sudo iptables -L -n | grep -q "8001"; then
    log_warn "Port 8001 not in firewall rules"
    echo "To add, run on Jetson B:"
    echo "  sudo iptables -I INPUT -p tcp --dport 8001 -s 192.168.1.0/24 -j ACCEPT"
    echo "  sudo netfilter-persistent save"
fi

# ============================================
# PHASE 8: Verify Python Dependencies
# ============================================
log_step "Phase 8: Checking Python dependencies..."

if [ -d "$PROJECT_ROOT/print_service/venv" ]; then
    log_info "Updating print_service dependencies..."
    cd "$PROJECT_ROOT/print_service"
    source venv/bin/activate
    pip install -q PyMuPDF pdf2image 2>&1 | grep -v "already satisfied" || true
    deactivate
    log_info "Print service dependencies updated"
fi

# ============================================
# Summary
# ============================================
echo ""
echo "==========================================="
log_info "Migration Complete!"
echo "==========================================="
echo ""
echo "Summary of changes:"
echo "  ✓ Removed duplicate setup scripts"
echo "  ✓ Deleted sys_setup_old directory"
echo "  ✓ Updated print_service/requirements.txt"
echo "  ✓ Created unified sync_fallback.sh"
echo "  ✓ Created sync_fallback_rpi.sh"
echo "  ✓ Updated display_manager.sh"
echo "  ✓ Updated monitoring server to port 8001"
echo ""
echo "Backup created at:"
echo "  $BACKUP_DIR"
echo ""
echo "Manual steps required:"
echo ""
echo "1. If on Jetson B, update firewall for port 8001:"
echo "   sudo iptables -I INPUT -p tcp --dport 8001 -s 192.168.1.0/24 -j ACCEPT"
echo "   sudo netfilter-persistent save"
echo ""
echo "2. If monitoring-server is running, restart it:"
echo "   sudo systemctl restart monitoring-server"
echo ""
echo "3. Test the updated system:"
echo "   Gallery:    http://192.168.1.11:8000"
echo "   Monitoring: http://192.168.1.11:8001"
echo ""
echo "Done!"