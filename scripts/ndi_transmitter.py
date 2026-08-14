#!/usr/bin/env python3
"""
NDI Audio Transmitter — NDI SDK 6.x via ctypes
================================================
Reads 8ch FLOAT32LE / 96kHz audio from the NDITX ALSA snd-aloop read side
(hw:NDITX,1,0) and transmits it as a named NDI audio source on the network.

Key design decisions vs. ndi-free-audio binary:
  - Reads NDITX by name ("hw:NDITX,1,0"), never by fragile PortAudio device index.
  - Enforces exact ALSA params: 8ch / 96kHz / FLOAT32LE — will not silently
    negotiate a wrong format.
  - snd-aloop constrains device 1 to the same params as device 0 once CamillaDSP
    opens the write side; this script will retry until that happens.
  - NDI audio frame: FLTP (planar float32), 8ch, 96kHz — all correct.
  - Interleaved→planar conversion via numpy (zero-copy transpose).
  - No PortAudio, no binary EULA, no guessing.

Reads from  : hw:NDITX,1,0  (snd-aloop read side — CamillaDSP writes to ,0,0)
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
import logging
import os
import signal
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
ALSA_DEVICE    = os.environ.get("NDI_ALSA_DEV",   "hw:NDITX,1,0")
SAMPLE_RATE    = int(os.environ.get("NDI_RATE",    "96000"))
ALSA_CHANNELS  = int(os.environ.get("NDI_ALSA_CH", "8"))   # ALSA read width (NDITX is 8ch)
NDI_CHANNELS   = ALSA_CHANNELS                              # NDI send width = read width (pure passthrough)
PERIOD_FRAMES  = int(os.environ.get("NDI_PERIOD",  "1024"))   # match CamillaDSP chunksize
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
    Open the NDITX snd-aloop read side with exact params.

    snd-aloop locks device 1 to the same format as device 0 once CamillaDSP
    opens the write side. We retry until CamillaDSP has applied its 8ch config
    and the loopback accepts FLOAT32LE / 8ch / 96kHz.

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
        f"Is CamillaDSP running a 6ch output config?"
    )


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

        # data is bytes: FLOAT32LE interleaved, shape (length × ALSA_CHANNELS)
        interleaved = np.frombuffer(data, dtype=np.float32).reshape(length, ALSA_CHANNELS)

        # Pure 6ch passthrough: ch1-2 = FL/FR, ch3-6 carry whatever the sum bus has
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
