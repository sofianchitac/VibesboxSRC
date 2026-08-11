#!/usr/bin/env python3
"""
Now Playing Server

aiohttp HTTP + WebSocket service. Single source of truth for the kiosk's
Now Playing card.

  GET  /healthz            — per-producer last-seen + service liveness
  GET  /api/nowplaying     — last cached event
  POST /api/nowplaying     — producer pushes a new event (validated, broadcast)
  POST /api/fingerprint    — audio clip upload (WAV body); we run shazamio,
                              on match build the standard 'fingerprint' producer
                              payload and broadcast it like any other producer
  POST /api/control/{control}/{direction}
                            — nudge a DSP control one step; sends OSC to REAPER
                              on the LattePanda (fire-and-forget, no echo back)
  WS   /ws                 — kiosk subscribes; receives cached state on connect

Producers (Pi-local orchestrator, Windows fingerprint service, anything else)
all push via the same POST endpoint — uniform API regardless of location.

JSON schema (see plan):
  events: "track_change" | "state_change" | "cleared"
  producers: "shairport" | "lms" | "bluealsa" | "fingerprint" | "generic" | "tidal"
  schema: 1
"""

import asyncio
import json
import logging
import os
import socket
import struct
import tempfile
import time
from pathlib import Path

from aiohttp import WSMsgType, web
from shazamio import Shazam

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s [NowPlayingServer] %(message)s')
log = logging.getLogger(__name__)

BIND       = os.environ.get("NOWPLAYING_BIND", "0.0.0.0")
PORT       = int(os.environ.get("NOWPLAYING_PORT", "8090"))
CACHE_FILE = Path(os.environ.get("NOWPLAYING_CACHE_FILE",
                                 "/run/vibesbox-nowplaying-cache.json"))

SCHEMA_VERSION = 1
VALID_EVENTS   = {"track_change", "state_change", "cleared"}
VALID_PRODUCERS = {"shairport", "lms", "bluealsa", "fingerprint", "generic", "tidal"}

def _default_dsp_host() -> str:
    """DSP unit hostname, written by install.sh from $DSP_HOST."""
    try:
        return Path("/etc/vibesbox/dsp-host").read_text().strip() or "vibesbox-dsp.local"
    except OSError:
        return "vibesbox-dsp.local"


REAPER_OSC_HOST = os.environ.get("REAPER_OSC_HOST", _default_dsp_host())
REAPER_OSC_PORT = int(os.environ.get("REAPER_OSC_PORT", "8000"))

# The only OSC the remote can emit. ReaLearn maps each <base>/up and <base>/down
# to an Incremental button on the same target the Pico MIDI knobs drive; /down
# uses ReaLearn's Reverse, so both directions carry a plain 1.0.
OSC_CONTROLS = {
    "volume": "/vibesbox/master/db",
    "bass":   "/vibesbox/eq/bass",
    "treble": "/vibesbox/eq/treble",
}


def validate(payload: dict) -> str | None:
    """Return None if valid, else a short error string."""
    if not isinstance(payload, dict):
        return "payload not an object"
    if payload.get("schema") != SCHEMA_VERSION:
        return f"unsupported schema {payload.get('schema')!r}"
    event = payload.get("event")
    if event not in VALID_EVENTS:
        return f"invalid event {event!r}"
    producer = payload.get("producer")
    if producer not in VALID_PRODUCERS:
        return f"invalid producer {producer!r}"
    if not isinstance(payload.get("sent_at"), (int, float)):
        return "sent_at must be a number"
    if event == "track_change":
        track = payload.get("track")
        if not isinstance(track, dict) or not track.get("title"):
            return "track_change requires track.title"
    return None


