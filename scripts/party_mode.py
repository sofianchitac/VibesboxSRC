#!/usr/bin/env python3
"""
Party Mode Daemon — Govee LAN music sync for VibesboxSRC.

Toggled from the touchscreen (logo tap = colour mode + party mode together,
relayed by source_router over its :8080 WebSocket as "party_mode" state and the
"set_party_mode" command). When enabled, drives the linked lights (DEVICES below)
with a music show over the Govee LAN API:

  * colour follows VOLUME/intensity: red -> orange -> light orange,
  * brightness PULSES on beats: energy-based onset detection against a rolling
    average of the summed-bus RMS,
  * silence (a few seconds below the floor) turns the lights fully OFF until
    signal returns,
  * disabling restores whatever state each light had before the show.

Signal source is CamillaDSP's own WebSocket (:1234, GetPlaybackSignalRms at
10 Hz) — the same read the QML meters use, so this daemon needs no PipeWire
access and cannot disturb the audio chain. All Govee traffic is fire-and-forget
unicast UDP; nothing here blocks on the network.

Device protection (the ESP chips are not built for a flood):
  * the show ticks at 10 Hz, which is also the absolute message ceiling,
  * change-gating per device: a packet only goes out when brightness moved >= 3
    points or colour moved >= 10/255, so quiet passages produce near-zero traffic,
  * unicast only — no broadcast/multicast during the show.
Community experience says sustained rates above ~10 msg/s make these devices
flaky; we stay under that by construction.

LAN API primer (https://app-h5.govee.com/user-manual/wlan-guide):
  discovery : the manual says UDP multicast 239.255.255.250:4001 {"cmd":"scan",...},
              replies landing on our port 4002 — BUT multicast is DEAD on this LAN
              (wired Pi <-> Wi-Fi lights; verified 2026-08-24, zero replies while a
              unicast sweep finds every device). Discovery is therefore a UNICAST
              SWEEP of the local /24, which the devices answer identically.
  control   : UDP unicast to <ip>:4003 — "turn", "brightness" (1-100 %),
              "colorwc" {color:{r,g,b}, colorTemInKelvin:0}, "devStatus"
  identity  : the scan-reported "device" id is NOT the BLE/MAC printed on the box
              (e.g. H70B3 D0:C9:07:88:73:94 answers as 1D:6F:C5:75:2E:0E:7F:8A) —
              so DEVICES pins the scan-reported id, never the box MAC.
The cloud OpenAPI (Govee-API-Key) is rate-limited to 2 req/s/device and is
useless for beat sync — everything here is local, key-free.
"""

import asyncio
import base64
import json
import logging
import socket
import sys
import time

import websockets

logging.basicConfig(level=logging.INFO, format='%(asctime)s [PartyMode] %(message)s')
log = logging.getLogger("party_mode")

ROUTER_WS = "ws://127.0.0.1:8080"
CDSP_WS   = "ws://127.0.0.1:1234"

# ── Devices ─────────────────────────────────────────────────────────────── #
# "id" is what the LAN scan reports — NOT the BLE MAC printed on the box.
# A device without an id is matched by SKU, which only works while exactly one
# respondent carries that SKU (three H7020s on this LAN means the bulbs NEED
# their pinned id).
DEVICES = [
    # Curtain: washes out to white when driven hard, so cap lower; floor -50 dB
    # makes quieter passages still move the effect.
    {"id": "1D:6F:C5:75:2E:0E:7F:8A", "sku": "H70B3",  "label": "curtain",
     "floor_db": -50.0, "max_brightness": 70,
     "vol_span": 4},   # near-dark between beats; it washes out if it idles high
    # Identified 2026-08-24 by devStatus watch while toggling in the Govee app:
    # the only one of three LAN H7020s that flipped (BLE MAC 60:74:F4:A5:A6:9C).
    # "scene" pins its idle look to an app SCENE ("Illumination"): scenes are
    # invisible to devStatus, so they can never be snapshotted — they must be
    # pinned here. Scene codes come from Govee's per-SKU catalog
    #   https://app2.govee.com/appsku/v1/light-effect-libraries?sku=<SKU>
    # activated via the undocumented ptReal LAN command (BLE frame, XOR-19 checksum;
    # verified live 2026-08-24).
    {"id": "74:49:D6:37:30:33:6F:48", "sku": "H7020",  "label": "bulbs",
     "scene": 63, "max_brightness": 100},
]

