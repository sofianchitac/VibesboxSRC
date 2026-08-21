#!/bin/bash
# ALSA Loopback Initialization Script
#
# Creates 5 virtual loopback soundcards using snd-aloop.
#
# IMPORTANT: We use indices 10-15 to avoid colliding with hardware cards
# (UAC2 gadget, TOSLINK Pico, eARC I2S capture — all enumerate at low indices).
# Using low indices like 1-5 can cause cards to fail loading silently.
#
# Each loopback has 2 subdevices:
#   subdevice 0 = playback  (services write here)
#   subdevice 1 = capture   (CamillaDSP / transmit daemons read here)
#
# Card map:
#   Lyrion    (10) — Squeezelite writes sub0; CamillaDSP reads sub1
#   AirPlay   (11) — shairport-sync writes sub0; CamillaDSP reads sub1
#   Bluetooth (12) — bluealsa writes sub0; CamillaDSP reads sub1
#   (13)          — RETIRED 2026-08-21. Was NDITX: CamillaDSP wrote sub0 and the
#                   ndi-output daemon read sub1. The transmitter now takes CamillaDSP's
#                   output straight from PipeWire, which measured 18.5-20.0 ms cheaper
#                   (docs/ndi-loopback-hop-brief.md). The index is left unused rather
#                   than reassigned so 14 and 15 keep their numbers.
#   Resampled (14) — ardftsrc-bridge writes sub0; CamillaDSP reads sub1
#                    (only used when srcEngine=ardftsrc)
#   Tidal     (15) — tidal_connect container writes sub0 (PortAudio/ALSA);
#                    ardftsrc-bridge@tidal reads sub1
#
# NDI Input (receive) is handled externally and is not part of this setup.

set -e

# Unload any existing instance cleanly
rmmod snd-aloop 2>/dev/null && echo "Unloaded existing snd-aloop" || true

modprobe snd-aloop \
    enable=1,1,1,1,1 \
    index=10,11,12,14,15 \
    pcm_substreams=2,2,2,2,2 \
    id=Lyrion,AirPlay,Bluetooth,Resampled,Tidal

echo "ALSA loopback cards loaded:"
echo "  10=Lyrion  11=AirPlay  12=Bluetooth  14=Resampled  15=Tidal"
