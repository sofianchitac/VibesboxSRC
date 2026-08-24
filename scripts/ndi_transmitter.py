#!/usr/bin/env python3
"""
NDI Audio Transmitter — NDI SDK 6.x via ctypes
================================================
Reads 8ch FLOAT32LE / 96kHz audio from the capture device named by NDI_ALSA_DEV —
CamillaDSP's output, taken straight out of PipeWire through the ALSA `pipewire` plugin —
and transmits it as a named NDI audio source on the network.

★ 2026-08-21: this used to read `hw:NDITX,1,0`, an snd-aloop loopback that CamillaDSP
wrote into. Going direct measured **18.5-20.0 ms** cheaper end to end (four interleaved
block pairs at two rates, docs/ndi-loopback-hop-brief.md) — more than the 9.3 ms the
loopback itself held, because the sink's own ALSA output buffer went with it. Everything
below is device-agnostic: only NDI_ALSA_DEV changed.

Key design decisions vs. ndi-free-audio binary:
  - Reads the device named in the unit, never by fragile PortAudio device index.
  - Enforces exact ALSA params: 8ch / 96kHz / FLOAT32LE — will not silently
    negotiate a wrong format.
  - The capture retries until PipeWire is up and the graph accepts the params.
  - NDI audio frame: FLTP (planar float32), 8ch, 96kHz — all correct.
  - Interleaved→planar conversion via numpy (zero-copy transpose).
  - No PortAudio, no binary EULA, no guessing.

Reads from  : NDI_ALSA_DEV (pipewire:… — source_router links dsp-out into it)
Transmits as: NDI source, name from NDI_TX_NAME env var (default VibesboxSRC-5.1)

Pure 8ch passthrough: ALSA reads the 8ch sum bus and we transmit it verbatim.
A narrower source simply leaves the lanes it does not use digitally silent. No
per-mode logic — REAPER on the LattePanda owns all channel routing, and this
script must never reorder, fold or remap anything.
NDI_ALSA_CH controls the ALSA read width (= the NDI send width); default 8.

★ 8 since 2026-08-13, was 6: the eARC tap delivers LPCM surrounds on lanes 7-8
and the old 6-wide chain discarded them (measured with `arecord -c 8`). The
stream NAME stays "VibesboxSRC-5.1" — the receiver discovers it by that exact
string, so the name is historical and no longer describes the width.

Lifecycle:
  Managed by ndi-output.service. Started once and left running; source_router no
  longer restarts it on an output toggle (the transmitter is mode-agnostic).
"""

import ctypes
import ctypes.util
import bisect
import logging
import mmap
import os
import signal
import struct
import sys
import time

import alsaaudio
import numpy as np

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s [NDI-TX] %(levelname)s %(message)s",
    datefmt = "%Y-%m-%d %H:%M:%S",
)

# ── Configuration (environment overrides) ─────────────────────────────────────
NDI_LIB_PATH   = os.environ.get("NDI_LIB_PATH",   "/usr/local/lib/libndi.so")
ALSA_DEVICE    = os.environ.get("NDI_ALSA_DEV",   "pipewire:NODE=,PERIOD_BYTES=16384,BUFFER_BYTES=32768")
SAMPLE_RATE    = int(os.environ.get("NDI_RATE",    "96000"))
ALSA_CHANNELS  = int(os.environ.get("NDI_ALSA_CH", "8"))   # ALSA read width (the sum bus is 8ch)
NDI_CHANNELS   = ALSA_CHANNELS                              # NDI send width = read width (pure passthrough)
PERIOD_FRAMES  = int(os.environ.get("NDI_PERIOD",  "1024"))   # match CamillaDSP chunksize
# Start-up backlog drain — see drain_capture_backlog(). A period out of backlog
# returns instantly; one at the live edge cannot beat real time. Anything under
# this fraction of a period is therefore backlog, not live audio.
DRAIN_LIVE_FRACTION = float(os.environ.get("NDI_DRAIN_FRACTION", "0.5"))
DRAIN_MAX_S         = float(os.environ.get("NDI_DRAIN_MAX_S",   "3.0"))
# Passive occupancy telemetry — see the [tx window] block in the capture loop. Seconds per
# summary line; 0 disables. Costs one avail() ioctl per period (~188/s) and one log line
# per window, so it is cheap enough to leave on in production.
TELEMETRY_S    = float(os.environ.get("NDI_TELEMETRY_S", "5.0"))
# Runtime backlog trim — see trim_standing_backlog(). Rides on the telemetry window
# above (it needs the distribution to tell a standing residual from a sawtooth peak),
# so NDI_TELEMETRY_S=0 disables the trim too. Cooldown is per trim attempt; the cap
# bounds one attempt at a couple of periods more than the buffer can actually hold.
TRIM_COOLDOWN_S  = float(os.environ.get("NDI_TRIM_COOLDOWN_S",  "30.0"))
TRIM_MAX_PERIODS = int(os.environ.get("NDI_TRIM_MAX_PERIODS",   "4"))
# Delay-setpoint thermostat (2026-08-24 splice qualification session): the pipewire
# plugin's delay estimate steps between per-instance STATES (~0.2 / 2.2 / 4.2 ms
# measured), each state adding real forward latency, and a live-edge splice steps
# BOTH delay() and the measured forward leg down and it stays down. This control
# loop trims those states back to a setpoint: if delayp50 stands above
# SETPOINT_DELAY_F for two consecutive windows WHILE the avail residual is clean
# (surplus belongs to trim_standing_backlog above), discard ONE period at the
# live edge. Cost per firing: one audible ~5.33 ms splice. "0" disables.
SETPOINT_TRIM       = os.environ.get("NDI_SETPOINT_TRIM",       "1")   != "0"
SETPOINT_DELAY_F    = int(os.environ.get("NDI_SETPOINT_DELAY_F",    "128"))
SETPOINT_COOLDOWN_S = float(os.environ.get("NDI_SETPOINT_COOLDOWN_S", "30.0"))
# snd_pcm_delay() telemetry - see locate_pcm_handle(). "0" disables only the delay
# read-out; the avail() telemetry above is independent of it.
DELAY_TELEMETRY  = os.environ.get("NDI_DELAY_TELEMETRY", "1") != "0"
# Phase-2 b2tx watermark (bridge-write -> tx-read latency, see read_stamp_record).
B2TX_TELEMETRY   = os.environ.get("NDI_B2TX_TELEMETRY",  "1") != "0"
# Seconds after an instance anchor before b2tx samples are trusted: the new
# instance's audio needs ~100-150 ms to traverse the pipe, and windows that
# straddle the switch are garbage. 2 s covers it with margin.
B2TX_SETTLE_S    = float(os.environ.get("NDI_B2TX_SETTLE_S", "2.0"))
NDI_NAME       = os.environ.get("NDI_TX_NAME",    "VibesboxSRC-5.1")

