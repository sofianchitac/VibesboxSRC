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

  cap_watch.py --collect            append new samples from the journal
  cap_watch.py --report             summarise everything collected so far
  cap_watch.py --report --hours 24  summarise a trailing window
  cap_watch.py --daily              collect, then summarise the last 24 h

★ IT ANSWERED, AND ITS TIMERS ARE GONE. 2026-09-04, 76 h, 2018 windows, three sources —
@tv 1226 / @usb 700 / @lyrion 92 — `capp10` min == med == max == **64 f (1.5 ms)** on every
one of them, against a grant of 2720 f (usb) / 4096 f (lyrion, tv). Not one window went higher.
⇒ The capture ring never accumulates on any path, so the race reset's deliberate skip of it
costs about a millisecond. **Never write a capture-side clear.**
Coverage is complete BY KIND: the three capture kinds on this box — isochronous USB gadget,
I2S slave, snd-aloop — all read identically, so @airplay and @tidal add nothing.
★ Free second result: @tv's `readyp10` pins at 64 f across 1226 windows and 18 starts while
@usb/@lyrion sit at 1088 f near the 1024 f setpoint — the drift regulator being permanently
inert on @tv (eARC runs -27.39 ppm, opposite sign to USB's +11.53), now shown stable across
days and restarts rather than within one 27-minute instance.

⌀ `cap-watch.timer` and `cap-watch-report.timer` were removed on 2026-09-04 with the question
they were built for. THIS TOOL IS KEPT and stays runnable by hand — `--report` reads the
retained TSV, `--collect` resumes from wherever it left off. Re-add a timer only if something
reopens the question. Anomaly lines still carry a `<4>` prefix so journald would file them at
WARNING if this is ever run from a unit again.

Two statistics are watched, and they answer DIFFERENT questions. `capp10` is the capture
ring's trough — the compartment the race reset walks past — and a deep one re-opens B1.
`readyp10` is the resampler FIFO's trough, the quantity the drift regulator servos; a high one
means the REGULATOR has stopped holding and is not a capture question at all. ⛔ Do not read
either as an end-to-end latency figure.

Under-voltage events are collected into the SAME file so the two are time-aligned: if a source
ever shows a deep capture ring, the first question is whether the rail dipped underneath it.
"""
import argparse, json, os, subprocess, sys, time
from datetime import datetime

TSV = os.environ.get("CAP_WATCH_TSV", "/opt/vibesbox-src/state/cap-watch.tsv")
SOURCES = ["usb", "lyrion", "airplay", "tidal", "tv"]
COLS = ["epoch", "iso", "source", "kind", "capp10", "capp50", "capp90", "ringp50", "readyp10"]
OVERLAP_S = 120  # re-read a little history each pass; dedupe handles the rest

# ── Flag thresholds. Both are "the regulator has stopped holding", not "tune me". ──
# capp10 at or above TWO capture periods means the capture ring is carrying standing backlog
# rather than the sub-period slack measured on 2026-09-03 — that would re-open B1.
CAP_DEEP_F = 512
# readyp10 is the resampler FIFO's TROUGH in OUTPUT frames, the quantity the drift regulator
# servos. Its setpoint is one output period (1024 f) with a 128 f band, and the ratchet fault
# it exists to catch looked like 1088 -> 3904 f over 42 min. 2048 = twice setpoint: comfortably
# outside normal swing, comfortably inside the fault. ⛔ A trip means the REGULATOR is not
# holding; it is not a latency reading on its own.
READY_HIGH_F = 2048


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
    print(f"{'source':<9}{'windows':>7}{'starts':>7}  "
          f"{'capture capp10 min/med/max':>28}{'ms':>7}   "
          f"{'FIFO readyp10 min/med/max':>27}{'ms':>7}")
    print("-" * 100)
    cap_ok = ready_ok = True
    for src in SOURCES:
        w = [r for r in rows if r[2] == src and r[3] == "window" and r[4]]
        if not w:
            continue
        c10 = [int(r[4]) for r in w]
        st = len([r for r in starts if r[2] == src])
        # 44.1k is the worst case for ms-per-frame on the CAPTURE side; report against it so
        # the figure is an upper bound rather than an optimistic one.
        med10 = pct(c10, 0.5)
        cms = med10 * 1000.0 / 44100.0
        # readyp10 is at TARGET_RATE (96k) always — it counts output frames.
        rdy = [int(r[8]) for r in w if r[8]]
        rmed = pct(rdy, 0.5) if rdy else 0
        rms = rmed / 96.0
        flag = ""
        if max(c10) >= CAP_DEEP_F:
            flag += "  <-- CAPTURE DEEP"
            cap_ok = False
        if rdy and max(rdy) >= READY_HIGH_F:
            flag += "  <-- FIFO HIGH"
            ready_ok = False
        print(f"{src:<9}{len(w):>7}{st:>7}  "
              f"{min(c10):>8}/{med10:>8}/{max(c10):>8}f{cms:>7.1f}   "
              + (f"{min(rdy):>8}/{rmed:>8}/{max(rdy):>8}f{rms:>7.1f}" if rdy else f"{'-':>32}")
              + flag)

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

    # ── Verdict gate. ──
    # ⛔ DELIBERATELY HARD TO SATISFY. The whole reason this collector exists is that the
    # capture question was answered from ONE source over ~70 s, and this project's recurring
    # failure is a conclusion drawn from too little. So: no verdict until at least two sources
    # have been seen WITH ENOUGH WINDOWS EACH to be more than a warm-up transient. A blank
    # "not yet" is the correct output for a watch that has not run long enough.
    #
    # ⛔ The bar counts QUALIFYING sources; a thin one does not veto. That is a fix, not a
    # relaxation, and it was a real bug: the first version required EVERY seen source to clear
    # 180 windows, so `@lyrion` at 92 held the verdict at NOT YET CONCLUSIVE while `@usb` (700)
    # and `@tv` (1226) had both long since cleared it and agreed exactly. Under that rule one
    # brief appearance of any source would suppress the answer indefinitely — a watch that can
    # never conclude is worse than no watch. Thin sources are still LISTED, and still excluded
    # from the count, so the bar itself is unchanged.
    MIN_SOURCES, MIN_WINDOWS = 2, 180   # 180 windows = ~30 min of bridge uptime per source
    per_src = {}
    for src in SOURCES:
        n = len([r for r in rows if r[2] == src and r[3] == "window" and r[4]])
        if n:
            per_src[src] = n
    missing = [s for s in SOURCES if s not in per_src]
    qualify = sorted(s for s in per_src if per_src[s] >= MIN_WINDOWS)
    thin = [f"{s}({per_src[s]})" for s in sorted(per_src) if per_src[s] < MIN_WINDOWS]
    print(f"\nsources covered: {', '.join(f'{s}({n})' for s, n in sorted(per_src.items())) or 'none'}"
          + (f"   STILL MISSING: {', '.join(missing)}" if missing else "   (all)"))
    if thin:
        print(f"  thin, not weighed: {', '.join(thin)}   (need >={MIN_WINDOWS} windows)")

    if len(qualify) < MIN_SOURCES:
        print(f"verdict: NOT YET CONCLUSIVE — {len(qualify)} of {MIN_SOURCES} sources have"
              f" >={MIN_WINDOWS} windows."
              "\n         @usb matters most: isochronous, granted 170/2720, and the arm"
              " exposed to the rail.")
    elif cap_ok and ready_ok:
        print(f"verdict: {len(qualify)} sources qualify ({', '.join(qualify)}), all keeping the"
              " capture trough under two periods and the FIFO trough under twice setpoint."
              "\n         The race reset's skip of the capture ring stays negligible;"
              " no capture-side clear is warranted.")
    else:
        if not cap_ok:
            print("<4>verdict: A SOURCE IS HOLDING A DEEP CAPTURE TROUGH (flagged above)."
                  " Re-open B1 — check that source against the under-voltage column first.")
        if not ready_ok:
            print("<4>verdict: A SOURCE'S FIFO TROUGH IS ABOVE TWICE SETPOINT — the drift"
                  " regulator is not holding on it. That is the ratchet fault, not a capture"
                  " question; see the READY_BAND block in ardftsrc-bridge-rs/src/main.rs.")
    print()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--collect", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--daily", action="store_true",
                    help="collect, then summarise the last 24 h (the daily timer's mode)")
    ap.add_argument("--hours", type=float, default=0)
    a = ap.parse_args()
    if a.collect or a.daily:
        do_collect()
    if a.daily:
        do_report(24)
    elif a.report or not a.collect:
        do_report(a.hours)