class State:
    """Shared in-memory state. Singular cache, plus per-producer health."""

    def __init__(self):
        self.cached_event: dict | None = None
        self.clients: set[web.WebSocketResponse] = set()
        self.producer_last_seen: dict[str, float] = {}

    def update_from_payload(self, payload: dict) -> None:
        self.cached_event = payload
        self.producer_last_seen[payload["producer"]] = time.time()

    def load_cache_from_disk(self) -> None:
        try:
            data = json.loads(CACHE_FILE.read_text())
            err  = validate(data)
            if err is None:
                self.cached_event = data
                log.info(f"Warm-start: loaded cached event from {CACHE_FILE}")
            else:
                log.info(f"Warm-start cache invalid ({err}); starting cold")
        except FileNotFoundError:
            log.info("No warm-start cache file; starting cold")
        except (OSError, json.JSONDecodeError) as e:
            log.warning(f"Warm-start cache read failed: {e}")

    def persist_cache_to_disk(self) -> None:
        if self.cached_event is None:
            return
        try:
            CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            CACHE_FILE.write_text(json.dumps(self.cached_event))
        except OSError as e:
            log.warning(f"Cache persist failed: {e}")


async def broadcast(state: State, payload: dict) -> None:
    """Push to every connected WS client; drop slow ones."""
    if not state.clients:
        return
    data = json.dumps(payload)
    dead = []
    for ws in state.clients:
        try:
            await ws.send_str(data)
        except (ConnectionError, asyncio.CancelledError):
            dead.append(ws)
        except Exception as e:
            log.warning(f"WS send failed, dropping client: {e}")
            dead.append(ws)
    for ws in dead:
        state.clients.discard(ws)


async def handle_post(request: web.Request) -> web.Response:
    state: State = request.app["state"]
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid JSON"}, status=400)

    err = validate(payload)
    if err is not None:
        return web.json_response({"error": err}, status=400)

    state.update_from_payload(payload)
    state.persist_cache_to_disk()
    await broadcast(state, payload)

    log.info(f"POST accepted: {payload['producer']}/{payload['event']}")
    return web.Response(status=204)


async def handle_get(request: web.Request) -> web.Response:
    state: State = request.app["state"]
    return web.json_response(state.cached_event)


async def handle_healthz(request: web.Request) -> web.Response:
    state: State = request.app["state"]
    now = time.time()
    return web.json_response({
        "ok": True,
        "uptime_seconds": now - request.app["started_at"],
        "ws_clients": len(state.clients),
        "producer_last_seen_seconds_ago": {
            p: round(now - ts, 1) for p, ts in state.producer_last_seen.items()
        },
        "has_cached_event": state.cached_event is not None,
    })


# ── Audio fingerprinting (shazamio) ─────────────────────────────────────

# One Shazam client, reused across requests. shazamio is async and stateless;
# the heavy fingerprint compute runs in shazamio_core (Rust extension, GIL-releasing).
_shazam = Shazam()


def _extract_album(track: dict) -> str | None:
    """Album lives under sections[type=SONG].metadata[title=Album].text."""
    for sec in track.get("sections") or []:
        if sec.get("type") == "SONG":
            for m in sec.get("metadata") or []:
                if m.get("title") == "Album":
                    return m.get("text")
    return None


async def handle_fingerprint(request: web.Request) -> web.Response:
    """Accept a WAV upload, recognize via shazamio, broadcast as fingerprint
    producer event on match. No-op on miss."""
    state: State = request.app["state"]

    body = await request.read()
    if not body:
        return web.json_response({"error": "empty body"}, status=400)

    # Save to a temp file (shazamio takes a path or bytes; path is simpler
    # and lets us log the size for debugging).
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        tmp_path = f.name
        f.write(body)

    try:
        try:
            result = await _shazam.recognize(tmp_path)
        except Exception as e:
            log.warning(f"shazamio recognize failed: {e}")
            return web.json_response({"matched": False, "error": str(e)}, status=200)

        track = result.get("track")
        if not track or not track.get("title"):
            log.info(f"Fingerprint: no match (clip {len(body)} bytes)")
            return web.json_response({"matched": False}, status=200)

        title  = track.get("title")
        artist = track.get("subtitle")
        album  = _extract_album(track)
        key    = track.get("key", "") or ""

        payload = {
            "schema":       SCHEMA_VERSION,
            "producer":     "fingerprint",
            "sent_at":      time.time(),
            "event":        "track_change",
            "playing":      True,
            "playback_rate": 1.0,
            "track": {
                "unique_id":          f"fp:shazam:{key}",
                "title":              title,
                "artist":             artist,
                "album":              album,
                "duration_seconds":   None,
                "elapsed_seconds":    0.0,
                "elapsed_captured_at": time.time(),
            },
            "source_app": {"bundle_id": None, "display_name": "USB Audio"},
            "artwork":    None,
            "confidence": 1.0,
            "transport":  None,
        }
        state.update_from_payload(payload)
        state.persist_cache_to_disk()
        await broadcast(state, payload)
        log.info(f"Fingerprint match: {title} — {artist}")
        return web.json_response({"matched": True, "title": title, "artist": artist})
    finally:
        try: os.unlink(tmp_path)
        except OSError: pass


