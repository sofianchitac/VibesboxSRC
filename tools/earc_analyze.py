#!/usr/bin/env python3
"""Per-channel content analysis of an eARC I2S capture WAV (S32_LE, N ch)."""
import array
import math
import sys

path = sys.argv[1]
nch = int(sys.argv[2]) if len(sys.argv) > 2 else 8

d = open(path, "rb").read()
i = d.index(b"data")
n = int.from_bytes(d[i + 4:i + 8], "little")
a = array.array("i")
a.frombytes(d[i + 8:i + 8 + n])
if sys.byteorder != "little":
    a.byteswap()

frames = len(a) // nch
print("frames=%d  (%.2f s @48k)" % (frames, frames / 48000.0))
print("%3s %12s %8s %10s %9s %9s %8s"
      % ("ch", "peak", "pk dBFS", "rms dBFS", "lowbyte", "nonzero%", "hf"))

for c in range(nch):
    s = a[c::nch]
    pk = 0
    acc = 0.0
    dacc = 0.0
    lowset = 0
    nz = 0
    prev = 0.0
    for v in s:
        av = -v if v < 0 else v
        if av > pk:
            pk = av
        x = float(v)
        acc += x * x
        dx = x - prev
        dacc += dx * dx
        prev = x
        lowset |= v & 0xFF
        if v:
            nz += 1
    rms = math.sqrt(acc / len(s))
    # First-difference RMS ratio: a cheap spectral-centroid proxy used to tell
    # LFE apart from full-range channels WITHOUT trusting a declared layout.
    # Read it RELATIVELY, never against an absolute threshold — real programme
    # material sits around 0.11-0.18 at 48 kHz, and an LFE channel lands an
    # order of magnitude lower (0.016 measured on the 2026-07-28 DD+ decode).
    hf = math.sqrt(dacc / acc) if acc else 0.0
    dbfs = lambda x: 20 * math.log10(x / 2147483648.0) if x > 0 else float("-inf")
    print("%3d %12d %8.1f %10.1f %9x %8.1f%% %8.3f"
          % (c + 1, pk, dbfs(pk), dbfs(rms), lowset, 100.0 * nz / len(s), hf))

# Cross-correlation at lag 0 between channel pairs — identical lanes / silent pairs
print("\ncorrelation matrix (lag 0, first 48000 frames):")
lim = min(frames, 48000)
cols = [a[c::nch][:lim] for c in range(nch)]
norms = [math.sqrt(sum(float(v) * v for v in col)) or 1.0 for col in cols]
print("     " + " ".join("%6d" % (c + 1) for c in range(nch)))
for x in range(nch):
    row = []
    for y in range(nch):
        dot = sum(float(p) * q for p, q in zip(cols[x], cols[y]))
        row.append("%6.2f" % (dot / (norms[x] * norms[y])))
    print("%4d " % (x + 1) + " ".join(row))
