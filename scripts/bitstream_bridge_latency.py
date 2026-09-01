#!/usr/bin/env python3
"""Measure the real in-flight latency of a running earc-bitstream-bridge.

    sudo python3 bitstream_bridge_latency.py [--watch] [--interval 5]

The three bitstream paths (DD+, AC-3, DTS) are the only stages in the whole system
with no self-instrumentation — `ardftsrc-bridge` prints a `[diag]` line every 5 s,
these print nothing, so docs/latency-budget.md carries them as COMPUTED budgets
rather than measurements. This closes that gap.

READ-ONLY. It touches nothing the bridge owns: it reads /proc and issues FIONREAD
(a query, not a read) on duplicated handles to the pipes. Safe to run on the live
appliance while you are listening.

What it measures vs assumes:

  MEASURED   ALSA capture buffer occupancy   /proc/asound/<card>/pcm0c/sub0/status
  MEASURED   arecord -> extractor pipe fill  FIONREAD, exact bytes->ms (S32 2ch)
  APPROX     extractor -> decoder pipe fill  FIONREAD exactly; bytes->ms uses a nominal
                                             per-codec bitrate (compressed ES), so the
                                             ms is approximate but the magnitude is not
  MEASURED   decoder -> trim pipe fill       FIONREAD, exact bytes->ms (f32 6ch 48k)
  MEASURED   pcm-trim reservoir              its own `pcm-trim: fifo Xms` journal line —
                                             an in-process bytearray, so FIONREAD is
                                             blind to it and the pipes each side read 0
  MEASURED   trim -> pw-cat pipe fill        FIONREAD, exact bytes->ms (f32 6ch 48k)
  ASSUMED    decoder frame                   one frame: 1536 spl (DD+/AC-3), 512 (DTS)
  ASSUMED    pw-cat --latency                1536 frames @48k, from the unit's cmdline
  ASSUMED    PipeWire graph to ndi-tx-in     3 x 512/96k

Pipeline, as classified (the decoder is gst-launch-1.0 unless the DECODER drop-in
selects ffmpeg):  arecord -> tv_ac3_extract -> decoder -> pcm_backlog_trim -> pw-cat

The ASSUMED terms are structural constants, not guesses, but they are labelled so a
total can never be quoted as fully measured.
"""

import argparse
import array
import fcntl
import glob
import os
import re
import subprocess
import sys
import termios
import time

CODEC_UNITS = ("eac3", "ac3", "dts")
# IEC 61937 carrier rate the bridge opens the card at, per codec.
CARRIER_RATE = {"eac3": 192000, "ac3": 48000, "dts": 48000}
# ffmpeg emits one decode frame at a time; this is its size in 48 kHz samples.
DECODE_FRAME = {"eac3": 1536, "ac3": 1536, "dts": 512}
ES_RATE = 48000                     # the decoded elementary stream is always 48 kHz
OUT_BYTES_PER_SEC = 48000 * 6 * 4   # pw-cat input: f32, 6ch, 48k
GRAPH_QUANTA_MS = 3 * 512 / 96000 * 1000   # dsp-in -> CamillaDSP -> dsp-out -> sink

# ★ Nominal COMPRESSED bytes/sec, for turning the extractor->ffmpeg pipe fill into ms.
# APPROX by nature (bitrate varies, DD+ most of all), but the magnitude is the point:
# only the ffmpeg->pw-cat pipe was ever shrunk (to 4096 B). These two are still the
# 64 KiB default, and 64 KiB of AC-3 is ~0.8 SECONDS of audio. Any large reading here
# is a red flag whatever the exact bitrate.
ES_BYTES_PER_SEC = {
    "dts":  188625,   # MEASURED 2026-07-30: Pd 2012 B/frame x 93.75 fps = 1.509 Mbps
    "ac3":   80000,   # 640 kbps: 2560 B/frame x 31.25 fps
    "eac3":  96000,   # ~768 kbps typical streaming DD+ — varies most of the three
}


def sh(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True).stdout


def active_codec():
    for c in CODEC_UNITS:
        if sh("systemctl", "is-active", f"earc-bitstream-bridge@{c}.service").strip() == "active":
            return c
    return None


STAGES = {"arecord", "extract", "decode", "trim", "pwcat"}


def cgroup_pids(codec, fatal=False):
    """Every PID in the bridge unit's cgroup, in start order. [] once it is gone."""
    out = sh("systemctl", "show", f"earc-bitstream-bridge@{codec}.service",
             "-p", "ControlGroup").strip()
    cg = out.split("=", 1)[1] if "=" in out else ""
    path = f"/sys/fs/cgroup{cg}/cgroup.procs"
    try:
        with open(path) as f:
            return sorted(int(x) for x in f.read().split())
    except OSError as e:
        if fatal:
            sys.exit(f"cannot read the unit's cgroup ({path}): {e}\nRun with sudo.")
        return []


