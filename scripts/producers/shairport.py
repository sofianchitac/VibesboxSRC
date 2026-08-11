"""ShairportProducer — reads /tmp/shairport-sync-metadata.

shairport-sync emits an XML-ish stream of <item>...</item> blocks. Each
item has type / code (4-char ASCII as hex), length, and optional base64
data.

Parsed codes we care about:
    core/asar  → artist
    core/asal  → album
    core/minm  → title
    core/astm  → duration (ms; ASCII number)
    core/mper  → persistent track id
    core/prgr  → "start/current/end" in DACP frame units (44100 Hz default)
    ssnc/PICT  → cover art binary blob (raw bytes; mime detected from magic)
    ssnc/pbeg  → play begin
    ssnc/pend  → play end / session end → emit cleared if no longer active
    ssnc/prsm  → play resume
    ssnc/paus  → pause
    ssnc/pfls  → flush

We hold a draft track between pbeg/pend (or just on the first metadata
frames) and emit track_change once enough fields are populated and the
unique id has changed.

The pipe parser runs in a background thread to keep the asyncio loop
clean. Parsed events are pushed into an asyncio.Queue and forwarded by
an async task.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import threading
import time

from .base import Producer

log = logging.getLogger(__name__)

PIPE_PATH = "/tmp/shairport-sync-metadata"

# DACP frame unit divisor for the prgr (progress) field.
DACP_RATE = 44100.0

# How long to wait between EOF and reopen attempts (sec).
REOPEN_BACKOFF = 2.0

# If we have a draft track with title but no further events for this long
# AND auto_router reports active_source != AirPlay, the cleared envelope
# is emitted by the orchestrator (we just stop). For idle-while-active
# we don't emit cleared — shairport just isn't pushing.


def _mime_for(blob: bytes) -> str:
    if blob.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if blob.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    return "application/octet-stream"


class ShairportProducer(Producer):
    name = "shairport"

    def __init__(self, sink):
        super().__init__(sink)
        self._queue: asyncio.Queue = asyncio.Queue()
        self._reader_thread: threading.Thread | None = None
        self._forwarder_task: asyncio.Task | None = None
        self._stop = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None

        # Draft track state
        self._t_title:    str | None = None
        self._t_artist:   str | None = None
        self._t_album:    str | None = None
        self._t_dur_sec:  float | None = None
        self._t_mper:     str | None = None
        self._t_art_b64:  str | None = None
        self._t_art_mime: str | None = None
        self._t_art_sha:  str | None = None
        self._prgr_elapsed: float | None = None
        self._prgr_captured_at: float | None = None
        self._playing:   bool = True
        self._last_emitted_uid: str | None = None
        self._last_emitted_art_sha: str | None = None

    # ── lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._reader_thread = threading.Thread(
            target=self._reader_loop, name="shairport-pipe-reader",
            daemon=True,
        )
        self._reader_thread.start()
        self._forwarder_task = asyncio.create_task(self._forward_loop())
        log.info(f"ShairportProducer started, watching {PIPE_PATH}")

        # Immediate generic-style placeholder. Some AirPlay clients (e.g.
        # YouTube on iOS) never push DACP metadata at all — without this
        # the kiosk would show nothing for the entire session even though
        # audio is playing. Real metadata, if it arrives, supersedes via
        # _emit_track_change.
        await self.sink.push(self._envelope(
            event="track_change",
            playing=True,
            playback_rate=1.0,
            track={
                "unique_id":          "shairport:placeholder",
                "title":              "AirPlay",
                "artist":             None,
                "album":              None,
                "duration_seconds":   None,
                "elapsed_seconds":    0.0,
                "elapsed_captured_at": time.time(),
            },
            source_app={"bundle_id": None, "display_name": "AirPlay"},
            artwork=None,
            confidence=None,
            transport=None,
        ))

    async def stop(self) -> None:
        self._stop.set()
        if self._forwarder_task:
            self._forwarder_task.cancel()
            try:
                await self._forwarder_task
            except asyncio.CancelledError:
                pass
        if self._reader_thread and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=2.0)
        # Final cleared so the kiosk leaves AirPlay state cleanly
        try:
            await self.sink.push(self._envelope(event="cleared"))
        except Exception as e:
            log.warning(f"Final cleared push failed: {e}")

    # ── pipe reader thread ───────────────────────────────────────────────

    def _reader_loop(self) -> None:
        """Open the FIFO and parse <item> blocks. Reopen on EOF."""
        while not self._stop.is_set():
            try:
                fd = os.open(PIPE_PATH, os.O_RDONLY)
            except FileNotFoundError:
                log.info(f"{PIPE_PATH} missing; waiting")
                self._stop.wait(REOPEN_BACKOFF)
                continue
            except OSError as e:
                log.warning(f"open({PIPE_PATH}) failed: {e}")
                self._stop.wait(REOPEN_BACKOFF)
                continue

            try:
                with os.fdopen(fd, "rb", buffering=0) as f:
                    self._parse_stream(f)
            except Exception as e:
                log.warning(f"pipe reader crashed: {e}")
            finally:
                if not self._stop.is_set():
                    self._stop.wait(REOPEN_BACKOFF)

    def _parse_stream(self, f) -> None:
        """Read raw bytes from the FIFO, parse <item>...</item> blocks."""
        buf = b""
        while not self._stop.is_set():
            chunk = f.read(4096)
            if not chunk:
                return  # EOF — caller reopens
            buf += chunk
            while True:
                start = buf.find(b"<item>")
                end   = buf.find(b"</item>", start + 6) if start != -1 else -1
                if start == -1 or end == -1:
                    break
                item = buf[start + 6:end]
                buf  = buf[end + 7:]
                try:
                    self._handle_item(item)
                except Exception as e:
                    log.debug(f"item parse error: {e}")

    @staticmethod
    def _tag(item: bytes, name: bytes) -> bytes | None:
        open_tag  = b"<" + name + b">"
        close_tag = b"</" + name + b">"
        i = item.find(open_tag)
        if i == -1:
            return None
        j = item.find(close_tag, i + len(open_tag))
        if j == -1:
            return None
        return item[i + len(open_tag):j]

    def _handle_item(self, item: bytes) -> None:
        t_hex = self._tag(item, b"type")
        c_hex = self._tag(item, b"code")
        if t_hex is None or c_hex is None:
            return
        try:
            t = bytes.fromhex(t_hex.strip().decode()).decode("ascii", "replace")
            c = bytes.fromhex(c_hex.strip().decode()).decode("ascii", "replace")
        except (ValueError, UnicodeDecodeError):
            return

        data_tag = self._tag(item, b"data")
        data: bytes = b""
        if data_tag is not None:
            # data tag may be like: encoding="base64">BASE64
            gt = data_tag.find(b">")
            if gt != -1:
                b64 = data_tag[gt + 1:].strip()
            else:
                b64 = data_tag.strip()
            try:
                data = base64.b64decode(b64) if b64 else b""
            except (ValueError, base64.binascii.Error):
                data = b""

        self._dispatch(t, c, data)

    def _dispatch(self, t: str, c: str, data: bytes) -> None:
        if t == "core":
            if c == "asar":
                self._t_artist = data.decode("utf-8", "replace") or None
            elif c == "asal":
                self._t_album = data.decode("utf-8", "replace") or None
            elif c == "minm":
                self._t_title = data.decode("utf-8", "replace") or None
            elif c == "astm":
                try:
                    self._t_dur_sec = int(data.decode().strip()) / 1000.0
                except (ValueError, UnicodeDecodeError):
                    pass
            elif c == "mper":
                # 8-byte big-endian unsigned
                if len(data) == 8:
                    self._t_mper = data.hex()
                else:
                    self._t_mper = data.decode("utf-8", "replace").strip() or None
            elif c == "prgr":
                # ASCII "A/B/C"
                try:
                    a, b, c2 = data.decode().strip().split("/")
                    a_f, b_f, c_f = float(a), float(b), float(c2)
                    self._t_dur_sec = (c_f - a_f) / DACP_RATE
                    self._prgr_elapsed = (b_f - a_f) / DACP_RATE
                    self._prgr_captured_at = time.time()
                except (ValueError, UnicodeDecodeError):
                    pass
        elif t == "ssnc":
            if c == "PICT" and data:
                self._t_art_sha  = hashlib.sha256(data).hexdigest()
                self._t_art_mime = _mime_for(data)
                self._t_art_b64  = base64.b64encode(data).decode("ascii")
            elif c == "pbeg":
                self._playing = True
            elif c == "prsm":
                self._playing = True
                self._emit_state_change()
            elif c == "paus":
                self._playing = False
                self._emit_state_change()
            elif c == "pend":
                self._enqueue({"event": "cleared"})
                self._reset_draft()
            elif c == "pfls":
                # Flush — clears draft; new track usually follows
                self._reset_draft()
            elif c == "mdst":
                # Metadata bundle start — fresh draft
                self._reset_draft()
            elif c == "mden":
                # Metadata bundle end — emit if we have a title
                if self._t_title:
                    self._emit_track_change()

    # ── emit helpers ─────────────────────────────────────────────────────

    def _emit_track_change(self) -> None:
        uid = self._t_mper or self._fallback_uid()
        # Suppress duplicate emits unless artwork just arrived
        if (uid == self._last_emitted_uid and
                self._t_art_sha == self._last_emitted_art_sha):
            return

        artwork = None
        if self._t_art_b64 and self._t_art_mime:
            artwork = {
                "mime":        self._t_art_mime,
                "sha256":      self._t_art_sha,
                "data_base64": self._t_art_b64,
            }

        now = time.time()
        elapsed = self._prgr_elapsed if self._prgr_elapsed is not None else 0.0
        captured_at = self._prgr_captured_at if self._prgr_captured_at is not None else now

        self._enqueue({
            "event":         "track_change",
            "playing":       self._playing,
            "playback_rate": 1.0 if self._playing else 0.0,
            "track": {
                "unique_id":          f"shairport:{uid}",
                "title":              self._t_title,
                "artist":             self._t_artist,
                "album":              self._t_album,
                "duration_seconds":   self._t_dur_sec,
                "elapsed_seconds":    elapsed,
                "elapsed_captured_at": captured_at,
            },
            "source_app":   {"bundle_id": None, "display_name": "AirPlay"},
            "artwork":      artwork,
            "confidence":   1.0,
            "transport":    None,
        })
        self._last_emitted_uid     = uid
        self._last_emitted_art_sha = self._t_art_sha

    def _emit_state_change(self) -> None:
        if not self._last_emitted_uid:
            return
        now = time.time()
        elapsed = self._prgr_elapsed if self._prgr_elapsed is not None else 0.0
        captured_at = self._prgr_captured_at if self._prgr_captured_at is not None else now
        self._enqueue({
            "event":         "state_change",
            "playing":       self._playing,
            "playback_rate": 1.0 if self._playing else 0.0,
            "track_unique_id": f"shairport:{self._last_emitted_uid}",
            "elapsed_seconds": elapsed,
            "elapsed_captured_at": captured_at,
        })

    def _fallback_uid(self) -> str:
        h = hashlib.sha256(
            f"{self._t_title}|{self._t_artist}|{self._t_album}".encode("utf-8")
        ).hexdigest()[:16]
        return f"hash:{h}"

    def _reset_draft(self) -> None:
        self._t_title = self._t_artist = self._t_album = None
        self._t_dur_sec = None
        self._t_mper = None
        self._t_art_b64 = self._t_art_mime = self._t_art_sha = None
        self._prgr_elapsed = None
        self._prgr_captured_at = None

    def _enqueue(self, fields: dict) -> None:
        """Cross-thread enqueue from reader thread to asyncio loop."""
        if self._loop is None or self._loop.is_closed():
            return
        try:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, fields)
        except RuntimeError:
            pass

    # ── async forwarder ──────────────────────────────────────────────────

    async def _forward_loop(self) -> None:
        try:
            while True:
                fields = await self._queue.get()
                try:
                    await self.sink.push(self._envelope(**fields))
                except Exception as e:
                    log.warning(f"sink push failed: {e}")
        except asyncio.CancelledError:
            raise
