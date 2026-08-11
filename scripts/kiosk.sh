#!/bin/bash
# Kiosk session launcher
#
# Launched directly by greetd as the configured user — no compositor since
# 2026-07-22 (sway removed).
# The QML kiosk renders straight to KMS/DRM via Qt eglfs: the DSI panel scans
# out native 800x480 landscape, Main.qml rotates the portrait canvas into it,
# and Qt delivers libinput touch through the item transform — no compositor
# rotation or calibration matrix needed. No compositor cursor either, so the
# old ydotool sabotage block is gone.
#
# Backlight is controlled manually via the UI power menu (source_router WebSocket).
# Backlight path: /sys/class/backlight/*/brightness (0-255)
# Write access granted by udev rule: config/99-backlight.rules

export QT_QPA_PLATFORM=eglfs
export QT_QPA_EGLFS_INTEGRATION=eglfs_kms
# vc4-hdmi CEC nodes register as libinput pointers — never draw Qt's GBM cursor
export QT_QPA_EGLFS_HIDECURSOR=1
exec /usr/lib/qt6/bin/qml /opt/vibesbox-src/ui-qml/Main.qml > /tmp/kiosk-qml.log 2>&1