SCAN_PORT         = 4001    # client -> devices (scan, unicast sweep)
LISTEN_PORT       = 4002    # devices -> client (all replies)
CONTROL_PORT      = 4003    # client -> device (control)
DISCOVER_WINDOW_S = 4.0     # listen window after a sweep

# ── Show tuning ─────────────────────────────────────────────────────────── #
TICK_S           = 0.10    # RMS poll / show tick — also the msg-rate ceiling
MIN_TX_GAP_S     = 0.06    # hard throttle between packets to a device
BRI_DELTA        = 3       # resend only when brightness moved >= this much
COLOR_DELTA      = 10      # ...or any RGB channel moved >= this much
LEVEL_FLOOR_DB   = -30.0   # 0% intensity == quiet-house noise floor (~-30 dBFS);
                           # real silence (no signal) sits far below this
MAX_BRIGHTNESS   = 80      # the LEDs wash out to white near full power
BEAT_RATIO       = 1.10    # instantaneous vs rolling-average onset threshold
BEAT_FLOOR       = 0.08    # ignore onsets below this intensity (noise gate)
BEAT_REFRACTORY  = 0.22    # min seconds between detected beats
BEAT_DECAY       = 0.80    # pulse envelope decay per tick (~300ms half-life)
# Brightness mix: BEAT-driven, not volume-driven (user tuning 2026-08-24 —
# volume owns the colour ramp; brightness just idles near BRI_BASE and PUNCHES
# on beats).
BRI_BASE         = 0       # silent rooms go dark; only beats light up
BRI_VOL_SPAN     = 12      # gentle lift with loudness
BRI_BEAT_SPAN    = 80      # the punch
SILENCE_MARGIN_DB = 2.0   # below (floor + this) the room counts as silent...
SILENCE_OFF_S     = 8.0   # ...for this long -> lights fully OFF
POWERON_SETTLE_S = 0.3     # wait after turn-on before streaming (device drops early cmds)
STATUS_TIMEOUT_S = 2.0     # devStatus reply wait

# Colour ramp by smoothed intensity. Theme.qml's grey/orange read as dim/white
# on the curtain LEDs (poor colour accuracy), so the ramp runs red -> orange ->
# light orange: #ff4b4b at the bottom, then the two theme tokens.
RAMP = [
    (0.00, (0xFF, 0x4B, 0x4B)),   # low: deep red
    (0.50, (0xFF, 0x70, 0x49)),   # Theme.orange
    (1.00, (0xFD, 0xA5, 0x64)),   # Theme.orangeLight
]


def _norm_mac(s: str) -> str:
    return s.replace(":", "").replace("-", "").lower()


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def _ramp_color(t: float) -> tuple[int, int, int]:
    """Interpolate the palette at intensity t (0..1)."""
    t = _clamp(t, 0.0, 1.0)
    for (p0, c0), (p1, c1) in zip(RAMP, RAMP[1:]):
        if t <= p1:
            f = 0.0 if p1 == p0 else (t - p0) / (p1 - p0)
            return tuple(round(a + (b - a) * f) for a, b in zip(c0, c1))
    return RAMP[-1][1]


def _scene_frame(code: int) -> str:
    """BLE scene frame for ptReal: 0x33 0x05 0x04 <code LE> + zero pad, checksum
    = XOR of the first 19 bytes (verified against community-captured frames)."""
    frame = bytearray(20)
    frame[0] = 0x33
    frame[1] = 0x05
    frame[2] = 0x04
    frame[3] = code & 0xFF
    frame[4] = (code >> 8) & 0xFF
    ck = 0
    for b in frame[:19]:
        ck ^= b
    frame[19] = ck
    return base64.b64encode(frame).decode()


