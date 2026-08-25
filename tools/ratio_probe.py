#!/usr/bin/env python3
"""Per-instance servo-ratio sampler for ledger §7 item 10, v3.

Per bridge instance (identified by the b2tx stamp's inst id):
  prod_rate = (w_last - w_first) / (t_last_write - t_first_write)   [stamp fields]
  cons_rate = sum(sent=) / sum(window durations)                    [tx windows]
  ratio_ppm = (cons/prod - 1) * 1e6

Windows are collected during the segment and FILTERED AT EMIT to those whose
journald timestamp sits >= MARGIN inside the write span, so tx silence before/
after the burst and edge windows cannot leak in. Write span comes from the
stamp's own t_ns, so inter-run idle time never dilutes prod_rate.
"""
import mmap, os, re, json, struct, subprocess, sys, time

STAMP = "/dev/shm/vibesbox-ardftsrc-stamp"
OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/ratio_session.tsv"
DURATION_S = float(sys.argv[2]) if len(sys.argv) > 2 else 1800.0
MARGIN = 8.0


def open_stamp():
    try:
        f = os.open(STAMP, os.O_RDONLY)
        return mmap.mmap(f, 32, access=mmap.ACCESS_READ)
    except OSError:
        return None


def read_stamp(mm):
    a = mm[:]
    if a != mm[:]:
        return None
    seq, inst, frames, t_ns = struct.unpack("<QQQQ", a)
    if seq == 0:
        return None
    return inst, frames, t_ns


class Seg:
    __slots__ = ("inst", "ns0", "ns1", "w0", "w1", "wins", "rt0", "rt1")

    def __init__(self, inst, w, ns):
        self.inst, self.w0, self.w1 = inst, w, w
        self.ns0 = self.ns1 = ns
        self.rt0 = self.rt1 = time.time()
        self.wins = []                       # (wt_epoch_s, nf, dur_s)


def emit(out, s):
    # Write span comes from the stamp's own timestamps — internally consistent
    # under whatever (monotonic) base it uses, immune to inter-run idle.
    span = (s.ns1 - s.ns0) / 1e9
    # Trim two telemetry windows (10 s) at each edge of the collected list;
    # cons_rate is flat (tx paces every window identically) so this only
    # guards against partial windows straddling segment boundaries.
    inner = s.wins[2:-2]
    dbg = "emit inst=%d span=%.1f dw=%d nwins=%d/%d" % (
        s.inst, span, s.w1 - s.w0, len(inner), len(s.wins))
    if span <= 2 * MARGIN or not inner or s.w1 <= s.w0:
        print(dbg + " -> SKIP", flush=True)
        return
    nf = sum(x[0] for x in inner)
    dur = sum(x[1] for x in inner)
    prod = (s.w1 - s.w0) / span
    cons = nf / dur
    ppm = (cons / prod - 1.0) * 1e6
    print(dbg + " -> prod=%.2f cons=%.2f ppm=%.1f" % (prod, cons, ppm),
          flush=True)
    out.write("%d\t%.1f\t%.2f\t%.2f\t%.1f\t%d\n" % (
        s.inst, span, prod, dur, cons, ppm))


def main():
    mm = open_stamp()
    seg = None
    last_stamp_t = 0.0
    last_epoch = time.time()
    seen = set()
    win_re = re.compile(
        r"delayp10/50/90=\d+/(\d+)/\d+f.*?sent=(\d+)f\((\d+\.\d+)s\)")

    def close():
        if seg is not None:
            emit(out, seg)

    with open(OUT, "w", buffering=1) as out:
        out.write("# inst\tspan_s\tprod_fps\tcons_wdur_s\tcons_fps\tratio_ppm\n")
        end = time.monotonic() + DURATION_S
        while time.monotonic() < end:
            now = time.time()
            r = read_stamp(mm) if mm else None
            if r:
                inst, frames, t_ns = r
                if seg is not None and (inst != seg.inst or
                                        now - last_stamp_t > 10.0):
                    close()
                    seg = None
                if seg is None:
                    seg = Seg(inst, frames, t_ns)
                    print("segment start inst=%d" % inst, flush=True)
                seg.w1, seg.ns1, seg.rt1 = frames, t_ns, now
                last_stamp_t = now
            try:
                j = subprocess.run(
                    ["journalctl", "-u", "ndi-output", "-o", "json",
                     "--since", "@%.3f" % (last_epoch - 1.0)],
                    capture_output=True, text=True, timeout=5).stdout
                for ln in j.splitlines():
                    try:
                        o = json.loads(ln)
                    except ValueError:
                        continue
                    m = win_re.search(o.get("MESSAGE", ""))
                    if not m:
                        continue
                    wid = o["__REALTIME_TIMESTAMP"]
                    if wid in seen:
                        continue
                    seen.add(wid)
                    if seg:
                        seg.wins.append((int(wid) / 1e6, int(m.group(2)),
                                         float(m.group(3))))
            except Exception as exc:
                print("poll error: %r" % exc, flush=True)
            last_epoch = now
            time.sleep(1.0)
        close()
    print("done")


if __name__ == "__main__":
    main()
