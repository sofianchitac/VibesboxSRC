#!/usr/bin/env python3
"""
Fingerprint Capture (Pi-side)

Replaces the LattePanda AudioFingerprintService. That service was a second
ReaRoute ASIO client whose contention wedged the NdiAsioReceiver ring buffer
(garbled/silent NDI audio). Capturing on the Pi removes the second ReaRoute
client entirely.

This is a front-end swap, not a new pipeline: everything downstream of the
captured clip is unchanged. We tap the PipeWire sum bus, run the same
silence-gated clip state machine the Windows service used, and POST a 16 kHz
mono WAV to the local nowplaying_server /api/fingerprint endpoint — which
already runs shazamio and broadcasts the match.

Tap point: the MONITOR of `dsp-in` (CamillaDSP's capture node = the 6ch sum
bus of all unmuted sources), captured with `stream.capture.sink=true`.
PipeWire folds all 6 channels and resamples to 16 kHz mono, so a 5.1 source's
centre/LFE/rears are included for free. The sum bus is mode-independent — the
v2 architecture pins CamillaDSP's input to the 6ch sum bus regardless of the
2ch/6ch OUTPUT selection — so this covers both transports (NDI and S/PDIF) by
construction.

Why dsp-in and NOT source.usb.ardftsrc: the per-source resampler node has no
clock of its own, so a capture stream on it is barely scheduled (~13% of
realtime, bursty) and useless for a streaming gate. dsp-in is driven by the
clock-authoritative output sink, so its monitor is a continuous realtime
stream — and it emits silence frames during quiet passages, which the
track-change (silence-gap) detector relies on. --volume 0.5 keeps the 6->1
fold from clipping. (Measured: source node ~13% realtime; dsp-in monitor full
realtime, clean.)

Gate: only capture while focused_source == "USB" (source-router WS :8080).
Mute = unlink from dsp-in, so a muted source is neither in the sum bus nor
focused — focused_source is what correctly respects mute. The sum bus carries
the mix of all unmuted sources, but the USB-only gate (Lyrion/AirPlay get rich
metadata from their own producers) plus one-source-at-a-time use makes that a
non-issue; this mirrors the Windows service, which tapped REAPER's summed
output too.
"""

from __future__ import annotations

import array
import asyncio
import io
import json
import logging
import math
import os
import wave
from collections import deque

import aiohttp

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s [FingerprintCapture] %(message)s')
log = logging.getLogger(__name__)

# ── Config (ported 1:1 from VibesboxDSP/AudioFingerprintService appsettings.json) ──
AUTOROUTER_WS   = os.environ.get("AUTOROUTER_WS", "ws://127.0.0.1:8080")
FINGERPRINT_URL = os.environ.get("FINGERPRINT_URL",
                                 "http://127.0.0.1:8090/api/fingerprint")
TAP_NODE        = os.environ.get("FP_TAP_NODE", "dsp-in")   # sum-bus monitor (see module docstring)
TAP_VOLUME      = os.environ.get("FP_TAP_VOLUME", "0.5")     # tame the 6->1 fold so it can't clip
ACTIVE_SOURCE   = os.environ.get("FP_ACTIVE_SOURCE", "USB")

TARGET_RATE = 16000          # Shazam/shazamio want 16 kHz mono
CHUNK_FRAMES = 320           # 20 ms read granularity (gate tick)
CHUNK_BYTES = CHUNK_FRAMES * 2   # s16, mono

CLIP_SECONDS            = 5.0
SILENCE_THRESHOLD_RMS   = 0.005   # linear RMS over normalised [-1, 1] samples
PRE_ROLL_SECONDS        = 2.0     # continuous presence before opening a clip
SILENCE_GAP_SECONDS     = 2.0     # mid-clip silence aborts / re-fingerprints (track change)
RECHECK_INTERVAL_SECONDS = 30.0   # re-fingerprint cadence while audio is continuous
RMS_WINDOW_MS           = 200

UPLOAD_TIMEOUT_SECONDS  = 30


