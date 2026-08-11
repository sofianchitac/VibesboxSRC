#!/usr/bin/env python3
"""Capture a short burst off the eARC I2S tap and classify what the TV is sending.

One run per TV setting (source / channel count / audio format). Reports:
  * whether a bit clock is present at all
  * the ACTUAL incoming sample rate (slave-mode capture has no rate autodetect,
    so this is measured from wall-clock vs frames, not read from the stream)
  * per-lane activity -> how many channels are really being sent
  * LPCM vs IEC 61937 compressed bitstream, and which codec if compressed

Stdlib only: the Pi has neither sox nor numpy.

Usage (on the Pi):
    python3 earc_probe.py                 # 5 s @ 8ch, assume 48k
    python3 earc_probe.py -r 96000        # if you suspect a 96k source
    python3 earc_probe.py -k /tmp/x.wav   # keep the capture for earc_analyze.py
"""
import argparse
import array
import math
import os
import subprocess
import sys
import tempfile
import time

# IEC 61937 burst preamble. Pa/Pb sit in consecutive subframes (ch1/ch2 of the
# same frame); Pc/Pd follow in the next frame. Pc's low 5 bits are the data type.
PA, PB = 0xF872, 0x4E1F

# IEC 61937 Pc data types (low 5 bits). Cross-checked against the repo's own
# working parser, scripts/tv_ac3_extract.py (AC-3 = 0x01, DTS = 0x0B/0x0C/0x0D,
# DTS-HD = 0x11) — do NOT edit from memory, these are easy to get shifted.
DATA_TYPES = {
    0x00: "null", 0x01: "AC-3 (Dolby Digital)", 0x03: "pause",
    0x04: "MPEG-1 layer 1", 0x05: "MPEG-1 layer 2/3",
    0x06: "MPEG-2 w/ extension", 0x07: "MPEG-2 AAC ADTS",
    0x08: "MPEG-2 layer 1 LSF", 0x09: "MPEG-2 layer 2 LSF",
    0x0A: "MPEG-2 layer 3 LSF",
    0x0B: "DTS type I (512)", 0x0C: "DTS type II (1024)",
    0x0D: "DTS type III (2048)",
    0x0E: "ATRAC", 0x0F: "ATRAC 2/3", 0x10: "ATRAC-X",
    0x11: "DTS-HD", 0x12: "WMA Pro", 0x13: "MPEG-2 AAC LSF",
    0x15: "E-AC-3 (Dolby Digital Plus)", 0x16: "Dolby TrueHD / MAT",
}


def capture(device, nch, rate, seconds, path):
    """arecord for `seconds`, timing the wall clock so the true rate is derivable."""
    cmd = ["arecord", "-D", device, "-c", str(nch), "-f", "S32_LE",
           "-r", str(rate), "-d", str(seconds), "-q", path]
    t0 = time.monotonic()
    try:
        subprocess.run(cmd, timeout=seconds * 6 + 10, check=True,
                       stderr=subprocess.PIPE)
    except subprocess.TimeoutExpired:
        return None, "arecord blocked — NO BIT CLOCK arriving on GPIO 18"
    except subprocess.CalledProcessError as e:
        return None, "arecord failed: " + e.stderr.decode(errors="replace").strip()
    return time.monotonic() - t0, None


def read_wav(path):
    d = open(path, "rb").read()
    i = d.index(b"data")
    n = int.from_bytes(d[i + 4:i + 8], "little")
    a = array.array("i")
    a.frombytes(d[i + 8:i + 8 + n])
    if sys.byteorder != "little":
        a.byteswap()
    return a


