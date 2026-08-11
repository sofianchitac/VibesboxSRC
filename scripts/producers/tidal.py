"""TidalProducer — scrapes the Tidal Connect container's speaker_controller TUI.

The Dockerised `tidal_connect` speaker_controller_application renders a curses
box with the current artist / album / title / duration / position / state. There
is no structured metadata API, so — like upstream's volume-bridge.sh — we capture
its tmux pane via `docker exec` and parse it. The pane carries NO cover art, so
Tidal Now Playing is text-only (title/artist/album/progress).

Pane layout (box-drawn; `x` = vertical border, `xx` = the two-panel divider):
    PlaybackState::PLAYING
    xartists: Faith No More                xx...
    xalbum name: Introduce Yourself        xx...
    xtitle: We Care a Lot                  xx...
    xduration: 242000                      xx...   (milliseconds)
    xsampling rate: 44100                  xx...
                          75 / 242                 (elapsed / total seconds)

Runs only while Tidal is the focused source (orchestrator owns the lifecycle).
Polls at TIDAL_POLL_HZ (default 1.0). Emits track_change on (artist,album,title)
change, state_change on play/pause, cleared on idle/stop or repeated capture
failure. Track identity is hashed from artist|album|title (the pane exposes no
track id).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time

from .base import Producer

log = logging.getLogger(__name__)

CONTAINER = os.environ.get("TIDAL_CONTAINER", "tidal_connect")
POLL_HZ   = float(os.environ.get("TIDAL_POLL_HZ", "1.0"))

# `docker exec` the container's tmux and print the speaker_controller pane.
CAPTURE_CMD = ["docker", "exec", CONTAINER,
               "/usr/bin/tmux", "capture-pane", "-p", "-S", "-60"]

_STATE_RE = re.compile(r"PlaybackState::([A-Z]+)")
_POS_RE   = re.compile(r"^(\d+)\s*/\s*(\d+)$")


def _field(text: str, key: str) -> str | None:
    """Pull the value of a `x<key>: <value>` pane line, trimming the trailing
    padding and the `xx` panel divider. The divider is separated from the value
    by padding spaces, so cut at the first run of >=2 spaces before 'xx' — a
    value legitimately containing ' xx' (the band "The xx") survives. Fall back
    to the old single-space cut only when the value fills the field."""
    prefix = "x" + key + ":"
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        if line.startswith(prefix):
            v = line[len(prefix):]
            m = re.search(r"\s\s+xx", v)
            if m:
                v = v[:m.start()]
            else:
                i = v.find(" xx")
                if i == -1:
                    i = v.find("xx")
                if i != -1:
                    v = v[:i]
            v = v.strip()
            return v or None
    return None


def _parse(text: str) -> dict:
    m = _STATE_RE.search(text)
    state = m.group(1) if m else None

    dur = _field(text, "duration")            # milliseconds
    duration = int(dur) / 1000.0 if (dur and dur.isdigit()) else None

    elapsed = None
    for raw in text.splitlines():
        mm = _POS_RE.match(raw.strip())
        if mm:
            elapsed = float(mm.group(1))
            if duration is None:
                duration = float(mm.group(2))
            break

    rate = _field(text, "sampling rate")
    try:
        rate = int(rate) if rate else None
    except ValueError:
        rate = None

    return {
        "state":    state,
        "title":    _field(text, "title"),
        "artist":   _field(text, "artists"),
        "album":    _field(text, "album name"),
        "duration": duration,
        "elapsed":  elapsed,
        "rate":     rate,
    }


class TidalProducer(Producer):
    name = "tidal"

    def __init__(self, sink):
        super().__init__(sink)
        self._task: asyncio.Task | None = None
        self._last_id: str | None = None
        self._last_playing: bool | None = None
        self._fails = 0
        self._cleared = False

    async def start(self) -> None:
        self._task = asyncio.create_task(self._poll_loop())
        log.info(f"TidalProducer started (container={CONTAINER}, {POLL_HZ}Hz)")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        try:
            await self.sink.push(self._envelope(event="cleared"))
        except Exception as e:
            log.warning(f"final cleared push failed: {e}")

    # ── poll loop ────────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        period = 1.0 / max(POLL_HZ, 0.2)
        while True:
            t0 = time.monotonic()
            text = await self._capture()
            if text is not None:
                try:
                    await self._handle(_parse(text))
                except Exception as e:
                    log.debug(f"handle error: {e}")
            await asyncio.sleep(max(0.1, period - (time.monotonic() - t0)))

    async def _capture(self) -> str | None:
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *CAPTURE_CMD,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=4)
        except (asyncio.TimeoutError, OSError) as e:
            # wait_for cancels the read but not the child: kill it, or a wedged
            # docker leaks one live `docker exec` per poll.
            if proc is not None and proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            self._fails += 1
            if self._fails == 3 and not self._cleared:
                log.warning(f"tmux capture failing ({e}) — emitting cleared")
                self._cleared = True
                # Forget the track so recovery mid-track re-emits it (same tid
                # would otherwise leave the card blank until the next track).
                self._last_id = None
                self._last_playing = None
                try:
                    await self.sink.push(self._envelope(event="cleared"))
                except Exception:
                    pass
            return None
        self._fails = 0
        return out.decode("utf-8", "replace")

    async def _handle(self, p: dict) -> None:
        title = p["title"]
        # No track or not casting → clear the card once.
        if not title or p["state"] in (None, "IDLE", "STOPPED"):
            if self._last_id is not None and not self._cleared:
                self._cleared = True
                await self.sink.push(self._envelope(event="cleared"))
                self._last_id = None
                self._last_playing = None
            return

        self._cleared = False
        playing = p["state"] == "PLAYING"
        tid = hashlib.sha1(
            f"{p['artist']}|{p['album']}|{title}".encode()).hexdigest()[:16]

        if tid != self._last_id:
            await self._emit_track(p, tid, playing)
            self._last_id = tid
            self._last_playing = playing
        elif playing != self._last_playing:
            await self._emit_state(p, tid, playing)
            self._last_playing = playing

    async def _emit_track(self, p: dict, tid: str, playing: bool) -> None:
        await self.sink.push(self._envelope(
            event="track_change",
            playing=playing,
            playback_rate=1.0 if playing else 0.0,
            track={
                "unique_id":           f"tidal:{tid}",
                "title":               p["title"],
                "artist":              p["artist"],
                "album":               p["album"],
                "duration_seconds":    p["duration"],
                "elapsed_seconds":     p["elapsed"] or 0.0,
                "elapsed_captured_at": time.time(),
            },
            source_app={"bundle_id": "tidal", "display_name": "TIDAL"},
            artwork=None,
            confidence=1.0,
            transport={"sample_rate": p["rate"], "channels": 2, "config": None},
        ))

    async def _emit_state(self, p: dict, tid: str, playing: bool) -> None:
        await self.sink.push(self._envelope(
            event="state_change",
            playing=playing,
            playback_rate=1.0 if playing else 0.0,
            track_unique_id=f"tidal:{tid}",
            elapsed_seconds=p["elapsed"] or 0.0,
            elapsed_captured_at=time.time(),
        ))
