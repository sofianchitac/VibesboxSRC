#!/usr/bin/env python3
"""
Source Router Daemon — VibesboxSRC v2 (PipeWire backbone)

Replaces auto_router.py. PipeWire is the audio graph, hard-pinned to 96 kHz. Every
unmuted, playing source is summed (by PipeWire, in F32) into CamillaDSP's NATIVE PipeWire
capture node "dsp-in"; CamillaDSP's playback node "dsp-out" is linked to sink.ndi-feed
(6ch NDI — the only output transport since the Pi 5 migration dropped the HiFiBerry;
the v2-era S/PDIF transport selector is retired, see git history for the dual-sink era).

This daemon's whole job is routing + state, not pipeline reconfiguration:
  * discover source nodes in the graph (poll `pw-dump`, ~2 Hz — NOT pw-mon streaming,
    which the house style distrusts; cf. auto_router.py's bluetoothctl-monitor note)
  * link each playing + unmuted source into dsp-in (FL/FR for stereo, all 6 for USB),
    unlink muted or stopped ones — mute IS link/unlink
  * on startup: push dsp_6ch to CamillaDSP and link dsp-out -> sink.ndi-feed;
    start ndi-output
  * run the manual Bluetooth pairing state machine (pairing is the only manual step;
    once connected, BT mixes in like any source)
  * serve the :8080 WebSocket the UI subscribes to

Shed vs v1 auto_router.py: the CamillaDSP neutral-config stop/start dance + deactivate(),
camilladsp-controller, hifiberry-clock choreography, and the ENTIRE last-active-wins /
manual-override / Auto-Manual selection model (meaningless under mixing — all unmuted sources
just play). The global camilladsp↔ardftsrc src_engine toggle is also gone.

SRC engine (per-source, fixed — user decision after the 2026-05-29 blind listening gate): the
three active sources USB + Lyrion + AirPlay each feed an ffmpeg/librempeg ARDFTSRC bridge that
emits a source.X.ardftsrc PipeWire node into dsp-in. PipeWire q14 SRC is no longer in any active
path. Hence v1's hw_params polling is back (as the bridge-activity detector), NOT shed.

WebSocket state frame (server -> client), schema changed from v1:
  removed: active_source (singular), mode (Auto/Manual), src_engine (toggle retired),
           output_channels (S/PDIF transport retired with the Pi 5 migration — NDI only)
  added:   sources_muted {name: bool}, focused_source (most-recently-active unmuted, drives
           the Now Playing card), latency_ms (Pi DSP+PipeWire graph latency)
  kept:    config_name, sources_playing, input_rate, channels, resampler_type,
           resampler_profile, buffer_level, bt_state, bt_device
  changed: input_rate is now the focused source's NATIVE rate (e.g. 44100), not the 96k
           graph rate — feeds both the UI INPUT readout and the Now Playing transport line

PENDING BENCH VALIDATION (P4): the PipeWire node/port names below come from the P2/P3
bench findings; confirm against `pw-dump` on the box (esp. source node names, dsp-in/dsp-out
created by the native backend, sink.ndi-feed from the WirePlumber rules). The
daemon discovers ports dynamically by audio.channel, so port-name surprises are tolerated;
node-name surprises are a one-line edit to SOURCES / the sink constants.
"""

import os
import re
import glob
import json
import time
import asyncio
import logging
import subprocess

import yaml
import websockets
from camilladsp import CamillaClient

import tv_ac3_extract   # IEC 61937 constants (PA_LE/PB_LE, data-type sets) — same dir

logging.basicConfig(level=logging.INFO, format='%(asctime)s [SourceRouter] %(message)s')

CDSP_IP   = "127.0.0.1"
CDSP_PORT = 1234

CONFIG_DIR = "/opt/vibesbox-src/camilladsp"
# CamillaDSP always runs the static 6ch passthrough — REAPER on the LattePanda
# owns all channel routing (passthrough / downmix / stereo upmix).
DSP_PASSTHROUGH = "dsp_8ch.yml"   # 8ch since 2026-08-13 (dsp_6ch.yml kept for rollback)

SOURCES_MUTED_STATE_FILE = "/opt/vibesbox-src/state/sources_muted"

OUTPUT_RATE = 96000

# CamillaDSP's native-backend node names (camilladsp/dsp_*.yml devices block).
DSP_IN_NODE  = "dsp-in"    # CDSP capture  — sources link their outputs into its inputs
DSP_OUT_NODE = "dsp-out"   # CDSP playback — its outputs link into the active sink's inputs

# Output sink (WirePlumber-named, config/wireplumber/wireplumber.conf.d/51-vibesbox-sinks.conf).
SINK_NDI = "sink.ndi-feed"   # 6ch NDI feed (NDITX snd-aloop wrap) — the only output transport

CH6 = ["FL", "FR", "FC", "LFE", "RL", "RR"]
CH2 = ["FL", "FR"]
# 8-wide since 2026-08-13. The eARC LPCM surrounds arrive on lanes 7-8 and the chain now
# carries them all the way to REAPER. These are POSITIONAL NAMES ONLY — they identify
# ports so links can be authored, and imply nothing about what each lane carries. Channel
# meaning is REAPER's alone.
CH8 = CH6 + ["SL", "SR"]

# Per-source SRC engine (user decision 2026-05-29, after the blind listening gate): the three
# active sources ALL use the ffmpeg/librempeg ARDFTSRC bridge — USB + Lyrion + AirPlay each feed
# a raw-rate snd-aloop/gadget read into `ardftsrc_bridge.sh`, which emits a `source.X.ardftsrc`
# PipeWire node (Candidate A: ffmpeg `-f alsa pipewire`). The old global camilladsp↔ardftsrc
# toggle is retired; PipeWire q14 SRC is no longer in any active path. Bluetooth live audio stays
# DEFERRED (P3-f: bluez is a native PW node with no loopback to read; its engine is decided when
# that path is wired) — the pairing state machine still runs.
ARDFTSRC_SOURCES = {"USB", "Lyrion", "AirPlay", "Tidal", "TV"}

# Source registry.
#   names       = candidate exact node.name matches (the bridge's source.X.ardftsrc node first).
#   prefix      = match any node.name starting with this (bluez nodes are MAC-specific).
#   channels    = link width: 8 -> CH8, 6 -> CH6, anything else -> CH2 (FL/FR only).
#                 No on-Pi upmix — a narrow source just leaves the other lanes silent.
#   bridge_unit = the ardftsrc-bridge@<src> unit to start when the source goes ALSA-active.
#   alsa_detect = how to detect first activity at the ALSA layer (the ardftsrc source has no PW
#                 node until ffmpeg creates one, so PW-state can't see *first* activity):
#                   {"numid8": card}  -> UAC2 gadget Capture Rate control (>0 = host streaming)
#                   {"hw_params": p}  -> snd-aloop writer's hw_params (not "closed"/"no setup")
SOURCES = {
    "USB": {
        "names":    ["source.usb.ardftsrc"],
        "prefix":   None,
        "channels": 6,
        "bridge_unit": "ardftsrc-bridge@usb.service",
        "alsa_detect": {"numid8": "UAC2Gadget"},
    },
    "Lyrion": {
        "names":    ["source.lyrion.ardftsrc"],
        "prefix":   None,
        "channels": 2,
        "bridge_unit": "ardftsrc-bridge@lyrion.service",
        "alsa_detect": {"hw_params": "/proc/asound/Lyrion/pcm0p/sub0/hw_params"},
    },
    "AirPlay": {
        "names":    ["source.airplay.ardftsrc"],
        "prefix":   None,
        "channels": 2,
        "bridge_unit": "ardftsrc-bridge@airplay.service",
        "alsa_detect": {"hw_params": "/proc/asound/AirPlay/pcm0p/sub0/hw_params"},
    },
    # Tidal Connect: the (Dockerised) tidal_connect_application writes PCM into the Tidal
    # snd-aloop via PortAudio/ALSA; ardftsrc-bridge@tidal reads the capture side. Stereo
    # (FL/FR), same loopback pattern as Lyrion/AirPlay — the container is always-on so it
    # stays discoverable in the Tidal app; hw_params goes "closed" when nothing is playing.
    "Tidal": {
        "names":    ["source.tidal.ardftsrc"],
        "prefix":   None,
        "channels": 2,
        "bridge_unit": "ardftsrc-bridge@tidal.service",
        "alsa_detect": {"hw_params": "/proc/asound/Tidal/pcm0p/sub0/hw_params"},
    },
    # TV via the eARC I2S tap (SiI9437 in the Lindy 38368 -> RP1 i2s1 slave capture,
    # card "eARC"; see config/overlays/). Replaced the TOSLINK optical path 2026-07-28.
    #
    # UNLIKE every other source there is NO cheap activity check: the eARC card is a
    # device-tree platform card, so it is enumerated whether the TV is on or off (no
    # usb_card presence test), and there is no writer side to read hw_params from. The
    # only signal is "does a capture return data", which costs a ~1s probe — so activity
    # AND format detection are both folded into _update_tv_bridge()/_probe_earc(), which
    # rate-limits the probe and never runs it while a bridge holds the device.
    "TV": {
        "names":    ["source.tv.ardftsrc"],
        "prefix":   None,
        "channels": 2,
        "bridge_unit": "ardftsrc-bridge@tv.service",
        "alsa_detect": {"earc_probe": "eARC"},
    },
    # BT live audio deferred (P3-f); bluez source node name is MAC-specific -> prefix match.
    # Not in ARDFTSRC_SOURCES (no loopback); no bridge. Pairing state machine still runs.
    "Bluetooth": {
        "names":    ["source.bluetooth"],
        "prefix":   "bluez_input.",
        "channels": 2,
    },
}

