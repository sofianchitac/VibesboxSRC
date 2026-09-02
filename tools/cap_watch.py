#!/usr/bin/env python3
"""Longitudinal watch on the ardftsrc capture-ring depth (`capp10/50/90`).

WHY THIS EXISTS. The capture histogram shipped 2026-09-03 and answered its question the same
day — capp10/50/90 = 64/64/192 f (1.5/1.5/4.4 ms) on @lyrion, against a GRANT of 4096 f. That
was ONE source over ~70 s, which is not enough to conclude with: @lyrion is an snd-aloop writer
that never misses, while @usb is isochronous, is granted 170/2720 rather than the 256/4096 it
asks for, and is the arm exposed to the rail's under-voltage events. This collects the same
statistic across every source, over days, so the conclusion rests on a corpus.

WHY NOT JUST READ THE JOURNAL. Measured on the box 2026-09-03: journald holds ONE boot and
about two days here, so a multi-day watch would silently lose its early history — exactly the
failure that hid the FIFO ratchet for eight weeks. This appends to a TSV that rotation cannot
reach.

⛔ READ-ONLY AND OFF THE AUDIO PATH. It parses log lines already being written and never
touches a device, a service or the graph. Safe to run at any time, including mid-listening.

  cap_watch.py --collect            append new samples (what the timer runs)
  cap_watch.py --report             summarise everything collected so far
  cap_watch.py --report --hours 24  summarise a trailing window

Under-voltage events are collected into the SAME file so the two are time-aligned: if a source
ever shows a deep capture ring, the first question is whether the rail dipped underneath it.
"""
import argparse, json, os, subprocess, sys, time
from datetime import datetime

TSV = os.environ.get("CAP_WATCH_TSV", "/opt/vibesbox-src/state/cap-watch.tsv")
SOURCES = ["usb", "lyrion", "airplay", "tidal", "tv"]
COLS = ["epoch", "iso", "source", "kind", "capp10", "capp50", "capp90", "ringp50", "readyp10"]
OVERLAP_S = 120  # re-read a little history each pass; dedupe handles the rest


def journal(unit=None, kernel=False, since=None):
    cmd = ["journalctl", "--no-pager", "-o", "json"]
    cmd += ["-k"] if kernel else ["-u", unit]
    cmd += ["--since", since] if since else ["--since", "-7d"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=180).stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return
    for line in out.splitlines():
        try:
            o = json.loads(line)
        except ValueError:
            continue
        ts = o.get("__REALTIME_TIMESTAMP")
        msg = o.get("MESSAGE")
        if ts and isinstance(msg, str):
            yield int(ts) / 1e6, msg


def field(msg, key):
    """Pull `key=<digits>` out of a diag line. None when absent."""
    i = msg.find(key + "=")
    if i < 0:
        return None
    n = ""
    for ch in msg[i + len(key) + 1:]:
        if ch.isdigit():
            n += ch
        else:
            break
    return int(n) if n else None


def triple(msg, key):
    """`key10/50/90=a/b/cf` -> (a, b, c)."""
    i = msg.find(key + "10/50/90=")
    if i < 0:
        return None
    tail = msg[i + len(key) + 9:]
    parts, cur = [], ""
    for ch in tail:
        if ch.isdigit():
            cur += ch
        elif ch == "/" and cur:
            parts.append(int(cur)); cur = ""
        else:
            if cur:
                parts.append(int(cur))
            break
    return tuple(parts[:3]) if len(parts) >= 3 else None


def collect(since):
    rows = []
    for src in SOURCES:
        for t, msg in journal(unit=f"ardftsrc-bridge@{src}", since=since):
            if "[diag window]" in msg:
                cap = triple(msg, "capp")
                if not cap:
                    continue  # pre-2026-09-03 binary, no capture histogram
                ring = triple(msg, "ringp")
                ready = triple(msg, "readyp")
                rows.append([t, src, "window", cap[0], cap[1], cap[2],
                             ring[1] if ring else "", ready[0] if ready else ""])
            elif "[diag race-reset]" in msg:
                c = field(msg, "cap=NOT-CLEARED")
                rows.append([t, src, "reset", c if c is not None else "", "", "", "", ""])
            elif "hw granted" in msg and "pipewire" not in msg:
                rows.append([t, src, "start", "", "", "", "", ""])
    for t, msg in journal(kernel=True, since=since):
        if "Undervoltage detected" in msg:
            rows.append([t, "-", "undervolt", "", "", "", "", ""])
    return rows


def load():
    if not os.path.exists(TSV):
        return [], set()
    rows, seen = [], set()
    with open(TSV) as f:
        for line in f:
            p = line.rstrip("\n").split("\t")
            if len(p) != len(COLS) or p[0] == "epoch":
                continue
            rows.append(p)
            seen.add((p[0], p[2], p[3]))
    return rows, seen


