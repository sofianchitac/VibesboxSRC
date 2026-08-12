#!/usr/bin/env python3
"""IEC 61937 → raw AC-3/E-AC-3/DTS extractor for the TV path.

Serves both TV taps. Reads a raw stereo capture on stdin and writes a clean,
frame-aligned compressed elementary stream on stdout (suitable for
`ffmpeg -f ac3` / `-f eac3` / `-f dts`):

  * **eARC** (live) — `hw:eARC,0`, S32_LE, 24-bit left-justified. Pass `s32` as
    argv[2]; carries AC-3 at 48 kHz and DD+ at 192 kHz.
  * **TOSLINK Pico optical** (rollback) — S16_LE 48 kHz, the default.

Both taps present the same 16-bit IEC 61937 word stream once the S32 high half is
taken (see S32HighWordReader), so the burst parser is shared verbatim.

When the TV is bitstreaming Dolby Digital, the S/PDIF frames carry an IEC 61937
data burst, not LPCM. Each burst is:

    Pa = 0xF872   (sync word 1)
    Pb = 0x4E1F   (sync word 2)
    Pc = data_type (low 5 bits == 0x01 → AC-3)
    Pd = burst length in *bits*  (→ /8 = payload bytes; AC-3 5.1 640k = 2560)
    payload ... (the AC-3 frame), then zero-padding up to the 1536-frame
                (6144-byte) repetition period.

The Pico captures each 16-bit S/PDIF word little-endian (S16_LE), so the AC-3
payload words land byte-swapped relative to AC-3's big-endian byte order
(sync 0x0B77 shows up as bytes 0x77,0x0B). We byte-swap each 16-bit word of the
payload to recover the native AC-3 stream. (Confirmed 2026-06-28 against
/tmp/tv_capture2.raw: Pa@2424, Pc=0x0001, Pd=20480 bits, period=6144 bytes.)

The IEC 61937 framing + S16_LE byte-swap is identical for AC-3 (data_type 0x01) and
DTS Coherent Acoustics (data_types 0x0B/0x0C/0x0D); this extractor only strips the
61937 wrapper. The bridge script passes its codec as argv[1] (`ac3` in
tv-ac3-bridge.sh, `dts` in tv-dts-bridge.sh) so only that codec's bursts are emitted
into the matching ffmpeg decoder — a mid-stream AC-3<->DTS change then starves the
extractor and the no-burst timeout exits for a source_router re-probe, instead of
feeding the wrong decoder forever. Non-audio bursts (NULL/pause) are skipped.
(DTS path VERIFIED on hardware 2026-07-30 and 2026-08-13 — the old note here claimed the
TV re-encodes everything to Dolby, which is wrong. Both runs carried the DTS **core**
only, Pc=0x0B on a 48 kHz carrier; DTS-HD/DTS:X is 0x11 on a 192 kHz HBR carrier and is
deliberately excluded from DATA_TYPES_DTS below.)
"""

import sys
import time

PA = 0xF872
PB = 0x4E1F
PA_LE = bytes((0x72, 0xF8))      # Pa stored as S16_LE
PB_LE = bytes((0x1F, 0x4E))      # Pb stored as S16_LE
DATA_TYPE_AC3 = 0x01
# Dolby Digital Plus. Reachable only over eARC — S/PDIF optical cannot carry it
# (the TV downmixes DD+ to attenuated 2.0 PCM upstream of us; that ceiling is what
# the eARC tap exists to break). Streaming Atmos is DD+ JOC and arrives as this
# same data type; ffmpeg's eac3 decoder takes the 5.1 core and ignores the objects.
DATA_TYPE_EAC3 = 0x15
# DTS Coherent Acoustics over IEC 61937: Type I/II/III. Same 16-bit encapsulation +
# byte-swap as AC-3. DTS-HD (0x11) is excluded — it never traverses S/PDIF optical.
DATA_TYPES_DTS = frozenset((0x0B, 0x0C, 0x0D))
ACCEPTED_DATA_TYPES = (frozenset((DATA_TYPE_AC3, DATA_TYPE_EAC3))
                       | DATA_TYPES_DTS)

