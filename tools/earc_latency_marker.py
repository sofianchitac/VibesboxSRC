#!/usr/bin/env python3
"""File-fed marker latency probe for the eARC bitstream (DD+/AC-3) path.

Answers the one open question in docs/latency-budget.md: where the ~265 ms
unaccounted latency on the multichannel path lives. It replaces the TV with a
synthetic IEC 61937 file paced at real time, so the whole
`extractor -> decoder -> pw-cat -> PipeWire graph` chain can be timed on ONE
clock with no camera, no NDI and no TV.

Three taps, all CLOCK_MONOTONIC in this process:

    t0   the pacer has written the last byte of the burst carrying the marker
    t1   the marker leaves the decoder's stdout (f32le 48k 6ch)
    t2   the marker appears at dsp-in:monitor (96k 6ch) -- the PipeWire sum bus

`t2 - t0` is the quantity that has to be ~265 ms larger than the budget.
`t1 - t0` prices the extractor + decoder (the ffmpeg probe read-ahead suspect).

    build   synthesise the marker file (5.1 click train -> E-AC-3 -> IEC 61937
            -> S32 left-justified, i.e. byte-identical to what hw:eARC,0 yields)
    run     pace it through the real bridge pipeline and report the three taps

The pacer writes 5 ms chunks, matching `arecord --period-time=5000` in
scripts/earc-bitstream-bridge.sh, so the write granularity is the real one.
It does NOT include the arecord/ALSA capture term (budgeted 4.5 ms against a
40 ms buffer); use --via-loopback to add it.

Needs the venv interpreter (numpy): /opt/vibesbox-src/venv/bin/python3
"""

import argparse
import json
import os
import re
import select
import shutil
import subprocess
import sys
import time

import numpy as np

print = __import__("functools").partial(print, flush=True)

PA_LE = b"\x72\xf8"
PB_LE = b"\x1f\x4e"

# Marker geometry. A 20 ms 1 kHz burst in all six channels against silence.
# E-AC-3's MDCT smears the onset by ~10 ms, but identically on every run, so it
# cancels in any comparison and is a bounded bias on the absolutes.
MARKER_HZ = 1000.0
MARKER_MS = 20.0
MARKER_AMP = 0.7
MARKER_PERIOD_S = 2.0
LEAD_IN_S = 1.5             # silence before the first marker (decoder warm-up)

DECODED_RATE = 48000
DECODED_CH = 6
GRAPH_RATE = 96000
DETECT_THRESHOLD = 0.15     # |sample| across channels; markers are 0.7, noise ~0
REFRACTORY_S = 0.5          # min spacing between accepted detections


# --------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------