def do_collect():
    existing, seen = load()
    # Resume from the newest sample we already hold, minus an overlap.
    since = "-7d"
    if existing:
        newest = max(float(r[0]) for r in existing)
        since = f"@{int(newest - OVERLAP_S)}"
    new = 0
    os.makedirs(os.path.dirname(TSV), exist_ok=True)
    fresh = not os.path.exists(TSV)
    with open(TSV, "a") as f:
        if fresh:
            f.write("\t".join(COLS) + "\n")
        for r in sorted(collect(since)):
            t = f"{r[0]:.3f}"
            if (t, r[1], r[2]) in seen:
                continue
            seen.add((t, r[1], r[2]))
            iso = datetime.fromtimestamp(r[0]).strftime("%Y-%m-%d %H:%M:%S")
            f.write("\t".join([t, iso, r[1], r[2]] + [str(x) for x in r[3:]]) + "\n")
            new += 1
    print(f"cap_watch: +{new} samples ({len(seen)} total) -> {TSV}")


def pct(vals, q):
    if not vals:
        return 0
    v = sorted(vals)
    return v[min(len(v) - 1, int(len(v) * q))]


def do_report(hours):
    rows, _ = load()
    if not rows:
        print("cap_watch: nothing collected yet — is the timer running?")
        return
    cutoff = time.time() - hours * 3600 if hours else 0
    rows = [r for r in rows if float(r[0]) >= cutoff]
    span = (max(float(r[0]) for r in rows) - min(float(r[0]) for r in rows)) / 3600 if rows else 0
    uv = [r for r in rows if r[3] == "undervolt"]
    starts = [r for r in rows if r[3] == "start"]

    print(f"\ncap_watch — {len(rows)} samples over {span:.1f} h"
          f"   ({datetime.fromtimestamp(min(float(r[0]) for r in rows)):%Y-%m-%d %H:%M}"
          f" -> {datetime.fromtimestamp(max(float(r[0]) for r in rows)):%Y-%m-%d %H:%M})\n")
    print(f"{'source':<9}{'windows':>8}{'starts':>7}  "
          f"{'capp10 min/med/max':>22}  {'capp50 med':>11}  {'capp90 max':>11}   trough ms (med)")
    print("-" * 96)
    verdict_ok = True
    for src in SOURCES:
        w = [r for r in rows if r[2] == src and r[3] == "window" and r[4]]
        if not w:
            continue
        c10 = [int(r[4]) for r in w]
        c50 = [int(r[5]) for r in w]
        c90 = [int(r[6]) for r in w]
        st = len([r for r in starts if r[2] == src])
        # 44.1k is the worst case for ms-per-frame; report against it so the figure is an
        # upper bound rather than an optimistic one.
        med10 = pct(c10, 0.5)
        ms = med10 * 1000.0 / 44100.0
        flag = ""
        if max(c10) >= 512:          # >= 2 capture periods held at the TROUGH
            flag = "  <-- DEEP, investigate"
            verdict_ok = False
        print(f"{src:<9}{len(w):>8}{st:>7}  "
              f"{min(c10):>6}/{med10:>6}/{max(c10):>6}f  {pct(c50,0.5):>10}f  {max(c90):>10}f"
              f"   {ms:>6.1f} ms{flag}")

    resets = [r for r in rows if r[3] == "reset" and r[4]]
    if resets:
        rv = [int(r[4]) for r in resets]
        print(f"\nrace resets: n={len(rv)}  cap-left-behind min/med/max = "
              f"{min(rv)}/{pct(rv,0.5)}/{max(rv)}f  ({max(rv)*1000.0/44100.0:.1f} ms worst)")
    print(f"under-voltage events in window: {len(uv)}"
          + (f"   ({len(uv)/span:.1f}/h)" if span > 0 else ""))
    if starts and uv:
        uvt = sorted(float(r[0]) for r in uv)
        near = 0
        for s in starts:
            t = float(s[0])
            if any(abs(t - u) <= 10 for u in uvt):
                near += 1
        print(f"bridge starts within 10 s of one: {near}/{len(starts)}")

    covered = {r[2] for r in rows if r[3] == "window" and r[4]}
    missing = [s for s in SOURCES if s not in covered]
    print(f"\nsources covered: {', '.join(sorted(covered)) or 'none'}"
          + (f"   STILL MISSING: {', '.join(missing)}" if missing else "   (all)"))
    if verdict_ok and covered:
        print("verdict: every covered source keeps its capture trough under 2 periods —"
              " the reset's skip stays negligible.")
    print()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--collect", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--hours", type=float, default=0)
    a = ap.parse_args()
    if a.collect:
        do_collect()
    if a.report or not a.collect:
        do_report(a.hours)
