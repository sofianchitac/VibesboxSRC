#!/bin/sh
# Vibesbox: aac -> flc converter for LMS. Called from custom-convert.conf with
# the stream URL (Tidal CDN https:// for remote tracks, file:// for local ones).
#
# E-AC-3 (Tidal Atmos) is level-matched by MEASUREMENT: one ebur128 pass over
# the first MEASURE seconds of the exact decode + stereo fold-down that gets
# emitted, then a single static gain. Genuine AAC is decoded untouched. See
# custom-convert.conf for why.
#
# Remote streams download in the background; the measure pass starts once
# HEAD_BYTES have arrived (60 s of 768 kb/s E-AC-3 is ~5.8 MB) and the decode
# pass then follows the growing file, so playback starts well before the
# download ends. The true peak of the unmeasured remainder is unknown — that
# is what CEILING's margin is for.

TARGET=-14      # LUFS integrated the fold-down is brought to (streaming-service norm)
CEILING=-3      # dBTP the measured window may reach; the margin covers later peaks
MEASURE=60      # seconds measured
HEAD_BYTES=8000000
FFMPEG=/config/bin/ffmpeg

case $1 in
file://*)
	# local file: decode in place (percent-decode the path, no copy)
	in=$(printf '%s' "${1#file://}" | perl -pe 's/%([0-9A-Fa-f]{2})/chr(hex($1))/ge')
	;;
*)
	tmp=$(mktemp /tmp/vibesbox-atmos.XXXXXX) || exit 1
	trap 'rm -f "$tmp"' EXIT
	curl -fsSL --retry 2 --max-time 300 -o "$tmp" "$1" &
	dl=$!
	while [ "$(stat -c %s "$tmp")" -lt $HEAD_BYTES ] && kill -0 $dl 2>/dev/null; do
		sleep 0.2
	done
	in=$tmp
	;;
esac

if ! $FFMPEG -hide_banner -i "$in" 2>&1 | grep -q 'Audio: eac3'; then
	[ -n "$dl" ] && wait $dl
	$FFMPEG -loglevel quiet -i "$in" -ac 2 -f flac -compression_level 0 -
	exit
fi

fold='aformat=channel_layouts=stereo'

gain=$($FFMPEG -nostats -drc_scale 0 -t $MEASURE -i "$in" -af "$fold,ebur128=peak=true" -f null - 2>&1 \
	| awk -v t="$TARGET" -v c="$CEILING" '
		/^ +I:/    { i = $2 }
		/^ +Peak:/ { p = $2 }
		END {
			if (i == "" || i < -40) { print 0; exit }   # silence / no reading: leave alone
			g = t - i
			if (c - p < g) g = c - p
			printf "%.2f", g
		}')

if [ -n "$dl" ]; then
	# stream the file as it grows; tail exits once curl is done and drained
	tail -c +1 -f --pid=$dl "$tmp" | $FFMPEG -loglevel quiet -drc_scale 0 -i - -af "$fold,volume=${gain}dB" -f flac -compression_level 0 -
else
	$FFMPEG -loglevel quiet -drc_scale 0 -i "$in" -af "$fold,volume=${gain}dB" -f flac -compression_level 0 -
fi
