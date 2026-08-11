#!/usr/bin/env python3
"""Offline IEC 61937 -> raw elementary stream extractor for eARC tap captures.

Bench/verification counterpart to scripts/tv_ac3_extract.py (which is the live,
streaming version for the optical path). Takes an S32_LE WAV captured off the
eARC tap and writes the demuxed compressed stream, ready for
`ffmpeg -f eac3 -i -`.

The one real difference from the optical extractor: the Pico captures S16_LE, so
the 61937 word IS the sample. Here the tap gives 24-bit left-justified in S32, so
the 16-bit word sits at bits 31..16.

Burst layout (subframe A = ch1, subframe B = ch2, consecutive):
    Pa = 0xF872, Pb = 0x4E1F, Pc = data type, Pd = burst length in BITS
    then payload words, then zero padding to the repetition period.

Usage:
    python3 earc_61937_extract.py capture.wav out.eac3
"""
import array
import sys

PA, PB = 0xF872, 0x4E1F
DATA_TYPES = {0x01: "ac3", 0x15: "eac3", 0x16: "truehd",
              0x0B: "dts", 0x0C: "dts", 0x0D: "dts"}

# ★ Pd UNITS ARE NOT UNIVERSAL. AC-3 and DTS express the burst length in BITS;
# E-AC-3 (0x15) and TrueHD/MAT (0x16) express it in BYTES. Getting this wrong
# yields a stream ffmpeg still recognises as "eac3, 5.1(side)" — it just decodes
# to garbage at 1/8 the real bitrate, which is a slow way to find the bug.
# (Matches ffmpeg spdifenc.c: length_code = out_bytes for E-AC-3,
#  FFALIGN(size,2) << 3 for AC-3.)
PD_IN_BYTES = frozenset((0x15, 0x16))


def main():
    if len(sys.argv) < 3:
        sys.stderr.write(__doc__)
        return 2
    src, dst = sys.argv[1], sys.argv[2]
    nch = int(sys.argv[3]) if len(sys.argv) > 3 else 8

    d = open(src, "rb").read()
    i = d.index(b"data")
    n = int.from_bytes(d[i + 4:i + 8], "little")
    a = array.array("i")
    a.frombytes(d[i + 8:i + 8 + n])
    if sys.byteorder != "little":
        a.byteswap()

    # Flatten to the 61937 subframe word sequence: A0,B0,A1,B1,...
    frames = len(a) // nch
    words = array.array("H", bytes(2 * 2 * frames))
    for f in range(frames):
        words[2 * f] = (a[f * nch] >> 16) & 0xFFFF
        words[2 * f + 1] = (a[f * nch + 1] >> 16) & 0xFFFF

    out = bytearray()
    bursts = 0
    types = {}
    w = 0
    total = len(words)
    while w + 4 < total:
        if words[w] == PA and words[w + 1] == PB:
            pc = words[w + 2]
            pd = words[w + 3]
            dt = pc & 0x1F
            types[dt] = types.get(dt, 0) + 1
            nwords = (pd + 1) // 2 if dt in PD_IN_BYTES else pd // 16
            payload = words[w + 4:w + 4 + nwords]
            # 61937 carries the elementary stream byte-swapped within each
            # 16-bit word; swap back to native (AC-3 sync 0x0B77).
            for v in payload:
                out.append((v >> 8) & 0xFF)
                out.append(v & 0xFF)
            bursts += 1
            w += 4 + nwords
        else:
            w += 1

    open(dst, "wb").write(out)
    for dt, cnt in sorted(types.items()):
        sys.stderr.write("data_type 0x%02X (%s): %d bursts\n"
                         % (dt, DATA_TYPES.get(dt, "?"), cnt))
    sys.stderr.write("wrote %d bytes from %d bursts -> %s\n"
                     % (len(out), bursts, dst))
    return 0


if __name__ == "__main__":
    sys.exit(main())
