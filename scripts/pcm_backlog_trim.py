#!/usr/bin/env python3
"""Reservoir + backlog trimmer for the earc-bitstream-bridge decoded-PCM leg.

stdin -> stdout for 48k 6ch f32 PCM, holding a deliberate ~64 ms standing FIFO
and dropping oldest content past ~192 ms.

Why (measured 2026-08-01, ../tools/earc_inflight_probe.py + filmed lipsync): the
bitstream path is BISTABLE, set by a start-up race. If the graph consumes the
new source.tv.ardftsrc node quickly, the chain runs with ~zero reserve —
synced, but one 32 ms decode lump against a 32 ms deadline over a 3.6 ms pipe
starves pw-cat at up to ~20 ERR/s (audible clicks). If consumption starts
late, everything captured meanwhile is CONSERVED (in-flight audio never
sheds — project_latency_reduction_2026-06) and the path runs ~400 ms late
forever, with ERR 0 because the accidental reserve absorbs all jitter. Both
states fail: acceptance is ERR 0 AND synced. The fix is a reservoir that is
deliberate, small and deterministic:

  PRIME  64 ms  (pw-cat's 32 ms target + 1 decode frame) held back before
                forwarding starts, and
                restored after any trim; absorbs the burst jitter that
                causes the clicks. This is the path's one deliberate
                latency add — bounded, documented, tunable.
  THRESH 192 ms anything past this is stall recovery, never legit jitter
                (legit transient is PRIME + ~2 frames = 128 ms); drop
                oldest down to PRIME. Trims are whole frames, preserving
                channel phase.
  SETTLE  5 s   from the moment real output starts, hold the FIFO AT PRIME
                instead of merely under THRESH. See below.

★ 2026-08-01 (night): THRESH alone left the path BIMODAL. Filmed proof —
three bridge starts in one window settled at two steady states, fifo
21-46 ms (fast pickup) vs 82-125 ms (slow pickup, after THRESH had already
cut 540 ms). THRESH is a CEILING, not a regulator: producer and consumer
both run at 48 k, so once the depth lands anywhere below 192 ms it is a free
integrator and stays there forever. That standing ~75 ms is pure latency —
everything downstream of here is identical between the two branches — and it
is why the filmed number was 135 ms on the slow branch.

The start-up transient has TWO parts and the fix has to cover both:
  1. before the consumer drains at all, everything arriving is race backlog;
  2. AFTER it starts, upstream keeps delivering above realtime for seconds
     while its own conserved backlog drains — the real run climbed back to
     188 ms here, having already been cut to 64.
Capping only until the first successful writes fixes (1) and leaves (2),
which is where most of the depth actually came from. So the cap runs until
SETTLE seconds after real output begins, which bounds the latency from the
first sample rather than correcting it afterwards.

⚠ Discarding is the ONLY way to shed conserved audio — declining to read
just moves the backlog upstream, where it is still latency. So the trade is
real: a few small splices in the first seconds after a format change (when
the stream is already discontinuous) against a permanent ~75 ms of lipsync
error. Set SETTLE_S = 0 to disable and get the old ceiling-only behaviour.

★ 2026-08-28: SETTLE fixed the BIMODALITY but left the LANDING a lottery. `settled`
is latched by a 5 s TIMER, which samples the FIFO at whatever phase the timer
fires; below THRESH nothing regulates, so whatever it lands on free-integrates
there forever. Over 40 live instances the landing is QUANTIZED on a 512-frame
(10.67 ms = 2 graph quanta) lattice — 14.2 / 35.6 / 46.2 / 56.9 / 67.6 / 78.2 —
spanning 67.6 ms, and the steady trough follows it monotonically (0.0 / 10.7 /
32.0 / 49.8 / 60.5). That is ~45 ms of lipsync drawn fresh at every bridge start:
two filmed AC-3 takes 70 s apart measured -60 and -105 ms purely by landing on
78.2 vs 14.2.

The regulated quantity is now the FIFO's TROUGH (p10 over a window), not its
instantaneous depth. The trough is the STANDING backlog — the part no consumer
ever takes — so it is 1:1 the latency and shedding it can never starve pw-cat.
Peaks are in-flight lumps and must NOT be trimmed: gate on p10, never on max
(the NDI transmitter's regulators learned the same lesson).

⚠ TROUGH and FILL are different quantities, one in-flight lump apart, and
conflating them is a live bug not a nicety. The 2026-08-25 constraint above is on
PRIME, a FILL level; the safe TROUGH floor is correspondingly lower, and the field
proves it — landing 46.2 ms leaves a 32.0 ms trough and ran 23 instances with no
dry-outs. Only trough 10.7 ms actually misbehaved (2 dry-outs/instance).

⚠ This trims DOWNWARD only, by design. Raising a standing level means withholding
output, i.e. a gap, and an unguarded re-prime starves pw-cat into drinking the
refill greedily — one re-prime begets the next, which is the 2026-08-25
dry/re-prime oscillation. Trim-only reaches 38 of the 40 live landings; the 2
that land BELOW setpoint keep the old behaviour rather than buy 5 % with a
mechanism that can oscillate.

⚠ The PRIME that "disappears" on the fast branch (steady floor ~7 ms) has
moved DOWNSTREAM into pw-cat's own buffers, not vanished — that seeding is
what the prime is for. Do not read a low floor as erosion.

If the FIFO ever runs completely dry (clock drift eroding the reservoir, or
an upstream stall), forwarding pauses and re-primes — one bounded gap instead
of a permanent starve. FIFO depth min/max is logged every 10 s to make
erosion visible in the journal. EOF on stdin: flush and exit 0, preserving
the bridge's clean-EOF teardown chain.
"""
import fcntl
import os
import select
import sys
import time