class GoveeLink(asyncio.DatagramProtocol):
    """One UDP socket: unicast scan sweeps out, all device replies in."""

    def __init__(self, devices: list[dict]):
        self.wanted = devices
        self.scene_by_label = {d["label"]: d.get("scene") for d in devices}
        self.candidates = {}            # ip -> (sku, device id) of every respondent
        self.resolved: dict[str, str] = {}   # ip -> label
        self._status: dict[str, dict] = {}   # ip -> latest devStatus data
        self._status_evt: dict[str, asyncio.Event] = {}
        self.transport = None

    # -- lifecycle --------------------------------------------------------- #

    async def start(self):
        loop = asyncio.get_running_loop()
        self.transport, _ = await loop.create_datagram_endpoint(
            lambda: self, local_addr=("0.0.0.0", LISTEN_PORT))
        log.info(f"listening on udp/{LISTEN_PORT}")

    def connection_made(self, transport):
        self.transport = transport

    # -- inbound ----------------------------------------------------------- #

    def datagram_received(self, data, addr):
        try:
            msg = json.loads(data.decode())
            cmd = msg["msg"]["cmd"]
            payload = msg["msg"]["data"]
        except Exception:
            return

        if cmd == "scan":
            self.candidates[addr[0]] = (payload.get("sku"), payload.get("device"))
        elif cmd == "devStatus":
            self._status[addr[0]] = payload
            evt = self._status_evt.get(addr[0])
            if evt:
                evt.set()

    # -- outbound ---------------------------------------------------------- #

    def command(self, obj):
        """Fire-and-forget control packet to every resolved device."""
        for ip in self.resolved:
            self.transport.sendto(
                json.dumps({"msg": {"cmd": obj[0], "data": obj[1]}}).encode(),
                (ip, CONTROL_PORT))

    def command_one(self, ip, obj):
        self.transport.sendto(
            json.dumps({"msg": {"cmd": obj[0], "data": obj[1]}}).encode(),
            (ip, CONTROL_PORT))

    @staticmethod
    def _local_prefix() -> str:
        """The /24 prefix we route to the internet through ('192.168.1.')."""
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return ".".join(s.getsockname()[0].split(".")[:3]) + "."
        finally:
            s.close()

    async def discover(self):
        """Sweep the /24, resolve every configured device. Idempotent."""
        if len(self.resolved) == len(self.wanted):
            return True
        prefix = self._local_prefix()
        log.info(f"discovering {[d['label'] for d in self.wanted]} "
                 f"via unicast sweep of {prefix}0/24…")
        self.candidates.clear()
        scan = json.dumps({"msg": {"cmd": "scan",
                                   "data": {"account_topic": "reserve"}}}).encode()
        for i in range(1, 255):
            self.transport.sendto(scan, (f"{prefix}{i}", SCAN_PORT))
        await asyncio.sleep(DISCOVER_WINDOW_S)

        for ip, (sku, dev) in sorted(self.candidates.items()):
            log.info(f"  found {sku} at {ip} ({dev})")

        ok = True
        for want in self.wanted:
            if want["label"] in self.resolved.values():
                continue
            ip = self._pick(want)
            if ip:
                self.resolved[ip] = want["label"]
                log.info(f"{want['label']} ({want['sku']}) at {ip}")
            else:
                ok = False
                log.warning(f"{want['label']} ({want['sku']}) not found — "
                            f"is it powered and on this network?")
        return ok

    def _pick(self, want: dict):
        """Match by pinned id first; unique-SKU fallback when no id is set."""
        want_id = _norm_mac(want["id"]) if want["id"] else None
        by_sku = []
        for ip, (sku, dev) in self.candidates.items():
            if want_id and dev and _norm_mac(str(dev)) == want_id:
                return ip
            if sku == want["sku"]:
                by_sku.append(ip)
        return by_sku[0] if not want_id and len(by_sku) == 1 else None

    async def query_status(self, ip, timeout=STATUS_TIMEOUT_S):
        """One devStatus round-trip to one device; returns the data dict or {}."""
        self._status_evt.setdefault(ip, asyncio.Event()).clear()
        self.command_one(ip, ("devStatus", {}))
        try:
            await asyncio.wait_for(self._status_evt[ip].wait(), timeout=timeout)
            return self._status.get(ip, {})
        except asyncio.TimeoutError:
            return {}

    def close(self):
        if self.transport:
            self.transport.close()


