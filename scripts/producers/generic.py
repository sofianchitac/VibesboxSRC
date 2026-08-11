"""GenericProducer — fallback when no transport-specific producer applies.

Emits a Now Playing payload with source label + transport sidecar (sample
rate, channels, active CamillaDSP config). Used for USB Audio (where the
Windows fingerprint service may later override with a real match) and for
any source whose specialised producer is degraded/unavailable.
"""

from __future__ import annotations

import logging
import time

from .base import Producer

log = logging.getLogger(__name__)

# Friendly source labels for the kiosk
SOURCE_LABELS = {
    "USB":       "USB Audio",
    "Lyrion":    "Lyrion",
    "AirPlay":   "AirPlay",
    "Bluetooth": "Bluetooth",
    "Tidal":     "TIDAL",
}


class GenericProducer(Producer):
    name = "generic"

    def __init__(self, sink, source: str, transport: dict | None = None):
        super().__init__(sink)
        self.source     = source
        self.transport  = transport or {}
        self._started_at = time.time()
        self._session_id = f"{source.lower()}-{int(self._started_at)}"

    async def start(self) -> None:
        # Emit once on start. No periodic re-emit — a periodic tick would
        # race with the fingerprint producer and clobber any match the
        # Windows AudioFingerprintService POSTs while USB is the active
        # source.
        await self._emit_track_change()

    async def stop(self) -> None:
        await self.sink.push(self._envelope(
            event="cleared",
        ))

    async def update_transport(self, transport: dict) -> None:
        """Called by the orchestrator when transport changes but the source
        is unchanged (e.g. user toggled output channels). Re-emit so the
        downstream payload reflects the new transport."""
        self.transport = transport or {}
        await self._emit_track_change()

    async def _emit_track_change(self) -> None:
        elapsed = time.time() - self._started_at
        label   = SOURCE_LABELS.get(self.source, self.source)
        await self.sink.push(self._envelope(
            event="track_change",
            playing=True,
            playback_rate=1.0,
            track={
                "unique_id":         f"generic:{self.source}:{self._session_id}",
                "title":             label,
                "artist":            None,
                "album":             None,
                "duration_seconds":  None,
                "elapsed_seconds":   elapsed,
                "elapsed_captured_at": time.time(),
            },
            source_app={"bundle_id": None, "display_name": label},
            artwork=None,
            confidence=None,
            transport={
                "sample_rate": self.transport.get("sample_rate"),
                "channels":    self.transport.get("channels"),
                "config":      self.transport.get("config"),
            },
        ))