def scan_61937(ch_a, ch_b):
    """Look for a Pa/Pb preamble pair. Returns (frame_index, data_type) or None.

    Tries two bit alignments: 24-bit left-justified in S32 puts the 16-bit
    61937 word at bits 31..16; a 16-bit-in-S32 source would put it lower.
    """
    for shift in (16, 8):
        m = 0xFFFF
        for i in range(len(ch_a) - 1):
            if ((ch_a[i] >> shift) & m) == PA and ((ch_b[i] >> shift) & m) == PB:
                pc = (ch_a[i + 1] >> shift) & m
                return i, pc & 0x1F
    return None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("-D", "--device", default="hw:eARC,0")
    p.add_argument("-c", "--channels", type=int, default=8)
    p.add_argument("-r", "--rate", type=int, default=48000)
    # 10 s default: the rate estimate is biased LOW by arecord's process startup
    # and device open (~80 ms), which is inside the timed window. At 10 s that is
    # <1%, comfortably separating 44.1k from 48k (8.1% apart). At 3 s it is ~2.6%
    # and the margin gets thin — don't shorten this just to save time.
    p.add_argument("-d", "--seconds", type=int, default=10)
    p.add_argument("-k", "--keep", help="write the capture here instead of a temp file")
    p.add_argument("-l", "--label", default="", help="note for your own log")
    args = p.parse_args()

    path = args.keep or tempfile.mktemp(suffix=".wav")
    if args.label:
        print("=== %s" % args.label)

    wall, err = capture(args.device, args.channels, args.rate, args.seconds, path)
    if err:
        print("FAIL: " + err)
        return 1

    a = read_wav(path)
    nch = args.channels
    frames = len(a) // nch
    true_rate = frames / wall if wall > 0 else 0
    print("captured %d frames in %.2f s" % (frames, wall))
    nearest = min((44100, 48000, 88200, 96000, 176400, 192000),
                  key=lambda r: abs(r - true_rate))
    print("requested rate %d Hz -> MEASURED ~%.0f Hz (%.3fx) -> nearest standard: %d Hz"
          % (args.rate, true_rate, true_rate / args.rate, nearest))
    print("  (estimate is biased ~1% LOW at -d 10 by arecord startup; more at shorter -d)")
    if nearest != args.rate:
        print("  ** RATE MISMATCH — samples are valid but the stream is not %d Hz."
              % args.rate)
        print("  ** Re-run with -r %d for correct timing." % nearest)

    print("\n%3s %10s %10s %9s %8s" % ("ch", "pk dBFS", "rms dBFS", "nonzero%", "lowbyte"))
    live = []
    cols = []
    for c in range(nch):
        s = a[c::nch]
        cols.append(s)
        pk = 0
        acc = 0.0
        nz = 0
        low = 0
        for v in s:
            av = -v if v < 0 else v
            if av > pk:
                pk = av
            acc += float(v) * v
            low |= v & 0xFF
            if v:
                nz += 1
        rms = math.sqrt(acc / len(s))
        dbfs = lambda x: 20 * math.log10(x / 2147483648.0) if x > 0 else float("-inf")
        print("%3d %10.1f %10.1f %8.1f%% %8x"
              % (c + 1, dbfs(pk), dbfs(rms), 100.0 * nz / len(s), low))
        if pk:
            live.append(c + 1)

    print("\nlive channels: %s" % (live or "NONE — bit clock present but all lanes silent"))
    lanes = sorted({(c - 1) // 2 for c in live})
    if lanes:
        print("live lanes:    %s" % ", ".join("SD%d" % l for l in lanes))

    hit = scan_61937(cols[0], cols[1]) if nch >= 2 else None
    if hit:
        i, dt = hit
        print("\nFORMAT: IEC 61937 COMPRESSED BITSTREAM"
              " — %s (data_type %d), first burst at frame %d"
              % (DATA_TYPES.get(dt, "unknown type"), dt, i))
        print("        This is passthrough, not LPCM. Set the TV to PCM for the"
              " lane-map measurement.")
    else:
        print("\nFORMAT: LPCM (no IEC 61937 preamble found)")

    if not args.keep:
        os.unlink(path)
    else:
        print("\ncapture kept at %s" % args.keep)
    return 0


if __name__ == "__main__":
    sys.exit(main())