# ★ Pd UNITS ARE NOT UNIVERSAL. AC-3 and DTS give the burst length in BITS; E-AC-3
# (0x15) and TrueHD/MAT (0x16) give it in BYTES — matches ffmpeg spdifenc.c
# (length_code = out_bytes for E-AC-3, FFALIGN(size,2) << 3 for AC-3). The optical
# path never hit this because optical only ever carried AC-3/DTS. Getting it wrong
# fails deceptively: ffmpeg still reports `eac3, 5.1(side)` and still estimates a
# duration, it just decodes garbage at 1/8 the real bitrate.
PD_IN_BYTES = frozenset((0x15, 0x16))

# stdin read size. This is a blocking read, so it sets a latency floor: 16384 B of
# S16/48k/2ch capture = ~85ms per read. 4096 B = ~21ms — cheaper latency at negligible
# CPU cost (the OS pipe buffers; burst reassembly already spans reads).
CHUNK = 4096

# ★ CHUNK is a TIME budget wearing a byte constant's clothes. The S32 reader pulls
# 2*CHUNK raw bytes per read, so the SAME 4096 is 5.33ms at DD+'s 192 kHz carrier
# and 21.3ms at AC-3's 48 kHz one. That 4x-coarser granularity hands frames to
# pw-cat in ~21ms lumps against its 32ms period and 4096 B (3.5ms) stdin pipe, and
# it starves: MEASURED ~10 ERR/s on live AC-3 vs 0.07/s on DD+ (2026-07-31) —
# audible as clicks. Size the read from the carrier rate instead so every codec
# gets DD+'s ~5ms. See docs/latency-budget.md Stage 11.
READ_MS = 5.33


