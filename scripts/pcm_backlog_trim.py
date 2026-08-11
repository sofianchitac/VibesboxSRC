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

  PRIME  64 ms  (2 decode frames) held back before forwarding starts, and
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
PRIME = int(0.064 * RATE_BPS) // FRAME * FRAME
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
# Trimming to exactly PRIME fires on every few-ms overshoot: offline that was 98 splices in
# the first second, i.e. a burst of crackle where there had been one skip. One decode frame
# of slack cuts that to a handful and still bounds the settle-window depth at PRIME+32ms,
# well inside the ~75 ms the regulator exists to remove.
HYST = int(0.032 * RATE_BPS) // FRAME * FRAME


def ms(nbytes):
    return nbytes / RATE_BPS * 1000


def main():
    fifo = bytearray()
    eof = False
    priming = True
    flowing = None                  # monotonic time the consumer was proven to drain
    settled = False
    written = 0
    startup_dropped = 0
    lo = hi = 0
    t_stat = time.monotonic()
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
            drop = (len(fifo) - PRIME) // FRAME * FRAME
            del fifo[:drop]
            startup_dropped += drop
        elif not settling and not settled:
            settled = True
            print(f"pcm-trim: settled at {ms(len(fifo)):.1f}ms reservoir "
                  f"({ms(startup_dropped):.1f}ms discarded in total)",
                  file=sys.stderr, flush=True)
        if len(fifo) > THRESH:
            drop = (len(fifo) - PRIME) // FRAME * FRAME
            del fifo[:drop]
            print(f"pcm-trim: dropped {drop // FRAME}f ({ms(drop):.1f}ms) "
                  f"of backlog", file=sys.stderr, flush=True)
        lo, hi = min(lo, len(fifo)), max(hi, len(fifo))
        now = time.monotonic()
        if now - t_stat >= STAT_S:
            print(f"pcm-trim: fifo {ms(len(fifo)):.1f}ms "
                  f"(min {ms(lo):.1f} max {ms(hi):.1f} over {now - t_stat:.0f}s)",
                  file=sys.stderr, flush=True)
            t_stat = now
            lo = hi = len(fifo)


if __name__ == "__main__":
    main()
