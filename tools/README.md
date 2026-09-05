# tools/ — bench diagnostics

One-shot instruments, not part of the running system. Nothing here is installed by
`install.sh` or referenced by any service; copy a script to the Pi when you need it.
(`deploy.sh` does sync this directory to `/opt`, so a tool is runnable there without copying.)

They exist because the eARC path has almost no observability by default. The capture card
is always enumerated whether the TV is on or off, slave-mode I2S has no rate autodetect,
and a bitstream looks exactly like noise until something identifies it. Each of these
answers one question that was otherwise unanswerable.

## Finding out what the TV is actually sending

**`earc_probe.py`** is the one to reach for first. One run per TV setting: it captures
briefly and reports whether a bit clock is present, the **measured** incoming sample rate,
which of the four data lanes carry signal, and whether the content is LPCM or IEC 61937 —
naming the codec if it is. Slave mode has no rate autodetect, so a 192 kHz DD+ stream
opened at 48 kHz otherwise just looks like garbage rather than a rate mismatch.

**`earc_analyze.py`** reports per-channel peak, RMS and cross-correlation from a captured
WAV. Useful for identifying which lane carries which channel, and for telling real stereo
from a dual-mono downmix. Stdlib only — the Pi has neither numpy nor sox.

**`earc_61937_extract.py`** is the offline counterpart to the live bridge: demux a capture
into a raw elementary stream you can hand to a decoder. Good for confirming a stream is
intact before blaming the realtime path.

## Latency

**`earc_inflight_probe.py`** answers "where is the audio right now". It reopens the
pipeline's pipes via `/proc/<pid>/fd` and issues `FIONREAD` — a query, never a read — plus
`arecord`/`pw-cat` byte counters expressed as audio-seconds. Needs root. Arm it *before*
the bridge starts.

> ⚠ A buffer **inside** a process is invisible to this, and to any pipe introspection.
> Python's `BufferedReader.read(n)` drains a pipe continuously into its own buffer while
> waiting, so the pipe can read zero bytes while a full read's worth of audio sits in the
> reader. Measuring pipes alone undercounts. This cost real debugging time.

**`earc_latency_marker.py`** measures end-to-end decode latency with the decoder as the
only variable. This is what established that GStreamer holds one fewer decode frame than
ffmpeg on DD+ (~22 ms).

## Buffer occupancy

**`cap_watch.py`** collects the ardftsrc bridge's own `capp10/50/90` (the ALSA capture ring's
trough), `readyp10` (the resampler FIFO's trough), bridge starts and kernel under-voltage
events into one time-aligned TSV. It exists because journald on this box keeps only about two
days, so a multi-day question cannot be answered by reading the journal at the end.

It settled one: over 76 h and 2018 windows across `@tv`, `@usb` and `@lyrion`, `capp10` is
64 f (1.5 ms) with min == med == max — not one window higher, against a granted buffer of
2720–4096 f. The capture ring never accumulates, so the race reset's deliberate skip of it
costs about a millisecond. Its collection timers were removed with the question on
2026-09-04; run it by hand (`--report` reads the retained TSV).

> ⚠ Its verdict is gated at two sources × 180 windows and counts *qualifying* sources. An
> earlier version required every source it had ever seen to clear the bar, which let one brief
> appearance suppress the answer indefinitely.

## Decoder comparison

**`compare_decoders.sh`** compares the ffmpeg and GStreamer arms on a real capture.

> ⚠ Its per-channel level block is **broken on some ffmpeg builds** — the `astats` grep
> matches nothing, so it prints `DIFFER` followed by an empty table, which reads as a
> verdict when nothing was measured. The warning is repeated at the top of the script.
> The open question it was written to settle — whether GStreamer's ~1 dB level difference
> is static DRC or a fixed offset — needs a passage with wide dynamics in the same
> capture, since a near-uniform cut with the crest factor preserved looks identical either
> way.
