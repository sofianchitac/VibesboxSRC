#!/bin/bash
# USB UAC2 Gadget Initialization Script
#
# Creates a UAC2 (USB Audio Class 2) composite gadget via libcomposite/configfs.
# Configured as a 6-channel (5.1) device.
#
# We advertise the standard 5.1 mask 0x3F (FL FR FC LFE RL RR), but NOTE: iPadOS/macOS
# IGNORE this mask and emit 5.1 in a FIXED stream order — a SMPTE file (L R C LFE Ls Rs)
# arrives as L R Ls Rs C LFE, center & LFE last (proven 2026-06-17 with the channel-ID
# test files). This is NOT correctable on the Pi (PipeWire position labels don't reorder
# the index-ordered NDI wire); the channel correction is done downstream in REAPER.
#
# Multi-rate support for c_srate/p_srate requires Linux kernel >= 5.18.
# Pi 4B with Raspberry Pi OS Bookworm ships kernel 6.x — fully supported.

set -e

CONFIGFS=/sys/kernel/config/usb_gadget
GADGET_NAME=vibesbox_src
GADGET_DIR=$CONFIGFS/$GADGET_NAME

if [ -d "$GADGET_DIR" ]; then
    echo "Gadget already configured — skipping."
    exit 0
fi

modprobe libcomposite

mkdir -p "$GADGET_DIR"
cd "$GADGET_DIR"

echo 0x1d6b > idVendor
echo 0x0104 > idProduct
echo 0x0100 > bcdDevice
echo 0x0200 > bcdUSB

mkdir -p strings/0x409
echo "1234567890"          > strings/0x409/serialnumber
echo "Vibesbox"        > strings/0x409/manufacturer
echo "Vibesbox SRC"    > strings/0x409/product

mkdir -p configs/c.1/strings/0x409
echo "Audio Config" > configs/c.1/strings/0x409/configuration
echo 250            > configs/c.1/MaxPower

mkdir -p functions/uac2.usb0

# 6-channel 5.1 surround — bitmask 0x3F (bits 0–5: FL FR FC LFE RL RR)
# UAC2 channel cluster bits (USB Audio Class 2.0 spec §4.1):
#   bit 0 = FL    bit 1 = FR    bit 2 = FC
#   bit 3 = LFE   bit 4 = RL    bit 5 = RR
CHANNELS_MASK=63   # 0x3F = 0b00111111
SAMPLE_RATES="44100,48000,88200,96000,176400,192000"

echo "$CHANNELS_MASK" > functions/uac2.usb0/c_chmask
echo "$CHANNELS_MASK" > functions/uac2.usb0/p_chmask
echo 4              > functions/uac2.usb0/c_ssize
echo 4              > functions/uac2.usb0/p_ssize

KERNEL_MAJOR=$(uname -r | cut -d. -f1)
KERNEL_MINOR=$(uname -r | cut -d. -f2)

if [ "$KERNEL_MAJOR" -gt 5 ] || { [ "$KERNEL_MAJOR" -eq 5 ] && [ "$KERNEL_MINOR" -ge 18 ]; }; then
    echo "Kernel $(uname -r): multi-rate UAC2 supported — advertising $SAMPLE_RATES"
    echo "$SAMPLE_RATES" > functions/uac2.usb0/c_srate
    echo "$SAMPLE_RATES" > functions/uac2.usb0/p_srate
else
    echo "Kernel $(uname -r): pre-5.18 — falling back to 48000 Hz single-rate UAC2"
    echo 48000 > functions/uac2.usb0/c_srate
    echo 48000 > functions/uac2.usb0/p_srate
fi

ln -s functions/uac2.usb0 configs/c.1/

if [ ! -d "/sys/class/udc" ]; then
    echo "ERROR: /sys/class/udc not found. Is dwc2 loaded?"
    exit 1
fi

UDC_NAME=$(ls /sys/class/udc | head -n 1)
if [ -z "$UDC_NAME" ]; then
    echo "ERROR: No UDC found. Is dwc2 loaded?"
    exit 1
fi

echo "$UDC_NAME" > UDC
echo "USB Gadget (UAC2) initialized: 6ch 5.1 on UDC: $UDC_NAME"