ALSA_OPEN_RETRIES    = 30     # attempts before giving up
ALSA_OPEN_RETRY_WAIT = 2.0    # seconds between attempts

# ── NDI SDK constants ──────────────────────────────────────────────────────────
# NDIlib_FourCC_audio_type_FLTP:
#   = 'F'|('L'<<8)|('T'<<16)|('p'<<24) = 0x70544C46
NDIlib_FourCC_type_FLTP = 0x70544C46

# NDIlib_send_timecode_synthesize = INT64_MIN (0x8000000000000000 as signed int64)
# Tells the SDK to synthesise a timecode from wall-clock time.
NDIlib_send_timecode_synthesize = ctypes.c_int64(0x8000000000000000).value

# ── NDI SDK C structures ───────────────────────────────────────────────────────

class NDIlib_send_create_t(ctypes.Structure):
    """Maps to NDIlib_send_create_t in Processing.NDI.Send.h"""
    _fields_ = [
        ("p_ndi_name",  ctypes.c_char_p),   # UTF-8 source name
        ("p_groups",    ctypes.c_char_p),   # NULL = default group
        ("clock_video", ctypes.c_bool),     # False for audio-only sender
        ("clock_audio", ctypes.c_bool),     # True: SDK paces send to real-time clock
    ]


class NDIlib_audio_frame_v3_t(ctypes.Structure):
    """Maps to NDIlib_audio_frame_v3_t in Processing.NDI.structs.h"""
    _fields_ = [
        ("sample_rate",             ctypes.c_int),
        ("no_channels",             ctypes.c_int),
        ("no_samples",              ctypes.c_int),
        ("timecode",                ctypes.c_int64),
        ("FourCC",                  ctypes.c_uint32),
        ("p_data",                  ctypes.POINTER(ctypes.c_float)),
        ("channel_stride_in_bytes", ctypes.c_int),
        ("p_metadata",              ctypes.c_char_p),
        ("timestamp",               ctypes.c_int64),
    ]


# ── NDI SDK loader ─────────────────────────────────────────────────────────────

def load_ndi_library(path: str) -> ctypes.CDLL:
    """Load libndi.so and bind the functions we need with correct argtypes/restype."""
    logging.info(f"Loading NDI library: {path}")
    ndi = ctypes.CDLL(path)

    # NDIlib_initialize — must be called once before any other NDI call.
    ndi.NDIlib_initialize.restype  = ctypes.c_bool
    ndi.NDIlib_initialize.argtypes = []

    # NDIlib_send_create — creates a named NDI sender.
    ndi.NDIlib_send_create.restype  = ctypes.c_void_p
    ndi.NDIlib_send_create.argtypes = [ctypes.POINTER(NDIlib_send_create_t)]

    # NDIlib_send_send_audio_v3 — sends a planar float audio frame.
    ndi.NDIlib_send_send_audio_v3.restype  = None
    ndi.NDIlib_send_send_audio_v3.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(NDIlib_audio_frame_v3_t),
    ]

    # NDIlib_send_destroy — tear down sender.
    ndi.NDIlib_send_destroy.restype  = None
    ndi.NDIlib_send_destroy.argtypes = [ctypes.c_void_p]

    # NDIlib_destroy — global teardown; call at process exit.
    ndi.NDIlib_destroy.restype  = None
    ndi.NDIlib_destroy.argtypes = []

    return ndi


# ── ALSA capture opener ────────────────────────────────────────────────────────

def open_alsa_capture() -> alsaaudio.PCM:
    """
    Open the capture device with exact params.

    The `pipewire` plugin grants exactly what is asked (measured 2026-08-21: 512/1024
    frames for PERIOD_BYTES=16384/BUFFER_BYTES=32768), but the daemon has to be up and
    the graph has to accept FLOAT32LE / 8ch / 96kHz, so we retry until it does.

    Raises RuntimeError if the device never becomes available.
    """
    for attempt in range(1, ALSA_OPEN_RETRIES + 1):
        try:
            pcm = alsaaudio.PCM(
                type       = alsaaudio.PCM_CAPTURE,
                mode       = alsaaudio.PCM_NORMAL,   # blocking — simplest and correct
                device     = ALSA_DEVICE,
                channels   = ALSA_CHANNELS,
                rate       = SAMPLE_RATE,
                format     = alsaaudio.PCM_FORMAT_FLOAT_LE,
                periodsize = PERIOD_FRAMES,
            )
            logging.info(
                f"ALSA capture opened: {ALSA_DEVICE} | "
                f"{SAMPLE_RATE} Hz / {ALSA_CHANNELS}ch ALSA -> {NDI_CHANNELS}ch NDI / "
                f"FLOAT32LE / period={PERIOD_FRAMES}"
            )
            return pcm
        except alsaaudio.ALSAAudioError as exc:
            logging.warning(
                f"ALSA open attempt {attempt}/{ALSA_OPEN_RETRIES} failed: {exc}"
                + (" — CamillaDSP may not have opened write side yet." if attempt == 1 else "")
            )
            if attempt < ALSA_OPEN_RETRIES:
                time.sleep(ALSA_OPEN_RETRY_WAIT)

    raise RuntimeError(
        f"Could not open ALSA capture device '{ALSA_DEVICE}' "
        f"after {ALSA_OPEN_RETRIES} attempts. "
        f"Is CamillaDSP running an {ALSA_CHANNELS}ch output config?"
    )