def build(args):
    n_markers = args.markers
    total_s = LEAD_IN_S + n_markers * MARKER_PERIOD_S
    n = int(total_s * DECODED_RATE)
    pcm = np.zeros((n, DECODED_CH), dtype=np.float32)

    burst_n = int(MARKER_MS / 1000.0 * DECODED_RATE)
    t = np.arange(burst_n, dtype=np.float32) / DECODED_RATE
    # Raised-cosine envelope: a rectangular gate would spray broadband energy
    # the codec spends its whole bit budget on, blurring the very onset we time.
    env = 0.5 * (1.0 - np.cos(2 * np.pi * np.arange(burst_n) / burst_n))
    tone = (MARKER_AMP * env * np.sin(2 * np.pi * MARKER_HZ * t)).astype(np.float32)
    for k in range(n_markers):
        start = int((LEAD_IN_S + k * MARKER_PERIOD_S) * DECODED_RATE)
        pcm[start:start + burst_n, :] = tone[:, None]

    print(f"[build] {total_s:.1f}s, {n_markers} markers, 5.1 @ {DECODED_RATE}")

    # PCM -> compressed -> IEC 61937. E-AC-3 rides a 192 kHz carrier, AC-3 a 48 kHz
    # one; ffmpeg's spdif muxer emits s16le stereo at that carrier either way.
    rate_in = 192000 if args.codec == "eac3" else 48000
    spdif = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error",
         "-f", "f32le", "-ar", str(DECODED_RATE), "-ch_layout", "5.1", "-i", "-",
         "-c:a", args.codec, "-b:a", "640k", "-f", "spdif", "-"],
        input=pcm.tobytes(), stdout=subprocess.PIPE, check=True).stdout
    print(f"[build] spdif {len(spdif)} B "
          f"({len(spdif)/(rate_in*4):.2f}s @{rate_in} s16 2ch)")

    words = np.frombuffer(spdif, dtype="<u2")
    if args.width == "s32":
        # 24-bit left-justified in S32 = the eARC tap's wire format. The extractor's
        # S32HighWordReader is the exact inverse of this shift. The Pico/TOSLINK tap
        # is native s16, so that variant is written through unchanged.
        payload = (words.astype("<u4") << 16).astype("<u4").tobytes()
    else:
        payload = words.tobytes()
    with open(args.out, "wb") as f:
        f.write(payload)
    bpf = 8 if args.width == "s32" else 4
    print(f"[build] wrote {args.out} ({len(payload)} B, "
          f"{args.width.upper()}_LE 2ch @{rate_in})")

    # Burst table: byte offset in the S32 file at which each burst's PAYLOAD is
    # complete. That is the instant ALSA could first hand the whole burst on, so
    # it is the honest t0 anchor -- not the burst's start.
    bursts = _scan_bursts(words, args.width)
    print(f"[build] {len(bursts)} IEC 61937 bursts")

    # Decode offline through the real extractor + decoder to learn where each
    # marker lands in the decoded stream. Measured, never assumed: the burst ->
    # decoded-frame ratio and the codec's own delay both fall out of this.
    decoded = _decode_offline(args.out, args.extractor, args.codec, args.width)
    frames = len(decoded) // DECODED_CH
    dec = decoded.reshape(frames, DECODED_CH)
    hits = _detect(np.abs(dec).max(axis=1), DECODED_RATE)
    print(f"[build] decoded {frames} frames ({frames/DECODED_RATE:.2f}s), "
          f"{len(hits)} markers detected")
    if len(hits) != n_markers:
        print(f"[build] WARNING: expected {n_markers} markers, found {len(hits)}")

    # A burst carries a fixed number of decoded frames; derive it rather than
    # assuming 1536 (E-AC-3 bursts can carry more than one syncframe).
    frames_per_burst = frames / len(bursts)
    print(f"[build] {frames_per_burst:.1f} decoded frames per burst")

    markers = []
    for s in hits:
        j = min(int(s // frames_per_burst), len(bursts) - 1)
        markers.append({"decoded_sample": int(s),
                        "decoded_time": s / DECODED_RATE,
                        "burst_index": j,
                        "input_byte": bursts[j]})
    meta = {"rate_in": rate_in, "channels_in": 2, "bytes_per_frame_in": bpf,
            "codec": args.codec, "width": args.width,
            "decoded_rate": DECODED_RATE, "decoded_channels": DECODED_CH,
            "frames_per_burst": frames_per_burst,
            "n_bursts": len(bursts), "markers": markers}
    with open(args.out + ".json", "w") as f:
        json.dump(meta, f, indent=1)
    print(f"[build] wrote {args.out}.json")
    for m in markers[:3]:
        print(f"        marker @{m['decoded_time']:.3f}s -> burst {m['burst_index']} "
              f"-> input byte {m['input_byte']}")


def _scan_bursts(words, width):
    """Byte offsets in the OUTPUT file at which each burst's payload is complete."""
    raw = words.tobytes()
    out, i = [], 0
    while True:
        j = raw.find(PA_LE, i)
        if j < 0 or j + 8 > len(raw):
            break
        if raw[j + 2:j + 4] != PB_LE:
            i = j + 2
            continue
        pc = raw[j + 4] | (raw[j + 5] << 8)
        pd = raw[j + 6] | (raw[j + 7] << 8)
        # Pd is BYTES for E-AC-3 (0x15), BITS for AC-3/DTS -- see tv_ac3_extract.py
        payload = pd if (pc & 0x1F) in (0x15, 0x16) else pd // 8
        end_word_byte = j + 8 + payload
        # s16 word-stream offset -> offset in the file actually written
        out.append(end_word_byte * 2 if width == "s32" else end_word_byte)
        i = end_word_byte
    return out


def _decode_offline(path, extractor, codec, width):
    p1 = subprocess.Popen([sys.executable, extractor, codec, width],
                          stdin=open(path, "rb"), stdout=subprocess.PIPE)
    p2 = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error",
                           "-f", codec, "-i", "-",
                           "-ac", "6", "-c:a", "pcm_f32le", "-f", "f32le", "-"],
                          stdin=p1.stdout, stdout=subprocess.PIPE)
    p1.stdout.close()
    data = p2.stdout.read()
    p2.wait()
    p1.wait()
    return np.frombuffer(data, dtype="<f4")


