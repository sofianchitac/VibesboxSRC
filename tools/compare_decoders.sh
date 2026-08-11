#!/bin/bash
# ⚠⚠ KNOWN BROKEN IN ITS MOST IMPORTANT PART — READ BEFORE TRUSTING ITS OUTPUT.
# The per-channel level block below greps for "Channel:|Peak level|RMS level", which
# matches NOTHING on some ffmpeg builds (astats formats those lines differently). When
# that happens the script prints "=> DIFFER" and then an EMPTY level table — so it looks
# like a verdict when it has actually measured nothing. NEVER read that bare "DIFFER" as
# evidence of DRC; the two arms differ for level reasons alone. Compute levels directly
# (or fix the grep for your ffmpeg) before concluding anything.
#
# Settle the one question Stage 12 left open: does GStreamer apply Dolby DRC that the
# ffmpeg arm suppresses with `-drc_scale 0`?
#
# `gst-inspect-1.0 avdec_ac3` exposes no drc_scale equivalent and libavcodec defaults to
# 1.0 (DRC ON), so the expectation is YES on content that carries dynrng metadata.
# Stage 12 measured "bit-identical" — but on a SYNTHETIC file with neutral dynrng, which
# cannot distinguish "DRC off" from "no DRC metadata to apply". This runs the same
# comparison on a REAL capture off the eARC wire.
#
# Usage:
#   1) Play real Dolby content on the TV, then capture the elementary stream:
#        sudo systemctl stop earc-bitstream-bridge@ac3        # free the card
#        arecord -D hw:eARC,0 -f S32_LE -r 48000 -c 2 -t raw -d 30 \
#          | python3 /opt/vibesbox-src/scripts/tv_ac3_extract.py ac3 s32 48000 > /tmp/cap.ac3
#      (for DD+ use -r 192000 and `eac3 s32 192000`)
#   2) ./compare_decoders.sh /tmp/cap.ac3 ac3
#
# Reads DRC off the DECODED AUDIO, which is the only thing that matters here: DRC
# compresses dynamic range, so it RAISES the RMS relative to the peak. A crest factor
# (peak/RMS) that is materially lower on the gst arm IS the DRC being applied.
set -e

CAP="${1:?usage: compare_decoders.sh <captured.es> <eac3|ac3|dts>}"
FORMAT="${2:?usage: compare_decoders.sh <captured.es> <eac3|ac3|dts>}"
case "$FORMAT" in
    eac3) PARSE=ac3parse; DEC=avdec_eac3 ;;
    ac3)  PARSE=ac3parse; DEC=avdec_ac3  ;;
    dts)  PARSE=dcaparse; DEC=avdec_dca  ;;
    *) echo "unknown format '$FORMAT'" >&2; exit 2 ;;
esac

OUT=$(mktemp -d)
trap 'rm -rf "$OUT"' EXIT

ffmpeg -hide_banner -nostats -loglevel warning -nostdin \
    -drc_scale 0 -f "$FORMAT" -i "$CAP" \
    -ac 6 -c:a pcm_f32le -f f32le "$OUT/ffmpeg.raw"

# ★ `! queue !` forces PUSH mode. With a bare filesrc, ac3parse runs its pull-mode loop
# and fails to negotiate ("streaming stopped, reason not-negotiated"). The bridge uses
# fdsrc, which is push mode already, so this only bites here.
# ★ layout=interleaved + channel-mask=0xc0f (5.1-SIDE) are what avdec_* natively emit and
# what matches ffmpeg. 0x3f (5.1-REAR) makes audioconvert swap the surrounds — verified.
gst-launch-1.0 -q filesrc location="$CAP" ! queue ! "$PARSE" ! "$DEC" ! audioconvert ! \
    "audio/x-raw,format=F32LE,layout=interleaved,channels=6,rate=48000,channel-mask=(bitmask)0xc0f" ! \
    filesink location="$OUT/gst.raw"

echo "bytes:  ffmpeg $(stat -c%s "$OUT/ffmpeg.raw")   gst $(stat -c%s "$OUT/gst.raw")"
if cmp -s "$OUT/ffmpeg.raw" "$OUT/gst.raw"; then
    echo "=> BIT-IDENTICAL. No DRC difference on this content."
    echo "   (Only meaningful if the content actually carries dynrng — check the levels below.)"
else
    echo "=> DIFFER. Levels below say whether it is DRC (gst crest factor lower) or"
    echo "   channel order (per-channel levels permuted)."
fi

# Per-channel peak/RMS. Channel 4 is LFE in the canonical FL FR FC LFE RL RR order that
# both arms must produce — if the two arms disagree about WHICH channel is quiet in the
# high band, that is a channel-order mismatch, not DRC.
for arm in ffmpeg gst; do
    echo "--- $arm ---"
    ffmpeg -hide_banner -nostats -loglevel error \
        -f f32le -ar 48000 -ac 6 -i "$OUT/$arm.raw" \
        -af "channelsplit=channel_layout=5.1,astats=metadata=1:reset=0" \
        -f null - 2>&1 | grep -E "Channel:|Peak level|RMS level" | head -24
done
echo
echo "Read it as: DRC raises RMS toward the peak, so a LOWER crest factor (peak - RMS)"
echo "on the gst arm across all channels = GStreamer applying Dolby DRC."