def granted_period(pcm: alsaaudio.PCM) -> int:
    """The period size ALSA actually GRANTED, which is what every threshold must use.

    ⚠ Not PERIOD_FRAMES. The two differ on the live Pi: the code default is 1024, the
    unit sets NDI_PERIOD=512, and the device string's PERIOD_BYTES=16384 is what really
    pins the grant to 512 frames at 8ch x 4B. A threshold built on the requested size
    would silently move if that env override were ever dropped.

    Defensive: this runs inside the production transmitter and an exception here would
    take NDI output down entirely. A wrong-but-sane size is survivable; a crash is not.
    (pyalsaaudio 0.11.0 on the Pi does provide period_size.)
    """
    try:
        return int(pcm.info().get("period_size") or 0) or PERIOD_FRAMES
    except Exception as exc:
        logging.warning(f"could not read the granted period_size ({exc}); "
                        f"falling back to the requested {PERIOD_FRAMES}.")
        return PERIOD_FRAMES


def drain_capture_backlog(pcm: alsaaudio.PCM, period_frames: int) -> None:
    """Discard whatever is already sitting in the capture buffer before going live.

    ✅ Written for snd-aloop, verified against the `pipewire` plugin on 2026-08-21: it
    discards one period and then blocks, which is the shape the premise predicts. Watch
    the "drain:" line — "hit the … cap still reading faster than real time" would mean
    the premise stopped holding.

    Whenever this reader is away — a restart, or any stall long enough to gap the read —
    whatever is queued ahead of it sits IN FRONT OF every subsequent sample for the life
    of the process. On the retired snd-aloop path that was 10-43 ms per open; on the
    PipeWire path it is one period.

    ⛔ WHAT THIS IS *NOT*. It does NOT fix the latency ratchet in
    docs/latency-matrix-plan.md §17-18, and it was written believing that it would.
    Tested against the same procedure that found the ratchet: with this drain live, four
    `systemctl restart ndi-output` still stepped 250.9 -> 268.7 -> 288.8 -> 314.8 ms.
    The dominant accumulator is UPSTREAM of this reader — confirmed accumulator #1 is
    ardftsrc-bridge's own input ring, parked in the dead band below its `keep * 8`
    (8192-frame) trim threshold, and a second one between the bridge and here is still
    unlocated. Restarting `ardftsrc-bridge@tv` clears the step; restarting THIS service
    does not, and is in fact a cause.

    ⇒ Kept because discarding stale loopback audio at open is correct on its own terms,
    not because it solves that. Do not cite it as the fix, and do not let its presence
    stop the search for accumulator #2.

    Detection is by TIMING, deliberately, so it needs nothing from the binding beyond
    the blocking read we already do: a period served out of backlog returns
    immediately, whereas a period at the live edge cannot arrive faster than real time.
    We discard until a read actually blocks — that is the moment the backlog is gone.
    (The runtime trim below does use avail() — it works fine on this install. This one
    stays timing-based because it must run BEFORE the stream has started, where avail()
    has nothing to report yet.)
    """
    period_s = period_frames / SAMPLE_RATE
    live_threshold = period_s * DRAIN_LIVE_FRACTION
    deadline = time.monotonic() + DRAIN_MAX_S
    frames = periods = slow = 0
    first = True

    while time.monotonic() < deadline:
        t0 = time.monotonic()
        try:
            length, _ = pcm.read()
        except alsaaudio.ALSAAudioError as exc:
            logging.warning(f"drain: ALSA read error, stopping drain early: {exc}")
            break
        elapsed = time.monotonic() - t0
        if first:
            # ⚠ The FIRST read also STARTS the stream, so its duration measures
            # start-up, not backlog. Timing it broke the whole drain on the first
            # deploy: it reported "already at the live edge" every time while the
            # ratchet was still there. Discard it and start judging from the second.
            first = False
            if length > 0:
                frames += length
                periods += 1
            continue
        if elapsed >= live_threshold:
            # Require TWO consecutive slow reads: one scheduling hiccup mid-drain
            # would otherwise end it early and leave the backlog in place.
            slow += 1
            if slow >= 2:
                break
            continue
        slow = 0
        if length > 0:                 # a backlog period; drop it
            frames += length
            periods += 1
    else:
        logging.warning(
            f"drain: hit the {DRAIN_MAX_S:.1f}s cap still reading faster than real "
            f"time after {frames} frames. Going live anyway — the writer may be "
            f"running fast, which is a different problem to this one."
        )

    if frames:
        logging.info(f"drain: discarded {frames} frames ({frames / SAMPLE_RATE * 1000:.1f} ms, "
                     f"{periods} periods) of stale backlog before going live.")
    else:
        logging.info("drain: capture was already at the live edge, nothing to discard.")
    # Returned so the b2tx watermark's consumed-frame counter K stays exact —
    # drained frames crossed this reader without being counted anywhere else.
    return frames


