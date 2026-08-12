#!/bin/bash
# eARC bitstream bridge — the encoded (IEC 61937) variant of the TV eARC path,
# parameterized by codec: `earc-bitstream-bridge.sh <eac3|ac3|dts>`. Started by
# source_router.py (via earc-bitstream-bridge@<codec>.service, in place of
# ardftsrc-bridge@tv) when the eARC tap carries a compressed bitstream rather
# than LPCM.
#
# This is the successor to tv-bitstream-bridge.sh (TOSLINK Pico optical), kept for
# rollback. Two things changed with the tap, both load-bearing:
#
#   1. SAMPLE WIDTH. The Pico captured native S16_LE; the eARC tap gives 24-bit
#      left-justified in S32, so the 16-bit 61937 word sits in the TOP half of each
#      sample. tv_ac3_extract.py takes `s32` as argv[2] and strips the low half.
#   2. RATE VARIES WITH CODEC — and slave-mode I2S capture has NO rate autodetect,
#      so it must be declared correctly per codec or ALSA's flow control is wrong by
#      4x. Measured on hardware 2026-07-28: DD+ = 192 kHz, AC-3 = 48 kHz. (The
#      optical path never had this problem: the Pico's clock is fixed at 48 kHz in
#      firmware regardless of what the S/PDIF input carries.)
#
# ardftsrc CANNOT be used here: the frames carry a compressed bitstream, not PCM,
# so a DFT resampler would just produce noise. The chain is:
#
#   arecord  raw S32_LE <rate> 2ch from hw:eARC,0   (bit-exact — hw:, never plughw:)
#     │
#   tv_ac3_extract.py <codec> s32 <rate>   IEC 61937 → frame-aligned, byte-swapped ES
#                       ★ <rate> is passed so the extractor can size its blocking read
#                       as a TIME budget. Its old fixed 4096 B was 5.33ms at DD+'s
#                       192kHz carrier but 21.3ms at AC-3/DTS's 48kHz one, which
#                       starved pw-cat below — MEASURED ~10 ERR/s on live AC-3 against
#                       DD+'s 0.07/s, audible as clicks (2026-07-31, Stage 11).
#     │
#   ffmpeg -f <codec>   decode → 6ch PCM at the stream's native 48k (no SRC — see below)
#     │
#   pw-cat --raw        emit source.tv.ardftsrc as a 48k 6ch node (autoconnect off);
#                       PipeWire's adapter resamples 48k→96k into the graph and
#                       source_router links it into CamillaDSP dsp-in like USB 6ch.
#
# The output node name is identical to the LPCM ardftsrc bridge (source.tv.ardftsrc),
# so reconcile()/UI/Now-Playing treat the TV source uniformly. The bridges are
# MUTUALLY EXCLUSIVE — source_router runs exactly one at a time.
#
# Dolby Atmos needs no special handling: streaming Atmos (YouTube/Netflix/Disney+) is
# DD+ JOC — object metadata layered on a 5.1 core, carried as the SAME data_type 0x15
# — and ffmpeg's eac3 decoder decodes the core and ignores the objects, which is
# exactly the 5.1 bed we want. Only TrueHD/MAT (0x16, local media players) is out of
# scope; it is HBR and would need its own handling.
#
# ✔ DTS VERIFIED on hardware, twice, and the old "the TV re-encodes everything to Dolby"
#   premise was simply wrong — DTS reaches this path fine:
#     2026-07-30  Chromecast w/ Google TV: 937/937 bursts Pc=0x0B, 1.509 Mbps, clean 5.1
#     2026-08-13  VLC on a Mac over HDMI -> C9 Pass Through: dcaparse/avdec_dca, 0 errors
#   Both delivered the DTS **core** only (0x0B, 48 kHz carrier). DTS:X / DTS-HD MA need
#   0x11 on a 192 kHz HBR carrier and have never arrived — neither source can emit HBR.
#   source_router now names 0x11 explicitly rather than mis-refusing it as a rate problem.
#
# ⚠ The 2026-07-28 PipeWire-SRC move was measured on @eac3 only. @ac3 and @dts inherit it
#   untested, but the arithmetic transfers exactly: the elementary stream is 48k for every
#   codec (source_router snaps bitstream modes to 48000), and an AC-3 decode frame is also
#   1536 samples, so --latency=1536 is exactly one frame for them too, same pipe math.
#
# Resampler: 48k→96k is done by PIPEWIRE's adapter, not by ffmpeg (which used to carry
# `-af aresample=out_sample_rate=96000`; this Pi's ffmpeg has no soxr, so that was plain
# swr). Changed 2026-07-28. ardftsrc's "own the SRC" rule does not apply to this path —
# it is an already-lossy compressed stream where resampler *choice* is inaudible next to
# the codec — so the deciding argument is not quality, it is drift:
#
#   swr is a FIXED-RATIO resampler, so this path had NO clock-drift servo at all between
#   the TV's clock and the Pi's. Every other source gets one (the Rust ardftsrc bridges,
#   including the LPCM TV bridge); the bitstream path got nothing, and drift could only
#   ever be absorbed by eventually xrunning. PipeWire's adapter is ADAPTIVE, so moving
#   the SRC there closes a structural gap rather than merely relocating a filter.
#
# resample.quality=12 (256-tap sinc). Every other source on this box runs q14 (1024 taps),
# and this path started there, but q14's group delay is ~n_taps/2 at the INPUT rate =
# ~512/48000 = 10.7ms, versus ~2.7ms at q12 — real latency spent on SRC precision that is
# already far below the noise floor of a lossy codec. On the pristine Lyrion path that
# trade would be wrong; here latency wins. Both are computed from PipeWire's quality
# table (q12 = 256 taps, q14 = 1024), NOT measured — the delay is not exposed in pw-dump.
# It MUST be set here in --properties: the
# `stream.properties` drop-in in config/pipewire/pipewire.conf.d/11-vibesbox-resampler.conf
# is daemon-side, so it reaches in-daemon streams (module-bluez5) but NOT a separate
# client process like pw-cat — verified by pw-dump, which showed no resample.quality on
# this node at all before this was added.
#
# Teardown: systemd KillMode=control-group + KillSignal=SIGKILL tears down the whole
# pipeline cgroup on `systemctl stop`, so no manual child management is needed here.
#
# Stage 2 (mid-stream auto-switch): when the TV leaves this codec (to LPCM or another
# codec — the extractor passes only $FORMAT bursts), tv_ac3_extract.py exits 0; ffmpeg
# and pw-cat then hit EOF and exit 0 too, so this script exits 0 and systemd does NOT
# restart it (Restart=on-failure) — source_router re-probes the freed device and starts
# the right bridge. NOTE: deliberately NO `pipefail`, so the script's exit status is
# pw-cat's (0 on clean EOF). With pipefail, arecord's SIGPIPE (141) when the extractor
# closes the pipe would mask that 0 and trigger a spurious restart loop.
#
# Usage:  earc-bitstream-bridge.sh <eac3|ac3|dts>