FRAME = 6 * 4                       # 6ch f32
RATE_BPS = 48000 * FRAME
# Sized from the TWO constraints that bound it, not a ms literal:
#   LOWER bound — pw-cat downstream runs --latency=1536 (32 ms) of ITS OWN buffering,
#   and a reservoir no bigger than that target gets drunk whole every cycle:
#   measured live 2026-08-25 with PRIME = 1 decode frame — a sustained ~62
#   dry/re-prime cycles PER SECOND (each a forwarding gap), vs ZERO dry-outs across
#   all previous 64 ms runs. The standing reserve must exceed pw-cat's target.
#   UPPER contribution — one decode frame beyond it, because decode output arrives
#   in atomic 32 ms lumps: one spare lump covers one late lump (the ERR cliff in
#   earc-bitstream-bridge.sh's sweep sits BELOW one frame, not at it).
DECODE_FRAME_SAMPLES = 1536         # AC-3/E-AC-3/DTS-core decode frame @48k
PW_CAT_LATENCY_FRAMES = 1536        # MUST match EARC_PWCAT_LATENCY in earc-bitstream-bridge.sh
PRIME = (PW_CAT_LATENCY_FRAMES + DECODE_FRAME_SAMPLES) * FRAME
THRESH = int(0.192 * RATE_BPS) // FRAME * FRAME
STAT_S = 10.0

# "The consumer is draining" has to be proven, not assumed: bytes we merely handed to the
# kernel are still sitting in the pipe, so `written` only means something once it exceeds
# what the pipe holds by itself. Ask the kernel rather than hardcoding — and ask LATE, per
# call: the bridge shrinks this very pipe to 4096 B from the pw-cat end (F_SETPIPE_SZ on
# its fd 0), and that happens whenever that process starts, which races our import.
def pipe_capacity(fd, default=4096):
    try:
        return fcntl.fcntl(fd, fcntl.F_GETPIPE_SZ)
    except (AttributeError, OSError):
        return default          # no F_GETPIPE_SZ (non-Linux): assume the bridge's setting
SETTLE_S = 5.0                      # 0 disables the regulator (ceiling-only, pre-2026-08-01)
LUMP = DECODE_FRAME_SAMPLES * FRAME     # in-flight granularity; also the trim unit
# The graph is pinned to quantum 512 @ 96k, so this 48k node exchanges 256 frames per
# 5.333 ms cycle. TWO of those is 512 frames = 10.67 ms — exactly the lattice the live
# landings fall on, so it is the natural convergence band: tighter chatters on
# single-quantum jitter, wider re-admits the spread this exists to remove.
GRAPH_QUANTUM = 256
BAND = 2 * GRAPH_QUANTUM * FRAME
# TROUGH setpoint (not a fill level — see the docstring). pw-cat's own buffering target
# is both the derived floor and, independently, the field's most common operating point:
# 23 of 40 live instances sat here with a clean dry-out record.
TARGET = PW_CAT_LATENCY_FRAMES * FRAME
WINDOW_S = 0.5                          # trough measurement window
CONVERGE_WINDOWS = 2                    # consecutive in-band windows before latching
# Bound on convergence. Past this, latch wherever we are rather than keep splicing.
STARTUP_MAX_S = 15.0
# Trimming to exactly PRIME fires on every few-ms overshoot: offline that was 98 splices in
# the first second, i.e. a burst of crackle where there had been one skip. One decode frame
# of slack cuts that to a handful and still bounds the settle-window depth at PRIME+32ms,
# well inside the ~75 ms the regulator exists to remove.
HYST = int(0.032 * RATE_BPS) // FRAME * FRAME


def ms(nbytes):
    return nbytes / RATE_BPS * 1000


