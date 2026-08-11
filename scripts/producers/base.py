"""Producer base class and Sink protocol."""

from __future__ import annotations

import abc
import logging
import time
from typing import Protocol

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1


class Sink(Protocol):
    """Where producers push payloads. Implementation usually HTTP-POSTs them."""

    async def push(self, payload: dict) -> None:
        ...


class Producer(abc.ABC):
    """One producer at a time per orchestrator. Owns its own tasks."""

    name: str = ""              # must be set in subclass; matches schema producer field

    def __init__(self, sink: Sink):
        self.sink = sink

    @abc.abstractmethod
    async def start(self) -> None:
        """Begin emitting events. Returns once startup is done; producer
        keeps running in background tasks until stop() is called."""

    @abc.abstractmethod
    async def stop(self) -> None:
        """Cancel background tasks and release resources."""

    # ── helpers ──────────────────────────────────────────────────────────

    def _envelope(self, **fields) -> dict:
        return {
            "schema":   SCHEMA_VERSION,
            "producer": self.name,
            "sent_at":  time.time(),
            **fields,
        }