set -e

FORMAT="${1:?usage: earc-bitstream-bridge.sh <eac3|ac3|dts>}"
# Capture rate per codec. IEC 61937 carries E-AC-3 at 4x the base rate (its burst
# repetition period is 4x AC-3's), which is why DD+ shows up as a 192 kHz stream
# carrying 48 kHz audio. Measured, not assumed — see config/overlays/README.md.
case "$FORMAT" in
    eac3)    RATE=192000 ;;
    ac3|dts) RATE=48000  ;;
    *) echo "earc-bitstream-bridge: unknown format '$FORMAT' (eac3|ac3|dts)" >&2; exit 2 ;;
esac

CARD=eARC
IN_DEVICE="hw:${CARD},0"
EXTRACT=/opt/vibesbox-src/scripts/tv_ac3_extract.py
TRIM=/opt/vibesbox-src/scripts/pcm_backlog_trim.py

# DECODER: gst (default) | ffmpeg. Override with the systemd drop-in
# /etc/systemd/system/earc-bitstream-bridge@.service.d/decoder.conf, then restart the
# bridge (source_router restarts it on the next TV format change anyway).
#
# gst has been the live decoder since 2026-08-01 for the latency win below. ffmpeg is
# the fallback and the only arm with DRC control — see warning 1.
#
# GStreamer measured 2026-07-31 (Stage 12) as a drop-in for ffmpeg in
# ../tools/earc_latency_marker.py, decoder as the only variable: DD+ 61.6 -> 40.1ms
# (-21.9), AC-3 68.5 -> 71.6 (+2.1). The win is real and its mechanism is understood —
# ffmpeg holds ONE EXTRA DECODE FRAME (gst's t1-t0 leads by ~35ms ~= one 32ms frame).
#
# ⚠⚠ IT IS NOT A FREE SWAP — the trade accepted in choosing it as the default:
#
#  1. NO DRC CONTROL. `gst-inspect-1.0 avdec_ac3` exposes only max-errors / min-latency /
#     plc / tolerance — there is NO `drc_scale` equivalent, and libavcodec's default is
#     1.0 (DRC ON). The ffmpeg arm explicitly passes `-drc_scale 0` to keep Dolby's
#     dynamic-range compression out of a calibrated room. So gst = Dolby DRC applied.
#     Stage 12's "bit-identical" result was on a SYNTHETIC file with neutral dynrng and
#     does NOT clear this; ../tools/compare_decoders.sh settles it on real content.
#  2. ★★ CHANNEL ORDER — the caps below are load-bearing, do not "simplify" them.
#     avdec_ac3/avdec_eac3 natively emit `layout=non-interleaved` and
#     `channel-mask=0xc0f` (5.1-SIDE: FL FR FC LFE SL SR), which is exactly ffmpeg's
#     own 5.1 order. Both fields must be pinned:
#       - `layout=interleaved` because pw-cat needs interleaved f32;
#       - `channel-mask=0xc0f` because asking for anything else makes audioconvert
#         REMAP. Verified on hardware 2026-08-01 with a per-channel-tone 5.1 AC-3:
#           0xc0f + interleaved -> BIT-IDENTICAL to the ffmpeg arm
#           0x3f  (5.1-REAR)    -> DIFFERENT BYTES  <-- silently swaps the surrounds
#     This matters because the pw-cat node below deliberately MISLABELS that order as
#     FL,FR,RL,RR,FC,LFE to match the USB scramble (see the channel-map note), so a
#     reorder here lands as wrong speakers in REAPER, looking like a routing bug.
#     Re-verify after any change: per-channel RMS at dsp-in:monitor_1..6, channel 4
#     (LFE) must show an order of magnitude less HF energy than the rest.
#  3. ⛔ It CANNOT touch the ~230ms multichannel lipsync gap — Stage 14 placed that
#     upstream of arecord. This buys ~22ms on DD+, nothing else.
DECODER="${DECODER:-gst}"
case "$FORMAT" in
    eac3) GST_PARSE=ac3parse; GST_DEC=avdec_eac3 ;;
    ac3)  GST_PARSE=ac3parse; GST_DEC=avdec_ac3  ;;
    dts)  GST_PARSE=dcaparse; GST_DEC=avdec_dca  ;;
