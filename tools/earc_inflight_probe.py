#!/usr/bin/env python3
"""In-flight audio probe for the earc-bitstream-bridge pipeline (Stage 19).

Measures WHERE the ~400 ms multichannel lipsync offset sits by polling the
occupancy of every pipe in the bridge pipeline, non-invasively:

    arecord --[pipe A]--> tv_ac3_extract --[pipe B]--> ffmpeg --[pipe C]--> pw-cat

Each pipe is reopened read-only via /proc/<pid>/fd/1 and polled with
ioctl(FIONREAD). Not a single byte is read, so the audio path is untouched.
Holding an extra READ end cannot mask EOF for the downstream reader (EOF needs
all WRITE ends closed), but it WOULD suppress arecord's SIGPIPE on teardown —
so the probe exits and closes its fds the moment any pipeline pid vanishes.

Pipe A holds raw carrier (rate from arecord's cmdline), pipe C holds 48k 6ch
f32 PCM — both convert to milliseconds directly. Pipe B holds compressed ES,
converted via the extractor's live output byte rate (/proc/<pid>/io wchar
deltas; local pipe writes, where wchar is reliable). Pipe B is the prime
suspect: 64 KiB of ~640 kbps ES is ~800 ms of audio — the only buffer in the
chain physically able to hold the observed offset.

Usage (start BEFORE the bridge comes up; it waits for the processes):
    python3 earc_inflight_probe.py [logfile]
Output: CSV  t_ms,pipeA_B,pipeB_B,pipeC_B,extract_wchar,pipeA_ms,pipeB_ms,pipeC_ms
t=0 is the moment all four processes were found. Runs until the pipeline
exits or 60 s elapse, whichever is first.
"""
import array, fcntl, os, sys, termios, time

POLL_S = 0.05
MAX_RUN_S = 60.0


def find_pipeline():
    """Return {name: pid} for the four bridge processes, or None if incomplete."""
    want = {"arecord": None, "extract": None, "decode": None, "pwcat": None}
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                argv = f.read().split(b"\0")
        except OSError:
            continue
        joined = b" ".join(argv)
        if b"arecord" in argv[0] and b"eARC" in joined:
            want["arecord"] = int(pid)
        elif b"tv_ac3_extract" in joined:
            want["extract"] = int(pid)
        elif b"ffmpeg" in argv[0] or b"gst-launch" in argv[0]:
            want["decode"] = int(pid)
        elif b"pw-cat" in argv[0] and b"source.tv.ardftsrc" in joined:
            want["pwcat"] = int(pid)
    return None if None in want.values() else want


def carrier_rate(arecord_pid):
    with open(f"/proc/{arecord_pid}/cmdline", "rb") as f:
        argv = f.read().split(b"\0")
    return int(argv[argv.index(b"-r") + 1])


def iostat(pid, field):
    with open(f"/proc/{pid}/io") as f:
        for line in f:
            if line.startswith(field + ":"):
                return int(line.split()[1])
    return 0


def fionread(fd):
    buf = array.array("i", [0])
    fcntl.ioctl(fd, termios.FIONREAD, buf)
    return buf[0]


def main():
    log = open(sys.argv[1] if len(sys.argv) > 1 else "/tmp/earc_inflight.csv", "w")
    print("waiting for bridge pipeline...", file=sys.stderr)
    while (pids := find_pipeline()) is None:
        time.sleep(0.02)
    t_found = time.monotonic()
    print(f"found at {time.strftime('%Y-%m-%d %H:%M:%S')}.{int(time.time()%1*1000):03d}: {pids}",
          file=sys.stderr)

    # stdout of each stage = the pipe feeding the next stage
    fds = {}
    for name in ("arecord", "extract", "decode"):
        fds[name] = os.open(f"/proc/{pids[name]}/fd/1", os.O_RDONLY | os.O_NONBLOCK)

    rate_a = carrier_rate(pids["arecord"]) * 2 * 4        # bytes/s raw carrier
    rate_c = 48000 * 6 * 4                                # bytes/s decoded PCM
    w0, tw0 = iostat(pids["extract"], "wchar"), time.monotonic()  # ES byte-rate estimator
    es_rate = None

    # /proc io counters: arecord wchar = audio captured; pw-cat rchar = audio
    # ACCEPTED off its stdin. captured_s - accepted_s = in-flight upstream of
    # pw-cat; accepted_s ramping ahead of wall time = audio held INSIDE the
    # PipeWire stream queue — the segment pipe occupancy cannot see.
    log.write("t_ms,pipeA_B,pipeB_B,pipeC_B,extract_wchar,pipeA_ms,pipeB_ms,pipeC_ms,"
              "captured_s,accepted_s\n")
    try:
        while True:
            now = time.monotonic()
            if now - t_found > MAX_RUN_S:
                print("max runtime reached", file=sys.stderr)
                break
            if any(not os.path.exists(f"/proc/{p}") for p in pids.values()):
                print("pipeline exited — closing", file=sys.stderr)
                break
            a, b, c = (fionread(fds[n]) for n in ("arecord", "extract", "decode"))
            w = iostat(pids["extract"], "wchar")
            if now - tw0 > 2.0 and w > w0:                # settled average ES rate
                es_rate = (w - w0) / (now - tw0)
            t_ms = (now - t_found) * 1000
            log.write("%.1f,%d,%d,%d,%d,%.1f,%s,%.1f,%.3f,%.3f\n" % (
                t_ms, a, b, c, w,
                a / rate_a * 1000,
                "%.1f" % (b / es_rate * 1000) if es_rate else "",
                c / rate_c * 1000,
                iostat(pids["arecord"], "wchar") / rate_a,
                iostat(pids["pwcat"], "rchar") / rate_c))
            time.sleep(POLL_S)
    finally:
        for fd in fds.values():
            os.close(fd)
        log.close()


if __name__ == "__main__":
    main()
