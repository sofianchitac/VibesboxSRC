#!/bin/bash
# Setup script for Plymouth Splash Screen (Hides bootloader text and shows logo)
# Run with sudo

if [ "$EUID" -ne 0 ]; then
  echo "Please run as root (sudo)"
  exit 1
fi

echo "1. Installing Plymouth..."
apt-get update
apt-get install -y plymouth plymouth-themes

echo "2. Setting up Vibesbox theme..."
THEME_DIR="/usr/share/plymouth/themes/vibesbox"
mkdir -p "$THEME_DIR"

# Copy theme files and the user's splashscreen.png
cp ../config/plymouth/vibesbox.plymouth "$THEME_DIR/"
cp ../config/plymouth/vibesbox.script "$THEME_DIR/"
cp ../ui/splashscreen.png "$THEME_DIR/"

# Set as default
plymouth-set-default-theme -R vibesbox

echo "3. Modifying boot configuration..."
# Disable rainbow splash
sed -i 's/^#disable_splash=1/disable_splash=1/' /boot/firmware/config.txt
if ! grep -q "disable_splash=1" /boot/firmware/config.txt; then
    echo "disable_splash=1" >> /boot/firmware/config.txt
fi

# Modify cmdline.txt to add silent boot parameters
CMDLINE_FILE="/boot/firmware/cmdline.txt"
if [ ! -f "$CMDLINE_FILE" ]; then
    CMDLINE_FILE="/boot/cmdline.txt"
fi

if [ -f "$CMDLINE_FILE" ]; then
    # Create backup
    cp "$CMDLINE_FILE" "${CMDLINE_FILE}.bak"
    
    # Read current cmdline
    CMD=$(cat "$CMDLINE_FILE")
    
    # Remove conflicting parameters if they exist
    CMD=$(echo "$CMD" | sed -E 's/\b(console=tty1|splash|quiet|logo.nologo|consoleblank=[0-9]+|loglevel=[0-9]+|vt.global_cursor_default=[0-9]+|plymouth.ignore-serial-consoles)\b//g')
    
    # Add new parameters
    CMD="$CMD quiet splash plymouth.ignore-serial-consoles logo.nologo consoleblank=0 loglevel=1 vt.global_cursor_default=0"
    
    # Clean up multiple spaces
    CMD=$(echo "$CMD" | tr -s ' ' | sed 's/^ //;s/ $//')
    
    echo "$CMD" > "$CMDLINE_FILE"
    echo "Bootline updated."
else
    echo "Warning: cmdline.txt not found."
fi

echo "Done! The new splashscreen will take effect on the next reboot."