esac

# OUTPUT HEADROOM. AC-3/E-AC-3 decode of loud content runs past 0 dBFS. Measured
# 2026-08-02 on live DD+ — on the GST arm, i.e. WITH libavcodec's default DRC applied —
# at up to +1.68 dBFS, with 5 of 6 channels over full scale on ~80% of the receiver's
# telemetry samples (peak per channel: +1.32 +1.13 +1.68 +1.35 +0.38 +1.37 dB). Every
# stage from here to the RME is float so nothing clips in transit, but REAPER mutes a
# track for protection when it goes far enough over, which is what actually bit. -4 dB
# matches the headroom the ardftsrc bridges already carry for the same reason (20113ae).
# Applies to ac3/dts as well: same decode path, and a per-codec gain would put the TV
# source at a different level depending on what the broadcaster sent.
# ⚠ The two arms MUST stay level-matched — -4 dB == 0.63096 linear. Change both or
# neither, or flipping DECODER silently shifts the room calibration by 4 dB.
HEADROOM_DB=-4
HEADROOM_LIN=0.63096

# The decoder stage, as a function so the pipeline below reads the same either way.
# Both arms: elementary stream on stdin -> 6ch f32le 48k at -4 dB on stdout, nothing else.
decode() {
    if [ "$DECODER" = "gst" ]; then
        # volume sits AFTER the caps filter on purpose: downstream of audioconvert it
        # cannot provoke the remap that note 2 above warns about, and it is caps-passthrough
        # so the load-bearing 0xc0f survives it. Verified -4.00 dB on all 6 ch, 2026-08-02.
        exec gst-launch-1.0 -q \
            fdsrc fd=0 ! "$GST_PARSE" ! "$GST_DEC" ! audioconvert ! \
            "audio/x-raw,format=F32LE,layout=interleaved,channels=6,rate=48000,channel-mask=(bitmask)0xc0f" ! \
            volume volume="$HEADROOM_LIN" ! \
            fdsink fd=1 sync=false
    fi
    exec ffmpeg -hide_banner -nostats -loglevel warning -nostdin \
        -fflags nobuffer -flags low_delay -analyzeduration 100000 -probesize 32768 \
        -drc_scale 0 -f "$FORMAT" -i - \
        -af "volume=${HEADROOM_DB}dB" \
        -ac 6 -c:a pcm_f32le -f f32le -
}

echo "earc-bitstream-bridge: $IN_DEVICE S32_LE ${RATE} 2ch (IEC 61937 ${FORMAT^^}) -> decode 5.1 [$DECODER] -> 48k f32 -> source.tv.ardftsrc (PipeWire SRC -> 96k)"