def trim_standing_backlog(pcm: alsaaudio.PCM, period_frames: int) -> int:
    """Discard whole periods of STANDING residual backlog. Returns frames discarded.

    ★ Why this exists (measured 2026-08-23). The reader's residual RATCHETS: it sits at
    0 for minutes, steps up by exactly one granted period, and never comes back down.
    Observed live at 11:48:40 after 11 clean minutes, then held; a previous step held
    ~33 h and cost real latency. The open-time drain_capture_backlog() cannot help —
    it runs once, at open, and the step happens mid-run. Nothing else looked.

    The step's own trigger is NOT observable from here: no ALSA error, no xrun, no
    journal entry at that minute. So this does not prevent the step, it REMOVES it —
    the residual is bounded by the buffer (2 periods), which is why it is worth
    removing rather than diagnosing first.

    ⚠ The cost is honest and must not be hidden: discarding a period splices the
    stream. One period at 96 kHz is 5.33 ms. That is the same trade the upstream
    ardftsrc backlog trim already makes, and it buys back 5.33 ms of otherwise
    permanent latency per period removed.

    SAFETY: the caller's window statistic only decides whether to TRY. Every discard
    is gated on a live avail() re-check, so this can never read into live audio and
    cause an underrun — at worst it does nothing.
    """
    discarded = 0
    try:
        for _ in range(TRIM_MAX_PERIODS):
            if pcm.avail() < period_frames:
                break
            length, _ = pcm.read()
            if length <= 0:
                break
            discarded += length
    except Exception as exc:
        # Never let the trim take NDI output down; a missed trim is survivable.
        logging.warning(f"trim: aborted after {discarded} frames: {exc}")
    return discarded


# ── snd_pcm_delay() read-out, ctypes into libasound ───────────────────────────
# WHY (ledger, standing result 2026-08-23): every instrument on the forward leg is an
# OCCUPANCY read-out (avail, GetBufferLevel, /proc ring depth) and all of them read
# empty across a 34.5 ms swing. snd_pcm_delay() is a different quantity: the pipewire
# plugin's own estimate of the distance between this reader's read pointer and the
# sample at the graph edge, including whatever the plugin/adapter holds that avail()
# cannot see. The bridge's out= is already pcm.delay() on the OTHER end of the graph
# and stayed flat across the swing (2194f vs 2112f at fwd 126.04 vs 91.57); this is
# the same read-out on the one span still unmeasured, dsp-out -> this reader.
#
# ⚠ Interpretation guard, decided BEFORE the data exists: delay() moving with the
# per-run draw is decisive (the draw lives on this span; trim to a setpoint next).
# delay() FLAT is "not visible to this plugin's delay estimate", NOT an acquittal of
# the adapter — the pipewire plugin COMPUTES delay from its own timing info and what
# that includes has shifted across PipeWire versions. Flat here moves the search to
# the rs chunk-boundary phase and CamillaDSP's inter-stage holds, via the timestamp
# side-channel (phase 2), not back to occupancy probes.
#
# ⚠ MECHANISM: pyalsaaudio exposes avail() but not delay(), and does not expose the
# snd_pcm_t*. The handle lives at a fixed offset in the alsapcm C struct
# (PyObject_HEAD, long pcmtype, int pcmmode, char *cardname, snd_pcm_t *handle —
# offset 40 on a 64-bit build). The offset is PROBED AND VALIDATED, never assumed:
# each candidate pointer is exercised in a forked child first, so a wrong guess can
# only crash the child, never the transmitter. A validated offset is then reused for
# re-extraction after a device reopen (same extension type, same struct layout).
# If nothing validates, delay telemetry stays off and everything else is unchanged.

_ASOUND        = None    # libasound handle, loaded once
_HANDLE_OFFSET = None    # struct offset proven by the child probe, reused on reopen


def _asound_lib():
    """Load libasound once and bind the three calls used here."""
    global _ASOUND
    if _ASOUND is None:
        path = ctypes.util.find_library("asound") or "libasound.so.2"
        lib = ctypes.CDLL(path, use_errno=True)
        lib.snd_pcm_state.restype  = ctypes.c_int
        lib.snd_pcm_state.argtypes = [ctypes.c_void_p]
        lib.snd_pcm_delay.restype  = ctypes.c_int
        lib.snd_pcm_delay.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_long)]
        _ASOUND = lib
    return _ASOUND


def _ptr_at(obj, offset):
    """Read a pointer-sized field out of a CPython object at a raw byte offset."""
    return ctypes.c_void_p.from_address(id(obj) + offset).value


def _state_ok_in_child(lib, ptr):
    """Call snd_pcm_state(ptr) in a forked child: a garbage pointer can only kill
    the child. Exit 0 = the pointer answered with a legal PCM state (0..8).

    ⚠ The wait is BOUNDED. fork() from a process with running threads (the NDI SDK
    has its own) copies any lock mid-hold, so a child could in principle deadlock
    inside libasound. A blocking waitpid would then hang the transmitter; instead
    poll WNOHANG for up to 2 s, then SIGKILL the child and count the offset failed.
    """
    pid = os.fork()
    if pid == 0:
        try:
            st = lib.snd_pcm_state(ctypes.c_void_p(ptr))
            os._exit(0 if 0 <= st <= 8 else 1)
        except BaseException:
            os._exit(1)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        wpid, status = os.waitpid(pid, os.WNOHANG)
        if wpid == pid:
            return os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
        time.sleep(0.01)
    try:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
    except OSError:
        pass
    return False