class DeviceEngine:
    """Per-device intensity mapping: its own floor, brightness cap, auto-gain
    window and beat envelope — the curtain and the bulbs need very different
    response curves from the same signal."""

    def __init__(self, cfg: dict):
        self.label = cfg["label"]
        self.floor_db = cfg.get("floor_db", LEVEL_FLOOR_DB)
        self.max_bri = cfg.get("max_brightness", MAX_BRIGHTNESS)
        self.vol_span = cfg.get("vol_span", BRI_VOL_SPAN)
        self.window = []            # ~30 s auto-gain window (normalised units)
        self.history = []           # rolling window for onset detection
        self.smooth = 0.0
        self.beat_env = 0.0
        self.last_beat_t = float("-inf")

    def reset(self):
        self.window.clear()
        self.history.clear()
        self.smooth = 0.0
        self.beat_env = 0.0

    def update(self, db: float, now: float):
        """Feed one loudness sample (dBFS); returns (brightness, rgb)."""
        raw = _clamp((db - self.floor_db) / (0.0 - self.floor_db), 0.0, 1.0)

        # Auto-gain against the last ~30 s so the ramp rides the MUSIC's
        # dynamics rather than absolute level (program material sits in a narrow
        # dB band; without this the colour pins at one stop and looks static).
        self.window.append(raw)
        if len(self.window) > int(30.0 / TICK_S):
            self.window.pop(0)
        lo, hi = min(self.window), max(self.window)
        if hi - lo < 0.15:
            hi = lo + 0.15
        norm = _clamp((raw - lo) / (hi - lo), 0.0, 1.0)

        # smoothing on the normalised signal: fast attack, slow release
        k = 0.45 if norm > self.smooth else 0.10
        self.smooth += (norm - self.smooth) * k

        # beat detection: onset above the rolling average, noise-gated,
        # refractory-limited; strength feeds a decaying brightness envelope
        self.history.append(norm)
        if len(self.history) > 20:
            self.history.pop(0)
        avg = sum(self.history) / len(self.history)
        if (len(self.history) >= 5
                and norm > avg * BEAT_RATIO + 0.01
                and norm > BEAT_FLOOR
                and now - self.last_beat_t >= BEAT_REFRACTORY):
            strength = _clamp((norm - avg) * 3.0, 0.25, 1.0)
            self.beat_env = min(1.0, self.beat_env + strength * 0.85)
            self.last_beat_t = now
        self.beat_env *= BEAT_DECAY

        bri = int(_clamp(BRI_BASE + self.smooth * self.vol_span
                         + self.beat_env * BRI_BEAT_SPAN,
                         1, self.max_bri))
        return bri, _ramp_color(self.smooth)


