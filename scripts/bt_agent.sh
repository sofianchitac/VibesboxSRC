#!/bin/bash
# Bluetooth Pairing Agent
#
# Registers a headless BlueZ pairing agent (NoInputNoOutput capability).
# Previously paired A2DP sources will reconnect automatically when the
# adapter is powered on — no interactive confirmation required.
#
# Power management (on/off, discoverability) is handled entirely by
# auto_router.py, NOT here. This script only keeps the agent alive.
#
# Requires: bluez (bluetoothctl)
# Runs via: bt-agent.service

set -e

(
    echo "agent NoInputNoOutput"
    echo "default-agent"
    sleep inf
) | bluetoothctl
