#!/bin/bash
# Display Manager for Dual Monitor Setup
# Assigns windows to specific displays

export DISPLAY=:0

# Wait for displays to be detected
sleep 5

# MANUALLY SET: Identify your displays by running 'xrandr'
# Run 'xrandr' in terminal to find your specific HDMI IDs (e.g., HDMI-0)
CANVAS_DISPLAY="HDMI-0"    # The big screen
EYES_DISPLAY="HDMI-1"      # The small screen

echo "Primary (Canvas): $CANVAS_DISPLAY"
echo "Secondary (Eyes): $EYES_DISPLAY"

if [ ! -z "$CANVAS_DISPLAY" ] && [ ! -z "$EYES_DISPLAY" ]; then
    # Set the canvas as the primary (main) display
    xrandr --output $CANVAS_DISPLAY --auto --primary
    
    # Place the eyes display to the right of the canvas
    xrandr --output $EYES_DISPLAY --auto --right-of $CANVAS_DISPLAY
    echo "Dual display configured"
else
    echo "Warning: Displays not configured correctly in script"
fi