class PartyMode:
    def __init__(self):
        self.link = GoveeLink(DEVICES)
        self.requested = False      # party_mode flag from source_router
        self.active = False         # show currently running (devices engaged)
        self.snapshot: dict[str, dict] = {}   # ip -> pre-party state to restore
        # per-device last transmitted state (change-gating)
        self.tx_bri: dict[str, int | None] = {}
        self.tx_rgb: dict[str, tuple | None] = {}
        self.tx_on: dict[str, bool | None] = {}
        self.turn_at: dict[str, float] = {}   # ip -> monotonic of last turn-on
        self.last_tx_t: dict[str, float] = {d["label"]: float("-inf") for d in DEVICES}
        # signal state (shared) + per-device engines
        self.db = LEVEL_FLOOR_DB - 70.0     # loudest-channel dBFS from CamillaDSP
        self.silent_for = 0.0
        self.lights_off = False     # silenced off (vs party-active lit)
        self.engines = {d["label"]: DeviceEngine(d) for d in DEVICES}

    # ------------------------------------------------------------------ #
    #  SourceRouter :8080 consumer                                        #
    # ------------------------------------------------------------------ #

    async def router_loop(self):
        while True:
            try:
                async with websockets.connect(ROUTER_WS) as ws:
                    log.info("connected to source-router :8080")
                    while True:
                        msg = json.loads(await ws.recv())
                        if msg.get("type") == "state":
                            want = msg.get("party_mode") is True
                            if want != self.requested:
                                self.requested = want
                                log.info(f"party mode {'requested' if want else 'released'}")
            except Exception as exc:
                log.warning(f"source-router unreachable ({exc}); retrying")
                await asyncio.sleep(3)

    # ------------------------------------------------------------------ #
    #  CamillaDSP :1234 RMS poller (same read the QML meters use)          #
    # ------------------------------------------------------------------ #

    async def cdsp_loop(self):
        while True:
            try:
                async with websockets.connect(CDSP_WS) as ws:
                    log.info("connected to CamillaDSP :1234")
                    while True:
                        await ws.send(json.dumps({"GetPlaybackSignalRms": None}))
                        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
                        r = msg.get("GetPlaybackSignalRms")
                        if r and r.get("result") == "Ok" and isinstance(r.get("value"), list):
                            vals = [v for v in r["value"] if isinstance(v, (int, float))]
                            # loudest channel; CamillaDSP reports true silence as -1000 dB
                            self.db = max(vals) if vals else LEVEL_FLOOR_DB - 70.0
                        await asyncio.sleep(TICK_S)
            except Exception as exc:
                log.warning(f"CamillaDSP unreachable ({exc}); retrying")
                self.db = LEVEL_FLOOR_DB - 70.0
                await asyncio.sleep(3)

    # ------------------------------------------------------------------ #
    #  Enable / disable                                                   #
    # ------------------------------------------------------------------ #

    async def activate(self):
        if not await self.link.discover():
            return False
        # Per-device snapshot + power-on. Staggered turns give each ESP its
        # post-power-on settle window (it drops commands sent too early).
        for ip, label in self.link.resolved.items():
            status = await self.link.query_status(ip)
            if status:
                self.snapshot[ip] = {
                    "on": status.get("onOff") == 1,
                    "brightness": status.get("brightness"),
                    "color": status.get("color"),
                    # white/CT mode reports colour {0,0,0} and the real value here
                    "kelvin": status.get("colorTemInKelvin"),
                }
                log.info(f"{label}: saved pre-party state {self.snapshot[ip]}")
            self.link.command_one(ip, ("turn", {"value": 1}))
            await asyncio.sleep(POWERON_SETTLE_S)
            # the turn went out above; tell the engine it's on so the first
            # value packets ride the settled link instead of re-sending turn
            self.tx_on[ip] = True
            self.turn_at[ip] = time.monotonic()
        self.active = True
        self.lights_off = False
        for eng in self.engines.values():
            eng.reset()
        self.silent_for = 0.0
        self.tx_bri = {}
        self.tx_rgb = {}
        self.tx_on = {}
        log.info("show started")
        return True

    async def deactivate(self):
        # Commands are SPACED on purpose: these ESPs drop packets that arrive in
        # a rapid burst (the same quirk POWERON_SETTLE_S works around at show
        # start — a back-to-back turn/brightness/colorwc restore silently lost
        # its colour command, found 2026-08-24).
        self.active = False
        for ip, label in self.link.resolved.items():
            snap = self.snapshot.get(ip)
            if snap:
                self.link.command_one(ip, ("turn", {"value": 1 if snap["on"] else 0}))
                await asyncio.sleep(0.2)
                scene = self.link.scene_by_label.get(label)
                if snap["on"] and scene is not None:
                    # pinned app-scene restore (scenes never show up in devStatus,
                    # so the snapshot cannot represent them) — ptReal + turn
                    self.link.command_one(
                        ip, ("ptReal", {"command": [_scene_frame(scene)]}))
                    await asyncio.sleep(0.2)
                    self.link.command_one(ip, ("turn", {"value": 1}))
                    log.info(f"{label}: scene {scene} restored")
                    continue
                if snap["on"]:
                    if isinstance(snap["brightness"], int):
                        self.link.command_one(
                            ip, ("brightness", {"value": _clamp(snap["brightness"], 1, 100)}))
                        await asyncio.sleep(0.2)
                    # colourwc round-trips BOTH fields: kelvin > 0 means the device
                    # was in white/CT mode (colour reads {0,0,0} then — sending black
                    # rgb with kelvin 0 is invalid and the device ignores it).
                    c = snap.get("color") or {}
                    kelvin = snap.get("kelvin")
                    if isinstance(kelvin, int) and kelvin > 0:
                        self.link.command_one(
                            ip, ("colorwc", {"color": {"r": 0, "g": 0, "b": 0},
                                             "colorTemInKelvin": kelvin}))
                    elif all(isinstance(c.get(k), int) for k in "rgb") and any(c.values()):
                        self.link.command_one(
                            ip, ("colorwc", {"color": {"r": c["r"], "g": c["g"], "b": c["b"]},
                                             "colorTemInKelvin": 0}))
                log.info(f"{label}: pre-party state restored")
            else:
                self.link.command_one(ip, ("turn", {"value": 0}))
        self.snapshot = {}
        self.tx_bri = {}
        self.tx_rgb = {}
        self.tx_on = {}
        self.turn_at = {}
        log.info("show stopped")

    # ------------------------------------------------------------------ #
    #  Show engine (10 Hz)                                                #
    # ------------------------------------------------------------------ #

    def _transmit_one(self, ip, label, on, bri=None, rgb=None):
        """Change-gated packet to ONE device. A turn-on is sent ALONE and values
        wait out POWERON_SETTLE_S — the ESP drops commands that arrive too soon
        after a power transition (same quirk the restore path spaces around)."""
        now = time.monotonic()
        if now - self.last_tx_t.get(label, float("-inf")) < MIN_TX_GAP_S:
            return
        if on != self.tx_on.get(ip):
            self.link.command_one(ip, ("turn", {"value": 1 if on else 0}))
            self.tx_on[ip] = on
            self.turn_at[ip] = now
            self.last_tx_t[label] = now
            return
        if on and now - self.turn_at.get(ip, 0.0) < POWERON_SETTLE_S:
            return
        changed = ((on and bri is not None
                    and (self.tx_bri.get(ip) is None or abs(bri - self.tx_bri[ip]) >= BRI_DELTA))
                   or (on and rgb is not None
                       and (self.tx_rgb.get(ip) is None
                            or max(abs(a - b) for a, b in zip(rgb, self.tx_rgb[ip])) >= COLOR_DELTA)))
        if not changed:
            return
        if on:
            # rgb=None keeps the device's current look (e.g. a pinned app scene)
            # and pulses ONLY brightness with the music.
            if rgb is not None:
                self.link.command_one(
                    ip, ("colorwc", {"color": {"r": rgb[0], "g": rgb[1], "b": rgb[2]},
                                     "colorTemInKelvin": 0}))
            if bri is not None:
                self.link.command_one(
                    ip, ("brightness", {"value": _clamp(bri, 1, MAX_BRIGHTNESS)}))
        self.tx_bri[ip], self.tx_rgb[ip] = bri, rgb
        self.last_tx_t[label] = now

    async def show_loop(self):
        while True:
            if self.requested and not self.active:
                await self.activate()
            elif not self.requested and self.active:
                await self.deactivate()

            if self.active:
                self._tick()

            await asyncio.sleep(TICK_S)

    def _tick(self):
        now = time.monotonic()

        # silence gate: full OFF after SILENCE_OFF_S of quiet, back on at resume
        silent_now = self.db < LEVEL_FLOOR_DB + SILENCE_MARGIN_DB
        if silent_now:
            self.silent_for += TICK_S
        else:
            if self.lights_off and not silent_now:
                self.lights_off = False
                for ip in self.link.resolved:
                    self.tx_on[ip] = None      # force a fresh turn-on packet
            self.silent_for = 0.0
        if self.silent_for >= SILENCE_OFF_S and not self.lights_off:
            self.lights_off = True
            self.link.command(("turn", {"value": 0}))
            for ip in self.link.resolved:
                self.tx_on[ip] = False
            log.info("silence — lights off")
        if self.lights_off:
            return

        # per-device engines: same loudness sample, different response curves
        for ip, label in self.link.resolved.items():
            eng = self.engines[label]
            bri, rgb = eng.update(self.db, now)
            if self.link.scene_by_label.get(label) is not None:
                rgb = None    # pulse_scene: keep the scene's look, brightness only
            self._transmit_one(ip, label, True, bri=bri, rgb=rgb)

        # one-line engine heartbeat every ~5 s — makes "why is nothing happening"
        # answerable from journalctl alone.
        self._dbg_n = getattr(self, "_dbg_n", 0) + 1
        if self._dbg_n % 50 == 0:
            parts = [f"db={self.db:.1f}"]
            for label, eng in self.engines.items():
                parts.append(f"{label}: smooth={eng.smooth:.2f} "
                             f"beat={eng.beat_env:.2f}")
            log.info("tick: " + " | ".join(parts))


async def main():
    party = PartyMode()
    await party.link.start()

    stop = asyncio.Event()
    import signal
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_running_loop().add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    tasks = [asyncio.create_task(party.router_loop()),
             asyncio.create_task(party.cdsp_loop()),
             asyncio.create_task(party.show_loop())]
    await stop.wait()

    log.info("shutting down")
    for t in tasks:
        t.cancel()
    if party.active:
        await party.deactivate()
    party.link.close()


if __name__ == "__main__":
    asyncio.run(main())
