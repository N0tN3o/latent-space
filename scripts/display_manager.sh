#!/bin/bash
# Display Manager for Dual Monitor Setup
# Role: 1. Set Desktop Layout based on resolution. 
#       2. Move App Windows to correct screens.

export DISPLAY=:0

# 1. DETECT MONITORS AND RESOLUTIONS
# This gets a list of connected monitors and their resolutions (e.g., "HDMI-0 1920x1080")
# We assume the "Big" screen is the Canvas, and "Small" is the Eyes.

echo "Detecting displays..."
# Get list of connected displays with resolutions
# Format: NAME WIDTH HEIGHT
DISPLAYS=$(xrandr | grep " connected" | sed -e "s/\([A-Z0-9-]*\) connected.* \([0-9]*\)x\([0-9]*\)+.*/\1 \2 \3/")

# Sort them by width (column 2) to find biggest and smallest
# We assume the widest screen is the Canvas.
BIG_SCREEN=$(echo "$DISPLAYS" | sort -k2 -nr | head -n1)
SMALL_SCREEN=$(echo "$DISPLAYS" | sort -k2 -nr | tail -n1)

BIG_NAME=$(echo $BIG_SCREEN | awk '{print $1}')
BIG_WIDTH=$(echo $BIG_SCREEN | awk '{print $2}')

SMALL_NAME=$(echo $SMALL_SCREEN | awk '{print $1}')

echo "Canvas (Big): $BIG_NAME ($BIG_WIDTH px wide)"
echo "Eyes (Small): $SMALL_NAME"

# 2. CONFIGURE LAYOUT (XRANDR)
# Set Big screen as Primary, Small screen to the Right
if [ "$BIG_NAME" != "$SMALL_NAME" ]; then
    xrandr --output "$BIG_NAME" --auto --primary \
           --output "$SMALL_NAME" --auto --right-of "$BIG_NAME"
    echo "Layout applied: Small screen is to the right of Big screen."
else
    echo "Warning: Only one monitor detected or splitter masking monitors."
    # If using a splitter that shows as 1 giant screen, we skip xrandr setup
fi

# 3. MOVE WINDOWS (WMCTRL)
# Run this loop in background to catch the windows when they open
(
    # Wait for windows to appear (timeout after 60s)
    MAX_RETRIES=30
    count=0
    
    echo "Waiting for application windows..."
    
    while [ $count -lt $MAX_RETRIES ]; do
        # Check if windows exist
        if wmctrl -l | grep -q "Gaze Painter" && wmctrl -l | grep -q "Eyes"; then
            sleep 2 # wait for them to fully render
            
            echo "Moving 'Gaze Painter' to Primary (0,0)..."
            wmctrl -r "Gaze Painter" -e 0,0,0,-1,-1
            
            echo "Moving 'Eyes' to Secondary ($BIG_WIDTH,0)..."
            # Move to X = Width of Big Screen, Y = 0
            wmctrl -r "Eyes" -e 0,$BIG_WIDTH,0,-1,-1
            
            # Maximize the Gaze Painter (optional, remove if unwanted)
            wmctrl -r "Gaze Painter" -b add,fullscreen
            
            break
        fi
        sleep 2
        ((count++))
    done
) &

exit 0