def _detect(mag, rate, threshold=DETECT_THRESHOLD):
    """Rising-edge sample indices, one per marker."""
    above = mag > threshold
    edges = np.flatnonzero(above[1:] & ~above[:-1]) + 1
    out, last = [], -1e9
    for e in edges:
        if e - last > REFRACTORY_S * rate:
            out.append(int(e))
            last = e
    return out


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------

# Each profile reproduces one deployed bridge's downstream verbatim, so the two
# can be compared as measured numbers rather than as parameter lists. Read off
# scripts/earc-bitstream-bridge.sh and scripts/tv-bitstream-bridge.sh.
#
# ⚠ The optical bridge NEVER received the 2026-07-28 tuning: it is still on
# 80ms/20ms arecord, ffmpeg swr to 96k, the default 64 KiB stdin pipe and a
# pw-cat with NO --latency (i.e. the 100 ms default). That is why a filmed
# eARC-vs-optical A/B needs this delta subtracted.
PROFILES = {
    "earc": dict(chunk_ms=5.0, out_rate=48000, pipe_bytes=4096,
                 ff_extra=["-drc_scale", "0"], af=None,
                 pw=["--rate=48000", "--latency=1536"],
                 props="resample.quality=12"),
    "optical": dict(chunk_ms=20.0, out_rate=96000, pipe_bytes=65536,
                    ff_extra=[], af="aresample=out_sample_rate=96000",
                    pw=["--rate=96000"],          # no --latency => pw-cat default 100 ms
                    props=""),
}

class StreamDetector:
    """Rising-edge detector over a byte stream of interleaved f32 frames.

    Timestamps are attributed per SAMPLE, not per read: a chunk read at time T
    ends at T, so sample i of an n-sample chunk landed at T-(n-1-i)/rate. The
    pacer uses the same convention on the way in, so the two cancel and the
    difference is not biased by chunk size.
    """

    def __init__(self, rate, channels, label):
        self.rate, self.channels, self.label = rate, channels, label
        self.tail = b""
        self.above = False
        self.last_hit = -1e9
        self.hits = []
        self.total_frames = 0

    def feed(self, data, t_end):
        buf = self.tail + data
        nframes = len(buf) // (4 * self.channels)
        usable = nframes * 4 * self.channels
        self.tail = buf[usable:]
        if not nframes:
            return
        mag = np.abs(np.frombuffer(buf[:usable], dtype="<f4")
                     .reshape(nframes, self.channels)).max(axis=1)
        for i in np.flatnonzero(mag > DETECT_THRESHOLD):
            t = t_end - (nframes - 1 - int(i)) / self.rate
            if not self.above and t - self.last_hit > REFRACTORY_S:
                self.hits.append(t)
                self.last_hit = t
            self.above = True
            break
        else:
            self.above = bool(mag[-1] > DETECT_THRESHOLD)
        self.total_frames += nframes