# ── DSP control (OSC → REAPER on the LattePanda) ────────────────────────

def _osc_message(address: str) -> bytes:
    """Minimal OSC 1.0 packet: NUL-padded address, ',f' typetag, float 1.0."""
    addr = address.encode() + b"\0"
    addr += b"\0" * (-len(addr) % 4)
    return addr + b",f\0\0" + struct.pack(">f", 1.0)


async def handle_control(request: web.Request) -> web.Response:
    """One relative step on volume/bass/treble. Fire-and-forget: a 204 means
    the packet went out, not that REAPER applied it — the DSP kiosk's own HUD
    overlay is the confirmation."""
    base      = OSC_CONTROLS.get(request.match_info["control"])
    direction = request.match_info["direction"]
    if base is None or direction not in ("up", "down"):
        return web.json_response({"error": "unknown control"}, status=404)

    # Resolve every time: the LattePanda's DHCP lease churns and a cached
    # address stays pingable on whatever box inherited it.
    loop = asyncio.get_running_loop()
    try:
        info = await loop.getaddrinfo(REAPER_OSC_HOST, REAPER_OSC_PORT,
                                      family=socket.AF_INET,
                                      type=socket.SOCK_DGRAM)
    except socket.gaierror as e:
        log.warning(f"OSC target {REAPER_OSC_HOST} unresolvable: {e}")
        return web.json_response({"error": "dsp-unresolvable"}, status=503)

    address = f"{base}/{direction}"
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.sendto(_osc_message(address), info[0][4])
    log.info(f"OSC {address} -> {REAPER_OSC_HOST}:{REAPER_OSC_PORT}")
    return web.Response(status=204)


async def handle_ws(request: web.Request) -> web.WebSocketResponse:
    state: State = request.app["state"]
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    state.clients.add(ws)
    log.info(f"WS client connected ({len(state.clients)} total)")

    try:
        if state.cached_event is not None:
            await ws.send_str(json.dumps(state.cached_event))

        async for msg in ws:
            if msg.type == WSMsgType.ERROR:
                log.warning(f"WS connection error: {ws.exception()}")
                break
    finally:
        state.clients.discard(ws)
        log.info(f"WS client disconnected ({len(state.clients)} remaining)")

    return ws


def make_app() -> web.Application:
    app = web.Application(client_max_size=4 * 1024 * 1024)  # allow ~4MB artwork
    app["state"]       = State()
    app["started_at"]  = time.time()
    app["state"].load_cache_from_disk()

    app.router.add_post("/api/nowplaying",  handle_post)
    app.router.add_get( "/api/nowplaying",  handle_get)
    app.router.add_post("/api/fingerprint", handle_fingerprint)
    app.router.add_post("/api/control/{control}/{direction}", handle_control)
    app.router.add_get( "/healthz",         handle_healthz)
    app.router.add_get( "/ws",              handle_ws)
    return app


def main() -> None:
    log.info(f"Listening on {BIND}:{PORT}")
    web.run_app(make_app(), host=BIND, port=PORT, print=None,
                access_log=None)


if __name__ == "__main__":
    main()