# Channel map note: ffmpeg decodes to the canonical 5.1 order FL FR FC LFE RL RR (the
# eARC DD+ decode was confirmed to be exactly this on 2026-07-28 — channel 4 measured
# an order of magnitude less HF energy than the rest, i.e. LFE), but we deliberately
# label the pw-cat node FL,FR,RL,RR,FC,LFE — a NON-canonical map that swaps the
# (FC,LFE) and (RL,RR) pairs. This makes source.tv.ardftsrc carry the exact same
# label->content scramble as the USB 6ch source (iPadOS emits 5.1 as FL FR RL RR FC LFE
# but tags it FL FR FC LFE RL RR — see project_usb_channel_order_ipad). source_router
# links sources into dsp-in by channel NAME, so both paths land identically in REAPER,
# where a SINGLE remap corrects both. Without this, correctly-ordered decode output gets
# mis-scrambled by REAPER's USB remap (wrong speaker positions + low 5.1 volume).
#
# hw: (not plughw:) so the bitstream is passed byte-exact with no ALSA conversion.
# --buffer-time/--period-time: the driver default is a 500ms buffer which dominates this
# path's latency. 40ms buffer / 5ms period (8 periods) trims ~460ms. ★ The BUFFER must stay
# >= the ~32ms IEC 61937 repetition period (measured: 313 bursts in 10s) so a whole burst
# always fits — that is the floor, do not cut 40000 further. The PERIOD is the latency
# term (you wake once a period is full) and is free to shrink: 80ms/20ms until 2026-07-28,
# then 40ms/10ms, then 5ms period 2026-07-28 (ERR 0/30s).
#
# -drc_scale 0 is a DECODER option, so it must precede -i: keep Dolby's dynamic-range
# compression out of the bed. This is a home cinema feeding a calibrated room, not a TV
# speaker — the DRC metadata is aimed at the latter.
#
# ffmpeg low-latency probe: -f "$FORMAT" forces the format, so heavy probing is unneeded.
# 100ms analyzeduration + nobuffer/low_delay = ~0.15s to first output.
#
# pw-cat --latency is counted in THIS node's frames, i.e. at 48k now, and MUST stay an
# integer multiple of the quantum as seen on the node's own side — 256 frames, the 96k
# graph quantum of 512 scaled by 48/96. The pw-cat default of 100ms is not a multiple of
# it, and that misalignment spliced the output at a fixed phase within every quantum —
# audible clicks, ~6/s (2026-07-28). Re-check for clicks after ANY change here.
#
# 1536 = 6 quanta = 32ms, down from the old 64ms (which at 96k was `--latency=6144`).
# 1536 is EXACTLY ONE DD+ decode frame at 48k, and that is why it wins. Under the old 96k
# output this same one-frame floor starved hard — 176 dropouts in 35s — because ffmpeg
# emits a whole decode frame in ONE write and at 96k that was 73728 B, larger than the
# 64 KiB pipe, so the write blocked mid-frame. At 48k a decode frame is 36864 B and lands
# ATOMICALLY, so consumption granularity now equals production granularity and the old
# "must hold more than one frame" rule no longer binds.
#
# Swept on hardware 2026-07-28, ERR/s on the pw-top node line (30s samples, DD+ live):
#   3072 (64ms) 6/s | 2560 (53ms) 6/s | 2048 (43ms) 5/s | 1536 (32ms) ~0.07/s ← SHIPPED
#   | 1280 (27ms) 12/s ← degraded, below one decode frame
# ★ The 5-6/s figures are NOT this change's doing: the OLD 96k/swr build measured 8.6/s on
# the same counter, so ~5-8/s is this bursty pipe-fed node's pre-existing baseline. Always
# re-measure the old build as a control before treating an ERR rate here as a fault.
#
# Verified on 45s of real 5.1 (dialogue-heavy, FC -28 dBFS): no click signature. The test
# that matters is PHASE-LOCKING, not a raw step count — the historical failure spliced the
# output at a FIXED phase within every quantum, so bin the large sample-to-sample steps by
# (index mod 512) and compare the peak bin against uniform. Here: max bin 23 vs ~14
# expected = noise, i.e. the large steps are musical transients, not splices. One 5.33ms
# all-channel zero gap (exactly one quantum) appeared at t=31.4s in 45s; dsp-in's monitor
# is known to report gaps that are not in the real output, so treat it as unconfirmed —
# re-run against 2048 if a tick is ever actually heard.
#
# The python exec wrapper exists only to shrink pw-cat's stdin pipe before it starts
# reading. A pipe defaults to 64 KiB and this one sits PERMANENTLY full (measured: a
# rock-steady 64768 B) because ffmpeg races ahead at startup and then blocks in write
# forever after — pure in-flight latency that buys nothing. 4096 B caps it at ~3.5ms
# (48k * 6ch * 4B = 1152000 B/s); it was 16384 while this stage emitted 96k (~7ms), then
# 8192 at 48k for the same ~7ms — ★ this value is a TIME budget, so halve/double it with
# the rate or the ms silently follow. 4096 verified ERR 0/30s 2026-07-28.
# Nothing else about the invocation changes: execvp replaces the wrapper with
# pw-cat, so the exit status and the cgroup teardown behave exactly as before.
# EARC_LEGACY_CHUNK=1 withholds the carrier rate from the extractor, so it falls back to
# its historical FIXED 4096 B read. That is 5.33ms at DD+'s 192kHz carrier (i.e. @eac3 is
# UNAFFECTED) but 21.3ms at AC-3/DTS's 48kHz one — the Stage 11 starvation bug, ~10 ERR/s
# and audible clicks. ⚠ DIAGNOSTIC ONLY: it deliberately re-breaks the AC-3 path to test
# whether the 2026-07-31 near-sync anomaly reproduces (Stage 17). NOT for normal use.
EXTRACT_ARGS=("$FORMAT" s32 "$RATE")
if [ "${EARC_LEGACY_CHUNK:-0}" = "1" ]; then
    EXTRACT_ARGS=("$FORMAT" s32)
    echo "earc-bitstream-bridge: ⚠ EARC_LEGACY_CHUNK=1 — extractor on the OLD fixed 4096 B read (DIAGNOSTIC; expect clicks on ac3/dts)"
