#!/usr/bin/env python3
"""
Metadata Orchestrator

Subscribes to the router's WebSocket on :8080 to learn the focused source
(v2 source_router: the most-recently-active unmuted source under mixing; v1
auto_router: the single active source), then runs one matching producer at a
time. Each producer POSTs JSON
payloads to nowplaying_server (localhost:8090). Windows fingerprint
service POSTs to the same endpoint independently.

Source → producer mapping:
    USB       → GenericProducer            (Windows fingerprint may override)
    Lyrion    → LmsProducer    (TODO)      (Generic during degraded/loading)
    AirPlay   → ShairportProducer (TODO)
    Bluetooth → BluealsaProducer (TODO)
    Tidal     → TidalProducer             (scrapes the container's tmux pane)
    None      → no producer; orchestrator emits one "cleared" envelope.

Producers are short-lived: stopped immediately on source change. State is
not shared across producer instances.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time

import aiohttp

from producers import Producer
from producers.generic import GenericProducer
# Other producers imported lazily so the orchestrator can run before the
# specialised producer code lands.

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s [Orchestrator] %(message)s')
log = logging.getLogger(__name__)

AUTOROUTER_WS = os.environ.get("AUTOROUTER_WS", "ws://127.0.0.1:8080")
NOWPLAYING_URL = os.environ.get("NOWPLAYING_URL",
                                "http://127.0.0.1:8090/api/nowplaying")


class HttpSink:
    """POSTs payloads to nowplaying_server. Drops on failure."""

    def __init__(self, session: aiohttp.ClientSession, url: str):
        self.session = session
        self.url     = url

    async def push(self, payload: dict) -> None:
        try:
            async with self.session.post(self.url, json=payload,
                                         timeout=aiohttp.ClientTimeout(total=5)) as r:
                if r.status >= 400:
                    body = await r.text()
                    log.warning(f"POST {payload.get('producer')}/{payload.get('event')} "
                                f"→ {r.status}: {body[:200]}")
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.warning(f"POST failed: {e}")


def producer_for_source(source: str | None, sink, transport: dict,
                        bt_device: str | None = None) -> Producer | None:
    """Pick the right producer for the current source.

    Specialised producers (Shairport/Lms/Bluealsa) will be wired in once
    their modules land. For now everything falls through to Generic, which
    is the explicit v1 behaviour for USB anyway.
    """
    if source is None:
        return None

    if source == "AirPlay":
        try:
            from producers.shairport import ShairportProducer
            return ShairportProducer(sink)
        except ImportError:
            log.info("ShairportProducer not available yet; using Generic")
            return GenericProducer(sink, source, transport)

    if source == "Lyrion":
        try:
            from producers.lms import LmsProducer
            return LmsProducer(sink)
        except ImportError:
            log.info("LmsProducer not available yet; using Generic")
            return GenericProducer(sink, source, transport)

    if source == "Bluetooth":
        try:
            from producers.bluealsa import BluealsaProducer
            return BluealsaProducer(sink, device_name=bt_device)
        except ImportError:
            log.info("BluealsaProducer not available yet; using Generic")
            return GenericProducer(sink, source, transport)

    if source == "Tidal":
        # Unconditional (tidal.py ships with this repo): an ImportError fallback
        # here would mask a real defect in tidal.py as a silent Generic downgrade.
        from producers.tidal import TidalProducer
        return TidalProducer(sink)

    # USB and anything else → Generic
    return GenericProducer(sink, source, transport)


class Orchestrator:
    def __init__(self):
        self.current_source:   str | None = None
        self.current_producer: Producer | None = None
        self.current_transport: dict = {}

    async def run(self) -> None:
        async with aiohttp.ClientSession() as session:
            sink = HttpSink(session, NOWPLAYING_URL)

            backoff = 1.0
            while True:
                try:
                    await self._connect_and_consume(session, sink)
                    backoff = 1.0  # successful run resets backoff
                except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                    log.warning(f"source-router connection lost: {e} — "
                                f"reconnecting in {backoff:.0f}s")
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)

    async def _connect_and_consume(self,
                                   session: aiohttp.ClientSession,
                                   sink: HttpSink) -> None:
        log.info(f"Connecting to {AUTOROUTER_WS}")
        async with session.ws_connect(AUTOROUTER_WS,
                                      heartbeat=30,
                                      timeout=10) as ws:
            log.info("Connected to source-router")
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    frame = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                if frame.get("type") != "state":
                    continue
                await self._on_state(sink, frame)

    async def _on_state(self, sink: HttpSink, frame: dict) -> None:
        # v2 (PipeWire backbone): the daemon mixes all unmuted sources, so there is no
        # single active_source. "focused_source" = the most-recently-active unmuted source
        # and is what drives the Now Playing card.
        source    = frame.get("focused_source")
        transport = {
            "sample_rate": frame.get("input_rate"),
            # CamillaDSP playback channel count (always 6 — the retired
            # output_channels transport-selector field is gone from the frame).
            "channels":    frame.get("channels"),
            "config":      frame.get("config_name"),
        }

        if source != self.current_source:
            log.info(f"Source change: {self.current_source!r} -> {source!r}")
            await self._stop_current()
            self.current_source    = source
            self.current_transport = transport
            self.current_producer  = producer_for_source(source, sink, transport,
                                                          frame.get("bt_device"))
            if self.current_producer is not None:
                try:
                    await self.current_producer.start()
                except Exception as e:
                    log.exception(f"Producer start failed: {e}")
                    self.current_producer = None
                    await sink.push({
                        "schema": 1, "producer": "generic",
                        "event": "cleared", "sent_at": time.time(),
                    })
            return

        # Same source: transport may have changed (output channel toggle,
        # config switch, etc). Hand the new transport to the running producer
        # if it cares — otherwise no-op. No producer restart.
        if transport != self.current_transport:
            self.current_transport = transport
            if self.current_producer is not None:
                updater = getattr(self.current_producer, "update_transport", None)
                if updater is not None:
                    try:
                        await updater(transport)
                    except Exception as e:
                        log.warning(f"update_transport failed: {e}")

    async def _stop_current(self) -> None:
        if self.current_producer is None:
            return
        try:
            await self.current_producer.stop()
        except Exception as e:
            log.warning(f"Producer stop raised: {e}")
        self.current_producer = None


async def main() -> None:
    await Orchestrator().run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