def chunk_for(rate, width):
    """Read size giving READ_MS of audio at this carrier rate and sample width.

    Returns the size to ask the READER for: the S32 adaptor discards half of every
    sample, so it pulls twice what it is asked for and its argument is halved here.
    """
    raw_per_sec = rate * 2 * (4 if width == "s32" else 2)   # 2ch
    raw = int(raw_per_sec * READ_MS / 1000)
    return max(512, (raw // 2 if width == "s32" else raw) & ~3)

# Stage 2 (mid-stream auto-switch): if the TV leaves the compressed format (switches to
# LPCM stereo), the IEC 61937 bursts vanish and we emit nothing. After this long without
# a burst, exit 0 so the bridge script exits cleanly (no systemd restart) and source_router
# re-probes the now-free device and starts the LPCM ardftsrc bridge. AC-3/DTS emit a frame
# every ~32ms even during silence, so 1s of no-burst is unambiguous.
NO_BURST_TIMEOUT = 1.0          # seconds without a compressed burst -> format change -> exit


def u16le(buf, off):
    return buf[off] | (buf[off + 1] << 8)


class S32HighWordReader:
    """Adapt an S32_LE capture to the S16_LE word stream the burst parser expects.

    The eARC tap delivers 24-bit samples left-justified in 32-bit slots, so the
    16-bit IEC 61937 word sits in the TOP half — bytes 2..3 of each little-endian
    sample. Dropping the low half yields byte-for-byte the same stream the Pico's
    native S16_LE capture produced, so everything below is untouched.

    Reads are kept 4-byte aligned across chunk boundaries; a partial sample is
    carried over rather than corrupting the word phase (one lost byte of phase
    would desync every subsequent burst search).
    """

    def __init__(self, raw):
        self._raw = raw
        self._tail = b""

    def read(self, n):
        # Ask for 2x the caller's size since half of every sample is discarded.
        data = self._tail
        while len(data) < 4:
            more = self._raw.read(max(n * 2, 4))
            if not more:
                self._tail = b""
                return b""
            data += more
        usable = len(data) - (len(data) % 4)
        self._tail = data[usable:]
        chunk = data[:usable]
        out = bytearray(usable // 2)
        out[0::2] = chunk[2::4]
        out[1::2] = chunk[3::4]
        return bytes(out)


def main():
    # argv[1] restricts which codec's bursts pass (see module docstring). No arg =
    # accept all (standalone/debug use).
    # argv[2] is the capture sample width: s16 (Pico optical, default) or s32
    # (eARC tap — 24-bit left-justified).
    accepted = ACCEPTED_DATA_TYPES
    if len(sys.argv) > 1:
        try:
            accepted = {"ac3":  frozenset((DATA_TYPE_AC3,)),
                        "eac3": frozenset((DATA_TYPE_EAC3,)),
                        "dts":  DATA_TYPES_DTS}[sys.argv[1]]
        except KeyError:
            sys.stderr.write("usage: tv_ac3_extract.py [ac3|eac3|dts] [s16|s32]\n")
            sys.exit(2)

    width = sys.argv[2] if len(sys.argv) > 2 else "s16"
    if width not in ("s16", "s32"):
        sys.stderr.write("usage: tv_ac3_extract.py [ac3|eac3|dts] [s16|s32] [rate]\n")
        sys.exit(2)

    # argv[3] is the carrier rate, used only to size the read (see chunk_for).
    # Omitted -> the historical fixed 4096, so the TOSLINK rollback bridge, which
    # passes only argv[1], keeps its exact previous behaviour.
    chunk = chunk_for(int(sys.argv[3]), width) if len(sys.argv) > 3 else CHUNK

    inp = sys.stdin.buffer
    if width == "s32":
        inp = S32HighWordReader(inp)
    out = sys.stdout.buffer
    buf = bytearray()
    last_emit = time.monotonic()

    while True:
        data = inp.read(chunk)
        if not data:
            break
        buf += data

        # Process every complete burst currently in the buffer.
        i = 0
        n = len(buf)
        while True:
            j = buf.find(PA_LE, i)
            if j < 0:
                # No more Pa candidates — discard all but a short straddle tail so the
                # buffer can't grow without bound when the stream is LPCM (no sync).
                i = max(i, n - 3)
                break
            if j + 8 > n:
                i = j          # partial burst header — keep from here for the next read
                break
            # Require Pb immediately after Pa to reject false syncs in payload data.
            if buf[j + 2:j + 4] != PB_LE:
                i = j + 2
                continue
            pc = u16le(buf, j + 4)
            pd = u16le(buf, j + 6)
            # Pd is BYTES for E-AC-3/TrueHD, BITS for AC-3/DTS — see PD_IN_BYTES.
            payload_bytes = pd if (pc & 0x1F) in PD_IN_BYTES else pd // 8
            if payload_bytes == 0:
                i = j + 8
                continue
            # Wait until the whole payload is in the buffer.
            if j + 8 + payload_bytes > n:
                i = j
                break
            # An odd payload length (Pd not a whole number of 16-bit words) is a
            # corrupt or false-sync burst — the slice swap below would raise, so
            # skip it like a non-audio burst.
            if payload_bytes % 2 == 0 and (pc & 0x1F) in accepted:
                payload = buf[j + 8:j + 8 + payload_bytes]
                # Byte-swap each 16-bit word: S16_LE capture → AC-3/DTS byte order.
                payload[0::2], payload[1::2] = payload[1::2], payload[0::2]
                out.write(payload)
                last_emit = time.monotonic()
            # else: non-audio burst (NULL/pause), other codec, or corrupt — skip payload.
            i = j + 8 + payload_bytes

        out.flush()
        del buf[:i]             # drop everything we consumed/skipped this pass

        # No accepted burst for a full second -> TV left this format (LPCM or the
        # other codec) -> exit so source_router re-probes and switches bridges.
        if time.monotonic() - last_emit > NO_BURST_TIMEOUT:
            sys.stderr.write("tv_ac3_extract: no accepted burst for >1s — exiting (format change)\n")
            return


if __name__ == "__main__":
    try:
        main()
    except (BrokenPipeError, KeyboardInterrupt):
        pass