def run(args):
    meta = json.load(open(args.file + ".json"))
    prof = PROFILES[args.profile]
    markers = meta["markers"]
    bytes_per_frame = meta["bytes_per_frame_in"]
    rate_in = meta["rate_in"]
    codec, width = meta.get("codec", "eac3"), meta.get("width", "s32")
    chunk_ms = args.chunk_ms if args.chunk_ms else prof["chunk_ms"]
    chunk_frames = int(chunk_ms / 1000.0 * rate_in)
    chunk_bytes = chunk_frames * bytes_per_frame

    data = open(args.file, "rb").read()
    print(f"[run] {args.file}: {len(data)} B, {len(data)/(rate_in*bytes_per_frame):.1f}s, "
          f"{len(markers)} markers, {codec}/{width}, profile={args.profile}, "
          f"{chunk_ms} ms chunks")

    cap = None if args.no_sink else _start_capture(args)
    dec_cmd = _decoder_cmd(args, prof, codec)
    print(f"[run] decoder: {' '.join(dec_cmd)}")

    extract = subprocess.Popen([args.python, args.extractor, codec, width],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    decoder = subprocess.Popen(dec_cmd, stdin=extract.stdout, stdout=subprocess.PIPE)
    extract.stdout.close()
    # --no-sink: decoder output is read and discarded, so nothing reaches the
    # graph and nothing is audible. t1 only; pw-cat's back-pressure is absent.
    sink = None if args.no_sink else _start_pw_cat(args, prof)

    det1 = StreamDetector(prof["out_rate"], DECODED_CH, "t1")
    det2 = StreamDetector(GRAPH_RATE, DECODED_CH, "t2") if cap else None

    t0_hits = []
    next_marker = 0
    off = 0
    max_lag = 0.0
    start = time.monotonic()
    fd_dec = decoder.stdout.fileno()
    fd_cap = cap.stdout.fileno() if cap else None
    os.set_blocking(fd_dec, False)
    if fd_cap is not None:
        os.set_blocking(fd_cap, False)

    def pump(timeout):
        fds = [fd_dec] + ([fd_cap] if fd_cap is not None else [])
        r, _, _ = select.select(fds, [], [], timeout)
        now = time.monotonic()
        for fd in r:
            chunk = os.read(fd, 65536)
            if not chunk:
                continue
            if fd == fd_dec:
                det1.feed(chunk, now)
                if sink:
                    sink.stdin.write(chunk)
                    sink.stdin.flush()
            else:
                det2.feed(chunk, now)

    while off < len(data):
        end = min(off + chunk_bytes, len(data))
        deadline = start + end / (rate_in * bytes_per_frame)
        while True:
            slack = deadline - time.monotonic()
            if slack <= 0:
                break
            pump(min(slack, 0.002))
        extract.stdin.write(data[off:end])
        extract.stdin.flush()
        now = time.monotonic()
        # Pacer honesty check. pump() writes to pw-cat with a BLOCKING write, so a
        # full downstream pipe could stall the pacer and record t0 late, silently
        # understating every latency. Late writes show as drift across markers.
        max_lag = max(max_lag, now - deadline)
        # A marker's t0 is the instant its burst's last payload byte went in.
        while next_marker < len(markers) and markers[next_marker]["input_byte"] <= end:
            t0_hits.append(now)
            next_marker += 1
        off = end

    extract.stdin.close()
    # Drain: markers still in flight need long enough to clear the whole chain.
    drain_until = time.monotonic() + args.drain_s
    while time.monotonic() < drain_until:
        pump(0.05)

    for p in (sink, decoder, extract):
        if p is None:
            continue
        try:
            p.terminate()
        except Exception:
            pass
    if cap:
        cap.terminate()

    print(f"[run] max pacer lag vs schedule: {max_lag*1000:.1f} ms")
    _report(t0_hits, det1.hits, det2.hits if det2 else [], args)


def _decoder_cmd(args, prof, codec):
    if args.decoder == "ffmpeg":
        probe = (["-analyzeduration", "0", "-probesize", "32"] if args.fast_probe
                 else ["-analyzeduration", "100000", "-probesize", "32768"])
        af = ["-af", prof["af"]] if prof["af"] else []
        return (["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "warning", "-nostdin",
                 "-fflags", "nobuffer", "-flags", "low_delay"] + probe + prof["ff_extra"] +
                ["-f", codec, "-i", "-"] + af +
                ["-ac", "6", "-c:a", "pcm_f32le", "-flush_packets", "1", "-f", "f32le", "-"])
    # GStreamer, as a DROP-IN for ffmpeg only: same stdin, same f32le stdout, so
    # everything downstream (the t1 tap, the pipe size, pw-cat) is byte-identical
    # and the decoder is the single variable.
    #   blocksize    one 5 ms read, matching the extractor's granularity
    #   sync=false   fdsink must not re-clock; the pacer already sets the rate
    #   layout=interleaved  avdec emits NON-interleaved F32LE; audioconvert fixes it
    # ⚠ avdec_{ac3,eac3} expose only max-errors and min-latency -- there is NO DRC
    # property, so `-drc_scale 0` has no equivalent here. Fine for timing, NOT for
    # shipping: the bridge disables Dolby DRC deliberately.
    dec = "avdec_eac3" if codec == "eac3" else "avdec_ac3"
    blocksize = int(48000 * 6 * 4 * 0.005)
    return ["gst-launch-1.0", "-q",
            "fdsrc", "fd=0", f"blocksize={blocksize}", "do-timestamp=true", "!",
            "ac3parse", "!", dec, "!",
            "audioconvert", "!",
            f"audio/x-raw,format=F32LE,layout=interleaved,channels=6,"
            f"rate={prof['out_rate']}", "!",
            "fdsink", "fd=1", "sync=false"]


def _start_pw_cat(args, prof):
    cmd = (["pw-cat", "--playback", "--raw", "--channels=6", "--format=f32",
            "--channel-map=FL,FR,RL,RR,FC,LFE"] + prof["pw"] +
           [f"--properties=node.name={args.node} node.autoconnect=false "
            f"media.class=Stream/Output/Audio {prof['props']}".rstrip(), "-"])
    env = dict(os.environ, XDG_RUNTIME_DIR="/run/pipewire")
    # The stdin pipe size is a TIME budget and differs per bridge (4096 B on the
    # tuned eARC path, the 64 KiB default on the optical one) -- reproduce it, or
    # the comparison silently measures the wrong thing.
    p = subprocess.Popen(
        ["python3", "-c",
         "import fcntl,os,sys; fcntl.fcntl(0, fcntl.F_SETPIPE_SZ, %d); "
         "os.execvp(sys.argv[1], sys.argv[1:])" % prof["pipe_bytes"]] + cmd,
        stdin=subprocess.PIPE, env=env)
    time.sleep(1.5)
    _link(f"{args.node}:output_FL", "dsp-in:input_1", env)
    _link(f"{args.node}:output_FR", "dsp-in:input_2", env)
    _link(f"{args.node}:output_RL", "dsp-in:input_5", env)
    _link(f"{args.node}:output_RR", "dsp-in:input_6", env)
    _link(f"{args.node}:output_FC", "dsp-in:input_3", env)
    _link(f"{args.node}:output_LFE", "dsp-in:input_4", env)
    return p


def _start_capture(args):
    env = dict(os.environ, XDG_RUNTIME_DIR="/run/pipewire")
    cmd = ["pw-record", "--raw", "--rate=96000", "--channels=6", "--format=f32",
           f"--latency={args.cap_latency}",
           "--properties=node.name=vbmarkercap node.autoconnect=false", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, env=env)
    time.sleep(1.5)
    ports = subprocess.run(["pw-link", "-i", "-l"], env=env,
                           capture_output=True, text=True).stdout
    ins = sorted(re.findall(r"^(vbmarkercap:\S+)", ports, re.M))
    if len(ins) != 6:
        print(f"[run] FATAL: capture node has {len(ins)} input ports, expected 6: {ins}")
        sys.exit(1)
    for n, port in enumerate(ins, start=1):
        _link(f"dsp-in:monitor_{n}", port, env)
    return p


def _link(src, dst, env):
    # pw-link creates the link with --linger by DEFAULT, so the link outlives the
    # process. Some invocations then sit in ppoll instead of exiting (observed on
    # PipeWire 1.4.2), so give it a deadline and move on -- the link is already up.
    p = subprocess.Popen(["pw-link", src, dst], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        _, err = p.communicate(timeout=2)
        if p.returncode:
            print(f"[run] link {src} -> {dst} FAILED: {err.strip()}", flush=True)
    except subprocess.TimeoutExpired:
        p.kill()


def _report(t0, t1, t2, args):
    print()
    print(f"[result] markers in: {len(t0)}  t1(decoder out): {len(t1)}  "
          f"t2(dsp-in monitor): {len(t2)}")

    def stat(name, a, b):
        n = min(len(a), len(b))
        if n < 2:
            print(f"  {name}: too few pairs ({n})")
            return
        d = np.array([(b[i] - a[i]) * 1000.0 for i in range(n)])
        # Drop the first: it carries decoder/graph start-up, not steady state.
        d = d[1:]
        print(f"  {name}: median {np.median(d):8.1f} ms   mean {d.mean():8.1f}   "
              f"sd {d.std():5.1f}   n={len(d)}   [{d.min():.0f}..{d.max():.0f}]")
        print(f"           series: {' '.join(f'{x:.0f}' for x in d)}")

    stat("t1 - t0  extractor+decoder ", t0, t1)
    if t2:
        stat("t2 - t0  ==> capture->sum bus", t0, t2)
        stat("t2 - t1  pw-cat+PipeWire     ", t1, t2)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build")
    b.add_argument("--out", default="/tmp/eac3_marker.s32")
    b.add_argument("--markers", type=int, default=14)
    b.add_argument("--codec", choices=["eac3", "ac3"], default="eac3")
    b.add_argument("--width", choices=["s16", "s32"], default="s32",
                   help="s32 = eARC tap (24-bit left-justified); s16 = Pico/TOSLINK")
    b.add_argument("--extractor", default="/opt/vibesbox-src/scripts/tv_ac3_extract.py")

    r = sub.add_parser("run")
    r.add_argument("--file", default="/tmp/eac3_marker.s32")
    r.add_argument("--decoder", choices=["ffmpeg", "gst"], default="ffmpeg")
    r.add_argument("--fast-probe", action="store_true",
                   help="-analyzeduration 0 -probesize 32 (ffmpeg only)")
    r.add_argument("--profile", choices=list(PROFILES), default="earc",
                   help="which deployed bridge's downstream to reproduce")
    r.add_argument("--cap-latency", type=int, default=512, help="pw-record --latency @96k")
    r.add_argument("--chunk-ms", type=float, default=0,
                   help="pacer write granularity; 0 = the profile's arecord period")
    r.add_argument("--node", default="source.marker.test")
    r.add_argument("--no-sink", action="store_true",
                   help="no pw-cat, no capture: t1 only, and completely silent")
    r.add_argument("--drain-s", type=float, default=3.0)
    r.add_argument("--python", default="/usr/bin/python3")
    r.add_argument("--extractor", default="/opt/vibesbox-src/scripts/tv_ac3_extract.py")

    args = ap.parse_args()
    if args.cmd == "build":
        if not shutil.which("ffmpeg"):
            sys.exit("ffmpeg not found")
        build(args)
    else:
        run(args)


if __name__ == "__main__":
    main()