def p10(samples):
    """Nearest-rank 10th percentile — the window's TROUGH.

    Not min(): one transient empty loop iteration would set min for a whole window and
    provoke a trim the standing level never justified. Not mean or max either — those
    include the in-flight lumps, and trimming those is exactly what starves pw-cat."""
    q = sorted(samples)
    return q[max(0, (len(q) * 10 + 99) // 100 - 1)]


def main():
    fifo = bytearray()
    eof = False
    priming = True
    flowing = None                  # monotonic time the consumer was proven to drain
    settled = False
    written = 0
    startup_dropped = 0
    depths = []                     # FIFO depth samples for the current window
    trough = 0                      # last measured p10
    in_band = 0                     # consecutive windows with the trough at setpoint
    lo = hi = 0                     # re-seeded once the consumer is proven draining
    t_win = t_stat = time.monotonic()
    os.set_blocking(0, False)
    os.set_blocking(1, False)
    while not (eof and not fifo):
        forwarding = fifo and (not priming or eof)
        rl = [] if eof else [0]
        wl = [1] if forwarding else []
        r, w, _ = select.select(rl, wl, [], 1.0)
        if r:
            try:
                chunk = os.read(0, 65536)
                if chunk == b"":
                    eof = True
                else:
                    fifo += chunk
            except BlockingIOError:
                pass
        if priming and len(fifo) >= PRIME:
            priming = False
            print(f"pcm-trim: primed {ms(len(fifo)):.1f}ms reservoir",
                  file=sys.stderr, flush=True)
        if w and fifo:
            try:
                n = os.write(1, fifo[:65536])
            except BlockingIOError:
                n = 0
            except BrokenPipeError:
                sys.exit(0)
            del fifo[:n]
            written += n
            if flowing is None and written > 2 * pipe_capacity(1):
                flowing = time.monotonic()
                # Start the trough window and the min/max stats HERE. Before this the
                # FIFO is legitimately empty (priming), and folding those samples in is
                # what made every instance's first stat line read "min 0.0".
                depths, t_win = [], flowing
                lo = hi = len(fifo)
                print(f"pcm-trim: consumer draining; {ms(startup_dropped):.1f}ms "
                      f"discarded so far, regulating for {SETTLE_S:.0f}s",
                      file=sys.stderr, flush=True)
            if not fifo and not eof:
                priming = True
                print("pcm-trim: reservoir ran dry — re-priming",
                      file=sys.stderr, flush=True)
        # If the consumer never drains at all (pw-cat wedged, node never picked up), flowing
        # stays None and this stays True — the cap then holds forever, which is what we want:
        # bounded, not accumulating. "settled" simply never prints, and that is the tell.
        settling = not settled and (
            flowing is None or time.monotonic() - flowing < SETTLE_S)
        if settling and len(fifo) > PRIME + HYST:
            # Start-up window: hold AT the reservoir, not merely under the ceiling —
            # whatever is above PRIME here is conserved race/upstream backlog, and if it
            # is not shed now it becomes permanent latency (nothing downstream sheds it).
            # Instantaneous, because that backlog is ~600 ms and must go NOW, before any
            # measurement window could have elapsed.
            drop = (len(fifo) - PRIME) // FRAME * FRAME
            del fifo[:drop]
            startup_dropped += drop

        # ── trough regulator ──────────────────────────────────────────────────
        # Acts on the FIFO's p10, so it only ever moves the STANDING level, and only
        # downward. The latch is a MEASURED CONVERGENCE, not a timer — the timer is what
        # made the landing a lottery in the first place.
        depths.append(len(fifo))
        now = time.monotonic()
        if flowing is not None and now - t_win >= WINDOW_S and depths:
            trough = p10(depths)
            depths = []
            t_win = now
            # Pre-latch the band is tight (BAND); after the latch it widens to one
            # in-flight lump, so ordinary jitter is never spliced during listening —
            # only 1 of 54 live steady-state windows would have crossed that threshold.
            slack = BAND if not settled else LUMP
            if trough > TARGET + slack:
                drop = min((trough - TARGET) // FRAME * FRAME, len(fifo))
                del fifo[:drop]
                startup_dropped += drop
                in_band = 0
                print(f"pcm-trim: trough {ms(trough):.1f}ms over setpoint "
                      f"{ms(TARGET):.1f} — shed {ms(drop):.1f}ms of standing backlog",
                      file=sys.stderr, flush=True)
            else:
                in_band += 1
            if not settled and (in_band >= CONVERGE_WINDOWS or
                                now - flowing > STARTUP_MAX_S):
                settled = True
                # Keep `settled at <instantaneous depth>` verbatim: 40 instances of
                # journal history are keyed on that number. The trough is what the
                # regulator actually holds, so report it alongside, not instead.
                print(f"pcm-trim: settled at {ms(len(fifo)):.1f}ms reservoir "
                      f"({ms(startup_dropped):.1f}ms discarded in total), "
                      f"trough {ms(trough):.1f}ms",
                      file=sys.stderr, flush=True)
        if len(fifo) > THRESH:
            drop = (len(fifo) - PRIME) // FRAME * FRAME
            del fifo[:drop]
            print(f"pcm-trim: dropped {drop // FRAME}f ({ms(drop):.1f}ms) "
                  f"of backlog", file=sys.stderr, flush=True)
        lo, hi = min(lo, len(fifo)), max(hi, len(fifo))
        if now - t_stat >= STAT_S:
            print(f"pcm-trim: fifo {ms(len(fifo)):.1f}ms "
                  f"(min {ms(lo):.1f} max {ms(hi):.1f} over {now - t_stat:.0f}s)",
                  file=sys.stderr, flush=True)
            t_stat = now
            lo = hi = len(fifo)


if __name__ == "__main__":
    main()