def cmdline(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        return ""


def classify(pids):
    """Map the pipeline stages to PIDs by inspecting their command lines.

    ★ The decoder is `gst-launch-1.0` and has been since 2026-08-01; the
    DECODER=gst|ffmpeg drop-in keeps ffmpeg selectable, so both are matched. And
    `pcm_backlog_trim.py` sits between the decoder and pw-cat. Matching only "ffmpeg" —
    as this did until 2026-09-01 — dropped the extractor->decoder pipe AND the whole
    reservoir from every total it printed, under one "missing: ffmpeg" warning.
    """
    stage = {}
    for pid in pids:
        c = cmdline(pid)
        if "arecord" in c:
            stage["arecord"] = pid
        elif "tv_ac3_extract" in c:
            stage["extract"] = pid
        elif "pcm_backlog_trim" in c:
            stage["trim"] = pid
        elif "gst-launch" in c or c.startswith("ffmpeg") or " ffmpeg " in c or "/ffmpeg" in c:
            stage["decode"] = pid
        elif "pw-cat" in c:
            stage["pwcat"] = pid
    return stage


def trim_reservoir_ms(codec):
    """The pcm_backlog_trim reservoir depth, in ms, from the trim's own accounting.

    ★ INVISIBLE TO FIONREAD — it is an in-process bytearray, the same trap as the
    extractor's BufferedReader below. The trim prints its own byte count every 10 s, so
    read that instead of the pipes around it. Bounded to the last 20 s so a line left by
    a previous instance can never be picked up as if it were current.
    """
    out = sh("journalctl", "-u", f"earc-bitstream-bridge@{codec}.service",
             "--since", "-20s", "--no-pager", "-o", "cat")
    seen = None
    for line in out.splitlines():
        g = re.search(r"pcm-trim: fifo ([\d.]+)ms", line)
        if g:
            seen = float(g.group(1))
    return seen


def pipe_fill(pid, fd):
    """Bytes sitting in the pipe that <pid> reads on <fd>. FIONREAD is a query —
    it does not consume anything. Returns None if the fd is not readable/not a pipe."""
    path = f"/proc/{pid}/fd/{fd}"
    try:
        if "pipe:" not in os.readlink(path):
            return None
        h = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        buf = array.array("i", [0])
        fcntl.ioctl(h, termios.FIONREAD, buf, True)
        return buf[0]
    except OSError:
        return None
    finally:
        os.close(h)


def alsa_delay(card_glob, sub):
    """(delay_frames, rate) from an ALSA substream's status/hw_params."""
    for base in glob.glob(card_glob):
        try:
            with open(f"{base}/{sub}/status") as f:
                st = f.read()
            if "state: RUNNING" not in st:
                continue
            m = re.search(r"^delay\s*:\s*(-?\d+)", st, re.M)
            with open(f"{base}/{sub}/hw_params") as f:
                hw = f.read()
            r = re.search(r"^rate:\s*(\d+)", hw, re.M)
            if m and r:
                return int(m.group(1)), int(r.group(1))
        except OSError:
            continue
    return None, None


def pwcat_latency_frames(pid):
    m = re.search(r"--latency=(\d+)", cmdline(pid))
    return int(m.group(1)) if m else None


def sample(codec, stage):
    rows = []          # (label, ms, tag)
    unknown = []

    d, rate = alsa_delay("/proc/asound/eARC", "pcm0c/sub0")
    if d is not None:
        rows.append(("eARC ALSA capture", d / rate * 1000, "MEASURED"))
    else:
        unknown.append("eARC ALSA capture (card not RUNNING)")

    cr = CARRIER_RATE[codec]
    if "extract" in stage:
        b = pipe_fill(stage["extract"], 0)
        if b is not None:
            rows.append(("arecord -> extractor pipe", b / (cr * 8) * 1000, "MEASURED"))
    # ★ This term is INVISIBLE to FIONREAD and must be added, not measured.
    # tv_ac3_extract reads CHUNK=4096 hi-words = 8192 B of S32 = 1024 frames per call.
    # Python's BufferedReader.read(n) drains the pipe continuously into its OWN buffer
    # while it waits for n bytes, so the pipe above reads 0 even though up to 1024
    # frames of audio are sitting in the process. Measured 0.00 ms on 11/11 live
    # samples for exactly this reason — do not read that as "no latency here".
    # 1024 frames = 5.33 ms at the 192 kHz DD+ carrier, but 21.33 ms at 48 kHz,
    # so AC-3 and DTS carry 4x more hidden delay here than DD+ does.
    rows.append((f"extractor read granularity (1024 fr @{cr//1000}k)",
                 1024 / cr * 1000, "ASSUMED"))
    if "decode" in stage:
        b = pipe_fill(stage["decode"], 0)
        if b is not None:
            rows.append((f"extractor -> decoder pipe ({b} B compressed)",
                         b / ES_BYTES_PER_SEC[codec] * 1000, "APPROX"))

    rows.append((f"decoder frame ({DECODE_FRAME[codec]} spl)",
                 DECODE_FRAME[codec] / ES_RATE * 1000, "ASSUMED"))

    if "trim" in stage:
        b = pipe_fill(stage["trim"], 0)
        if b is not None:
            rows.append(("decoder -> trim pipe", b / OUT_BYTES_PER_SEC * 1000, "MEASURED"))

    # ★ The single largest term on this path and the one the probe was blind to until
    # 2026-09-01: a deliberate reservoir, 25-57 ms across the codecs measured that night.
    res = trim_reservoir_ms(codec)
    if res is not None:
        rows.append(("pcm-trim reservoir (its own byte count)", res, "MEASURED"))
    else:
        unknown.append("pcm-trim reservoir (no `fifo` line in the last 20 s)")

    if "pwcat" in stage:
        b = pipe_fill(stage["pwcat"], 0)
        if b is not None:
            rows.append(("trim -> pw-cat pipe", b / OUT_BYTES_PER_SEC * 1000, "MEASURED"))

    lat = pwcat_latency_frames(stage["pwcat"]) if "pwcat" in stage else None
    if lat:
        rows.append((f"pw-cat --latency={lat}", lat / ES_RATE * 1000, "ASSUMED"))
    else:
        unknown.append("pw-cat --latency (not on the cmdline)")

    rows.append(("PipeWire graph -> ndi-tx-in", GRAPH_QUANTA_MS, "ASSUMED"))

    # ⌀ The NDITX loopback row was dropped 2026-08-21 with the loopback itself. The
    # transmitter now reads PipeWire directly and reports its own backlog every 5 s as
    # `[tx window] availp10/50/90=…` in `journalctl -u ndi-output` — measured 0 frames
    # in steady state, which is why there is no row here to add it back.

    return rows, unknown


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", action="store_true", help="sample repeatedly")
    ap.add_argument("--interval", type=float, default=5.0)
    args = ap.parse_args()

    codec = active_codec()
    if not codec:
        sys.exit("no earc-bitstream-bridge@{eac3,ac3,dts} is active — "
                 "start playback of a bitstream source first.")
    stage = classify(cgroup_pids(codec, fatal=True))

    print(f"bridge   : earc-bitstream-bridge@{codec}  "
          f"(carrier {CARRIER_RATE[codec]} Hz, decode frame {DECODE_FRAME[codec]} spl)")
    print(f"stages   : " + "  ".join(f"{k}={v}" for k, v in stage.items()))
    if STAGES - set(stage):
        print(f"⚠ missing : {', '.join(sorted(STAGES - set(stage)))} — totals will be incomplete")

    while True:
        # ★ Re-read the stage PIDs EVERY sample. The bridge restarts whenever the source
        # restarts — rewinding a clip is enough — and stale PIDs do NOT raise: pipe_fill
        # returns None and `pw-cat --latency` vanishes off the dead cmdline, so its 32 ms
        # silently leaves the total while every line still looks plausible. That cost 13
        # of 14 DTS samples on 2026-09-01; the `assumed` figure moving was the only tell.
        live = active_codec()
        if live is None:
            print("\n⚠ no bitstream bridge active — waiting")
            if not args.watch:
                break
            time.sleep(args.interval)
            continue
        fresh = classify(cgroup_pids(live))
        if (live, fresh) != (codec, stage):
            codec, stage = live, fresh
            print(f"\n★ stages re-read : earc-bitstream-bridge@{codec}  "
                  + "  ".join(f"{k}={v}" for k, v in stage.items()))
            if STAGES - set(stage):
                print(f"⚠ missing : {', '.join(sorted(STAGES - set(stage)))} "
                      f"— totals will be incomplete")

        rows, unknown = sample(codec, stage)
        print()
        measured = assumed = 0.0
        for label, ms, tag in rows:
            print(f"  {label:<44s} {ms:7.2f} ms  {tag}")
            if tag in ("MEASURED", "APPROX"):
                measured += ms
            else:
                assumed += ms
        print(f"  {'-'*44} {'-'*7}")
        print(f"  {'measured':<44s} {measured:7.2f} ms")
        print(f"  {'assumed (structural constants)':<44s} {assumed:7.2f} ms")
        total_label = "TOTAL Pi-side (capture -> NDI out)"
        if unknown:
            total_label += "  [INCOMPLETE]"
        print(f"  {total_label:<44s} {measured+assumed:7.2f} ms")
        for u in unknown:
            print(f"  ⚠ not counted: {u}")
        if not args.watch:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