CDSP_STARTUP_TIMEOUT = 5.0
CDSP_STARTUP_POLL    = 0.2

BT_PAIRING_TIMEOUT = 90   # seconds the discoverable window stays open

POLL_INTERVAL          = 0.5    # 2 Hz reconcile + telemetry
WS_FULL_STATE_INTERVAL = 0.5    # 2 Hz state frame
PW_TIMEOUT             = 4      # subprocess timeout for pw-* commands

# Multichannel detection: any of the non-front channels (ch3 upward)
# carrying audio means the active source is multichannel. CamillaDSP reports a
# truly silent channel as -1000 dB, so the threshold is far above the floor and
# below any real content. Debounced over consecutive 2 Hz polls (6 = ~3 s) so a
# momentary rear blip doesn't flap. The LattePanda consumes the published flag.
MULTICH_RMS_THRESHOLD_DB = -80.0
MULTICH_DEBOUNCE_POLLS   = 6
# Stereo detection is NOT "not multichannel" — the rears also go silent when
# nothing is playing at all. Stereo means fronts carrying audio AND rears silent,
# so a pause in a 5.1 program is neither stereo nor multichannel and the kiosk
# takes no action on it. Debounced much longer than the multichannel edge: 5.1
# music can leave the rears below threshold for several seconds, and 10 s of
# delay before upmix engages on a stereo album is imperceptible.
STEREO_DEBOUNCE_POLLS    = 20