def locate_pcm_handle(pcm):
    """Return a validated ctypes.c_void_p to the pyalsaaudio object's snd_pcm_t*,
    or None (telemetry off, transmitter unaffected). Call only on a STARTED stream —
    the parent-side cross-check expects delay() to answer."""
    global _HANDLE_OFFSET
    if not DELAY_TELEMETRY:
        return None
    try:
        lib = _asound_lib()
    except OSError as exc:
        logging.warning(f"delay: libasound unavailable ({exc}); delay telemetry off.")
        return None
    known = _HANDLE_OFFSET is not None
    offsets = [_HANDLE_OFFSET] if known else [40, 32, 48, 24, 56, 64]
    for off in offsets:
        try:
            ptr = _ptr_at(pcm, off)
        except Exception:
            continue
        if not ptr or ptr % 8:
            continue
        if not known and not _state_ok_in_child(lib, ptr):
            continue
        # Parent-side cross-check on the (now child-proven, or previously proven)
        # pointer: delay() must return 0 with a value inside +/- 1 s.
        d = ctypes.c_long()
        if (lib.snd_pcm_delay(ctypes.c_void_p(ptr), ctypes.byref(d)) == 0
                and -SAMPLE_RATE <= d.value <= SAMPLE_RATE):
            if not known:
                logging.info(f"delay: snd_pcm_t* located at struct offset {off}; "
                             f"first delay()={d.value}f.")
            _HANDLE_OFFSET = off
            return ctypes.c_void_p(ptr)
    logging.warning("delay: no candidate offset validated; delay telemetry off, "
                    "avail() telemetry unaffected.")
    return None


# ── b2tx watermark: bridge-write → tx-read latency, continuously ─────────────
# Phase 2 per the agents-exchange (2026-08-24). The ardftsrc bridge publishes a
# seqlock record to /dev/shm/vibesbox-ardftsrc-stamp on every output write:
#   <u64 seq> <u64 instance> <u64 out_frames> <u64 mono_ns>
# seq guards torn reads; instance detects a bridge RESTART (which redraws the
# per-run draw and resets out_frames); out_frames is the cumulative 96k frame
# count handed to the output PCM; mono_ns is CLOCK_MONOTONIC — the same clock
# time.monotonic_ns() reads, so no clock-sync term exists.
#
# This side anchors its own cumulative consumed-frame counter K against that
# timeline once per bridge instance (delta = newest out_frames − K at first
# sight) and then interpolates the stamp timeline at the read edge each period:
#   b2tx = read_time − stamp_time(read_edge + delta)
# ⚠ The ABSOLUTE carries the hold that existed at anchor time — unknowable
# passively, because the pipe between bridge and reader is always full. What is
# EXACT is the variation: b2tx moves 1 ms for every 1 ms the true bridge-write→
# tx-read hold moves. It replaces the audible sweep probe for this question.
# ⚠ delta re-anchors on every new instance id, so values from different bridge
# instances are different references — never subtract across instances. Within an
# instance, window percentiles are comparable across hours.
# ⚠ Multiple concurrent bridges interleave one record path; >1 instance change in
# a single window marks it invalid (omitted keys).

STAMP_SHM = "/dev/shm/vibesbox-ardftsrc-stamp"
_STAMP_SIZE   = 32
_STAMP_KEEP   = 8192    # retained stamps (~5 min of timeline for interpolation)


def open_stamp_mmap():
    """mmap the shared record read-only, or None (bridge not running yet)."""
    try:
        f = os.open(STAMP_SHM, os.O_RDONLY)
        return mmap.mmap(f, _STAMP_SIZE, access=mmap.ACCESS_READ)
    except OSError:
        return None


def read_stamp_record(mm):
    """Read one record, rejecting tears by double-read stability. Returns
    (seq, instance, out_frames, mono_ns) or None (torn / never stamped)."""
    a = mm[:]
    b = mm[:]
    if a != b:
        a = mm[:]
        if a != mm[:]:
            return None
    seq, inst, frames, t_ns = struct.unpack("<QQQQ", a)
    if seq == 0:                    # writer poisoned or not yet stamped
        return None
    return seq, inst, frames, t_ns


# ── Main transmitter loop ──────────────────────────────────────────────────────