fi

# EARC_PWCAT_LATENCY overrides --latency. ⚠ DIAGNOSTIC ONLY below 1536. Stage 18 showed
# the legacy chunk ALONE no longer starves the path (ERR 0), because the real trigger was
# burst jitter, not average rate. The 2026-07-28 sweep gives a calibrated way to force the
# starved state instead of waiting for it:
#   1536 (32ms) ~0.07 ERR/s <- SHIPPED, exactly one AC-3/DD+ decode frame at 48k
#   1280 (27ms)   12 ERR/s  <- below one decode frame; ~= the 2026-07-31 anomaly's ~10/s
# Set it to 1280 to recreate that state on demand. Do not ship it.
PW_LATENCY="${EARC_PWCAT_LATENCY:-1536}"
if [ "$PW_LATENCY" != "1536" ]; then
    echo "earc-bitstream-bridge: ⚠ EARC_PWCAT_LATENCY=$PW_LATENCY (default 1536) — DIAGNOSTIC, expect xruns"
fi

# pcm_backlog_trim.py between decode and pw-cat: a deliberate ~64 ms reservoir plus
# an oldest-first backlog trim past ~192 ms. MEASURED 2026-08-01 (earc_inflight_probe
# + filmed lipsync): this path was BISTABLE on a start-up race. Fast graph pickup of
# the new node -> ~zero reserve -> synced but ~20 ERR/s of clicks (a 32 ms decode lump
# against a 32 ms deadline over a 3.6 ms pipe). Slow pickup -> everything captured
# meanwhile is CONSERVED (in-flight audio never sheds) -> ~400 ms late forever, ERR 0
# because the accidental reserve absorbs all jitter. The trimmer replaces both
# accidents with a small deterministic reservoir: clicks get 64 ms of slack, and any
# start-up/stall backlog is cut back to 64 ms. Same idea as the Rust ardftsrc bridge's
# stall trim, which is why the LPCM path never had the ~400 ms state.
arecord -D "$IN_DEVICE" -f S32_LE -r "$RATE" -c 2 -t raw \
        --buffer-time=40000 --period-time=5000 2>/dev/null \
  | python3 "$EXTRACT" "${EXTRACT_ARGS[@]}" \
  | decode \
  | python3 "$TRIM" \
  | python3 -c 'import fcntl, os, sys; fcntl.fcntl(0, fcntl.F_SETPIPE_SZ, 4096); os.execvp(sys.argv[1], sys.argv[1:])' \
        pw-cat --playback --raw --rate=48000 --channels=6 --format=f32 \
        --channel-map="FL,FR,RL,RR,FC,LFE" --latency="$PW_LATENCY" \
        --properties="node.name=source.tv.ardftsrc node.autoconnect=false media.class=Stream/Output/Audio resample.quality=12" -