class SourceRouter:
    def __init__(self):
        self.active_config_name = DSP_PASSTHROUGH

        # ── Per-source mute (UI-driven, persisted). Default all unmuted. ─────
        self.sources_muted   = self._load_sources_muted()
        self.sources_playing = {n: False for n in SOURCES}
        self._last_active    = {n: 0.0 for n in SOURCES}   # monotonic ts of last rising edge
        self.focused_source  = None

        # True whenever a TV bridge could be running, so the idle path can skip
        # the systemctl status spawn entirely. Starts True: a bridge may have
        # survived a daemon restart.
        self._tv_bridge_maybe_running = True

        # ── eARC tap state (the TV source) ──────────────────────────────────
        # The tap has no cheap activity check, so these cache what the last capture
        # probe found: the detected stream mode (None = no bit clock, i.e. TV off or
        # link down) and the rate to report as the source's native rate. Both are
        # owned by _update_tv_bridge(); _alsa_active()/_source_native_rate() read them.
        self._earc_mode  = None
        self._earc_rate  = None
        self._earc_probe_after = 0.0        # monotonic gate, see EARC_PROBE_INTERVAL
        # Set to the measured Hz when a probe found a live clock carrying LPCM at a rate we
        # refuse. Without it the UI cannot tell "unsupported rate" from "TV off" — both
        # leave _earc_mode None, so the source simply reads as absent and the silence looks
        # like a fault rather than a decision.
        self._earc_rate_unsupported = None
        # Set when the tap carries a bitstream we can identify but not decode (HBR:
        # DTS-HD/DTS:X or TrueHD). Distinct from _earc_rate_unsupported: that one means
        # "LPCM at a rate we refuse", this one means "a codec with no decode path".
        self._earc_unsupported_codec = None      # (data_type, label) from the probe
        self._earc_codec_unsupported = None      # label currently being reported

        self._reconcile_lock = asyncio.Lock()

        # ── CamillaDSP ───────────────────────────────────────────────────────
        self.cdsp = CamillaClient(CDSP_IP, CDSP_PORT)
        self.cdsp_state = {
            "channels":          None,
            "buffer_level":      None,
            "resampler_type":    None,
            "resampler_profile": None,
            "chunksize":         None,
            "target_level":      None,
            "samplerate":        None,
        }

        # Native (pre-resample) sample rate of each source, refreshed by
        # _update_ardftsrc_bridges from the ALSA layer (numid=8 / hw_params).
        # Drives the UI INPUT rate readout (the source rate, not the 96k graph rate).
        self._native_rate = {n: None for n in SOURCES}

        # ── Bluetooth (never sleeps: powered on at boot, kept on) ────────────
        self.bt_state          = "idle"   # idle | pairing | paired | playing
        self.bt_device         = None
        self.bt_device_mac     = None
        self._bt_pairing_task  = None
        self._bt_poll_counter  = 0

        self._last_config_poll   = 0.0
        self._cdsp_was_connected = False

        # Channel-format detection from the summed bus. Both flags are published in
        # the :8080 state frame; the LattePanda kiosk edge-triggers Penteo upmix off
        # on `multichannel` and on with `stereo`. Mutually exclusive by construction,
        # and both false while silent.
        self.multichannel       = False
        self._multich_on_count  = 0
        self._multich_off_count = 0
        self.stereo             = False
        self._stereo_on_count   = 0
        self._stereo_off_count  = 0

        # The output sink must sit at unity — all attenuation headroom lives in CamillaDSP
        # (-4 dB intersample). WirePlumber restores a persisted node volume on a fresh boot
        # (observed 0.40 = ~8 dB quiet), which has no manual correction on the appliance, so
        # we pin the output sink to 1.0 once it first appears. Tracked per node.name so the
        # wpctl call runs once, not every 2 Hz reconcile.
        self._vol_pinned = set()

    # ====================================================================== #
    #  Persistence                                                            #
    # ====================================================================== #

    def _load_sources_muted(self) -> dict:
        muted = {n: False for n in SOURCES}
        try:
            with open(SOURCES_MUTED_STATE_FILE) as f:
                saved = json.load(f)
            for n in SOURCES:
                if isinstance(saved.get(n), bool):
                    muted[n] = saved[n]
            logging.info(f"Restored sources_muted={muted}.")
        except Exception:
            logging.info("sources_muted defaulting to all unmuted.")
        return muted

    def _save_sources_muted(self):
        self._write_state(SOURCES_MUTED_STATE_FILE, json.dumps(self.sources_muted))

    @staticmethod
    def _write_state(path: str, text: str):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(text)
        except Exception as exc:
            logging.warning(f"Could not write state {path}: {exc}")

    # ====================================================================== #
    #  PipeWire helpers (subprocess to pw-dump / pw-link)                     #
    # ====================================================================== #
    #
    # The daemon shells out to the pw-* CLI exactly as the P2/P3 bench did — no new deps,
    # and the validated recipes transfer verbatim. The system-mode PipeWire socket env
    # (XDG_RUNTIME_DIR / PIPEWIRE_REMOTE) is provided by the service unit at deploy
    # (Risk #8); subprocesses inherit it from os.environ.

    def _pw_dump(self) -> list:
        """Return the parsed `pw-dump` object list, or [] on failure."""
        try:
            r = subprocess.run(["pw-dump", "-N"], capture_output=True, text=True,
                               timeout=PW_TIMEOUT)
            return json.loads(r.stdout)
        except Exception as exc:
            logging.debug(f"pw-dump failed: {exc}")
            return []

    @staticmethod
    def _props(obj: dict) -> dict:
        return (obj.get("info") or {}).get("props") or {}

    # Native-backend CDSP nodes (dsp-in / dsp-out) emit ports with audio.channel="UNK"
    # and positional names input_<N> / output_<N> (N = 1..8). We key those by CH8[N-1]
    # so reconcile() can look them up under the Vibesbox channel convention.
    _CDSP_PORT_NAME_RE = re.compile(r"^(?:input|output)_(\d+)$")

    def _index(self, dump: list):
        """Build lookup tables from a pw-dump: node-name->id, node-id->state, and
        node-id-> {channel: port_id} for both input and output ports, plus the set of
        existing (out_port_id, in_port_id) links and out_port_id->owning node-id."""
        nodes_by_name = {}
        node_state    = {}
        node_id_name  = {}   # node_id -> node.name (reverse of nodes_by_name)
        out_ports     = {}   # node_id -> {channel: port_id}
        in_ports      = {}   # node_id -> {channel: port_id}
        port_node     = {}   # port_id -> node_id
        links         = set()
        # IDs are cast to int throughout: pw-dump gives a Node's own `id` as a JSON int,
        # but a Port's parent "node.id" (an spa_dict prop) is rendered as a STRING in some
        # versions. Without the cast, every node-id keyed lookup (out_ports.get(dsp_out_id),
        # port_node[...] in managed_out_nodes) silently misses -> nothing links/unlinks.
        #
        # Two passes over `dump` (Nodes first, then Ports/Links): the Port branch needs to
        # know the *name* of its owning node to apply the dsp-in/dsp-out positional fallback,
        # and pw-dump's emission order is not guaranteed.
        for o in dump:
            if not o.get("type", "").endswith("Interface:Node"):
                continue
            p = self._props(o)
            name = p.get("node.name")
            nid  = int(o["id"])
            if name:
                nodes_by_name[name] = nid
                node_id_name[nid]   = name
            node_state[nid] = (o.get("info") or {}).get("state")
        for o in dump:
            t = o.get("type", "")
            if t.endswith("Interface:Port"):
                p = self._props(o)
                nid = p.get("node.id")
                direction = (o.get("info") or {}).get("direction")  # "input"/"output"
                if nid is None:
                    continue
                nid = int(nid)
                pid = int(o["id"])
                port_node[pid] = nid
                ch = p.get("audio.channel")
                # Positional fallback for the CDSP native-backend nodes: their ports report
                # audio.channel="UNK" (literal string, not None) — without this remap all 6
                # collapse to a single "UNK" key and reconcile sees zero channel matches.
                if ch in (None, "UNK") and node_id_name.get(nid) in (DSP_IN_NODE, DSP_OUT_NODE):
                    m = self._CDSP_PORT_NAME_RE.match(p.get("port.name") or "")
                    if m:
                        idx = int(m.group(1))
                        if 1 <= idx <= len(CH8):
                            ch = CH8[idx - 1]
                if not ch:
                    continue
                table = out_ports if direction == "output" else in_ports
                table.setdefault(nid, {})[ch] = pid
            elif t.endswith("Interface:Link"):
                info = o.get("info") or {}
                op = info.get("output-port-id")
                ip = info.get("input-port-id")
                if op is not None and ip is not None:
                    links.add((int(op), int(ip)))
        return nodes_by_name, node_state, out_ports, in_ports, port_node, links

    def _find_source_node(self, nodes_by_name: dict, spec: dict):
        """Return the node id for a source spec (exact names first, then prefix)."""
        for name in spec["names"]:
            if name in nodes_by_name:
                return nodes_by_name[name]
        prefix = spec.get("prefix")
        if prefix:
            for name, nid in nodes_by_name.items():
                if name.startswith(prefix):
                    return nid
        return None

    def _pw_link(self, out_pid: int, in_pid: int):
        try:
            subprocess.run(["pw-link", str(out_pid), str(in_pid)],
                           capture_output=True, timeout=PW_TIMEOUT)
        except Exception as exc:
            logging.warning(f"pw-link {out_pid}->{in_pid} failed: {exc}")

    def _pw_unlink(self, out_pid: int, in_pid: int):
        try:
            subprocess.run(["pw-link", "-d", str(out_pid), str(in_pid)],
                           capture_output=True, timeout=PW_TIMEOUT)
        except Exception as exc:
            logging.warning(f"pw-link -d {out_pid}->{in_pid} failed: {exc}")

    def _pin_sink_volumes(self, names: dict):
        """Force the output sink to unity once it appears (see _vol_pinned rationale)."""
        for sink in (SINK_NDI,):
            if sink in self._vol_pinned:
                continue
            nid = names.get(sink)
            if nid is None:
                continue
            try:
                subprocess.run(["wpctl", "set-volume", str(nid), "1.0"],
                               capture_output=True, timeout=PW_TIMEOUT)
                self._vol_pinned.add(sink)
                logging.info(f"{sink}: volume pinned to 1.0.")
            except Exception as exc:
                logging.warning(f"wpctl set-volume {sink} failed: {exc}")

    # ====================================================================== #
    #  ARDFTSRC bridge lifecycle (USB / Lyrion / AirPlay)                     #
    # ====================================================================== #
    #
    # Each ardftsrc source is fed by a per-source ffmpeg bridge (ardftsrc-bridge@<src>) that
    # reads the raw-rate ALSA device and emits a source.X.ardftsrc PipeWire node. The node
    # does not exist until ffmpeg runs, so *first* activity must be detected at the ALSA layer
    # (numid=8 / hw_params) — reconcile()'s PW-state view only sees the source once the bridge
    # is already up. We start the bridge when the source goes ALSA-active and stop it when it
    # goes idle; the bridge's own watchdog also self-terminates on disconnect, so this is
    # idempotent against `systemctl is-active`. Mute is orthogonal: a muted source keeps its
    # bridge running (node present) and is merely unlinked from dsp-in by reconcile().

    @staticmethod
    def _systemctl(action: str, unit: str):
        try:
            subprocess.run(["systemctl", action, unit], capture_output=True, timeout=10)
        except Exception as exc:
            logging.warning(f"systemctl {action} {unit} failed: {exc}")

    def _alsa_active(self, spec: dict) -> bool:
        """ALSA-layer activity for an ardftsrc source (no PW node exists yet)."""
        det = spec.get("alsa_detect") or {}
        if det.get("earc_probe"):
            # eARC has no cheap check — activity is whatever the last capture probe
            # found. _update_tv_bridge() owns that state; see _probe_earc().
            return self._earc_mode is not None
        usb_card = det.get("usb_card")
        if usb_card:
            # Fixed-rate USB card (e.g. TOSLINK Pico): active when the card is enumerated.
            return os.path.isdir(f"/proc/asound/{usb_card}")
        card = det.get("numid8")
        if card:
            # UAC2 gadget Capture Rate: 0 until a host streams.
            try:
                r = subprocess.run(["amixer", "-D", f"hw:{card}", "cget", "numid=8"],
                                   capture_output=True, text=True, timeout=2)
                for line in r.stdout.splitlines():
                    if ": values=" in line:
                        return int(line.split("values=", 1)[1].split(",", 1)[0]) > 0
            except Exception:
                pass
            return False
        path = det.get("hw_params")
        if path:
            # snd-aloop writer hw_params: "closed"/"no setup" when the source isn't playing.
            try:
                with open(path) as f:
                    txt = f.read().lower()
                return ("closed" not in txt) and ("no setup" not in txt)
            except Exception:
                return False
        return False

    def _source_native_rate(self, spec: dict):
        """Native (pre-resample) sample rate of a source in Hz, or None if idle/unknown.
        numid=8 sources (USB gadget): the control value IS the host stream rate.
        hw_params sources (snd-aloop writers): parse the 'rate:' line, present only while
        the writer is playing (the file is empty/"closed" when idle).
        usb_card sources (TOSLINK Pico): fixed 48000 when the card is present.
        earc_probe (eARC tap): the rate MEASURED by the last probe — a slave-mode I2S
        capture carries no rate information, so it is derived from frames-per-second."""
        det = spec.get("alsa_detect") or {}
        if det.get("earc_probe"):
            return self._earc_rate
        usb_card = det.get("usb_card")
        if usb_card:
            return 48000 if os.path.isdir(f"/proc/asound/{usb_card}") else None
        card = det.get("numid8")
        if card:
            try:
                r = subprocess.run(["amixer", "-D", f"hw:{card}", "cget", "numid=8"],
                                   capture_output=True, text=True, timeout=2)
                for line in r.stdout.splitlines():
                    if ": values=" in line:
                        v = int(line.split("values=", 1)[1].split(",", 1)[0])
                        return v if v > 0 else None
            except Exception:
                pass
            return None
        path = det.get("hw_params")
        if path:
            try:
                with open(path) as f:
                    for line in f:
                        if line.startswith("rate:"):     # "rate: 44100 (44100/1)"
                            return int(line.split(":", 1)[1].split()[0])
            except Exception:
                pass
            return None
        return None

    def _bluez_native_rate(self, dump: list):
        """A2DP rate (Hz) of the connected bluez source node, or None. Bluetooth is
        PW-native (no ALSA loopback, so no numid8/hw_params probe like the ardftsrc
        sources): its native rate comes from the node's clock, which pw-dump exposes
        as node.rate = "1/<rate>" (e.g. "1/44100")."""
        prefix = SOURCES["Bluetooth"]["prefix"]
        for o in dump:
            if not o.get("type", "").endswith("Interface:Node"):
                continue
            p = self._props(o)
            if str(p.get("node.name", "")).startswith(prefix):
                r = p.get("node.rate", "")        # "1/44100"
                if isinstance(r, str) and "/" in r:
                    try:
                        return int(r.split("/", 1)[1])
                    except ValueError:
                        pass
                return None
        return None

    @staticmethod
    def _bridge_active(unit: str) -> bool:
        try:
            r = subprocess.run(["systemctl", "is-active", unit],
                               capture_output=True, text=True, timeout=3)
            return r.stdout.strip() == "active"
        except Exception:
            return False

    async def _update_ardftsrc_bridges(self):
        """Start/stop per-source ffmpeg ardftsrc bridges to match ALSA-layer activity.
        Idempotent (checks `systemctl is-active`); mute does NOT stop a bridge.
        Async only for the TV probe (a >=1s capture that must not block the loop)."""
        for name in ARDFTSRC_SOURCES:
            spec = SOURCES[name]
            active  = self._alsa_active(spec)
            self._native_rate[name] = self._source_native_rate(spec) if active else None
            if name == "TV":
                # TV has mutually-exclusive bridges (LPCM ardftsrc vs bitstream decode),
                # chosen by probing the eARC tap. _update_tv_bridge owns activity
                # detection too (there is no cheap check), so `active` above is just the
                # cached result of its last probe.
                await self._update_tv_bridge()
                continue
            unit = spec["bridge_unit"]
            running = self._bridge_active(unit)
            if active and not running:
                logging.info(f"{name}: ALSA-active -> start {unit}.")
                self._systemctl("start", unit)
            elif not active and running:
                logging.info(f"{name}: ALSA-idle -> stop {unit}.")
                self._systemctl("stop", unit)

    # The TV eARC tap carries either LPCM stereo or an IEC 61937 bitstream (the TV
    # switches format with content). They need different bridges, different capture
    # RATES, and different downstream channel counts, so we probe the tap and pick one.
    # Keys match _probe_earc()'s returned mode.
    TV_BRIDGES = {                                  # mode -> (unit, channels)
        # ★ LPCM is 8ch since 2026-08-13. A 6ch open reads only SD0-SD2, and the source
        # puts the LPCM surrounds on lanes 7-8 (SD3) with lanes 5-6 digitally SILENT —
        # measured directly with `arecord -c 8`, see docs/latency-matrix-plan.md. Reading
        # 6 therefore collected 4 populated lanes plus 2 empty ones and threw the
        # surrounds away. 8 unconditionally, no channel-count detection to flap on quiet
        # passages. ⛔ The lanes are passed through as captured; REAPER maps them.
        # The coded modes stay at 6 ON PURPOSE: their bridges DECODE to 5.1 downstream of
        # the tap, so they were never affected and their output really is 6 wide.
        "lpcm": ("ardftsrc-bridge@tv.service", 8),          # DFT resampler @48k
        "eac3": ("earc-bitstream-bridge@eac3.service", 6),  # DD+ / streaming Atmos @192k
        "ac3":  ("earc-bitstream-bridge@ac3.service", 6),   # legacy DD @48k
        "dts":  ("earc-bitstream-bridge@dts.service", 6),   # legacy DTS core @48k
    }

    # eARC probe pacing. The probe is a ~1s capture, so it must not run every 2 Hz tick.
    # It only ever runs when NO bridge holds the device, and then at most this often —
    # which also bounds the cost of the TV-off case, where the capture just blocks until
    # its timeout because an I2S slave with no bit clock delivers nothing.
    EARC_PROBE_INTERVAL = 5.0       # seconds between probes while no bridge is running
    EARC_PROBE_TIMEOUT  = 3.0       # give up on a capture that never returns (no clock)
    # IEC 61937 data types we can RECOGNISE but have no decode path for. Both are HBR
    # (carried on a 192 kHz carrier, spread across all four I2S lanes). tv_ac3_extract
    # deliberately excludes 0x11 from DATA_TYPES_DTS because it never traverses S/PDIF
    # optical — true, but the eARC tap CAN see it, and "recognised, undecodable" is a far
    # more useful answer than the rate-mismatch refusal these used to produce.
    #
    # Reaching either requires an HBR-capable source: a Blu-ray player, an Nvidia Shield,
    # or Windows in exclusive mode. Neither macOS (VLC passthrough tops out at the DTS
    # core / AC-3) nor a Pi (`vc4-hdmi` has no HBR) can emit one, so as of 2026-08-12
    # this branch has never been reached on this system.
    EARC_UNDECODABLE = {0x11: "DTS-HD / DTS:X", 0x16: "Dolby TrueHD / MAT"}

    # LPCM rates an eARC/HDMI source can present. Adjacent entries are >=8.1% apart, so the
    # +/-4% snap window is unambiguous. Must stay in step with STD_RATES/RATE_TOLERANCE in
    # ardftsrc-bridge-rs/src/main.rs — the bridge measures its own rate and would otherwise
    # accept one this refuses (or the reverse).
    EARC_STD_RATES      = (44100, 48000, 88200, 96000, 176400, 192000)
    EARC_RATE_TOLERANCE = 0.04      # +/-4%: separates 48k from 44.1k (8.1% apart)

    def _snap_earc_rate(self, measured: int | None) -> int | None:
        """Snap a measured tap rate onto a standard rate, or None if it is near none of
        them. None means 'do not start a bridge' — a stalled probe and an exotic source
        both land here, and both are cases where resampling would be wrong-pitch."""
        if not measured or measured <= 0:
            return None
        best = min(self.EARC_STD_RATES, key=lambda r: abs(measured - r))
        return best if abs(measured / best - 1.0) <= self.EARC_RATE_TOLERANCE else None

    def _tv_bridge_states(self) -> dict[str, bool]:
        """Which TV bridges are running, in ONE systemctl spawn for all units
        (`is-active` prints one state per line, in argument order)."""
        units = [unit for unit, _ in self.TV_BRIDGES.values()]
        try:
            r = subprocess.run(["systemctl", "is-active", *units],
                               capture_output=True, text=True, timeout=3)
            lines = r.stdout.splitlines()
        except Exception:
            lines = []
        lines += [""] * (len(units) - len(lines))
        return {mode: line.strip() == "active"
                for mode, line in zip(self.TV_BRIDGES, lines)}

    async def _drain_probe(self, proc) -> tuple[bytes, int, float, float]:
        """Read a probe capture to EOF. Returns (data, first_chunk_len, t_first, t_last).

        The two stamps bracket the stream's own delivery: t_first is when the capture
        produced its first byte, t_last when it produced its last. _probe_earc() times
        that window — not the process lifetime — so startup cost stays out of the rate.
        A live capture always spans several reads (the pipe holds 64 KiB and the probe
        keeps >=192000 bytes), so the window is never degenerate in practice."""
        chunks: list[bytes] = []
        first_len = 0
        t_first = t_last = time.monotonic()
        while True:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                break
            t_last = time.monotonic()
            if not chunks:
                t_first, first_len = t_last, len(chunk)
            chunks.append(chunk)
        await proc.wait()
        return b"".join(chunks), first_len, t_first, t_last

    async def _probe_earc(self) -> tuple[str | None, int | None]:
        """Probe the eARC tap. Returns (mode, measured_rate_hz).

        mode is 'eac3' / 'ac3' / 'dts' / 'lpcm', or None when there is nothing to
        capture — no bit clock (TV off / eARC link down), or the device is busy
        because a stopping bridge's teardown still holds it. The caller retries later
        rather than guessing: guessing LPCM used to start the DFT resampler on a
        compressed bitstream, which is white noise.

        Async — the capture takes up to a second and a sync run froze the whole event
        loop (WS state frames, UI commands, BT poll) for every probe.
        Only safe to call when NO TV bridge holds hw:eARC (exclusive ALSA device).

        We open at 48 kHz regardless of what the stream really is. That is deliberate:
        sample DATA survives a rate mismatch intact (proven on hardware — a 192 kHz DD+
        stream probed at 48 kHz still yields findable preambles), only flow control is
        off, so one probe classifies every format. The wall-clock timing then recovers
        the true rate: a 192 kHz stream delivers the requested 48000 frames in ~0.25 s.

        AC-3, E-AC-3 and DTS share the SAME Pa/Pb preamble, so the preamble alone
        cannot tell them apart — matching only the preamble is a latent flap bug: the
        stream would be routed to the wrong bridge, emit nothing, time out and
        re-probe in a loop. We read the Pc data_type after the preamble."""
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "arecord", "-D", "hw:eARC,0", "-f", "S32_LE", "-r", "48000",
                "-c", "2", "-t", "raw", "-d", "1",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            data, first_len, t_first, t_last = await asyncio.wait_for(
                self._drain_probe(proc), timeout=self.EARC_PROBE_TIMEOUT)
            # 1s @ 48k/2ch/S32 = 384000 bytes; accept >=half. A short read means the
            # clock died mid-capture, which says nothing reliable about the format.
            if proc.returncode != 0 or len(data) < 192000:
                raise RuntimeError(f"rc={proc.returncode}, {len(data)} bytes")
        except Exception as exc:
            if proc is not None and proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            logging.debug(f"TV: eARC probe found nothing ({exc}).")
            return None, None
        # Time ONLY the bytes delivered after the first chunk. Timing from the spawn
        # instead charged fork/exec + ALSA open + the driver's first period against a
        # ~1s capture and read ~13% low — 48 kHz measured as 41769/41964 Hz on hardware
        # (2026-07-28), outside EARC_RATE_TOLERANCE, so a healthy LPCM stream was
        # refused and the TV source dropped for a probe interval each time it happened.
        elapsed = max(t_last - t_first, 1e-3)
        rate = int(round(((len(data) - first_len) // 8) / elapsed))

        # The tap is 24-bit left-justified in S32, so the 16-bit IEC 61937 word is the
        # TOP half of each sample. Taking bytes 2..3 of every 4 reproduces exactly the
        # S16_LE word stream the optical path had, so the constants below still apply.
        hi = bytearray(len(data) // 2)
        hi[0::2] = data[2::4]
        hi[1::2] = data[3::4]

        # Burst constants come from tv_ac3_extract — single source of truth with the
        # bridge-side extractor (preamble + Pc data-type sets must agree, or we would
        # start a bridge whose extractor drops every burst and flaps).
        preamble = tv_ac3_extract.PA_LE + tv_ac3_extract.PB_LE
        start = 0
        while True:
            pos = hi.find(preamble, start)
            if pos < 0 or pos + 6 > len(hi):
                break
            data_type = (hi[pos + 4] | (hi[pos + 5] << 8)) & 0x1F
            if data_type == tv_ac3_extract.DATA_TYPE_EAC3:
                return "eac3", rate
            if data_type == tv_ac3_extract.DATA_TYPE_AC3:
                return "ac3", rate
            if data_type in tv_ac3_extract.DATA_TYPES_DTS:
                return "dts", rate
            if data_type in self.EARC_UNDECODABLE:
                # Recognised, but there is no decode path. Returning a named mode instead
                # of falling through to "lpcm" is the whole point: an HBR stream measures
                # as a 192 kHz carrier, so the LPCM branch would refuse it as a RATE
                # problem and report nothing about what actually arrived.
                self._earc_unsupported_codec = (data_type, self.EARC_UNDECODABLE[data_type])
                return "unsupported", rate
            start = pos + 4                 # non-audio burst — keep scanning
        return "lpcm", rate

    async def _update_tv_bridge(self):
        """Run exactly one TV bridge matching the detected eARC stream format.

        Also owns TV activity detection, because the eARC tap has no cheap check
        (see SOURCES["TV"]) — the probe that classifies the format is the same probe
        that tells us the TV is on at all.

        Probe-before-start; mid-stream format switches (Stage 2) work because each
        bridge self-exits when the stream stops matching its format (the ardftsrc
        bridge's IEC 61937 detector, the extractor's no-burst timeout) — the next
        poll tick then re-probes here and starts the right bridge."""
        spec = SOURCES["TV"]
        # Only WE start bridges, so once we've seen them all down, they stay down
        # until we start one — skip the 4-unit `systemctl is-active` spawn entirely
        # in the idle case rather than paying it at 2 Hz forever. Starts True: a
        # bridge may have survived a daemon restart. Self-correcting if a `start`
        # silently fails (next tick sees none up and clears the flag again).
        if self._tv_bridge_maybe_running:
            running = self._tv_bridge_states()
            for mode, is_up in running.items():
                if is_up:
                    # A bridge owns the device, so we CANNOT probe (exclusive ALSA
                    # capture) — and must not, since a failed probe would read as
                    # "TV off" and stop a perfectly healthy bridge. Re-derive
                    # channels from the running bridge every tick, not just at start
                    # time: after a daemon restart the registry default (2) would
                    # otherwise stick and reconcile would link only FL/FR of a
                    # running 6ch decode bridge.
                    spec["channels"] = self.TV_BRIDGES[mode][1]
                    self._earc_mode = mode
                    return
            self._tv_bridge_maybe_running = False

        # Nothing running. Rate-limit the probe: it costs ~1s when the TV is on and
        # blocks to EARC_PROBE_TIMEOUT when it is off, so at 2 Hz an ungated probe
        # would saturate the poll loop.
        now = time.monotonic()
        if now < self._earc_probe_after:
            return
        self._earc_probe_after = now + self.EARC_PROBE_INTERVAL

        mode, rate = await self._probe_earc()
        if mode is None:
            # No bit clock (TV off / link down) or device busy. Mark idle so
            # reconcile drops the TV source; retry after the interval.
            if self._earc_mode is not None:
                logging.info("TV: eARC clock gone -> source idle.")
            self._earc_mode, self._earc_rate = None, None
            self._earc_rate_unsupported = None   # no clock at all, not a rate problem
            self._earc_codec_unsupported = None
            spec["channels"] = 2
            return

        if mode == "unsupported":
            # A real bitstream we can identify but cannot decode. _earc_mode MUST stay
            # None — _alsa_active() reads it, and the TV source genuinely is not playing
            # (no bridge, no node, no audio). The codec is carried separately so the log
            # and the UI can say what arrived. Log on the rising edge only: the probe
            # re-runs every EARC_PROBE_INTERVAL while no bridge is up, so a source parked
            # on such a stream would otherwise fill the journal.
            dt, label = self._earc_unsupported_codec
            if self._earc_codec_unsupported != label:
                logging.warning(
                    f"TV: eARC {label} (IEC 61937 data type {dt:#04x}, HBR ~{rate} Hz "
                    f"carrier) — no decode path, not starting a bridge.")
            self._earc_mode, self._earc_rate = None, None
            self._earc_codec_unsupported = label
            self._earc_rate_unsupported = None      # a codec problem, not a rate problem
            spec["channels"] = 2
            return

        if mode == "lpcm":
            # The LPCM bridge measures the slave capture's rate itself and resamples from
            # whatever it finds, so any standard rate is playable (2026-08-12; it used to
            # be hardcoded 48k and everything else was refused). We still snap here to
            # reject a measurement that is not near ANY standard rate — a stall or an
            # exotic source — because resampling against a bad estimate is wrong pitch,
            # and wrong pitch is far less discoverable than silence.
            snapped = self._snap_earc_rate(rate)
            if snapped is None:
                logging.warning(
                    f"TV: eARC LPCM measured {rate} Hz — not a standard rate, refusing to "
                    f"start the bridge (it would play at the wrong pitch).")
                self._earc_mode, self._earc_rate = None, None
                self._earc_rate_unsupported = rate     # surfaced in the WS state
                spec["channels"] = 2
                return
            rate = snapped                  # snap off the read-boundary jitter
        else:
            # Bitstream: the elementary stream is 48k audio regardless of the 192k
            # (DD+) or 48k (AC-3/DTS) IEC 61937 carrier the bridge opens at.
            rate = 48000

        self._earc_mode, self._earc_rate = mode, rate
        self._earc_rate_unsupported = None
        self._earc_codec_unsupported = None
        unit, channels = self.TV_BRIDGES[mode]
        spec["channels"] = channels         # reconcile links this many into dsp-in
        self._tv_bridge_maybe_running = True
        logging.info(f"TV: eARC {mode} detected -> start {unit}.")
        self._systemctl("start", unit)

    # ====================================================================== #
    #  The reconcile loop — the heart of v2 routing                           #
    # ====================================================================== #

    async def reconcile(self):
        """Bring the live PipeWire link set in line with desired routing:
           dsp-out -> active sink, and each *unmuted* source -> dsp-in (pre-linked
           on node appearance, NOT gated on play state — see Finding 2 in
           docs/pipewire-v2-P5-session4-findings.md).
        Idempotent: only links whose OUTPUT port belongs to a node we manage
        (dsp-out + known source nodes) are added/removed, so unrelated links are
        never touched. Muted/stopped sources and stale dsp-out links fall out
        naturally (present in 'current managed' but absent from 'desired')."""
        async with self._reconcile_lock:
            dump = self._pw_dump()
            if not dump:
                return
            names, state, out_ports, in_ports, port_node, links = self._index(dump)

            self._pin_sink_volumes(names)

            dsp_in_id  = names.get(DSP_IN_NODE)
            dsp_out_id = names.get(DSP_OUT_NODE)

            desired = set()                 # (out_port_id, in_port_id)
            managed_out_nodes = set()       # node ids whose output links we own

            # ── dsp-out -> sink.ndi-feed (the only output transport) ─────────
            # CamillaDSP always runs the 6ch passthrough; all 6 dsp-out channels
            # link into the NDI feed sink. The v2-era NDI/S-PDIF transport
            # selector is retired (no S/PDIF hardware on the Pi 5).
            if dsp_out_id is not None:
                managed_out_nodes.add(dsp_out_id)
                src_p = out_ports.get(dsp_out_id, {})
                ndi_id = names.get(SINK_NDI)
                if ndi_id is not None:
                    dst_p = in_ports.get(ndi_id, {})
                    # CH8, not CH6 — dsp-out and sink.ndi-feed are both 8 wide now, and
                    # a CH6 loop silently left lanes 7-8 unlinked (i.e. the surrounds we
                    # widened the chain to keep). Safe at either width: the membership
                    # test below only links ports that actually exist on both nodes.
                    for ch in CH8:
                        if ch in src_p and ch in dst_p:
                            desired.add((src_p[ch], dst_p[ch]))

            # ── each source -> dsp-in (FL/FR or all 6), if unmuted ──────────
            # Linking is decoupled from `state == "running"`: a PW client launched
            # with node.autoconnect=false (squeezelite -o pipewire, shairport via
            # pipewire-alsa, source_router-managed bluez nodes) needs a downstream
            # link before its writes can drain. Without one, snd_pcm_writei blocks
            # in pipewire-alsa, the client thread stalls, and slimproto/RAOP/etc
            # time out — the node never reaches "running" so a state-gated reconcile
            # never authors the link (chicken-and-egg). See P5 session 4 step 1
            # findings. We therefore link unmuted sources unconditionally on node
            # appearance; the `sources_playing` telemetry below stays state-driven
            # and continues to drive `focused_source` / Now Playing.
            now = time.monotonic()
            for name, spec in SOURCES.items():
                nid = self._find_source_node(names, spec)
                if nid is None:
                    if self.sources_playing[name]:
                        logging.info(f"{name}: node gone (stopped).")
                    self.sources_playing[name] = False
                    continue
                managed_out_nodes.add(nid)   # so muted/stale links get cleaned even if not desired
                running = state.get(nid) == "running"
                if running and not self.sources_playing[name]:
                    self._last_active[name] = now
                    logging.info(f"{name}: active.")
                elif not running and self.sources_playing[name]:
                    logging.info(f"{name}: idle.")
                self.sources_playing[name] = running

                if not self.sources_muted[name] and dsp_in_id is not None:
                    # Explicit width -> port-list map. This was `CH6 if channels == 6 else
                    # CH2`, which sent the new 8ch TV source down the STEREO branch and
                    # linked 2 of its 8 lanes — the deploy looked healthy and quietly
                    # dropped more than the bug it was fixing (2026-08-13).
                    chs = {8: CH8, 6: CH6}.get(spec["channels"], CH2)
                    src_p = out_ports.get(nid, {})
                    dst_p = in_ports.get(dsp_in_id, {})
                    for ch in chs:
                        if ch in src_p and ch in dst_p:
                            desired.add((src_p[ch], dst_p[ch]))

            # ── apply the diff (only over managed output nodes) ──────────────
            current = {(o, i) for (o, i) in links
                       if port_node.get(o) in managed_out_nodes}
            for (o, i) in desired - current:
                self._pw_link(o, i)
            for (o, i) in current - desired:
                self._pw_unlink(o, i)

            # ── Bluetooth telemetry (PW-native source, derived from the dump) ────
            # The ardftsrc sources get their native rate from the ALSA layer in
            # _update_ardftsrc_bridges; Bluetooth has no loopback, so read it from the
            # bluez node here. And bridge the node's run-state into bt_state: the pairing
            # state machine only ever reaches "paired", so without this the UI BT button
            # never lights (bt-playing) while audio is actually flowing.
            self._native_rate["Bluetooth"] = self._bluez_native_rate(dump)
            if self.bt_state in ("paired", "playing"):
                self.bt_state = ("playing" if self.sources_playing["Bluetooth"]
                                 else "paired")

            self._update_focused_source()

    def _update_focused_source(self):
        """Focused source = the most-recently-activated source that is currently
        playing AND unmuted. Drives the Now Playing card under mixing."""
        candidates = [n for n in SOURCES
                      if self.sources_playing[n] and not self.sources_muted[n]]
        self.focused_source = (max(candidates, key=lambda n: self._last_active[n])
                               if candidates else None)

    # ====================================================================== #
    #  CamillaDSP                                                             #
    # ====================================================================== #

    def _cdsp_connected(self) -> bool:
        try:
            return self.cdsp.is_connected()
        except Exception:
            return False

    async def _wait_for_cdsp(self) -> bool:
        deadline = asyncio.get_running_loop().time() + CDSP_STARTUP_TIMEOUT
        while asyncio.get_running_loop().time() < deadline:
            try:
                self.cdsp.connect()
                logging.info("CamillaDSP ready.")
                return True
            except Exception:
                await asyncio.sleep(CDSP_STARTUP_POLL)
        logging.error("CamillaDSP did not come up in time.")
        return False

    def _push_config(self) -> bool:
        """Push the static 6ch passthrough config to CamillaDSP. There is only one
        config and we always push it."""
        path = os.path.join(CONFIG_DIR, DSP_PASSTHROUGH)
        try:
            with open(path) as f:
                cfg = yaml.safe_load(f)
            self.cdsp.config.set_active(cfg)
            self.active_config_name = DSP_PASSTHROUGH
            logging.info(f"CamillaDSP config -> {self.active_config_name}.")
            return True
        except Exception as exc:
            logging.error(f"Failed to push {DSP_PASSTHROUGH}: {exc}")
            return False

    def _manage_ndi_output(self, enable: bool):
        action = "restart" if enable else "stop"
        try:
            subprocess.run(["systemctl", action, "ndi-output.service"],
                           check=True, timeout=10)
            logging.info(f"ndi-output {action}ed.")
        except Exception as exc:
            logging.warning(f"ndi-output {action} failed: {exc}")

    def _poll_cdsp(self):
        """Telemetry for the UI. buffer_level at the poll rate; the heavier config
        fetch (for channels / resampler labels) throttled to ~1 Hz."""
        try:
            if not self._cdsp_connected():
                self._cdsp_was_connected = False
                try:
                    self.cdsp.connect()
                except Exception:
                    return
            # On the connect rising edge (incl. a CamillaDSP restart), re-push the static
            # config — CamillaDSP runs with -w and holds no devices until a config arrives,
            # so the dsp-in/dsp-out PipeWire nodes only exist after this push.
            if not self._cdsp_was_connected:
                self._push_config()
                self._cdsp_was_connected = True
            try:
                self.cdsp_state["buffer_level"] = round(self.cdsp.status.buffer_level(), 1)
            except Exception:
                pass
            self._poll_multichannel()
            now = asyncio.get_running_loop().time()
            if now - self._last_config_poll >= 1.0:
                try:
                    cfg = self.cdsp.config.active()
                    dev = cfg.get("devices", {})
                    rs  = dev.get("resampler", {})
                    self.cdsp_state["channels"] = dev.get("playback", {}).get("channels", 6)
                    self.cdsp_state["resampler_type"] = rs.get("type", "None")
                    self.cdsp_state["resampler_profile"] = (
                        rs.get("profile") or rs.get("interpolation") or None)
                    self.cdsp_state["chunksize"]    = dev.get("chunksize")
                    self.cdsp_state["target_level"] = dev.get("target_level")
                    self.cdsp_state["samplerate"]   = dev.get("samplerate")
                    self._last_config_poll = now
                except Exception:
                    pass
        except Exception as exc:
            logging.debug(f"CamillaDSP poll error: {exc}")

    def _poll_multichannel(self):
        """Detect the channel format of the summed bus by reading CamillaDSP's
        per-channel playback RMS. Any non-front channel (ch3 upward) above threshold =
        multichannel; fronts above threshold with silent rears = stereo; silence
        is neither. Debounced in both directions; only sustained changes update
        the flags. The LattePanda kiosk acts on the False->True edge of each."""
        try:
            rms = self.cdsp.levels.playback_rms()
        except Exception:
            return
        if not rms or len(rms) < 6:
            return

        # rms[2:] (open-ended) not rms[2:6]: the bus is 8 wide since 2026-08-13, and this
        # stays correct at either width if dsp_6ch.yml is ever rolled back. Correct for
        # all three cases — 2.0 leaves ch2+ silent; 5.1 lights ch2/ch3; and eARC LPCM
        # lands its surrounds on ch6/ch7 with ch4/ch5 silent, which only a slice reaching
        # the last channel will see.
        active = max(rms[2:]) > MULTICH_RMS_THRESHOLD_DB
        # Gated on the debounced flag too, so a null-rear passage inside a 5.1
        # program can't start the stereo counter while multichannel still holds.
        stereo_now = (not active and not self.multichannel
                      and max(rms[0:2]) > MULTICH_RMS_THRESHOLD_DB)
        if active:
            self._multich_on_count += 1
            self._multich_off_count = 0
        else:
            self._multich_off_count += 1
            self._multich_on_count = 0
        if stereo_now:
            self._stereo_on_count += 1
            self._stereo_off_count = 0
        else:
            self._stereo_off_count += 1
            self._stereo_on_count = 0

        if not self.multichannel and self._multich_on_count >= MULTICH_DEBOUNCE_POLLS:
            self.multichannel = True
            logging.info(f"multichannel detected (non-front RMS {max(rms[2:]):.1f} dB).")
        elif self.multichannel and self._multich_off_count >= MULTICH_DEBOUNCE_POLLS:
            self.multichannel = False
            logging.info("multichannel cleared (front-only / silent).")

        if not self.stereo and self._stereo_on_count >= STEREO_DEBOUNCE_POLLS:
            self.stereo = True
            logging.info(f"stereo detected (front RMS {max(rms[0:2]):.1f} dB, rears silent).")
        elif self.stereo and self._stereo_off_count >= STEREO_DEBOUNCE_POLLS:
            self.stereo = False
            logging.info("stereo cleared (rears active / silent).")

    # ====================================================================== #
    #  UI commands                                                           #
    # ====================================================================== #

    async def set_mute(self, source: str, muted: bool):
        if source not in SOURCES:
            logging.warning(f"set_mute: unknown source {source!r}.")
            return
        if self.sources_muted[source] == muted:
            return
        self.sources_muted[source] = muted
        self._save_sources_muted()
        logging.info(f"{source} {'muted' if muted else 'unmuted'}.")
        await self.reconcile()

    async def toggle_mute(self, source: str):
        if source in SOURCES:
            await self.set_mute(source, not self.sources_muted[source])

    # ====================================================================== #
    #  Bluetooth (state machine ported from v1; pairing is the only manual    #
    #  step — once connected, BT mixes in like any source via reconcile()).   #
    # ====================================================================== #

    def _bt_ctl(self, *commands):
        inp = "\n".join(commands) + "\n"
        try:
            subprocess.run(["bluetoothctl"], input=inp, text=True,
                           capture_output=True, timeout=5)
        except Exception as exc:
            logging.warning(f"bluetoothctl failed: {exc}")

    def _bt_power_on(self):
        subprocess.run(["rfkill", "unblock", "bluetooth"], capture_output=True, check=False)
        self._bt_ctl("power on", "pairable on")
        logging.info("Bluetooth powered on.")

    async def _bt_pairing_timer(self):
        try:
            await asyncio.sleep(BT_PAIRING_TIMEOUT)
            logging.info("BT pairing window closed.")
            self._bt_ctl("discoverable off")
            if self.bt_state == "pairing":
                self.bt_state = "paired" if self.bt_device else "idle"
        except asyncio.CancelledError:
            pass

    def _bt_start_pairing(self):
        if self._bt_pairing_task and not self._bt_pairing_task.done():
            self._bt_pairing_task.cancel()
        self._bt_ctl("discoverable on")
        self.bt_state = "pairing"
        self._bt_pairing_task = asyncio.create_task(self._bt_pairing_timer())
        logging.info(f"BT discoverable for {BT_PAIRING_TIMEOUT}s.")

    def _bt_stop_pairing(self):
        if self._bt_pairing_task and not self._bt_pairing_task.done():
            self._bt_pairing_task.cancel()
        self._bt_pairing_task = None
        self._bt_ctl("discoverable off")
        self.bt_state = "paired" if self.bt_device else "idle"

    async def _poll_bt_state(self):
        """Poll bluetoothctl for connect/disconnect (~1 Hz). Audio 'playing' state comes
        from the PipeWire bluez node in reconcile() like any other source."""
        self._bt_poll_counter += 1
        if self._bt_poll_counter % 2 != 0:   # reconcile runs at 2 Hz -> this is ~1 Hz
            return
        try:
            r = subprocess.run(["bluetoothctl", "devices", "Connected"],
                               capture_output=True, text=True, timeout=2)
            lines = [l.strip() for l in r.stdout.splitlines() if l.strip()]
            if lines:
                parts = lines[0].split(None, 2)
                if len(parts) >= 2:
                    mac  = parts[1]
                    name = parts[2] if len(parts) > 2 else mac
                    if self.bt_device_mac != mac:
                        self.bt_device_mac = mac
                        self.bt_device     = name
                        if self.bt_state != "playing":
                            self.bt_state = "paired"
                        self._bt_stop_pairing()
                        logging.info(f"BT connected: {name} ({mac})")
                        asyncio.create_task(self._bt_post_connect(mac))
            else:
                if self.bt_device_mac is not None:
                    logging.info(f"BT disconnected: {self.bt_device}")
                    self.bt_device     = None
                    self.bt_device_mac = None
                    if self.bt_state in ("paired", "playing"):
                        self.bt_state = "idle"
        except Exception as exc:
            logging.debug(f"BT poll error: {exc}")

    async def _bt_post_connect(self, mac: str):
        """Resolve the device name and trust it (silent future reconnects)."""
        try:
            r = subprocess.run(["bluetoothctl", "info", mac],
                               capture_output=True, text=True, timeout=3)
            for line in r.stdout.splitlines():
                if "Name:" in line and self.bt_device_mac == mac:
                    self.bt_device = line.split("Name:", 1)[1].strip()
                    break
        except Exception:
            pass
        try:
            subprocess.run(["bluetoothctl", "trust", mac], capture_output=True, timeout=3)
            logging.info(f"BT trusted: {mac}")
        except Exception as exc:
            logging.warning(f"BT trust failed for {mac}: {exc}")

    async def _handle_bt_tap(self):
        """BT button tap under the mixing model (BT is always powered — never sleeps):
             idle (no device) -> start pairing (discoverable)
             pairing          -> cancel pairing
             paired/playing   -> toggle BT mute (mixes in / out like any source)
        """
        if self.bt_state == "idle":
            self._bt_start_pairing()
        elif self.bt_state == "pairing":
            self._bt_stop_pairing()
        else:  # paired / playing
            await self.toggle_mute("Bluetooth")

    # ====================================================================== #
    #  WebSocket server                                                       #
    # ====================================================================== #

    def _ui_input_rate(self):
        # The focused source is resampled to the 96k graph rate by its ardftsrc bridge, but the
        # UI INPUT readout shows the *source's native rate* (e.g. 44.1k) — the meaningful "input"
        # the user cares about. None when nothing is focused or the rate is unknown (Bluetooth's
        # A2DP rate has no numid8/hw_params probe; BT live audio is deferred).
        return self._native_rate.get(self.focused_source)

    def _ui_latency_ms(self):
        # Pi-side DSP + PipeWire graph latency in ms, derived from the pinned 96k config:
        # CamillaDSP holds ~target_level frames of playback buffer, plus one PipeWire quantum
        # (== chunksize) on each of the two graph domains (source->dsp-in, dsp-out->sink).
        # Excludes the ardftsrc bridge ALSA capture buffer and the NDITX loopback buffer
        # (those are outside PipeWire's view and need a loopback measurement, not introspection).
        tl = self.cdsp_state.get("target_level")
        cs = self.cdsp_state.get("chunksize")
        sr = self.cdsp_state.get("samplerate")
        if not (tl and cs and sr):
            return None
        return round((tl + 2 * cs) / sr * 1000)

    async def ws_handler(self, websocket):
        last_full_send = 0.0
        try:
            while True:
                t0 = asyncio.get_running_loop().time()
                if t0 - last_full_send >= WS_FULL_STATE_INTERVAL:
                    await websocket.send(json.dumps({
                        "type":              "state",
                        "config_name":       self.active_config_name,
                        "sources_playing":   self.sources_playing,
                        "sources_muted":     self.sources_muted,
                        "focused_source":    self.focused_source,
                        "input_rate":        self._ui_input_rate(),
                        # Hz when the eARC tap is carrying LPCM at a rate we refuse, else
                        # None. Lets the UI say "unsupported rate" instead of showing the
                        # TV source as simply absent.
                        "tv_rate_unsupported": self._earc_rate_unsupported,
                        # Codec name when the tap carries an identifiable but undecodable
                        # bitstream (HBR), else None. Pairs with tv_rate_unsupported so the
                        # UI can say WHY the TV source is silent rather than just absent.
                        "tv_codec_unsupported": self._earc_codec_unsupported,
                        "channels":          self.cdsp_state.get("channels"),
                        "resampler_type":    self.cdsp_state.get("resampler_type"),
                        "resampler_profile": self.cdsp_state.get("resampler_profile"),
                        "multichannel":      self.multichannel,
                        "stereo":            self.stereo,
                        "buffer_level":      self.cdsp_state.get("buffer_level"),
                        "latency_ms":        self._ui_latency_ms(),
                        "bt_state":          self.bt_state,
                        "bt_device":         self.bt_device,
                    }))
                    last_full_send = t0

                elapsed   = asyncio.get_running_loop().time() - t0
                remaining = max(0.001, 0.1 - elapsed)
                try:
                    msg  = await asyncio.wait_for(websocket.recv(), timeout=remaining)
                    data = json.loads(msg)
                    cmd  = data.get("command")

                    if cmd == "set_mute":
                        src = data.get("source")
                        muted = data.get("muted")
                        if src in SOURCES and isinstance(muted, bool):
                            await self.set_mute(src, muted)
                        else:
                            logging.warning(f"set_mute: invalid payload {data}")

                    elif cmd == "toggle_mute" or cmd == "set_source":
                        # set_source kept as a back-compat alias = toggle that source's mute.
                        src = data.get("source")
                        if src == "Bluetooth":
                            await self._handle_bt_tap()
                        elif src in SOURCES:
                            await self.toggle_mute(src)
                        else:
                            logging.warning(f"{cmd}: invalid source {data}")

                    elif cmd == "shutdown":
                        logging.info("Shutdown command from UI.")
                        await websocket.send(json.dumps({"type": "shutdown_ack"}))
                        await asyncio.sleep(0.5)
                        subprocess.run(["shutdown", "-h", "now"])

                    elif cmd == "reboot":
                        logging.info("Reboot command from UI.")
                        await websocket.send(json.dumps({"type": "shutdown_ack"}))
                        await asyncio.sleep(0.5)
                        subprocess.run(["reboot"])

                    elif cmd == "set_brightness":
                        level = data.get("level")
                        if isinstance(level, int) and 0 <= level <= 255:
                            # Glob, not a fixed name: the DSI backlight's kernel device
                            # name encodes the I2C bus number ("10-0045" on the Pi 4B),
                            # which changes across Pi models/kernels.
                            paths = glob.glob("/sys/class/backlight/*/brightness")
                            if not paths:
                                logging.warning("set_brightness: no backlight device found")
                            for path in paths:
                                try:
                                    with open(path, "w") as f:
                                        f.write(str(level))
                                except OSError as exc:
                                    logging.warning(f"set_brightness {path}: {exc}")
                        else:
                            logging.warning(f"set_brightness: invalid {data}")

                except asyncio.TimeoutError:
                    pass

                elapsed = asyncio.get_running_loop().time() - t0
                if elapsed < 0.1:
                    await asyncio.sleep(0.1 - elapsed)
        except websockets.exceptions.ConnectionClosed:
            pass

    # ====================================================================== #
    #  Main loop / entry point                                                #
    # ====================================================================== #

    async def poll_loop(self):
        while True:
            try:
                await self._update_ardftsrc_bridges()   # start/stop bridges -> source.X.ardftsrc nodes
                await self.reconcile()             # link the nodes the bridges created
                self._poll_cdsp()
                await self._poll_bt_state()
            except Exception as exc:
                logging.error(f"poll_loop iteration failed: {exc}")
            await asyncio.sleep(POLL_INTERVAL)

    async def start(self):
        # Bluetooth never sleeps: power it on at boot and leave it on (pairable, so
        # trusted devices reconnect on their own). The BT poll loop upgrades the state
        # to paired/playing once a device connects.
        self._bt_power_on()
        self.bt_state = "idle"
        logging.info(
            f"Starting: ardftsrc_sources={sorted(ARDFTSRC_SOURCES)}, "
            f"sources_muted={self.sources_muted}."
        )

        # Bring CamillaDSP up to its static config so the dsp-in/dsp-out PipeWire nodes
        # exist; reconcile() then links dsp-out -> sink.ndi-feed and sources -> dsp-in.
        if await self._wait_for_cdsp():
            self._push_config()
            self._cdsp_was_connected = True
        else:
            logging.warning("Starting without CamillaDSP; will retry config push on reconnect.")
        # Start the 6ch passthrough transmitter once and leave it running.
        self._manage_ndi_output(enable=True)

        async with websockets.serve(self.ws_handler, "0.0.0.0", 8080):
            logging.info("WebSocket server listening on :8080")
            await self.poll_loop()


if __name__ == "__main__":
    asyncio.run(SourceRouter().start())