class SilenceGate:
    """Rolling-RMS presence detector over a ~200 ms window. Accumulates per
    chunk (not per sample) for efficiency; behaviour matches the C# gate."""

    def __init__(self, sample_rate: int, threshold_rms: float, window_ms: int = RMS_WINDOW_MS):
        self._threshold = threshold_rms
        self._window_samples = max(1, sample_rate * window_ms // 1000)
        self._chunks: deque[tuple[float, int]] = deque()  # (sum_squares, n)
        self._sum_squares = 0.0
        self._count = 0

    def push(self, samples: array.array) -> bool:
        sq = 0.0
        for s in samples:
            f = s / 32768.0
            sq += f * f
        n = len(samples)
        self._chunks.append((sq, n))
        self._sum_squares += sq
        self._count += n
        while self._count - self._chunks[0][1] >= self._window_samples and len(self._chunks) > 1:
            old_sq, old_n = self._chunks.popleft()
            self._sum_squares -= old_sq
            self._count -= old_n
        if self._count == 0:
            return False
        rms = math.sqrt(self._sum_squares / self._count)
        return rms >= self._threshold


class ClipBuffer:
    """Accumulates raw s16 mono bytes until ClipSeconds is reached."""

    def __init__(self, seconds: float):
        self._capacity_bytes = math.ceil(seconds * TARGET_RATE) * 2
        self._buf = bytearray()

    @property
    def full(self) -> bool:
        return len(self._buf) >= self._capacity_bytes

    def clear(self) -> None:
        self._buf.clear()

    def append(self, chunk: bytes) -> None:
        self._buf.extend(chunk)

    def to_wav_bytes(self) -> bytes:
        data = bytes(self._buf[:self._capacity_bytes])
        out = io.BytesIO()
        with wave.open(out, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(TARGET_RATE)
            w.writeframes(data)
        return out.getvalue()


class FingerprintCapture:
    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._focused: str | None = None
        self._capture_task: asyncio.Task | None = None

    async def run(self) -> None:
        async with aiohttp.ClientSession() as session:
            self._session = session
            backoff = 1.0
            while True:
                try:
                    await self._ws_loop()
                    backoff = 1.0
                except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                    log.warning(f"source-router connection lost: {e} — reconnecting in {backoff:.0f}s")
                    self._set_focus(None)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)

    async def _ws_loop(self) -> None:
        log.info(f"Connecting to {AUTOROUTER_WS}")
        assert self._session is not None
        async with self._session.ws_connect(AUTOROUTER_WS, heartbeat=30, timeout=10) as ws:
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
                self._set_focus(frame.get("focused_source"))
        self._set_focus(None)

    def _set_focus(self, src: str | None) -> None:
        if src == self._focused:
            return
        log.info(f"focused_source: {self._focused!r} -> {src!r}")
        self._focused = src
        should_run = src == ACTIVE_SOURCE
        if should_run and self._capture_task is None:
            self._capture_task = asyncio.create_task(self._capture_loop())
        elif not should_run and self._capture_task is not None:
            self._capture_task.cancel()
            self._capture_task = None

    async def _capture_loop(self) -> None:
        """Runs while USB is focused. Spawns pw-record on the dsp-in sum-bus
        monitor and feeds its raw 16 kHz mono stream into the gate. Restarts the
        tap if the node isn't up yet or the stream ends (e.g. CamillaDSP reload)."""
        try:
            while True:
                proc = await asyncio.create_subprocess_exec(
                    "pw-record", "--target", TAP_NODE,
                    "-P", "stream.capture.sink=true",
                    "--volume", TAP_VOLUME,
                    "--rate", str(TARGET_RATE), "--channels", "1",
                    "--format", "s16", "--raw", "-",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                log.info(f"Capture started (pw-record --target {TAP_NODE} monitor)")
                try:
                    await self._run_gate(proc.stdout)
                finally:
                    if proc.returncode is None:
                        proc.kill()
                    err = (await proc.stderr.read()).decode(errors="replace").strip()
                    await proc.wait()
                    if err:
                        log.warning(f"pw-record exited: {err[:200]}")
                # Stream ended (node not up yet / bridge restart). Retry while focused.
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            log.info("Capture stopped")
            raise

    async def _run_gate(self, stdout: asyncio.StreamReader) -> None:
        gate = SilenceGate(TARGET_RATE, SILENCE_THRESHOLD_RMS)
        clip = ClipBuffer(CLIP_SECONDS)

        WAITING, FILLING, COOLDOWN = 0, 1, 2
        state = WAITING
        present_ms = silent_ms = cooldown_ms = 0.0
        pre_roll_ms     = PRE_ROLL_SECONDS * 1000.0
        silence_gap_ms  = SILENCE_GAP_SECONDS * 1000.0
        recheck_ms      = RECHECK_INTERVAL_SECONDS * 1000.0
        dt_ms           = 1000.0 * CHUNK_FRAMES / TARGET_RATE

        while True:
            try:
                chunk = await stdout.readexactly(CHUNK_BYTES)
            except asyncio.IncompleteReadError:
                return  # EOF — let _capture_loop restart the tap

            samples = array.array("h")
            samples.frombytes(chunk)
            present = gate.push(samples)
            silent_ms = 0.0 if present else silent_ms + dt_ms

            if state == WAITING:
                present_ms = present_ms + dt_ms if present else 0.0
                if present_ms >= pre_roll_ms:
                    log.debug("Audio present — opening clip")
                    state = FILLING
                    clip.clear()
                    clip.append(chunk)
            elif state == FILLING:
                clip.append(chunk)
                if silent_ms >= silence_gap_ms:
                    log.debug("Silence gap mid-clip — restarting")
                    state = WAITING
                    present_ms = 0.0
                    clip.clear()
                elif clip.full:
                    await self._recognize(clip)
                    state = COOLDOWN
                    present_ms = 0.0
                    cooldown_ms = recheck_ms
            elif state == COOLDOWN:
                cooldown_ms -= dt_ms
                if silent_ms >= silence_gap_ms or cooldown_ms <= 0:
                    state = WAITING
                    present_ms = 0.0

    async def _recognize(self, clip: ClipBuffer) -> None:
        wav = clip.to_wav_bytes()
        assert self._session is not None
        try:
            async with self._session.post(
                FINGERPRINT_URL, data=wav,
                headers={"Content-Type": "audio/wav"},
                timeout=aiohttp.ClientTimeout(total=UPLOAD_TIMEOUT_SECONDS),
            ) as r:
                body = await r.text()
                if r.status >= 400:
                    log.warning(f"/api/fingerprint -> {r.status}: {body[:200]}")
                else:
                    log.info(f"Clip uploaded ({len(wav)} bytes) -> {body[:200]}")
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.warning(f"Upload failed: {e}")


async def main() -> None:
    await FingerprintCapture().run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
