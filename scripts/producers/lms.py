"""LmsProducer — polls Lyrion Media Server's JSON-RPC for the squeezelite player.

LMS lives on a different machine on the LAN. Config from env:
    LMS_HOST       (required)
    LMS_PORT       (default 9000)
    LMS_PLAYER_ID  (MAC address or player name; required)
    LMS_POLL_HZ    (default 1.0)

Track change detected by playlist_loop[0].id. Pause/resume detected by
top-level mode ("play" / "pause" / "stop").

Cover art is fetched as a separate GET to /music/<coverid>/ (asking LMS to
resize — see COVER_SPEC) and base64-inlined into the track_change payload
(cached by SHA-256 between re-emits of the same track).

Degrades to "cleared" if LMS unreachable for >3 consecutive polls; the
orchestrator will fall back to Generic for Lyrion in that case.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import time

import aiohttp

from .base import Producer

log = logging.getLogger(__name__)

LMS_HOST = os.environ.get("LMS_HOST", "")
LMS_PORT = int(os.environ.get("LMS_PORT", "9000"))
PLAYER   = os.environ.get("LMS_PLAYER_ID", "")
POLL_HZ  = float(os.environ.get("LMS_POLL_HZ", "1.0"))

STATUS_TAGS = "adcKlmprt"  # a=artist l=album d=duration c=coverid K=artwork_url etc.

# LMS serves the cover at whatever resolution is embedded in the file, which on
# hi-res releases is multi-megabyte (one measured at 4.34MB → 5.8MB base64,
# past nowplaying-server's 4MB client_max_size, so every POST 413'd and every
# client's card froze on the last track that fit). Ask LMS to resize instead:
# the same cover at 600x600 is ~85KB. Both consumers draw it at 200px, so 600
# is already 3x for HiDPI.
#   Syntax is cover_<W>x<H>_<mode>.jpg — `cover_600.jpg` is NOT a resize form,
#   it silently returns the original.
COVER_SPEC = "cover_600x600_o.jpg"

# Backstop for art we can't ask to be resized (a remote artwork_url). Raw
# bytes; base64 adds ~33%. Dropping the art still delivers the metadata —
# a frozen card everywhere is far worse than a missing thumbnail.
MAX_ART_BYTES = 2 * 1024 * 1024


def _sized(path: str) -> str:
    """Swap a bare LMS cover filename for the resized one.

    The K tag hands back a server-relative path like /music/<id>/cover.jpg.
    Anything else (a remote URL, an already-sized name) is left alone — the
    MAX_ART_BYTES guard covers those.
    """
    if path.endswith("/cover.jpg") or path.endswith("/cover"):
        return path.rsplit("/", 1)[0] + "/" + COVER_SPEC
    return path


def _mime_for_url(url: str) -> str:
    u = url.lower()
    if u.endswith(".png"):
        return "image/png"
    return "image/jpeg"


class LmsProducer(Producer):
    name = "lms"

    def __init__(self, sink):
        super().__init__(sink)
        self._task: asyncio.Task | None = None
        self._session: aiohttp.ClientSession | None = None

        self._last_track_id: str | None = None
        self._last_mode:     str | None = None
        self._last_art_sha:  str | None = None
        self._last_art_payload: dict | None = None
        self._consecutive_failures = 0
        self._degraded_emitted = False

    async def start(self) -> None:
        if not LMS_HOST or not PLAYER:
            log.warning("LMS_HOST or LMS_PLAYER_ID unset — LmsProducer will not run")
            return
        self._session = aiohttp.ClientSession()
        self._task = asyncio.create_task(self._poll_loop())
        log.info(f"LmsProducer started, polling {LMS_HOST}:{LMS_PORT} player={PLAYER}")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._session:
            await self._session.close()
            self._session = None
        try:
            await self.sink.push(self._envelope(event="cleared"))
        except Exception as e:
            log.warning(f"Final cleared push failed: {e}")

    # ── poll loop ────────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        period = 1.0 / max(POLL_HZ, 0.1)
        while True:
            t0 = time.monotonic()
            try:
                status = await self._fetch_status()
                self._consecutive_failures = 0
                self._degraded_emitted = False
                if status is not None:
                    await self._handle_status(status)
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                self._consecutive_failures += 1
                if self._consecutive_failures == 3 and not self._degraded_emitted:
                    log.warning(f"LMS unreachable: {e} — emitting cleared")
                    self._degraded_emitted = True
                    try:
                        await self.sink.push(self._envelope(event="cleared"))
                    except Exception:
                        pass

            elapsed = time.monotonic() - t0
            await asyncio.sleep(max(0.05, period - elapsed))

    async def _fetch_status(self) -> dict | None:
        url = f"http://{LMS_HOST}:{LMS_PORT}/jsonrpc.js"
        body = {
            "id": 1,
            "method": "slim.request",
            "params": [PLAYER, ["status", "-", 1, f"tags:{STATUS_TAGS}"]],
        }
        async with self._session.post(url, json=body,
                                      timeout=aiohttp.ClientTimeout(total=3)) as r:
            r.raise_for_status()
            data = await r.json()
        return data.get("result")

    async def _handle_status(self, result: dict) -> None:
        loop = result.get("playlist_loop") or []
        mode = result.get("mode", "stop")
        if not loop or mode == "stop":
            if self._last_track_id is not None:
                await self.sink.push(self._envelope(event="cleared"))
                self._reset_track()
            return

        track    = loop[0]
        track_id = str(track.get("id", ""))
        if not track_id:
            return

        if track_id != self._last_track_id:
            await self._emit_track_change(result, track)
            self._last_track_id = track_id
            self._last_mode = mode
        elif mode != self._last_mode:
            await self._emit_state_change(result, mode)
            self._last_mode = mode

    async def _emit_track_change(self, result: dict, track: dict) -> None:
        duration = result.get("duration") or track.get("duration") or 0
        elapsed  = result.get("time") or 0
        try:
            duration = float(duration)
        except (TypeError, ValueError):
            duration = None
        try:
            elapsed = float(elapsed)
        except (TypeError, ValueError):
            elapsed = 0.0

        artwork = await self._fetch_artwork(track)

        playing = result.get("mode") == "play"
        await self.sink.push(self._envelope(
            event="track_change",
            playing=playing,
            playback_rate=1.0 if playing else 0.0,
            track={
                "unique_id":          f"lms:track:{track.get('id')}",
                "title":              track.get("title"),
                "artist":             track.get("artist"),
                "album":              track.get("album"),
                "duration_seconds":   duration,
                "elapsed_seconds":    elapsed,
                "elapsed_captured_at": time.time(),
            },
            source_app={"bundle_id": None, "display_name": "Lyrion"},
            artwork=artwork,
            confidence=1.0,
            transport=None,
        ))

    async def _emit_state_change(self, result: dict, mode: str) -> None:
        playing = mode == "play"
        elapsed = result.get("time") or 0
        try:
            elapsed = float(elapsed)
        except (TypeError, ValueError):
            elapsed = 0.0
        await self.sink.push(self._envelope(
            event="state_change",
            playing=playing,
            playback_rate=1.0 if playing else 0.0,
            track_unique_id=f"lms:track:{self._last_track_id}",
            elapsed_seconds=elapsed,
            elapsed_captured_at=time.time(),
        ))

    async def _fetch_artwork(self, track: dict) -> dict | None:
        # Prefer artwork_url (K tag) if present; else /music/<coverid>/cover.jpg
        url = track.get("artwork_url")
        if url and url.startswith("/"):
            url = f"http://{LMS_HOST}:{LMS_PORT}{_sized(url)}"
        elif not url:
            coverid = track.get("coverid")
            if coverid:
                url = f"http://{LMS_HOST}:{LMS_PORT}/music/{coverid}/{COVER_SPEC}"
        if not url:
            return None

        try:
            async with self._session.get(url,
                                         timeout=aiohttp.ClientTimeout(total=5)) as r:
                if r.status != 200:
                    return None
                blob = await r.read()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.debug(f"artwork fetch failed: {e}")
            return None

        if len(blob) > MAX_ART_BYTES:
            log.warning(f"artwork {len(blob)}B exceeds {MAX_ART_BYTES}B "
                        f"({url}) — sending track without it")
            return None

        sha = hashlib.sha256(blob).hexdigest()
        if sha == self._last_art_sha and self._last_art_payload is not None:
            return self._last_art_payload

        payload = {
            "mime":        _mime_for_url(url),
            "sha256":      sha,
            "data_base64": base64.b64encode(blob).decode("ascii"),
        }
        self._last_art_sha = sha
        self._last_art_payload = payload
        return payload

    def _reset_track(self) -> None:
        self._last_track_id = None
        self._last_mode = None
        self._last_art_sha = None
        self._last_art_payload = None
