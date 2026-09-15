#!/usr/bin/env python3
"""Watch which input / app the LG TV has in the foreground, over its webOS websocket.

Prototype (2026-09-15) for the AC3-level correction: the Pi already knows the eARC FORMAT
(source_router's bridge selection) but not which DEVICE is behind the TV, and AC3 from the
Google device lands ~100 ms EARLY while other AC3 sources do not. The TV knows: its foreground
app is `com.webos.app.hdmiN` for an external input, or the webOS app itself.

    lgtv_input.py            # discover the TV (SSDP), pair on first run, then log changes

The foreground appId is also written to STATE_FILE (atomically; emptied while the TV is
unreachable) — earc-bitstream-bridge.sh reads it at start to decide whether the AC3 stream
comes from the Google device and needs the ~100 ms delay. Runs as lgtv-input.service.

First run: the TV shows a pairing prompt — accept it on the TV. The client key is stored in
KEY_FILE and reused. No new dependencies: plain `websockets`, port 3000 (ws).
"""

import asyncio
import json
import logging
import os
import socket

import websockets

KEY_FILE = "/opt/vibesbox-src/state/lgtv-client-key"
STATE_FILE = "/run/vibesbox-lgtv-foreground"
SSDP_ST = "urn:lge-com:service:webos-second-screen:1"

# The payload aiowebostv 0.10 registers with (no signature needed on current webOS).
REGISTER = {
    "type": "register", "id": "register_0",
    "payload": {
        "forcePairing": False, "pairingType": "PROMPT",
        "manifest": {"appVersion": "1.1", "manifestVersion": 1, "permissions": [
            "APP_TO_APP", "READ_APP_STATUS", "READ_CURRENT_CHANNEL", "READ_INPUT_DEVICE_LIST",
            "READ_INSTALLED_APPS", "READ_LGE_TV_INPUT_EVENTS", "READ_NETWORK_STATE",
            "READ_POWER_STATE", "READ_RUNNING_APPS", "READ_SETTINGS", "TEST_OPEN",
            "TEST_PROTECTED", "TEST_SECURE"]},
    },
}


def discover(timeout=3.0):
    """SSDP M-SEARCH for the webOS second-screen service; returns the TV's IP or None."""
    msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\n"
           f"MX: 2\r\nST: {SSDP_ST}\r\n\r\n").encode()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    s.sendto(msg, ("239.255.255.250", 1900))
    try:
        while True:
            data, addr = s.recvfrom(4096)
            if SSDP_ST in data.decode(errors="ignore"):
                return addr[0]
    except socket.timeout:
        return None
    finally:
        s.close()


def write_state(app):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write(app)
    os.replace(tmp, STATE_FILE)


async def session(host):
    key = open(KEY_FILE).read().strip() if os.path.exists(KEY_FILE) else None
    async with websockets.connect(f"ws://{host}:3000", max_size=None) as ws:
        reg = json.loads(json.dumps(REGISTER))
        if key:
            reg["payload"]["client-key"] = key
        await ws.send(json.dumps(reg))
        while True:
            msg = json.loads(await ws.recv())
            if msg.get("type") == "registered":
                new_key = msg["payload"]["client-key"]
                if new_key != key:
                    os.makedirs(os.path.dirname(KEY_FILE), exist_ok=True)
                    with open(KEY_FILE, "w") as f:
                        f.write(new_key)
                    logging.info("paired; client key stored")
                break
            if msg.get("type") == "error":
                raise RuntimeError(f"register: {msg}")
            logging.info("pairing: ACCEPT THE PROMPT ON THE TV")

        await ws.send(json.dumps({"type": "request", "id": "inputs",
                                  "uri": "ssap://tv/getExternalInputList"}))
        await ws.send(json.dumps({"type": "subscribe", "id": "fg",
                                  "uri": "ssap://com.webos.applicationManager/getForegroundAppInfo"}))
        labels, last = {}, None
        while True:
            msg = json.loads(await ws.recv())
            p = msg.get("payload", {})
            if msg.get("id") == "inputs":
                labels = {d["appId"]: d.get("label", "?") for d in p.get("devices", [])}
                logging.info("inputs: " + ", ".join(f"{k}={v}" for k, v in labels.items()))
            elif msg.get("id") == "fg":
                app = p.get("appId", "")
                if app != last:
                    logging.info(f"foreground: {app or '(none)'}"
                                 + (f" [{labels[app]}]" if app in labels else ""))
                    write_state(app)
                    last = app


async def main():
    while True:
        host = discover()
        if not host:
            logging.info("TV not discoverable (off?) — retrying in 30 s")
            write_state("")
            await asyncio.sleep(30)
            continue
        try:
            await session(host)
        except Exception as exc:                # TV went to standby, network blip, ...
            logging.info(f"session ended ({exc.__class__.__name__}: {exc}); reconnecting in 10 s")
        write_state("")                         # unknown beats stale
        await asyncio.sleep(10)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [lgtv] %(message)s")
    asyncio.run(main())