def run():
    # ── Load and initialise NDI SDK ───────────────────────────────────────────
    ndi = load_ndi_library(NDI_LIB_PATH)

    if not ndi.NDIlib_initialize():
        raise RuntimeError("NDIlib_initialize() failed — check libndi.so and CPU requirements.")
    logging.info("NDI SDK initialized.")

    # ── Create NDI audio-only sender ─────────────────────────────────────────
    create_desc = NDIlib_send_create_t(
        p_ndi_name  = NDI_NAME.encode("utf-8"),
        p_groups    = None,
        clock_video = False,
        clock_audio = True,   # SDK paces audio delivery to real-time clock
    )
    sender = ndi.NDIlib_send_create(ctypes.byref(create_desc))
    if not sender:
        ndi.NDIlib_destroy()
        raise RuntimeError("NDIlib_send_create() failed.")
    logging.info(f"NDI sender created: '{NDI_NAME}'")

    # ── Open ALSA capture ─────────────────────────────────────────────────────
    pcm = open_alsa_capture()
    period_frames = granted_period(pcm)
    consumed = drain_capture_backlog(pcm, period_frames)
    # The drain's reads have started the stream, so delay() can answer now.
    delay_handle = locate_pcm_handle(pcm)

    # ── b2tx watermark state (see read_stamp_record above) ───────────────────
    stamp_mm        = open_stamp_mmap() if B2TX_TELEMETRY else None
    stamp_cur_inst  = None      # instance currently anchored
    stamp_last_seq  = 0
    stamp_delta     = None      # K offset mapping the read edge onto the stamp timeline
    stamp_ws        = []        # out_frames of retained stamps (ascending)
    stamp_ts        = []        # their mono_ns
    b2tx_samples    = []        # per-period b2tx ms, one window's worth
    stamp_new_this_window = False
    stamp_inst_changes    = 0
    stamp_anchor_mono     = None   # monotonic s of last (re)anchor — settle gate

    # SIGUSR1 = discard ONE period at the live edge (deliberate splice). Debug /
    # regulator-qualification actuator: the standing-backlog trim can only fire
    # when surplus is present; this one drops at the live edge unconditionally,
    # costing an audible 5.33 ms splice, to prove the forward leg steps down.
    splice_req = False
    splice_why = "SIGUSR1"
    def _request_splice(sig, _frm):
        nonlocal splice_req
        splice_req = True
    signal.signal(signal.SIGUSR1, _request_splice)

    # ── NDI audio frame (reused every period) ────────────────────────────────
    frame = NDIlib_audio_frame_v3_t()
    frame.sample_rate             = SAMPLE_RATE
    frame.no_channels             = NDI_CHANNELS
    frame.no_samples              = PERIOD_FRAMES
    frame.timecode                = NDIlib_send_timecode_synthesize
    frame.FourCC                  = NDIlib_FourCC_type_FLTP
    frame.channel_stride_in_bytes = PERIOD_FRAMES * ctypes.sizeof(ctypes.c_float)
    frame.p_metadata              = None
    frame.timestamp               = 0

    # ── Graceful shutdown ────────────────────────────────────────────────────
    def shutdown(sig, _frame):
        signame = signal.Signals(sig).name
        logging.info(f"Received {signame} — shutting down.")
        try:
            pcm.close()
        except Exception:
            pass
        ndi.NDIlib_send_destroy(sender)
        ndi.NDIlib_destroy()
        logging.info("NDI sender destroyed. Bye.")
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT,  shutdown)

    # ── Capture → transmit loop ───────────────────────────────────────────────
    logging.info("NDI transmitter running.")
    consecutive_errors = 0
    avail_samples = []          # [tx window] telemetry, see the capture loop
    delay_samples = []          # snd_pcm_delay() per period, same window
    _delay_val    = ctypes.c_long()
    delay_errs    = 0           # consecutive delay() failures; persistent -> disable
    frames_sent   = 0
    tele_t0       = time.monotonic()
    last_trim_t   = 0.0         # monotonic; 0 = never trimmed
    trim_streak   = 0           # consecutive windows that trimmed — see the warning
    setpoint_streak = 0         # consecutive windows with delay above setpoint
    last_splice_t   = 0.0       # monotonic; thermostat cooldown clock

    while True:
        try:
            length, data = pcm.read()
        except alsaaudio.ALSAAudioError as exc:
            consecutive_errors += 1
            logging.warning(f"ALSA read error ({consecutive_errors}): {exc}")
            if consecutive_errors >= 5:
                logging.error("Too many ALSA errors — reopening capture device.")
                try:
                    pcm.close()
                except Exception:
                    pass
                try:
                    pcm = open_alsa_capture()
                    # Same drain as at startup: this reopen follows an error burst,
                    # so the loopback has been filling unread for at least as long.
                    period_frames = granted_period(pcm)
                    consumed += drain_capture_backlog(pcm, period_frames)
                    # New PCM object, new handle; the proven offset is reused.
                    delay_handle = locate_pcm_handle(pcm)
                    delay_errs = 0
                    consecutive_errors = 0
                except RuntimeError as e:
                    logging.error(str(e))
                    # Give systemd Restart=on-failure a chance to handle it
                    sys.exit(1)
            continue

        consecutive_errors = 0

        if length < 0:
            # Overrun — snd-aloop buffer was not read in time
            logging.warning(f"ALSA overrun (length={length}) — data lost, continuing.")
            continue

        if length == 0:
            # No data yet (shouldn't happen in PCM_NORMAL mode, but be safe)
            continue

        # ── live-edge splice (SIGUSR1) ────────────────────────────────────────
        # Drop the period we just read instead of sending it. Counted in
        # `consumed` like every frame that crosses this reader; NOT counted in
        # frames_sent, so `sent=` stays an honest stall witness.
        if splice_req:
            splice_req = False
            consumed += length
            logging.info(
                f"splicetrim[{splice_why}]: discarded {length}f "
                f"({length*1000.0/SAMPLE_RATE:.2f}ms) at the live edge — forward leg "
                f"should step down by the same amount."
            )
            continue

        # data is bytes: FLOAT32LE interleaved, shape (length × ALSA_CHANNELS)
        interleaved = np.frombuffer(data, dtype=np.float32).reshape(length, ALSA_CHANNELS)

        # Pure passthrough: ch1-2 = FL/FR, the rest carry whatever the sum bus has
        # (silent if the source is stereo). No per-mode logic — REAPER owns all
        # channel routing on the LattePanda. Transpose to planar (channels × frames)
        # for NDI FLTP; ascontiguousarray makes it C-contiguous for ctypes.
        planar = np.ascontiguousarray(interleaved.T)  # shape (NDI_CHANNELS, length)

        # Update frame fields if period size changed (rare — CamillaDSP restart)
        if length != frame.no_samples:
            logging.info(f"Period size changed: {frame.no_samples} → {length}")
            frame.no_samples              = length
            frame.channel_stride_in_bytes = length * ctypes.sizeof(ctypes.c_float)

        frame.p_data = planar.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
        ndi.NDIlib_send_send_audio_v3(sender, ctypes.byref(frame))

        # ── [tx window] passive occupancy telemetry ───────────────────────────────
        # The chain has NO read-out between dsp-in and the NDI send, and that is exactly
        # the span the 105-125 ms run-to-run variance lives in (2026-08-21: the bridge's
        # own ring/ready/out stayed flat across a 14.5 ms swing). avail() straight after a
        # read is this reader's residual backlog — the same quantity /proc/asound gave for
        # the aloop arm (9.3 ms p50, 2026-08-20) and the ONLY way to see it on the
        # PipeWire arm, which has no /proc entry at all.
        #
        # ⚠ Reported as a DISTRIBUTION, never a point sample: this is a sawtooth, and a
        # point sample of a sawtooth is what made `ring=` on the bridge's 5 s line
        # useless (ledger §6.1). Frames sent per window is included so a window that
        # stalled cannot be mistaken for a window that ran clean.
        if TELEMETRY_S > 0:
            try:
                avail_samples.append(pcm.avail())
            except Exception:
                pass                       # never let telemetry kill the transmitter
            # snd_pcm_delay() beside avail(): same instant, same cadence, so the two
            # distributions decompose — delay minus avail is the graph-side component
            # this reader's occupancy cannot see. Sampled AFTER the read, like avail.
            if delay_handle is not None:
                if _ASOUND.snd_pcm_delay(delay_handle, ctypes.byref(_delay_val)) == 0:
                    delay_samples.append(_delay_val.value)
                    delay_errs = 0
                else:
                    delay_errs += 1
                    if delay_errs >= 1000:   # ~5 s of failures; something is wrong
                        logging.warning("delay: read-out failing persistently; "
                                        "disabling until the next (re)open.")
                        delay_handle = None
            # b2tx watermark: consumed-frame bookkeeping first — EVERY frame that
            # crosses this reader must be counted, or the anchor drifts.
            consumed += length
            if B2TX_TELEMETRY:
                try:
                    if stamp_mm is None:
                        stamp_mm = open_stamp_mmap()   # bridge may start any time
                    if stamp_mm is not None:
                        rec = read_stamp_record(stamp_mm)
                        if rec is not None:
                            seq, inst, w_frames, t_ns = rec
                            if inst != stamp_cur_inst:
                                # New bridge instance: re-anchor. From here on,
                                # b2tx = 0 at this instant minus its own hold.
                                stamp_cur_inst = inst
                                stamp_last_seq = seq
                                stamp_delta  = w_frames - consumed
                                stamp_ws.clear()
                                stamp_ts.clear()
                                b2tx_samples.clear()
                                stamp_inst_changes += 1
                                stamp_anchor_mono = time.monotonic()
                            elif seq != stamp_last_seq and stamp_delta is not None:
                                stamp_last_seq = seq
                                stamp_new_this_window = True
                                stamp_ws.append(w_frames)
                                stamp_ts.append(t_ns)
                                if len(stamp_ws) > _STAMP_KEEP:
                                    del stamp_ws[:_STAMP_KEEP // 2]
                                    del stamp_ts[:_STAMP_KEEP // 2]
                            # Latency of the period just read: interpolate the
                            # timeline at the read edge. Needs two stamps AND the
                            # settle window — before that, this instance's audio
                            # has not reached us yet and the mapping is garbage.
                            if (len(stamp_ws) >= 2 and stamp_anchor_mono is not None
                                    and time.monotonic() - stamp_anchor_mono >= B2TX_SETTLE_S):
                                target = consumed + stamp_delta
                                i = bisect.bisect_left(stamp_ws, target)
                                if i <= 0:
                                    w0, w1 = stamp_ws[0], stamp_ws[1]
                                    t0, t1 = stamp_ts[0], stamp_ts[1]
                                elif i >= len(stamp_ws):
                                    w0, w1 = stamp_ws[-2], stamp_ws[-1]
                                    t0, t1 = stamp_ts[-2], stamp_ts[-1]
                                else:
                                    w0, w1 = stamp_ws[i - 1], stamp_ws[i]
                                    t0, t1 = stamp_ts[i - 1], stamp_ts[i]
                                if w1 > w0:
                                    frac = (target - w0) / (w1 - w0)
                                    b2tx_ms = (time.monotonic_ns()
                                               - (t0 + frac * (t1 - t0))) / 1e6
                                    b2tx_samples.append(b2tx_ms)
                except Exception:
                    pass                       # never let telemetry kill the transmitter
            frames_sent += length
            now = time.monotonic()
            if now - tele_t0 >= TELEMETRY_S and avail_samples:
                s = sorted(avail_samples)
                def _q(p):                 # nearest-rank percentile, no numpy dependency
                    return s[min(len(s) - 1, int(len(s) * p))]
                ms = 1000.0 / SAMPLE_RATE

                # ── standing-residual trim ────────────────────────────────────
                # ⚠ Gate on p10, NEVER on max. This window's own data is the
                # discriminator: a standing residual reads 512/512/512 max=512, a
                # transient blip reads 0/0/0 max=512. Gating on max would splice the
                # stream on every blip. Trim before logging so the line reports it.
                #
                # p10 deliberately does NOT fire on the window the step lands in (that
                # window reads 0/512/512 — half of it was clean, so it is ambiguous).
                # It fires on the NEXT one, 5 s later, and only if the residual really
                # stood; a transient that self-clears never fires at all. The ratchet
                # has held for 33 h, so a 5 s deferral is free. Do not lower this gate
                # to catch the step window — that trades a guarantee for nothing.
                # ⚠ trim_streak resets ONLY when the residual is genuinely gone, never
                # merely because we are inside the cooldown. With a 30 s cooldown and 5 s
                # windows, resetting in the cooldown branch would pin the streak at 1 and
                # the runaway-writer warning below could never fire.
                trimmed = 0
                if _q(0.1) < period_frames:
                    trim_streak = 0
                elif now - last_trim_t >= TRIM_COOLDOWN_S:
                    trimmed = trim_standing_backlog(pcm, period_frames)
                    consumed += trimmed          # discarded frames crossed this reader
                    last_trim_t = now
                    if trimmed:
                        trim_streak += 1
                        logging.info(
                            f"trim: discarded {trimmed}f ({trimmed*ms:.1f}ms) of standing "
                            f"backlog — residual had stood at p10={_q(0.1)}f "
                            f"({_q(0.1)*ms:.1f}ms) for the whole window."
                        )
                        if trim_streak >= 3:
                            logging.warning(
                                f"trim: fired {trim_streak} windows running. The residual is "
                                f"being REBUILT, not ratcheted once — suspect the writer "
                                f"running fast, which is a different problem to this one."
                            )
                    else:
                        # p10 said standing, the live re-check disagreed: it drained on
                        # its own between the last sample and now.
                        trim_streak = 0

                # ── delay-setpoint thermostat ─────────────────────────────────
                # ⚠ Fires only on a CLEAN residual (_q(0.1) < period): surplus in
                # the buffer is the avail-trim's job above — firing here too would
                # double-splice the same fault. Two consecutive windows above the
                # setpoint before acting, so a transient delay blip never clicks.
                # One splice per cooldown; each costs one audible ~5.33 ms splice.
                if SETPOINT_TRIM and delay_samples:
                    dsp = sorted(delay_samples)
                    dp50 = dsp[min(len(dsp) - 1, int(len(dsp) * 0.5))]
                    if dp50 <= SETPOINT_DELAY_F or _q(0.1) >= period_frames:
                        setpoint_streak = 0
                    elif now - last_splice_t >= SETPOINT_COOLDOWN_S:
                        setpoint_streak += 1
                        if setpoint_streak >= 2:
                            splice_req = True
                            splice_why = "setpoint"
                            last_splice_t = now
                            logging.warning(
                                f"setpoint: delayp50 stood at {dp50}f ({dp50*ms:.2f}ms) "
                                f"above setpoint {SETPOINT_DELAY_F}f for {setpoint_streak} "
                                f"windows with clean avail — splicing one period."
                            )
                    else:
                        pass   # above setpoint but inside cooldown; keep streak

                # ⚠ `trim=` is reported SEPARATELY, never folded into `sent=`. `sent=` is
                # the "did this window stall" witness; note it alternates 937/938 periods
                # naturally (187.5 reads/s at 512f), so a 512f-short window is the 5 s
                # boundary landing differently, NOT a missed deadline.
                # ⚠ New keys are APPENDED (delayp10/50/90, dmax, dn) — every existing
                # key keeps its name, order and format, per the parsing pitfall in the
                # ledger. A window with no delay samples omits the delay keys entirely.
                if delay_samples:
                    ds = sorted(delay_samples)
                    def _dq(p):
                        return ds[min(len(ds) - 1, int(len(ds) * p))]
                    delay_part = (
                        f"delayp10/50/90={_dq(0.1)}/{_dq(0.5)}/{_dq(0.9)}f"
                        f"({_dq(0.1)*ms:.1f}/{_dq(0.5)*ms:.1f}/{_dq(0.9)*ms:.1f}ms) "
                        f"dmax={ds[-1]}f dn={len(ds)} "
                    )
                else:
                    delay_part = ""
                # ⚠ b2tx keys are APPENDED like the delay keys. They are omitted
                # unless the watermark is anchored, saw a stamp THIS window
                # (bridge alive — a dead bridge would otherwise log unboundedly
                # growing values), and did not change instance more than once
                # (multi-source churn invalidates the anchor).
                if (len(b2tx_samples) >= 2 and stamp_delta is not None
                        and stamp_new_this_window and stamp_inst_changes <= 1):
                    bs_ = sorted(b2tx_samples)
                    def _bq(p):
                        return bs_[min(len(bs_) - 1, int(len(bs_) * p))]
                    b2tx_part = (
                        f"b2txp10/50/90={_bq(0.1):.2f}/{_bq(0.5):.2f}/{_bq(0.9):.2f}ms "
                        f"bn={len(bs_)} "
                    )
                else:
                    b2tx_part = ""
                logging.info(
                    f"[tx window] availp10/50/90={_q(0.1)}/{_q(0.5)}/{_q(0.9)}f"
                    f"({_q(0.1)*ms:.1f}/{_q(0.5)*ms:.1f}/{_q(0.9)*ms:.1f}ms) "
                    f"max={s[-1]}f({s[-1]*ms:.1f}ms) n={len(s)} "
                    f"{delay_part}"
                    f"{b2tx_part}"
                    f"sent={frames_sent}f({frames_sent*ms/1000.0:.2f}s) "
                    f"trim={trimmed}f wall={now - tele_t0:.2f}s"
                )
                avail_samples.clear()
                delay_samples.clear()
                b2tx_samples.clear()
                stamp_new_this_window = False
                stamp_inst_changes = 0
                frames_sent = 0
                tele_t0 = now


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.info(f"NDI transmitter starting — source='{NDI_NAME}' | {NDI_CHANNELS}ch passthrough / {SAMPLE_RATE}Hz")
    logging.info(f"  ALSA device : {ALSA_DEVICE}")
    logging.info(f"  libndi      : {NDI_LIB_PATH}")
    try:
        run()
    except Exception as exc:
        logging.error(f"Fatal: {exc}")
        sys.exit(1)
