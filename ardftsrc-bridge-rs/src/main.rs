// VibesboxSRC v3 — native Rust per-source ARDFTSRC resampler bridge.
//
// Drop-in replacement for scripts/ardftsrc_bridge.sh (ffmpeg/librempeg). Same single
// arg (`usb|lyrion|airplay|tidal`), same `source.<name>.ardftsrc` PipeWire output node, so
// source_router.py and the PipeWire/WirePlumber graph are untouched.
//
// Pipeline (two threads + a watchdog around a bounded ring):
//
//   capture thread        bounded SPSC ring       main/process thread
//   ──────────────        ─────────────────       ────────────────────────────────
//   open hw:<dev> S32  →  raw f64 frames      →    pop IN_CHUNK → ardftsrc 96k →
//   small ALSA buffer     (absorbs the DFT          → S32 → writei "pipewire" PCM
//   readi → f64 → push    compute burst)            (blocking write paces the loop)
//   (drop on overflow)
//
//   watchdog thread: polls source disconnect / native-rate change every 1s and stops
//   the process (exit 0) so systemd + source_router restart it (mirrors the v2 bash).
//
// Why this shape: the heavy DFT runs on the process thread, OFF the isochronous capture
// thread — that decoupling is what lets the 6ch USB gadget run a large FFT block without
// capture overruns (the v2 single-threaded ffmpeg filter graph could not). The blocking
// writei into the "pipewire" PCM hard-locks output delivery to the 96k graph clock, so
// source-vs-graph clock drift has to accumulate SOMEWHERE (just as v2's drift accumulated in
// ffmpeg's 131072-frame capture buffer and produced the accepted "occasional flush").
//
// ⚠ It is no longer the ring. Since the ingest high-water gate, the drift reservoir is the
// RESAMPLER'S OUTPUT FIFO — see READY_BAND for the measurement, and for why every ring-side
// trim below is blind to it. The ring is now only the compute-burst buffer.
//
// 2026-06-10: moved from the InterleavedResampler chunk API to RealtimeResampler
// (push/pull, priming introspection) + PRESET_GOOD, and added a sustained-backlog
// trim guard — together these removed the chunk-gathering and stall-residue latency
// the chunk-API first cut carried (see the process-loop comments).

use std::fs;
use std::process::Command;
use std::sync::atomic::{AtomicBool, AtomicI64, AtomicU64, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

use alsa::pcm::{Access, Format, HwParams, IO, PCM};
use alsa::{Direction, ValueOr};
use ardftsrc::{RealtimeResampler, PRESET_GOOD};
use ringbuf::traits::{Consumer, Observer, Producer, Split};

mod stamp;
use ringbuf::HeapRb;

const TARGET_RATE: u32 = 96_000;

// ardftsrc quality = FFT block size in FRAMES, which DIRECTLY sets the resampler's
// latency floor (group delay ≈ quality / input_rate). 2026-06-10: switched from the
// hand-rolled q=2048/bw=0.993/phase=-0.3 to the crate's PRESET_GOOD (quality 1878,
// bandwidth 0.911, Cosine(3.4375) taper — the taper is the proper pre-ringing fix the
// librempeg filter lacked, which the phase warp used to approximate). The crate rates
// PRESET_GOOD 99%+ on the HydrogenAudio SRC comparison and recommends it for realtime.
// 1878 @ 44.1k ≈ 43 ms group delay.

// ALSA capture buffer — the whole point of v3: a tiny fraction of ffmpeg's 131072-frame
// (~1 s occupancy) default. Capture is decoupled from compute, so this can be small.
// Tunable in Gate 3 against observed xruns vs latency.
// 512 chain (2026-06-10 latency phase 2), capture side excepted: a 512 capture period
// WIDENED the ring slack band (46-81 ms wander vs 23-46 ms at 1024 — more wakeups = more
// scheduling-noise accumulation), so capture stays at 1024.
//
// ★ 2026-06-11 REGRESSION FIX: OUT 512/1024 (shipped with the 512 chain) is BROKEN.
// The blocking writei keeps the pipewire-PCM output buffer ~full, so its occupancy is
// latency — but that same occupancy is the averaging depth of the fill signal feeding
// PipeWire's per-stream rate-match servo. At OUT_BUFFER 1024 (≤2 graph quanta of slack)
// the servo limit-cycled ±1.5-2.4%: audible time-stretch ("trembling") on BOTH the
// gadget and loopback sources, plus conserved backlog that made the stall guard skip
// ~209 ms every 10-60 s. Confirmed by A/B on the live box (v3 clean / v4 storms /
// v4+OUT-1024/4096 clean: 0 trims, ring locked 23-46 ms). The broken sizing once ran
// clean for 15 minutes before tipping over — do NOT shrink OUT_BUFFER again without a
// multi-hour soak. Burst absorption is still the input ring's job, not these buffers'.
// ★ 2026-08-19 A/B: 1024 -> 256. This is the ALSA READ QUANTUM, and it is the term `cap=`
// never showed: `readi` is called with a CAP_PERIOD-sized buffer on a BLOCKING pcm, so the
// capture thread waits for the whole quantum regardless of the period ALSA granted (the USB
// gadget grants 170 and it makes no difference). Measured at 1024: `readq=1024f(23.2ms,
// blocked 23108us)` — so a frame waits a mean of half a quantum, 11.6 ms @44.1k, before it
// even enters the ring. At 256 that becomes 2.9 ms, on EVERY source.
// ⚠ The one piece of counter-evidence is the 2026-06 note above (a 512 capture period
// "WIDENED the ring slack band"). That experiment did test a real variable — the read
// quantum is effective everywhere — so it has to be re-tested, not waved away. Watch the
// `ring=` band and the trim rate, and watch for capture overruns from the extra wakeups
// (172/s at 256 vs 43/s at 1024).
const CAP_PERIOD: i64 = 256;
/// Ring slack unit for the backlog trim, DELIBERATELY decoupled from `CAP_PERIOD`.
///
/// ⛔ These were the same constant, which would have made the A/B above worthless: `keep`
/// sets the trim quantum, both dead-band guard thresholds AND the depth-histogram bucket
/// width, so moving `CAP_PERIOD` would have moved the entire ratchet machinery with it and
/// no comparison would have meant anything. Every bad conclusion in this project's history
/// came from moving two things at once; this holds the trim side fixed at its measured
/// value so the read quantum is the only variable.
const TRIM_UNIT: i64 = 1024;
/// Window over which the ring's minimum occupancy is tracked for the dead-band trim.
/// Long on purpose: it must comfortably outlast normal drain cycles so that only backlog
/// which NEVER drains raises the floor. See the trim block in the process loop.
const FLOOR_WINDOW: Duration = Duration::from_secs(10);
/// Fraction of each window the ring must spend BELOW `keep` for it to count as draining.
/// ★ This exists because the FLOOR provably cannot see a ring that sits one period high and
/// dips to empty occasionally: one touch of zero anywhere in the window zeroes the minimum,
/// so that state reads identical to a clean one (measured, plan section 28 — the floor-based
/// trim recovered 21.3 ms of a ~42 ms step and then went blind to the rest). Lengthening
/// FLOOR_WINDOW makes it strictly WORSE, since a longer window is MORE likely to contain a
/// zero. A low percentile separates them; this is that percentile, expressed as a time
/// fraction so it does not depend on the poll loop's cadence. Deliberately conservative at
/// 5%: a false trip needs the ring non-empty 95% of a window, and the latch below bounds it
/// to one trim per bridge start anyway.
const LOW_FRAC_MAX: f64 = 0.05;
/// Share of a window the ring must spend at **two or more whole periods** for it to count as
/// backlogged. THIS is the trim trigger; `LOW_FRAC_MAX`/`persistent` are now reported only.
///
/// ⛔⛔ `low` (time at depth 0) was the wrong statistic and no threshold could fix it, because
/// it conflates "sits at exactly one period" — which is the slack `keep` is *supposed* to be —
/// with "is backlogged". Measured at window close, 2026-08-19, one config, three sources:
///
/// | source | `low` | `hist%` | time at depth >=2 | actually |
/// |---|---|---|---|---|
/// | tv (LPCM 48k) | 31-34% | [33,67,0,0] | **0%** | healthy |
/// | lyrion        | 12-14% | [13,82,5,0] | **4-6%** | healthy |
/// | usb           | 5-6%   | [5,43,52,0] | **41-52%** | ratcheted |
///
/// `low` puts tv and usb on OPPOSITE sides while both are normal for their source, and any
/// threshold that catches usb (needs >5%) also catches lyrion (12-14%) and would trim the
/// best source in the system for a ring holding one period. Depth>=2 separates all three with
/// nothing at all between 6% and 41%; 0.25 sits in the middle of that gap.
const DEEP_FRAC_MIN: f64 = 0.25;
/// How many whole capture periods of backlog the depth histogram can resolve. The ratchet
/// has never been seen past ~4; 8 is headroom, and the cost is 9 `Duration` adds per window.
const DEPTH_BUCKETS: usize = 8;

// ── Fine ring-depth statistic (2026-08-20). PURELY AN INSTRUMENT — it feeds no decision. ──
//
// ⛔ The trim's own histogram is bucketed in `TRIM_UNIT` (1024 frames), so it cannot resolve
// anything finer than 21.3 ms @48k, and `ring=` on the diag line is ONE INSTANTANEOUS SAMPLE
// of a sawtooth — which is why it only ever prints exact multiples of the capture quantum.
// Two failures traced to exactly that:
//   1. `@tv` parks at 1792 frames = 1.75 `TRIM_UNIT`s, so a trigger counting time at >=2 never
//      fires while the path holds a permanent 37.3 ms. The trim is blind there by CONSTRUCTION,
//      and that is a resolution limit, not a threshold choice.
//   2. Two probe runs whose SAMPLED `ring`+`out` matched exactly measured 7.6 ms apart. A point
//      sample of a sawtooth is not its mean, so that comparison could never have been valid —
//      and a whole hypothesis about a varying constant term was built on it.
// This records the time-weighted DISTRIBUTION in frames, so p10/p50/p90 are readable and the
// sawtooth's shape is visible (a ring alternating 0<->2048 and one parked at 1024 have the same
// mean and very different p10/p90).
//
// ⚠ DELIBERATELY does not touch the trigger. The standing rule in this file is to change the
// instrument and the threshold in separate steps; every wrong conclusion here came from moving
// two things at once. Read this for a few days BEFORE proposing a new trim statistic.
const RING_HIST_UNIT: usize = 128;    // frames per bucket — 2.7 ms @48k, 1/8 of TRIM_UNIT
const RING_HIST_BUCKETS: usize = 48;  // 0..6144 frames; the last bucket is an overflow catch-all

// ── Fine READY-depth statistic (2026-08-20). Also purely an instrument. ──
//
// ⛔ Added together with the output-cadence loop, and it is not optional decoration: that loop
// stops the ring from holding a chunk, and the ONLY other place the same audio can sit is the
// resampler's own output FIFO. Without this, `ringp50` falling proves nothing — a real saving
// and a pure relocation of the queue look identical. Read the two percentiles as a PAIR:
//     ringp50 down AND readyp50 flat  -> the chunk really left the pipeline
//     ringp50 down AND readyp50 up    -> it only moved somewhere with no trim path
// ⌀ Unlike `rs~` (a modelled constant standing in for a sawtooth — ledger section 6.6), this is a
// direct occupancy query, so it is a legitimate number to attribute from.
// In OUTPUT frames at TARGET_RATE, because that is what `num_samples_ready` counts.
// ⚠ THE RANGE IS TIED TO `ingest_high_water` (one DFT block + one output period, computed in the
// process loop). The widest ceiling is the 44.1k arm at ~5500 frames; this covers 2x that, so the
// percentiles stay READABLE rather than pinning in the overflow bucket. An instrument that
// saturates exactly where the gate binds cannot answer the saving-vs-relocation question it exists
// for. If the gate ceiling ever rises, raise this with it.
const READY_HIST_UNIT: usize = 128;   // 1.3 ms @96k
const READY_HIST_BUCKETS: usize = 96; // 0..12288 frames (128 ms) — ~2x the widest gate ceiling

// ── Fine CAPTURE-depth statistic (2026-09-03). Also purely an instrument. ──
//
// ⛔ THE LAST COMPARTMENT WITHOUT A DISTRIBUTION, and the omission was structural rather than
// deliberate. The ring got `ring_hist` and the resampler FIFO got `ready_hist` — and it was
// `ready_hist` that found the drift ratchet eight weeks after the fact. Capture had only two
// POINT SAMPLES: `cap=` (`pcm.delay()` after `readi`, one overwritten atomic) and `readq=`
// (the block time, likewise). This file's own standing rule is that a point sample of a
// sawtooth is not its mean and that a whole hypothesis was once built on treating it as one —
// so capture was the one place still exempt from the rule everything else is held to.
//
// ⇒ WHY IT MATTERS RIGHT NOW. The race reset zeroes the ring and rebuilds the resampler, and
// deliberately keeps `out`. It does NOT touch the ALSA capture ring, which holds up to the
// granted buffer — 2720 f on the USB gadget (61.7 ms @44.1k), 4096 f on Lyrion/eARC (85.3 ms
// @48k). ⛔⛔ THAT IS CAPACITY, NOT OCCUPANCY: if the capture thread keeps up, occupancy at the
// reset is about one period and the gap is worth ~5 ms, not 60. Quoting the capacity would be
// exactly the amplitude-vs-pedestal error §5b records. Nothing here can currently tell the two
// apart, which is why no capture-side clear is being written until this has been read.
//
// ⚠ INSTRUMENT ONLY. It feeds no decision and changes no behaviour, per the standing rule that
// the instrument and the threshold move in separate steps. Read `capp10` across a race reset
// first; the shed is only worth writing if the trough is materially above one period.
//
// In INPUT frames at `rate`, because that is what `pcm.delay()` counts on the capture side.
const CAP_HIST_UNIT: usize = 128;    // 2.9 ms @44.1k — same resolution as the ring's
const CAP_HIST_BUCKETS: usize = 40;  // 0..5120 frames — clears the widest grant (4096) with room

// ── The drift regulator (2026-08-29). The instrument above finally FEEDS a decision. ──
//
// ★ MEASURED, live, two sources: `readyp10` — the resampler FIFO's TROUGH, i.e. the part no write
// ever takes, i.e. 1:1 standing latency — climbs MONOTONICALLY from bridge start.
//     @usb    1088f (11.3 ms) -> 3904f (40.7 ms) over 42 min   = 11.6 ppm
//     @lyrion 2944f           -> 4672f           over 40 min   =  7.5 ppm
// Ledger §4 measured the USB clock at +11.53 ppm independently, a year of sessions ago. Same
// number. Drift is UNCORRECTED here: the source delivers (1+e)x realtime, PipeWire consumes at the
// graph rate, and the surplus has to pool somewhere.
//
// ⇒ THIS IS NOT A NEW PHENOMENON. It is ledger §5's limit cycle, RELOCATED. §5 documents the same
// cycle in the RING — `deadband-deep` every ~15 min, 1024 f a time, "end-to-end latency sawtooths
// 10.7 ms with a ~15 min period", and concludes "✅ Inaudible — the variance is the defect, not
// the skip." Since the ingest high-water gate the gate is a CEILING ON `ready`, so `ready` fills
// FIRST — from the ~11 ms the race reset lands on up to `ingest_high_water` (~5112f = 53 ms at
// 44.1k) — and only then does the surplus spill into the ring. Watched that spill begin live at
// 13:30 (`ring_md` 0 -> 585) exactly where the arithmetic put it. Same session: 42 min, ZERO
// trims, `deep=0.0%` every window, ring p50 flat 576-960, against §5's 3.69 trims/h and a ring p50
// ramping 1344 -> 1856. ⇒ The reservoir moved from the ring (1024 f, trimmable, 15 min) to the
// resampler FIFO (~4100 f, NO shed path, ~1 h). Same defect, 4x the amplitude, where no trim can
// reach it.
//
// ⛔⛔ THIS IS A @usb / @lyrion FAULT. IT IS **NOT** THE FILMED LPCM VARIANCE, WHICH IS ALL @tv.
// §4 puts eARC at -27.39 ppm, the OPPOSITE SIGN to USB's +11.53. Tested directly — 27-min single
// @tv instance, 2026-08-29, no restart: `readyp10` FALLS 832 -> 64 f and pins at the bottom bucket
// for the final 18 min; 0 regulator actions, 0 underruns, 0 xruns. This regulator is therefore
// correctly and permanently INERT on @tv, which never rises above setpoint.
//
// ⇒ DIRECTION ALONE decides which symptom you get: over-production has nowhere to go downstream
// (PipeWire will not consume faster than the graph rate) so it backs up into `ready` = a ratchet;
// under-production just drains buffers to empty and pins = no ratchet. Same absent regulation,
// opposite sign. ⛔ Do not "fix" the @tv floor by making this regulator bidirectional — raising a
// standing level means withholding output, and §7.9's dry/re-prime oscillation is what that costs.
//
// ⛔⛔ THE @tv SOAK IS A STATEMENT ABOUT THIS FIFO, NOT ABOUT @tv LATENCY. Do not repeat the slide
// I made from one to the other. The filmed number is END-TO-END, and re-measured 2026-08-29 the
// within-clip PCM drift goes BOTH WAYS (4 takes up, 4 down, 1 flat; the two long takes disagree,
// +5.8 vs -0.4 ms/min). Over the same 90 min this FIFO was pinned and the NDI transmitter was flat
// (avail 0/0/0f, trim=0f), so the filmed drift is downstream of the Pi. See docs/HANDOFF.md.
//
// ⛔ WHY EIGHT WEEKS OF INSTRUMENTS MISSED IT — read before trusting any older number here.
// Not for want of an instrument: `ready_hist` has been printing since 2026-08-20. Three reasons,
// each of which is a live trap for the next measurement too:
//   1. EVERY A/B IN THE PROGRAMME RESTARTS THE BRIDGE PER RUN (see `ingest_high_water` below; §7
//      "the probe starts a fresh @lyrion per run"). A restart zeroes the pedestal, so all 24
//      output-cadence runs, all 44 landing-lottery runs and every ppm probe were sampled in the
//      first minutes of an instance — at the BOTTOM of the cycle, by construction. The one round
//      that could not reset (filmed takes an hour apart on live instances) is precisely where the
//      unexplained 26-44 ms turned up.
//   2. §5b's "there is no excess to trim — the bridge is at its structural floor" read `readyp90`
//      on a FRESH instance. It is a true statement about the sawtooth's AMPLITUDE (one DFT block;
//      still true today, p90-p10 = 3584 f at both ends of the run) and says nothing about the
//      PEDESTAL it rides on. Amplitude and pedestal are different quantities — the same mistake in
//      kind as TROUGH vs FILL in `pcm_backlog_trim.py`.
//   3. §4's "drift is not what costs the latency — 27 ppm cannot require 28 ms of buffer" is true
//      of a REGULATED buffer and assumes a servo exists everywhere. Unregulated, 11.6 ppm is
//      42 ms/hour. §5 had already logged the residue this leaves — "the ring fills 2.3x faster
//      than its clock allows … a second accumulation source exists and is unidentified."
//
// ⛔ `summ~` CANNOT corroborate any of this and must not be quoted at it: `rsm~` carries
// `-ready` and `ready_ms` carries `+ready`, so `ready` cancels EXACTLY out of the sum. What is
// left of it grows with any error in the NOMINAL input rate (11.6 ppm x 2520 s = 29 ms, against
// the 25.7 ms "rise" observed) — the same class of artifact as the withdrawn +533 ppm servo
// finding. `num_samples_ready()` is a direct occupancy query and stands alone.
//
// ⚠ THIS REGULATOR DOES NOT LATCH, and that is the one way it differs from its template.
// `pcm_backlog_trim.py` sheds a one-time START-UP backlog, proves convergence, then sets
// `settled` and widens its band precisely so ordinary jitter is never spliced during listening.
// Drift is PERMANENT, so there is no converged state to latch: this sheds ~1.1 f/s for the life
// of the instance. The cadence is therefore an audibility choice, not a free one. Chosen with the
// user 2026-08-29: the TIGHTEST band the instrument can resolve — one histogram bucket — so each
// splice is ~1.3 ms and the standing level is held inside 1.3 ms instead of roaming 42.
const READY_BAND: usize = READY_HIST_UNIT;   // 128 f = 1.33 ms @96k; ~2 min/splice at 11.6 ppm
// Setpoint for the TROUGH, derived from the CONSUMER's granularity exactly as the bitstream
// regulator's `TARGET` is (pw-cat's own buffering target there; the writei loop's period here):
// one output period guarantees every write can be a whole period without the FIFO ever being the
// binding constraint. It is a TROUGH, not a fill — the FIFO still swings a whole DFT block above
// it — and conflating the two is the live bug §7.9 cost a run to. Bound at runtime to
// `out_period_frames`, so it tracks OUT_PERIOD rather than restating it as a literal.
const CAP_BUFFER: i64 = 4096;
// ⚠ `OUT_PERIOD` is not a latency term. The blocking `writei` keeps the output PCM near
// full, so standing latency tracks OUT_BUFFER, not the write granularity. Its only leverage
// is that ALSA requires buffer >= 2 periods, so 1024 here is what makes 2048 the floor for
// OUT_BUFFER. Cutting it to 512 would unlock 1536 (3 quanta) — see the note below before
// trying that.
const OUT_PERIOD: i64 = 1024;
/// Slack above one DFT block that the resampler's output FIFO may hold before ingest is gated.
///
/// ⚠ PINNED, deliberately not derived from `OUT_PERIOD`, so an `OUT_PERIOD` A/B varies the output
/// side ONLY. Derived, it would quietly move the ingest gate too and the rung would stop being one
/// variable. 1024 is the value in force when this loop was first measured, so the baseline binary
/// is behaviourally identical either way.
const INGEST_SLACK: usize = 1024;
// ★ 2026-08-19 SOAK CANDIDATE: 3072 -> 2048 (4 graph quanta @96k, vs the 2 that broke).
// 3072 soaked ~3 h clean across the TV and USB paths (0 trims, band stable), banking the
// first 10.7 ms; this takes the second and last one available at OUT_PERIOD 1024.
//
// ⛔ Going BELOW 2048 needs OUT_PERIOD 512, and the 2026-06 regression CONFOUNDED the two:
// it changed period and buffer together ("OUT 512/1024") and then attributed the failure to
// the buffer — "At OUT_BUFFER 1024 (<=2 graph quanta of slack) the servo limit-cycled". The
// averaging depth feeding PipeWire's rate-match servo IS the buffer occupancy, so that
// attribution is probably right and OUT_PERIOD 512 with a 1536/2048 buffer has simply never
// been tested. Worth only ~5.3 ms though, at 3 quanta against the 2 that broke, so it is
// the last thing to try and it deserves its own soak — not a rider on this one.
// ★ 2026-08-18 (previous): 4096 -> 3072 (6 graph quanta @96k, vs the 2 that broke).
// Plan section 29: the bridge's `out` occupancy is a STANDING 42.6-47.9 ms reservoir and is
// roughly HALF the 71.5 ms measured premium over an arecord|pw-cat path — recoverable with
// no quality argument and no swap to PipeWire. The warning above still governs: the failure
// mode is not an error but a SERVO LIMIT-CYCLE (audible time-stretch, plus conserved backlog
// that makes the stall guard skip periodically), and it once ran clean for 15 minutes before
// tipping over. Hence a multi-hour soak, watching for periodic `[diag trim]` stall lines and
// for `out=` failing to settle. Revert = /opt/vibesbox-src/bin/ardftsrc-bridge.previous.
const OUT_BUFFER: i64 = 2048;

struct SourceCfg {
    name: &'static str,       // node becomes source.<name>.ardftsrc
    card: &'static str,       // ALSA card name (for amixer numid=8 / messages)
    channels: usize,
    in_device: &'static str,  // ALSA capture device
    position: &'static str,   // PipeWire audio.position list
    // None => USB gadget: native rate via amixer numid=8 (Capture Rate).
    // Some(path) => snd-aloop writer: native rate via /proc hw_params `rate:`.
    hw_params: Option<&'static str>,
    // true => I2S SLAVE capture (the eARC tap). The rate cannot be READ from anywhere —
    // there is no amixer control and no hw_params writer — so it is MEASURED from frame
    // delivery, once at startup and then every watchdog tick (see measure_rate/snap_rate).
    // Also selects the disconnect check: /proc/asound/<card> directory existence, since
    // there is no hw_params to consult and the DT platform card never disappears.
    slave_measured: bool,
}

// The rates an eARC/HDMI LPCM source can present. Adjacent entries are >=8.1% apart, so a
// +/-4% snap window is unambiguous and comfortably wider than the measurement error.
const STD_RATES: [u32; 6] = [44_100, 48_000, 88_200, 96_000, 176_400, 192_000];
const RATE_TOLERANCE: f64 = 0.04;

// How long to measure at startup. Both the frame count and the elapsed time come from the
// SAME completed reads, so there is no period-quantisation error and this need not be long.
const MEASURE_WINDOW: Duration = Duration::from_millis(500);

// Consecutive 1s watchdog windows that must agree ON THE SAME snapped rate before we treat
// it as a real rate change. A stall or a ring overflow makes one window read low, which is
// indistinguishable from a downward rate change at 1 Hz sampling — requiring agreement on a
// specific target (not merely "not the current rate") is what stops a stall storm from
// walking the bridge onto a wrong rate and reintroducing wrong-pitch playback.
const RATE_CHANGE_WINDOWS: u32 = 3;

/// Time-weighted percentile of a ring-depth histogram, returned in FRAMES.
///
/// `hist[k]` is the time the ring spent holding between `k*unit` and `(k+1)*unit` frames, so the
/// answer is reported at the bucket's MIDPOINT — the bucket is the resolution, and claiming its
/// lower edge would bias every reading low by half a bucket.
fn ring_pct(hist: &[Duration], unit: usize, q: f64) -> usize {
    let total: f64 = hist.iter().map(|d| d.as_secs_f64()).sum();
    if total <= 0.0 {
        return 0;
    }
    let target = total * q;
    let mut cum = 0.0;
    for (k, d) in hist.iter().enumerate() {
        cum += d.as_secs_f64();
        if cum >= target {
            return k * unit + unit / 2;
        }
    }
    (hist.len() - 1) * unit
}

/// Frames to hand to one `writei`: never more than the output PCM already has space for, never
/// more than one period, never more than the resampler has ready.
///
/// ⛔ The bound on `avail_frames` is the whole point of the output-cadence loop — a write that
/// exceeds the space ALSA reports BLOCKS for the difference, and the input arriving during that
/// block is exactly the ring depth the loop exists to remove (ledger section 5b).
fn out_chunk_frames(avail_frames: usize, period_frames: usize, ready_frames: usize) -> usize {
    avail_frames.min(period_frames).min(ready_frames)
}

/// Output frames the drift regulator should shed, given the window's measured trough.
///
/// Downward only, and zero inside the band — raising a standing level means withholding output,
/// i.e. a gap, and an unguarded re-prime is the 2026-08-25 dry/re-prime oscillation.
///
/// ⚠ `trough` arrives QUANTIZED to `ring_pct`'s bucket midpoints, and that quantization is what
/// supplies the hysteresis: with the band at one bucket, shedding `trough - target` lands the next
/// window's p10 one bucket BELOW the trigger, so a regulated instance alternates trim/no-trim
/// rather than trimming every window. Widening the band without re-checking that interaction is
/// how this turns into a per-window splicer.
fn drift_drop_frames(trough: usize, target: usize, band: usize, ready_now: usize) -> usize {
    if trough <= target + band {
        return 0;
    }
    (trough - target).min(ready_now)
}

/// Snap a measured frame rate onto a standard rate. None when it is not within
/// RATE_TOLERANCE of any of them — a stall, a startup transient, or a genuinely exotic
/// rate. All three mean "do not resample against this", so None is never a rate change.
fn snap_rate(measured: f64) -> Option<u32> {
    if !measured.is_finite() || measured <= 0.0 {
        return None;
    }
    STD_RATES
        .iter()
        .copied()
        .min_by(|a, b| {
            let da = (measured - *a as f64).abs();
            let db = (measured - *b as f64).abs();
            da.partial_cmp(&db).unwrap()
        })
        .filter(|r| (measured / *r as f64 - 1.0).abs() <= RATE_TOLERANCE)
}

/// Debounce for the slave-rate watchdog.
///
/// Fires only when RATE_CHANGE_WINDOWS consecutive windows agree on the SAME snapped rate,
/// and that rate differs from the one we are running at. `None` (stall, clock gone, garbage)
/// RESETS the evidence rather than counting toward a change — that asymmetry is the whole
/// point: at 1 Hz sampling a stalled window is indistinguishable from a slower rate, so
/// counting them would let a flapping link walk the bridge onto a wrong rate, which is the
/// wrong-pitch failure this design exists to prevent.
///
/// In practice an eARC rate change re-handshakes the link and the capture takes an ALSA I/O
/// error first, which stops the bridge faster than this can (~1 s vs 3 s). This is the
/// backstop for a source that changes rate WITHOUT dropping the clock.
struct RateChangeDebounce {
    current: u32,
    candidate: Option<u32>,
    agreed: u32,
}

impl RateChangeDebounce {
    fn new(current: u32) -> Self {
        Self { current, candidate: None, agreed: 0 }
    }

    /// Feed one window's snapped measurement. Some(rate) => change confirmed.
    fn observe(&mut self, snapped: Option<u32>) -> Option<u32> {
        match snapped {
            Some(r) if r != self.current => {
                if self.candidate == Some(r) {
                    self.agreed += 1;
                } else {
                    self.candidate = Some(r);
                    self.agreed = 1;
                }
                if self.agreed >= RATE_CHANGE_WINDOWS {
                    return Some(r);
                }
            }
            _ => {
                // Either the expected rate, or no usable measurement. Both clear the run.
                self.candidate = None;
                self.agreed = 0;
            }
        }
        None
    }
}

/// Measure an I2S slave capture's true rate from how fast it delivers frames.
///
/// The requested open rate is bookkeeping for a slave DAI — the hardware clocks at whatever
/// BCLK the transmitter provides — so we open at a nominal 48k purely to get a handle, and
/// derive the real rate from delivery. (Proven: source_router's probe opens at 48k and a
/// 192 kHz stream returns the requested 48000 frames in ~0.25 s.)
///
/// ★ The first read is DISCARDED. It carries the ALSA open plus the driver's first period,
/// which biases the estimate LOW — that exact bias read 48 kHz as ~41.8 kHz (~13%) and made
/// source_router refuse a healthy LPCM stream on 2026-07-28. Do not "simplify" it away.
///
/// Blocks if no bit clock is arriving, matching the capture loop's own semantics (TV off =
/// the capture simply blocks). source_router only starts this bridge after a probe has
/// already seen a clock.
fn measure_rate(device: &str, channels: usize) -> Option<u32> {
    let pcm = open_pcm(device, Direction::Capture, 48_000, channels as u32, CAP_PERIOD, CAP_BUFFER).ok()?;
    let io = pcm.io_i32().ok()?;
    let mut buf = vec![0i32; CAP_PERIOD as usize * channels];

    io.readi(&mut buf).ok()?;                 // discard — see above

    let t0 = Instant::now();
    let mut frames = 0usize;
    while t0.elapsed() < MEASURE_WINDOW {
        frames += io.readi(&mut buf).ok()?;
    }
    let secs = t0.elapsed().as_secs_f64();
    drop(io);
    drop(pcm);                                // free the device before the real open

    let measured = frames as f64 / secs;
    let snapped = snap_rate(measured);
    match snapped {
        Some(r) => eprintln!("ardftsrc-bridge: measured {measured:.0} Hz -> {r} Hz"),
        None => eprintln!("ardftsrc-bridge: measured {measured:.0} Hz — not a standard rate"),
    }
    snapped
}

fn source_cfg(src: &str) -> Option<SourceCfg> {
    match src {
        "usb" => Some(SourceCfg {
            name: "usb",
            card: "UAC2Gadget",
            channels: 6,
            in_device: "hw:UAC2Gadget,0,0",
            // NOTE: iPadOS/macOS ignore the UAC2 channel mask and emit 5.1 in a FIXED
            // physical order regardless of file tag/codec — a standard SMPTE file
            // (L R C LFE Ls Rs) arrives as L R Ls Rs C LFE (center & LFE last). Proven
            // 2026-06-17 with the channel-ID test files. Do NOT try to correct it here —
            // the correction is done in REAPER, by standing rule.
            // ⚠ `position` is LOAD-BEARING, not cosmetic: source_router links a source into
            // dsp-in by channel NAME, so this list picks the dsp-in lane each captured lane
            // lands on, and since 2026-08-23 dsp-in lane N is NDI channel N. Relabeling here
            // DOES move channels at the receiver.
            position: "[ FL FR FC LFE RL RR ]",
            hw_params: None,
            slave_measured: false,
        }),
        "lyrion" => Some(SourceCfg {
            name: "lyrion",
            card: "Lyrion",
            channels: 2,
            in_device: "hw:Lyrion,1,0",
            position: "[ FL FR ]",
            hw_params: Some("/proc/asound/Lyrion/pcm0p/sub0/hw_params"),
            slave_measured: false,
        }),
        "airplay" => Some(SourceCfg {
            name: "airplay",
            card: "AirPlay",
            channels: 2,
            in_device: "hw:AirPlay,1,0",
            position: "[ FL FR ]",
            hw_params: Some("/proc/asound/AirPlay/pcm0p/sub0/hw_params"),
            slave_measured: false,
        }),
        // Tidal Connect: the (Dockerised, armhf) tidal_connect_application writes PCM
        // to the Tidal snd-aloop (hw:Tidal,0,0) via PortAudio/ALSA; we read the capture
        // side. Stereo, FL/FR. UNLIKE lyrion/airplay (which write S32, so we read hw:),
        // Tidal's PortAudio output negotiates the snd-aloop format from the TRACK: S16_LE
        // for 16-bit tracks, S24/S32 for hi-res. open_pcm requests S32, so we read via
        // `plughw:` — the ALSA plug layer converts the loopback's native format up to S32
        // transparently (bit-exact), making the bridge robust to any track quality.
        "tidal" => Some(SourceCfg {
            name: "tidal",
            card: "Tidal",
            channels: 2,
            in_device: "plughw:Tidal,1,0",
            position: "[ FL FR ]",
            hw_params: Some("/proc/asound/Tidal/pcm0p/sub0/hw_params"),
            slave_measured: false,
        }),
        // TV via the eARC I2S tap (SiI9437 inside the Lindy 38368 -> RP1 i2s1 slave;
        // see config/overlays/). This REPLACED the TOSLINK optical path on 2026-07-28
        // — eARC strictly dominates it (DD+ and DD and stereo, vs optical's DD and
        // stereo only), and both taps carry the same TV, so running both would sum two
        // copies at different latencies. The optical rollback (`tv-optical`) was
        // retired 2026-08-03 — archived to vibesbox-backups/.
        //
        // LPCM mode, 6ch since 2026-08-12 (was 2ch = lane SD0 only). `hw:` (not plughw:)
        // — the tap is natively S32 24-bit left-justified, so no conversion layer is wanted.
        //
        // A 6ch open reads lanes SD0-SD2. Channel order is passed through AS CAPTURED —
        // do NOT add a remap here. All channel mapping lives in REAPER, exactly as for the
        // USB source above. Verified end to end 2026-08-12: multichannel LPCM arrives in
        // REAPER identically to USB and the bitstream bridge.
        //
        // ★ THE TAP ITSELF DOES 8ch (7.1) — measured live 2026-08-12, all four lanes
        // carrying discrete audio at an identical level. 6 is a DOWNSTREAM ceiling, not a
        // tap limit: CamillaDSP's dsp_6ch.yml is capture 6 / playback 6, ndi_transmitter.py
        // sends 6ch, and the NDI stream is literally named VibesboxSRC-5.1. So a 7.1 source
        // loses the pair on lane SD3. Raising this to 8 means changing the CamillaDSP
        // config, the transmitter's read width, the stream identity and
        // REAPER's input — a system-wide change, NOT a one-line edit here.
        //
        // Always 6ch, with no stereo/multichannel detection: when the TV sends 2.0 the
        // unused lanes read EXACTLY zero (verified 2026-07-27), so stereo simply sits in
        // FL/FR with silent rears — the same contract as the 6ch USB source, and it matches
        // the system's no-Pi-side-upmix rule. Detecting channel count instead would flap on
        // every quiet passage. Note the C9's own webOS player cannot source multichannel
        // LPCM at all; this path only carries it from an HDMI input in "Pass Through".
        //
        // slave_measured is a real constraint, not an optimisation: an I2S SLAVE capture
        // has NO incoming-rate autodetect, and unlike the loopback sources there is no
        // hw_params/numid8 to read one from. So the rate is MEASURED off frame delivery —
        // at startup, and every second by the watchdog, which restarts the bridge on a
        // sustained change so the resampler is never fed a stale input rate. Before
        // 2026-08-12 this was hardcoded 48 kHz and any other rate was simply refused.
        //
        // The eARC card is a device-tree platform card, so /proc/asound/eARC always
        // exists — the watchdog's disconnect check never fires. That matches the optical
        // semantics it replaces (the bridge ran as long as the Pico was plugged in) and
        // is harmless: TV off = no bit clock = the capture simply blocks, and the node
        // stays linked carrying silence.
        // ★ 8ch since 2026-08-13, was 6. A 6ch open reads SD0-SD2 only, and the source
        // puts LPCM surrounds on lanes 7-8 (SD3) leaving lanes 5-6 digitally SILENT —
        // measured with `arecord -c 8` against per-channel ID tones (the tap reports
        // CHANNELS: [2 8]). Reading 6 collected 4 populated lanes plus 2 empty ones and
        // discarded the surrounds entirely. Quiet lanes stay digitally silent, so 8 is
        // unconditional — no channel-count detection to flap on quiet passages.
        // ⛔ `position` is canonical CH8 in order, which makes the name-based link into
        // dsp-in the IDENTITY — so lanes pass through exactly as captured, and since
        // 2026-08-23 captured lane N is NDI channel N. It is not cosmetic: reordering this
        // list would move channels. REAPER maps them. Do not "fix" it here.
        "tv" => Some(SourceCfg {
            name: "tv",
            card: "eARC",
            channels: 8,
            in_device: "hw:eARC,0",
            position: "[ FL FR FC LFE RL RR SL SR ]",
            hw_params: None,
            slave_measured: true,
        }),
        _ => None,
    }
}

// ── native-rate detection (mirrors ardftsrc_bridge.sh) ───────────────────────────────

fn read_rate_amixer(card: &str) -> Option<u32> {
    let out = Command::new("amixer")
        .args(["-D", &format!("hw:{card}"), "cget", "numid=8"])
        .output()
        .ok()?;
    let s = String::from_utf8_lossy(&out.stdout);
    for line in s.lines() {
        if let Some(idx) = line.find(": values=") {
            let rest = &line[idx + ": values=".len()..];
            let first = rest.split(',').next()?.trim();
            if let Ok(r) = first.parse::<u32>() {
                if r > 0 {
                    return Some(r);
                }
            }
        }
    }
    None
}

fn read_rate_hw_params(path: &str) -> Option<u32> {
    let txt = fs::read_to_string(path).ok()?;
    for line in txt.lines() {
        if let Some(rest) = line.strip_prefix("rate:") {
            // "rate: 44100 (44100/1)"
            let num = rest.trim().split_whitespace().next()?;
            if let Ok(r) = num.parse::<u32>() {
                if r > 0 {
                    return Some(r);
                }
            }
        }
    }
    None
}

fn detect_rate(card: &str, hw_params: Option<&str>) -> Option<u32> {
    match hw_params {
        None => read_rate_amixer(card),
        Some(p) => read_rate_hw_params(p),
    }
}

/// True when the source is gone or in the snd-aloop "no setup" rate-retry transient
/// (treated as disconnect, exactly like the v2 bash watchdog — restart re-detects rate).
fn source_disconnected(card: &str, hw_params: Option<&str>) -> bool {
    match hw_params {
        None => read_rate_amixer(card).unwrap_or(0) == 0,
        Some(p) => match fs::read_to_string(p) {
            Ok(txt) => {
                let l = txt.to_lowercase();
                l.contains("closed") || l.contains("no setup")
            }
            Err(_) => true,
        },
    }
}

// ── ALSA helpers ─────────────────────────────────────────────────────────────────────

fn open_pcm(
    device: &str,
    dir: Direction,
    rate: u32,
    channels: u32,
    period: i64,
    buffer: i64,
) -> Result<PCM, alsa::Error> {
    let pcm = PCM::new(device, dir, false)?;
    {
        let hwp = HwParams::any(&pcm)?;
        hwp.set_channels(channels)?;
        hwp.set_rate(rate, ValueOr::Nearest)?;
        hwp.set_format(Format::s32())?; // native-endian S32 == S32_LE on the Pi (ARM LE)
        hwp.set_access(Access::RWInterleaved)?;
        let _ = hwp.set_buffer_size_near(buffer);
        let _ = hwp.set_period_size_near(period, ValueOr::Nearest);
        pcm.hw_params(&hwp)?;
    }
    pcm.prepare()?;
    // ⚠ `set_*_near` is a REQUEST, not a setting. ✅ ANSWERED — the grant is read back here
    // and the answers are in the ledger's config table: `hw:UAC2Gadget,0,0` gives 170/2720
    // against a 256/4096 ask, while `hw:Lyrion,1,0`, `hw:eARC,0` and the `pipewire` playback
    // PCM all grant exactly what they are asked. Keep printing it: every run then
    // self-verifies rather than resting on the requested number.
    if let Ok(cur) = pcm.hw_params_current() {
        eprintln!(
            "ardftsrc-bridge: {device} {} hw granted period={} buffer={} (requested {period}/{buffer})",
            match dir { Direction::Capture => "capture", Direction::Playback => "playback" },
            cur.get_period_size().map(|v| v.to_string()).unwrap_or_else(|_| "?".into()),
            cur.get_buffer_size().map(|v| v.to_string()).unwrap_or_else(|_| "?".into()),
        );
    }
    // ── The sw_params this open INHERITS. Nothing here ever SETS them, so they are whatever
    //    alsa-lib and (on the output side) the `pipewire` plugin choose. ──
    //
    // ★ MEASURED 2026-09-03 with a standalone probe that mirrors this function exactly, on
    // `hw:Resampled,1,0` (the unused card, so no live path was touched), `hw:Lyrion,1,0` and
    // the `pipewire` playback PCM. All three came back IDENTICAL in shape:
    //     start_threshold = 1        stop_threshold = buffer_size
    //     avail_min       = granted period          silence_threshold/size = 0
    //
    // ⛔ SO THE OBVIOUS "FIX" IS A REGRESSION, AND THIS COMMENT EXISTS TO STOP IT BEING TRIED.
    // The generic advice for deterministic startup is `set_start_threshold(period_size)`. That
    // is PLAYBACK advice. Here `start_threshold` is already 1 on BOTH streams, which is the
    // minimum possible: capture auto-starts on the first `readi` and playback goes live on the
    // first `writei`, with no "wait for N frames" delay to phase against. Raising it would ADD
    // a startup hold, not remove one.
    //
    // ⇒ The one knob genuinely untested is `stop_threshold`: at `buffer_size` an xrun STOPS the
    // stream and `write_all`/`try_recover` re-prepares it, which redraws that stream's phase.
    // Setting it to `boundary` would free-run through an xrun instead. Untested, no evidence it
    // fires in steady state (0 graph xruns over days), and it is a semantics change — so it is
    // recorded, not shipped.
    if let Ok(sw) = pcm.sw_params_current() {
        eprintln!(
            "ardftsrc-bridge: {device} {} sw inherited start_thr={} stop_thr={} avail_min={} (never set here)",
            match dir { Direction::Capture => "capture", Direction::Playback => "playback" },
            sw.get_start_threshold().map(|v| v.to_string()).unwrap_or_else(|_| "?".into()),
            sw.get_stop_threshold().map(|v| v.to_string()).unwrap_or_else(|_| "?".into()),
            sw.get_avail_min().map(|v| v.to_string()).unwrap_or_else(|_| "?".into()),
        );
    }
    Ok(pcm)
}

#[inline]
fn i32_to_f64(s: i32) -> f64 {
    s as f64 / 2_147_483_648.0 // 2^31
}

// Intersample headroom, applied BEFORE the clamp below. Upsampling reconstructs the true
// peaks a loud/limited master hides between its samples, so ardftsrc output routinely
// exceeds ±1.0 on such material — and this i32 conversion is the ONLY integer stage in the
// chain (everything after it is F32: PipeWire sum bus, CamillaDSP, NDI). Clipping here is
// unrecoverable. 2026-07-29: moved here from the CamillaDSP `intersample_headroom` filter in
// dsp_6ch.yml, which sat DOWNSTREAM of this node and so attenuated already-clipped audio.
// End-to-end level is UNCHANGED: gain is linear and PipeWire's sum is linear, so -4 dB
// per-source pre-sum equals the -4 dB post-sum it replaces.
const HEADROOM: f64 = 0.630_957_344_480_193_4; // 10^(-4/20) = -4.0 dB

#[inline]
fn f64_to_i32(x: f64) -> i32 {
    ((x * HEADROOM).clamp(-1.0, 1.0) * 2_147_483_647.0) as i32 // 2^31 - 1
}

/// Default RT priority for the CAPTURE THREAD ONLY. 0 disables; override with
/// `ARDFTSRC_CAP_RTPRIO` (a systemd drop-in) so the arm can be A/B'd without a rebuild —
/// the standard protocol here is one variable per binary swap, and a rebuild is not one.
const CAP_RTPRIO_DEFAULT: i32 = 20;

/// Put the CALLING thread on SCHED_FIFO at `prio`. Returns the priority actually in force.
///
/// ★ 2026-09-03. Until now nothing in this chain except `ndi-output` had any scheduling
/// policy: `systemctl show ardftsrc-bridge@usb -p CPUSchedulingPolicy` returned 0
/// (SCHED_OTHER) for every bridge, for `camilladsp` and for `source-router`. CamillaDSP is
/// covered anyway — its `data-loop.0` threads get FIFO 83 client-side from root's
/// CAP_SYS_NICE — and so is this process's own graph thread. What was NOT covered is the
/// thread with the hardest deadline in the whole chain: the one blocking in `readi` on an
/// isochronous USB gadget or an I2S slave, which at SCHED_OTHER competes on equal terms
/// with the QML kiosk.
///
/// ⛔ CAPTURE THREAD ONLY, deliberately. A unit-level `CPUSchedulingPolicy=fifo` would also
/// promote the PROCESS thread, and that thread runs the DFT — a long, bursty compute that
/// must not be allowed to preempt anything. This thread does almost no work per wakeup
/// (readi -> f64 -> push), which is exactly the profile RT is for.
///
/// ⚠ 20 mirrors `ndi-output.service`, and the reason there governs here: it preempts every
/// SCHED_OTHER process while staying far below PipeWire's audio-graph threads (FIFO 83-88),
/// so promoting this can never starve the graph it feeds.
///
/// Non-fatal by design: the bridge runs as root (CAP_SYS_NICE), but a future non-root move
/// or a missing RLIMIT_RTPRIO must degrade to the old SCHED_OTHER behaviour, not fail to
/// carry audio.
fn set_capture_thread_fifo() -> i32 {
    let prio = std::env::var("ARDFTSRC_CAP_RTPRIO")
        .ok()
        .and_then(|v| v.parse::<i32>().ok())
        .unwrap_or(CAP_RTPRIO_DEFAULT);
    if prio <= 0 {
        return 0;
    }
    // SAFETY: `param` is a fully-initialised sched_param; pthread_self() is always valid.
    let ok = unsafe {
        let param = libc::sched_param { sched_priority: prio };
        libc::pthread_setschedparam(libc::pthread_self(), libc::SCHED_FIFO, &param) == 0
    };
    if ok {
        prio
    } else {
        eprintln!(
            "ardftsrc-bridge: could not set capture thread SCHED_FIFO {prio} \
             (need CAP_SYS_NICE or LimitRTPRIO) — continuing at SCHED_OTHER"
        );
        0
    }
}

/// Write a full interleaved buffer, recovering from xruns, until all frames land.
fn write_all(io: &IO<i32>, pcm: &PCM, buf: &[i32], channels: usize) -> Result<(), alsa::Error> {
    let total = buf.len() / channels;
    let mut done = 0usize;
    while done < total {
        match io.writei(&buf[done * channels..]) {
            Ok(n) => done += n,
            Err(e) => {
                pcm.try_recover(e, true)?;
            }
        }
    }
    Ok(())
}

fn main() {
    let src = std::env::args().nth(1).unwrap_or_default();
    if src == "bluetooth" {
        eprintln!("ardftsrc-bridge: bluetooth audio path is deferred (S16 capture not implemented in v3)");
        std::process::exit(1);
    }
    let cfg = match source_cfg(&src) {
        Some(c) => c,
        None => {
            eprintln!("usage: ardftsrc-bridge <usb|lyrion|airplay|tidal|tv>");
            std::process::exit(1);
        }
    };

    // Resolve native rate. The I2S slave (eARC tap) has nothing to read it from, so it is
    // measured off frame delivery; every other source reads it from ALSA and retries while
    // the writer settles.
    let rate = if cfg.slave_measured {
        match measure_rate(cfg.in_device, cfg.channels) {
            Some(r) => r,
            None => {
                // Not a standard rate: refuse rather than resample against a bad estimate.
                // source_router re-probes on its own interval and restarts us.
                eprintln!("ardftsrc-bridge: {} rate not resolvable, aborting", cfg.card);
                std::process::exit(2);
            }
        }
    } else {
        let mut detected = None;
        for _ in 0..50 {
            if let Some(r) = detect_rate(cfg.card, cfg.hw_params) {
                detected = Some(r);
                break;
            }
            thread::sleep(Duration::from_millis(200));
        }
        match detected {
            Some(r) => r,
            None => {
                eprintln!("ardftsrc-bridge: {} rate unresolved after 10s, aborting", cfg.card);
                std::process::exit(2);
            }
        }
    };

    let channels = cfg.channels;

    // ── Output node: ALSA "pipewire" PCM with PIPEWIRE_PROPS (Gate 1 confirmed this
    //    creates source.<name>.ardftsrc, autoconnect off — source_router links it). ──
    std::env::set_var(
        "PIPEWIRE_PROPS",
        format!(
            "{{ node.name=source.{}.ardftsrc node.autoconnect=false \
             media.class=Stream/Output/Audio audio.channels={} audio.position={} }}",
            cfg.name, channels, cfg.position
        ),
    );
    let out_pcm = match open_pcm("pipewire", Direction::Playback, TARGET_RATE, channels as u32, OUT_PERIOD, OUT_BUFFER) {
        Ok(p) => p,
        Err(e) => {
            eprintln!("ardftsrc-bridge: failed to open 'pipewire' output PCM: {e}");
            std::process::exit(3);
        }
    };
    let out_io = match out_pcm.io_i32() {
        Ok(io) => io,
        Err(e) => {
            eprintln!("ardftsrc-bridge: output io_i32 failed: {e}");
            std::process::exit(3);
        }
    };
    // The output-cadence loop wakes on output space, so its granularity is the period the driver
    // actually GRANTED — not `OUT_PERIOD`, which `set_period_size_near` only ever REQUESTS. That
    // gap is already flagged in `open_pcm`, and hardcoding a frame count here would rebuild the
    // same untested assumption in the one place the loop now depends on it.
    let out_period_frames = out_pcm
        .hw_params_current()
        .and_then(|p| p.get_period_size())
        .map(|v| v.max(1) as usize)
        .unwrap_or(OUT_PERIOD as usize);
    // Smallest write worth waking for: half a granted period. Below this the loop would spin on
    // partial writes that barely shorten the queue, and each pass still costs an `avail_update`.
    let min_out_write = (out_period_frames / 2).max(1);

    // ── Resampler (2026-06-10: RealtimeResampler push/pull API, replacing the chunk API).
    //    The chunk API forced the process loop to hoard a full quality-sized chunk (~43 ms
    //    @ 44.1k) in the ring before every process call — measured as the dominant
    //    reducible term in the 145 ms bridge decomposition. RealtimeResampler accepts
    //    writes of any size and is drained as output becomes ready, so the ring holds only
    //    scheduling slack. ──
    // Built through a closure because the race reset below REBUILDS the resampler, and
    // `RealtimeResampler::new` consumes the config. Rebuilding from the same expression
    // avoids depending on the config type being Clone.
    let make_config = || {
        PRESET_GOOD
            .with_input_rate(rate as usize)
            .with_output_rate(TARGET_RATE as usize)
            .with_channels(channels)
    };
    let config = make_config();
    eprintln!(
        "ardftsrc-bridge: {} ({}) {}Hz S32 x{}ch -> {}Hz x{}ch (PRESET_GOOD: quality={}, bw={:.3}) in={}",
        cfg.name, cfg.card, rate, channels, TARGET_RATE, channels, config.quality, config.bandwidth, cfg.in_device
    );
    let mut rs = match RealtimeResampler::<f64>::new(config) {
        Ok(r) => r,
        Err(e) => {
            eprintln!("ardftsrc-bridge: resampler init failed: {e:?}");
            std::process::exit(3);
        }
    };
    // Priming ≈ 2 FFT blocks of input (interleaved samples); /2 ≈ one block = group delay.
    let prim_samples = rs.estimate_priming_samples();

    // ── Bounded ring (raw f64 interleaved). Capacity ≈ 0.25 s of source audio: covers the
    //    per-block DFT compute burst AND serves as the source-vs-graph drift reservoir
    //    (its size sets the flush interval under drift). Far smaller than v2's ~950 ms. ──
    // 🐛 `prim_samples` is INTERLEAVED SAMPLES; `rate / 4` is FRAMES. Comparing them made the
    // `max` pick 49392 at 44.1k/6ch and sized the ring 1.12 s — while the comment above claims
    // it is "far smaller than v2's ~950 ms". It was larger. Divide to frames first.
    let ring_cap = (rate as usize / 4).max(prim_samples / channels * 2) * channels;
    let rb = HeapRb::<f64>::new(ring_cap);
    let (mut prod, mut cons) = rb.split();

    let running = Arc::new(AtomicBool::new(true));

    // LATENCY DIAGNOSTIC (temporary, 2026-06-10): capture-side snd_pcm_delay, updated by
    // the capture thread after each read, sampled by the process loop's periodic report.
    let cap_delay_frames = Arc::new(AtomicI64::new(-1));
    let cap_frames = Arc::new(AtomicI64::new(0));

    // ── Capture thread: opens the input PCM itself (keeps the !Sync PCM thread-local),
    //    reads raw frames, converts to f64, pushes to the ring (drop on overflow). ──
    let cap_running = running.clone();
    let cap_device = cfg.in_device.to_string();
    let cap_channels = channels;
    let cap_delay_w = cap_delay_frames.clone();
    // Frames delivered since start — the watchdog's rate measurement reads this. Only ever
    // incremented by successful reads, so a stalled or clockless capture simply stops
    // advancing it, which reads as "no signal" rather than as a slower rate.
    let cap_frames_w = cap_frames.clone();
    // ⛔ `cap=` is `pcm.delay()` sampled AFTER readi returns, i.e. the residual left once we
    // have already drained a full buffer. It structurally cannot see the time spent WAITING
    // for those frames — and `readi` is called with a CAP_PERIOD-sized buffer on a blocking
    // PCM, so it waits for the whole quantum regardless of what period ALSA granted. That
    // wait is real capture latency and has never been measured. These two record it.
    let cap_readi_us = Arc::new(AtomicI64::new(0));
    let cap_readi_n = Arc::new(AtomicI64::new(0));
    let cap_readi_us_w = cap_readi_us.clone();
    let cap_readi_n_w = cap_readi_n.clone();
    // Stage 2 (mid-stream auto-switch): only the TV source can receive an IEC 61937
    // bitstream (the TV switches stereo<->Dolby with content). When that happens
    // this DFT resampler would emit noise, so the capture thread scans for the burst
    // preamble and clean-stops (exit 0) — source_router then re-probes and starts the
    // decode bridge. No other source ever sees IEC 61937, so the scan is gated off.
    //
    // Works unchanged on the eARC tap: the detector reads the preamble from bits 31..16,
    // which is where it lands BOTH for optical (plughw left-justifies S16->S32) and for
    // eARC (natively 24-bit left-justified in S32). On eARC the bitstream also arrives at
    // 192 kHz against this bridge's 48 kHz open, so reads overrun — that is fine and in
    // fact helps: the detector fires within ~60ms and we exit before the overruns matter.
    let cap_is_tv = cfg.name == "tv";
    let capture = thread::spawn(move || {
        // Promote BEFORE opening the device, so the very first readi — the one whose wakeup
        // phase the start draw is latched from — already runs at the priority the rest will.
        let cap_prio = set_capture_thread_fifo();
        eprintln!(
            "ardftsrc-bridge: capture thread {}",
            if cap_prio > 0 { format!("SCHED_FIFO {cap_prio}") } else { "SCHED_OTHER".into() }
        );
        let pcm = match open_pcm(&cap_device, Direction::Capture, rate, cap_channels as u32, CAP_PERIOD, CAP_BUFFER) {
            Ok(p) => p,
            Err(e) => {
                eprintln!("ardftsrc-bridge: failed to open capture {cap_device}: {e}");
                cap_running.store(false, Ordering::SeqCst);
                return;
            }
        };
        let io = match pcm.io_i32() {
            Ok(io) => io,
            Err(e) => {
                eprintln!("ardftsrc-bridge: capture io_i32 failed: {e}");
                cap_running.store(false, Ordering::SeqCst);
                return;
            }
        };
        let frames = CAP_PERIOD as usize;
        let mut raw = vec![0i32; frames * cap_channels];
        let mut f = vec![0.0f64; frames * cap_channels];
        // IEC 61937 detector state (TV only). A burst preamble Pa=0xF872,Pb=0x4E1F recurs
        // every 1536 frames for AC-3/DTS (3072 interleaved samples) but every 6144 frames
        // for E-AC-3 (12288 samples — DD+ repeats at 4x on its 192 kHz carrier). plughw
        // left-justifies S16->S32, so the words land in the high 16 bits. Require the pair
        // in 2 buffers within a 16384-sample window: one-off PCM coincidences are still
        // rejected (two exact 32-bit pairs in ~170 ms of real audio is ~2^-64 territory),
        // but a DD+ spacing now FITS. The old 5000 window predated the eARC tap (optical
        // only ever carried AC-3's 3072 spacing) and could never bracket two DD+ preambles
        // — the bridge then latched onto DD+ playing noise until an overrun happened to
        // drop the right span (observed stuck for 10+ min, 2026-08-01).
        let mut tv_syncs: u32 = 0;
        let mut tv_gap: usize = 0;
        // ⛔ Was 16384 INTERLEAVED SAMPLES, and the sizing argument above ("~170 ms of real
        // audio", DD+ spacing "12288 samples") assumed 2 samples per frame — a 2ch capture.
        // The eARC tap went 2 -> 6 -> 8 channels, so both the window and its time-equivalent
        // silently shrank 4x: 16384 samples is only 42.7 ms and 2048 frames at 8ch, against a
        // DD+ preamble spacing of 6144 FRAMES. `tv_syncs` therefore always reset before
        // reaching 2 and DD+ self-detection has been dead since the 8ch widening — the exact
        // "latched onto DD+ playing noise" failure the 5000 -> 16384 fix once cured, silently
        // un-fixed. In FRAMES it is channel-count-proof.
        const TV_SYNC_WINDOW_FRAMES: usize = 8192; // ~170 ms @48k, the original intent
        let tv_sync_window = TV_SYNC_WINDOW_FRAMES * cap_channels;
        while cap_running.load(Ordering::Relaxed) {
            let t_readi = Instant::now();
            match io.readi(&mut raw) {
                Ok(n) => {
                    cap_readi_us_w.store(t_readi.elapsed().as_micros() as i64, Ordering::Relaxed);
                    cap_readi_n_w.store(n as i64, Ordering::Relaxed);
                    let samples = n * cap_channels;
                    for i in 0..samples {
                        f[i] = i32_to_f64(raw[i]);
                    }
                    // push_slice returns how many it accepted; a short push == ring full
                    // == we drop the newest frames (rare drift/stall event, brief glitch).
                    let _ = prod.push_slice(&f[..samples]);
                    // LATENCY DIAGNOSTIC: frames still queued device-side after this read.
                    cap_delay_w.store(pcm.delay().unwrap_or(-1), Ordering::Relaxed);
                    cap_frames_w.fetch_add(n as i64, Ordering::Relaxed);

                    if cap_is_tv {
                        let mut found = false;
                        let mut k = 0;
                        while k + 1 < samples {
                            if ((raw[k] >> 16) as u16) == 0xF872
                                && ((raw[k + 1] >> 16) as u16) == 0x4E1F
                            {
                                found = true;
                                break;
                            }
                            k += 1;
                        }
                        if found {
                            tv_syncs += 1;
                            tv_gap = 0;
                        } else {
                            tv_gap += samples;
                            if tv_gap > tv_sync_window {
                                tv_syncs = 0;
                            }
                        }
                        if tv_syncs >= 2 {
                            eprintln!("ardftsrc-bridge: tv IEC 61937 bitstream detected — stopping for switch");
                            cap_running.store(false, Ordering::SeqCst);
                            return;
                        }
                    }
                }
                Err(e) => {
                    if pcm.try_recover(e, true).is_err() {
                        eprintln!("ardftsrc-bridge: capture unrecoverable: {e}, stopping");
                        cap_running.store(false, Ordering::SeqCst);
                        return;
                    }
                }
            }
        }
    });

    // ── Watchdog thread: source disconnect / rate change → clean stop (exit 0). ──
    let wd_running = running.clone();
    let wd_hw = cfg.hw_params;
    let wd_card = cfg.card.to_string();
    let wd_name = cfg.name.to_string();
    let wd_slave = cfg.slave_measured;
    let wd_frames = cap_frames.clone();
    let watchdog = thread::spawn(move || {
        // Rolling mark for the slave rate measurement, and the debounce state.
        let mut mark = (wd_frames.load(Ordering::Relaxed), Instant::now());
        let mut debounce = RateChangeDebounce::new(rate);

        while wd_running.load(Ordering::Relaxed) {
            thread::sleep(Duration::from_secs(1));
            if !wd_running.load(Ordering::Relaxed) {
                break;
            }
            // Slave sources: disconnect = card directory gone (no startup race, no
            // hw_params "no setup" false-positive). Others: the normal hw_params / amixer
            // probe.
            let disconnected = if wd_slave {
                !std::path::Path::new(&format!("/proc/asound/{wd_card}")).is_dir()
            } else {
                source_disconnected(&wd_card, wd_hw)
            };
            if disconnected {
                eprintln!("ardftsrc-bridge: {wd_name} source disconnected — stopping");
                wd_running.store(false, Ordering::SeqCst);
                break;
            }

            if wd_slave {
                // Measure over the window just elapsed. Both terms come from the same
                // interval, so a long scheduling delay cancels rather than skewing.
                let now_frames = wd_frames.load(Ordering::Relaxed);
                let now_t = Instant::now();
                let df = (now_frames - mark.0) as f64;
                let dt = now_t.duration_since(mark.1).as_secs_f64();
                mark = (now_frames, now_t);

                // snap_rate returns None for a stalled/clockless window (frames stop
                // advancing), and None is deliberately NOT evidence of a rate change —
                // it resets the debounce. This is what keeps a flapping eARC link, a bad
                // HDMI cable, or a ring overflow from walking us onto a wrong rate.
                let snapped = if dt > 0.0 { snap_rate(df / dt) } else { None };
                if let Some(r) = debounce.observe(snapped) {
                    eprintln!(
                        "ardftsrc-bridge: {wd_name} rate changed {rate}->{r} \
                         ({RATE_CHANGE_WINDOWS} consecutive windows) — restarting"
                    );
                    wd_running.store(false, Ordering::SeqCst);
                    break;
                }
            } else if let Some(r) = detect_rate(&wd_card, wd_hw) {
                if r != rate {
                    eprintln!("ardftsrc-bridge: {wd_name} rate changed {rate}->{r} — restarting");
                    wd_running.store(false, Ordering::SeqCst);
                    break;
                }
            }
        }
    });

    // ── Process loop: drive from the output side at the output cadence. ──
    // Each pass: wait for the output PCM to have at least `min_out_write` frames of space, ingest
    // whatever capture has produced, and write a BOUNDED chunk (never more than the space ALSA
    // already reports). Because a write never exceeds available space, `writei` never blocks for a
    // whole resampler chunk, so the input arriving during that block — which IS the ring depth,
    // ledger section 5b — never accumulates. Ring holds scheduling slack (~one period) instead of
    // ~39 ms of chunk at 48k.
    //
    // ⛔⛔ THE INGEST IS HIGH-WATER GATED, AND REMOVING THAT GATE SILENTLY DISABLES ALL THREE TRIMS.
    // Every trim below reads `cons.occupied_len()`. An ungated "pop whatever the ring holds" runs on
    // every pass INCLUDING the ones where the output is stalled and we are about to wait, so the ring
    // can never hold backlog: the startup one-shot finds nothing, the 8x stall guard never trips, and
    // `deep_frac` reads ~0 forever so the dead-band trim takes its "it genuinely drains" branch every
    // window. The backlog is not gone — it is in the resampler's output FIFO, which has NO drop path
    // at all, and `ringp50` cannot see it. The gate is a HIGH-water mark, never a low-water one: a
    // low-water mark ("only feed when ready is low") would hold ingest off for the several passes it
    // takes to drain one emitted block, pushing the queue straight back into the ring and giving up
    // the entire saving. A high-water mark is inactive in steady state by construction — so it cannot
    // confound the measurement — and during a stall it keeps backlog in the ring, where the existing,
    // thrice-iterated trims already work. ──
    let pop_cap = TRIM_UNIT as usize * channels * 4; // unchanged by the CAP_PERIOD A/B
    let mut in_buf = vec![0.0f64; pop_cap];
    let out_cap = out_period_frames * channels * 4;
    let mut out_f = vec![0.0f64; out_cap];
    let mut out_i = vec![0i32; out_cap];
    // Partial-frame remainder, in case read_samples() ever returns a count that is not a
    // whole number of frames (writei needs whole frames). Normally stays empty.
    let mut frame_carry: Vec<f64> = Vec::with_capacity(channels);

    // LATENCY DIAGNOSTIC (temporary, 2026-06-10): decompose the bridge's in-flight audio.
    // One line after the first block lands, then a report every 5 s.
    let group_ms = (prim_samples / 2 / channels) as f64 * 1000.0 / rate as f64;
    // One DFT block, expressed in OUTPUT frames — the unit the resampler emits in, and the unit
    // `num_samples_ready` counts. `estimate_priming_samples` is ~2 blocks of interleaved input.
    let block_out_frames =
        ((prim_samples / 2 / channels).max(1) as u64 * TARGET_RATE as u64 / rate as u64).max(1) as usize;
    // Ceiling on the resampler's output FIFO: ONE block plus one output period of slack.
    //
    // ⛔ Sized DOWN from a first attempt at three blocks, which was wrong in the way this whole file
    // keeps being wrong — it moved the reservoir instead of removing it. The FIFO has no drop path,
    // so every frame the gate permits is untrimmable latency: three blocks is ~117 ms at 48k, and an
    // unbounded pop on top of that reaches ~203 ms. Worse, it binds on EVERY BRIDGE START — the
    // startup one-shot below exists to drop the 2-5 capture periods (10.7-26.7 ms @48k) of
    // graph-link residue, and a ceiling that deep swallows all of it before the ring ever sees it,
    // so the trim drops nothing and the residue becomes permanent. The measurement protocol
    // restarts the bridge for every run, so that would have contaminated every number taken.
    // ⚠ That range read "43-107 ms" until 2026-09-03, computed at CAP_PERIOD 1024 and never
    // updated when it became 256 on 2026-08-19. The reset's TRIGGER never depended on it — it is
    // `out_frames_written > 2 * OUT_BUFFER`, in output frames — but the figure was being quoted as
    // the size of what the reset recovers, which is a different and 4x larger claim.
    //
    // Steady-state peak is exactly one block: the resampler emits a whole block at once and we drain
    // it over the following passes. One period of slack keeps the gate off the boundary. If it binds
    // for the odd pass in steady state that is the CORRECT outcome, not a defect — the excess lands
    // in the ring, which is the one place the trims can reach.
    let ingest_high_water = block_out_frames + INGEST_SLACK;
    // Setpoint for the drift regulator's TROUGH, in OUTPUT frames. See READY_BAND.
    let ready_target = out_period_frames;
    let t_start = Instant::now();
    // ── Phase-2 latency side-channel (stamp.rs) ──────────────────────────────
    // Publishes (cumulative out_frames, CLOCK_MONOTONIC completion time) per output
    // write to /dev/shm so the transmitter can difference stamp against read time.
    // Failure is non-fatal by design: one warning, audio path unaffected.
    let mut stamp = match stamp::StampWriter::open(&t_start) {
        Some(s) => Some(s),
        None => {
            eprintln!("ardftsrc-bridge: could not open {} — b2tx watermark disabled",
                      stamp::STAMP_PATH);
            None
        }
    };
    let mut first_write_done = false;
    // ── RACE RESET ──────────────────────────────────────────────────────────────────
    // Everything the bridge accumulates between capture starting and the graph actually
    // pulling is CONSERVED: input and output both run at real time, so whatever depth the
    // race leaves behind is permanent latency for the life of the instance (ledger §5b).
    // That is what redraws the latency at every start.
    //
    // ⛔ It cannot be trimmed out of the ring. Measured 2026-08-21 across four starts: at
    // the moment the old start-up trim fired, `ring=0f(0.0ms)` while `ready=4610f(48.0ms)`
    // — the audio had already been pushed through into the resampler's FIFO, which is an
    // unbounded `VecDeque` inside the crate with no shed API. A ring trim watches a
    // compartment the backlog has left. That is §5b's "there is no excess to trim", which
    // is a statement about REACHABILITY, not size.
    //
    // ⛔ It cannot be drained downstream either. Conserved upstream backlog does not
    // produce a downstream queue — it is a phase offset, not stored data. The NDI
    // transmitter's `drain_capture_backlog()` reports `availp10/50/90=0/0/0f` precisely
    // because there is nothing there to find.
    //
    // ⇒ The only thing conservation cannot defeat is ZEROING EVERY REACHABLE COMPARTMENT
    // AT ONE INSTANT, once the far end is provably awake. Clearing is shedding, not
    // moving, so unlike every buffer-size knob tried before it the audio does not
    // reappear one stage over. `out` is deliberately NOT cleared — see the reset block.
    let mut out_frames_written: i64 = 0;
    let mut race_reset_done = false;
    let mut over_since: Option<Instant> = None;
    // Ring FLOOR over a window — the statistic that separates slack from backlog. See
    // the trim block below for why the PEAK cannot.
    let mut floor_min = usize::MAX;
    let mut floor_since = Instant::now();
    // Latch + strike counter for the marginal band of the dead-band trim below. `armed`
    // clears on a trim and only re-arms once a window proves the ring can still drain, so
    // a ring that legitimately floors above `keep` gets exactly ONE trim per bridge start
    // rather than one every other window.
    let mut floor_armed = true;
    let mut floor_strikes = 0u32;
    let mut trim_bursts = 0u32;
    // Time-weighted, so the statistic does not depend on how often the loop happens to poll
    // (the empty branch spins at 500 us while the busy branch does real work per pass).
    // Time-weighted histogram of ring depth in units of `keep`, bucket k = "between k and
    // k+1 periods". ★ A histogram, not a scalar, because the ACTION needs a percentile too —
    // see the trim block. depth_hist[0] alone is the old `low` statistic.
    let mut depth_hist = [Duration::ZERO; DEPTH_BUCKETS + 1];
    // Fine time-weighted ring-depth distribution, in RING_HIST_UNIT frames. See the consts.
    let mut ring_hist = [Duration::ZERO; RING_HIST_BUCKETS + 1];
    // Fine time-weighted distribution of the RESAMPLER'S OUTPUT FIFO. Read as a pair with
    // ring_hist — see READY_HIST_UNIT for why neither means anything alone under this loop.
    let mut ready_hist = [Duration::ZERO; READY_HIST_BUCKETS + 1];
    // Time-weighted distribution of the ALSA CAPTURE ring's residual depth, in CAP_HIST_UNIT
    // input frames. The one compartment the race reset does not clear — see CAP_HIST_UNIT.
    let mut cap_hist = [Duration::ZERO; CAP_HIST_BUCKETS + 1];
    let mut window_time = Duration::ZERO;
    let mut last_poll = Instant::now();
    // One source of truth for the trim threshold: the in-loop `keep` binds to this, and the
    // low-time accumulator above the early-continue needs it before that binding exists.
    let keep_elems = TRIM_UNIT as usize * channels;
    let mut last_diag = Instant::now();
    // MEASURED resampler occupancy (2026-08-28). `rs~` above it is `group_ms`, a CONSTANT derived
    // once from prim_samples/rate — so `sum~` cannot show movement originating in the resampler,
    // which is exactly the term the ledger records as a +/-17 ms sawtooth standing behind a fixed
    // number. There is no occupancy API on RealtimeResampler, so derive it by conservation:
    // everything written in, minus everything read out, minus what is sitting ready, is what the
    // transform still holds. Counters are samples (interleaved), input at `rate`, output at
    // TARGET_RATE. Atomics only to keep the `diag` closure's immutable borrow legal.
    let rs_in_samples = AtomicU64::new(0);
    let rs_out_samples = AtomicU64::new(0);
    let diag = |tag: &str, cap_f: i64, ring_samples: usize, ready_samples: usize, out_f64: i64,
                floor_samples: usize, low_frac: f64| {
        let cap_ms = cap_f as f64 * 1000.0 / rate as f64;
        // The read quantum actually in force, and how long the capture thread blocked for it.
        // Mean wait for a frame to enter the ring is half this — the term `cap=` misses.
        let rq_n = cap_readi_n.load(Ordering::Relaxed);
        let rq_us = cap_readi_us.load(Ordering::Relaxed);
        let rq_ms = rq_n as f64 * 1000.0 / rate as f64;
        let ring_frames = ring_samples / channels;
        let ring_ms = ring_frames as f64 * 1000.0 / rate as f64;
        let ready_frames = ready_samples / channels;
        let ready_ms = ready_frames as f64 * 1000.0 / TARGET_RATE as f64;
        let out_ms = out_f64 as f64 * 1000.0 / TARGET_RATE as f64;
        let floor_frames = if floor_samples == usize::MAX { 0 } else { floor_samples / channels };
        // Share of the window so far spent with the ring below one capture period. The trim
        // acts on this, not on the floor — see LOW_FRAC_MAX.
        let low_pct = low_frac * 100.0;
        // Input samples -> their output-rate equivalent, minus what has left, minus what is ready.
        // Signed on purpose: a persistently negative value would falsify the conservation model
        // rather than hide behind a clamp.
        let rs_held_samples = rs_in_samples.load(Ordering::Relaxed) as f64
            * (TARGET_RATE as f64 / rate as f64)
            - rs_out_samples.load(Ordering::Relaxed) as f64
            - ready_samples as f64;
        let rsm_ms = rs_held_samples / channels as f64 * 1000.0 / TARGET_RATE as f64;
        eprintln!(
            "ardftsrc-bridge[diag {tag}]: cap={cap_f}f({cap_ms:.1}ms) readq={rq_n}f({rq_ms:.1}ms,blocked {rq_us}us) \
             ring={ring_frames}f({ring_ms:.1}ms) \
             floor={floor_frames}f low={low_pct:.0}% \
             rs~{group_ms:.1}ms rsm~{rsm_ms:.1}ms ready={ready_frames}f({ready_ms:.1}ms) out={out_f64}f({out_ms:.1}ms) \
             sum~{:.1}ms summ~{:.1}ms",
            cap_ms + rq_ms / 2.0 + ring_ms + group_ms + ready_ms + out_ms,
            cap_ms + rq_ms / 2.0 + ring_ms + rsm_ms + ready_ms + out_ms
        );
    };

    'process: while running.load(Ordering::Relaxed) {
        let avail = cons.occupied_len();
        // Sampled at the TOP, before the trim and before the ingest, on purpose: a healthy ring
        // alternates 0 <-> one period and its floor IS the zero, so skipping the empty passes
        // would make every state look permanently backlogged.
        floor_min = floor_min.min(avail);
        // One query per pass, reused by the histogram AND by the ingest gate below — nothing
        // between here and there touches the resampler.
        let ready_frames_now = rs.num_samples_ready() / channels;
        {
            let now = Instant::now();
            let dt = now.duration_since(last_poll);
            last_poll = now;
            window_time += dt;
            depth_hist[(avail / keep_elems).min(DEPTH_BUCKETS)] += dt;
            ring_hist[(avail / channels / RING_HIST_UNIT).min(RING_HIST_BUCKETS)] += dt;
            ready_hist[(ready_frames_now / READY_HIST_UNIT).min(READY_HIST_BUCKETS)] += dt;
            // `cap_delay_frames` is -1 until the first successful readi; a negative depth is
            // "not yet known", not zero, so it must not be folded in as an empty bucket.
            let cap_now = cap_delay_frames.load(Ordering::Relaxed);
            if cap_now >= 0 {
                cap_hist[(cap_now as usize / CAP_HIST_UNIT).min(CAP_HIST_BUCKETS)] += dt;
            }
        }

        // ── Backlog trims. In-flight audio is CONSERVED in this pipeline (input and
        //    output both run at real-time rate), so backlog accumulated during ANY output
        //    stall is PERMANENT latency once flow resumes. Each trim costs one brief
        //    audible skip instead of a constant extra delay.
        //
        //    Everything from here down handles STALLS DURING STEADY RUNNING. The START-UP
        //    transient is not handled here at all — see the race reset at the bottom of the
        //    loop. A start-up one-shot used to live in this chain and was removed
        //    2026-08-21: it read `avail` (the ring), and the ring is measurably EMPTY at
        //    start-up while the backlog sits in the resampler's FIFO, so it could only ever
        //    trim zero. Do not re-add a ring-based start-up trim.
        //
        //    (1) sustained stall guard at 8x keep: catches big stalls — a pipewire/stack
        //        restart pinned the ring at ~557 ms live 2026-06-10. Normal post-trim
        //        slack re-locks at 2-4 periods (resampler chunk quantization), so 8x
        //        with the sustained-1s filter never trips in steady state;
        //    (2) the dead-band trim further down, for backlog that never drains. ──
        let keep = keep_elems;
        let mut trim_to_keep = false;
        let mut trim_tag = "stall";
        // The window's floor, captured for the p10 trim's ACTION (see the trim block).
        let mut trim_floor = 0usize;
        if first_write_done && avail > keep * 8 {
            match over_since {
                None => over_since = Some(Instant::now()),
                Some(t) if t.elapsed() >= Duration::from_secs(1) => {
                    over_since = None;
                    trim_to_keep = true;
                    trim_tag = "stall";
                }
                Some(_) => {}
            }
        } else {
            over_since = None;
        }

        //    (3) dead-band trim, on the ring's FLOOR rather than its peak. (2) watches the
        //        MAXIMUM, and every ratchet ever measured sat at 1024-4096 frames — 2-8x
        //        BELOW its 8x threshold and permanent there (docs/latency-matrix-plan.md
        //        section 20, where it cost ~86 ms of the Pi's ~128 ms path). Raising (2)'s
        //        sensitivity is the wrong fix: the 8x exists so it cannot false-trip, and
        //        lowering it re-creates exactly that risk.
        //        What separates the two states is the MINIMUM. Measured:
        //            clean     ring alternates 0 <-> 1024 frames  -> floor 0
        //            ratcheted ring sits      2048..4096 frames   -> floor 2048
        //        Genuine slack drains to nominal constantly; backlog never comes down. So
        //        trim only what the ring provably never consumed — the floor over a long
        //        window — and require two full periods resident before acting, which the
        //        clean state never reaches. Each trim costs one brief audible skip, so this
        //        must fire once per stall event, not continuously: the window resets after
        //        every trim, and post-trim the floor returns to 0. ──
        //        ★ 2026-08-18 (later): `keep * 2` was ONE PERIOD TOO HIGH for the ratchet
        //        actually seen in the field. Measured live on @lyrion: ring pinned at
        //        2048 frames, sum~148 ms against a clean ~105 — and `floor=` bouncing
        //        1024 <-> 2048, so any window containing a 1024 sample held the trim off
        //        and the backlog stayed permanent. The clean state floors at 0, so `keep`
        //        already carries the full margin; what `keep * 2` bought was not margin
        //        but insensitivity. Two bands now:
        //            floor >= keep*2  -> trim after ONE window   (the shipped behaviour,
        //                                unchanged: deep backlog needs no confirmation)
        //            floor >= keep    -> trim after TWO windows  (new: the marginal band,
        //                                confirmed before acting)
        //        and the marginal band is LATCHED. A source whose ring legitimately floors
        //        above `keep` forever would otherwise be trimmed every other window, one
        //        audible skip each; with the latch it is trimmed once and then needs a
        //        window that drops below `keep` — proof the ring can still drain — before
        //        it can fire again. That is what makes this once-per-stall rather than a
        //        periodic tick, which is the property the 8x guard was protecting. ──
        if !trim_to_keep && first_write_done && floor_since.elapsed() >= FLOOR_WINDOW {
            // How many whole periods the ring holds for all but LOW_FRAC_MAX of the window.
            // ⛔⛔ This replaces BOTH the floor-as-statistic and the floor-as-action, and it
            // replaces them for the SAME reason: a momentary touch of zero destroys a
            // minimum. Measured live — in the ratcheted state the ring dips empty for 1–2%
            // of the window, so `floor` reads 0 while the ring genuinely holds a full period
            // 98% of the time. The floor-based ACTION therefore dropped 0f three times in a
            // row and the path stayed ratcheted (plan section 28c). A percentile survives the
            // dip; a minimum cannot.
            let total = window_time.as_secs_f64();
            let mut persistent = 0usize; // in units of `keep`
            if total > 0.0 {
                let mut cum = 0.0;
                for k in 0..=DEPTH_BUCKETS {
                    cum += depth_hist[k].as_secs_f64();
                    if cum / total >= LOW_FRAC_MAX {
                        break;
                    }
                    persistent = k + 1;
                }
            }
            // Time-weighted share of the window spent holding TWO OR MORE whole periods.
            // Buckets 2.. are exactly that, so this needs no percentile walk and no
            // sensitivity to bucket 0 — see DEEP_FRAC_MIN.
            let deep_frac = if total > 0.0 {
                depth_hist[2..].iter().map(|d| d.as_secs_f64()).sum::<f64>() / total
            } else {
                0.0
            };
            if floor_min >= keep * 2 {
                // Deep backlog — the shipped guard, unchanged, no confirmation needed.
                trim_to_keep = true;
                trim_tag = "deadband";
            } else if deep_frac >= DEEP_FRAC_MIN {
                // The ring held at least one whole period for all but LOW_FRAC_MAX of the
                // window — that is backlog, however often it momentarily touches empty.
                if floor_armed {
                    floor_strikes += 1;
                    if floor_strikes >= 2 {
                        trim_to_keep = true;
                        trim_tag = "deadband-deep";
                        // Drop ONE period: the excess over a healthy source (which sits at
                        // one period) is one period. If more is owed, the burst logic below
                        // fires again next window rather than over-trimming on one guess.
                        trim_floor = keep;
                    }
                }
            } else {
                // It genuinely drains — re-arm and forget the burst count.
                floor_armed = true;
                floor_strikes = 0;
                trim_bursts = 0;
            }
            // ★ 2026-08-18: log the WINDOW-CLOSE DECISION, not just the 5 s samples.
            // Three iterations of this trim were designed by inferring the window state from
            // `diag 5s` lines — but those are sampled mid-window, so `low=` there is a
            // partial cumulative figure and NOT the value the trim actually tested. Two of
            // the three iterations were wrong, and this is the missing instrument: it prints
            // exactly what the decision saw. Do not tune LOW_FRAC_MAX or the strike rule
            // from `diag 5s` again.
            let hist_pct: Vec<String> = depth_hist
                .iter()
                .take(4)
                .map(|d| format!("{:.0}", if total > 0.0 { d.as_secs_f64() / total * 100.0 } else { 0.0 }))
                .collect();
            // ★ The fine ring distribution, in FRAMES and in ms. This is the statistic to read
            // — `ring=` on the 5 s line is a point sample of a sawtooth and p50 is not it.
            // p10/p90 together show the sawtooth's SHAPE: a ring alternating 0<->2048 and one
            // parked at 1024 share a mean but differ completely here.
            let (p10, p50, p90) = (
                ring_pct(&ring_hist, RING_HIST_UNIT, 0.10),
                ring_pct(&ring_hist, RING_HIST_UNIT, 0.50),
                ring_pct(&ring_hist, RING_HIST_UNIT, 0.90),
            );
            let f2ms = |f: usize| f as f64 * 1000.0 / rate as f64;
            // ★ The resampler's output FIFO over the SAME window. Under the output-cadence loop this
            // is the only other place a chunk can sit, so a fall in `ringp50` is only a real saving
            // if `readyp50` holds — see READY_HIST_UNIT. Reported here and not only on the 5 s line
            // because ledger section 6.1 forbids attributing from a mid-window point sample.
            let (rdy10, rdy50, rdy90) = (
                ring_pct(&ready_hist, READY_HIST_UNIT, 0.10),
                ring_pct(&ready_hist, READY_HIST_UNIT, 0.50),
                ring_pct(&ready_hist, READY_HIST_UNIT, 0.90),
            );
            let o2ms = |f: usize| f as f64 * 1000.0 / TARGET_RATE as f64;
            // ★ The ALSA capture ring over the SAME window — the compartment the race reset
            // leaves alone. `capp10` is the TROUGH, i.e. the part no read ever takes, i.e. the
            // standing depth a capture-side clear would actually shed; `capp90 - capp10` is
            // just the read quantum's sawtooth and sheds nothing. ⛔ Read the trough, not the
            // fill — that distinction is what §7.9 cost a run to learn.
            let (cap10, cap50, cap90) = (
                ring_pct(&cap_hist, CAP_HIST_UNIT, 0.10),
                ring_pct(&cap_hist, CAP_HIST_UNIT, 0.50),
                ring_pct(&cap_hist, CAP_HIST_UNIT, 0.90),
            );
            eprintln!(
                "ardftsrc-bridge[diag window]: low={:.1}% deep={:.1}% persistent={persistent}                  strikes={floor_strikes} armed={floor_armed} bursts={trim_bursts}                  hist%=[{}] capp10/50/90={cap10}/{cap50}/{cap90}f({:.1}/{:.1}/{:.1}ms)                  ringp10/50/90={p10}/{p50}/{p90}f({:.1}/{:.1}/{:.1}ms)                  readyp10/50/90={rdy10}/{rdy50}/{rdy90}f({:.1}/{:.1}/{:.1}ms) trim={}",
                if total > 0.0 { depth_hist[0].as_secs_f64() / total * 100.0 } else { 100.0 },
                deep_frac * 100.0,
                hist_pct.join(","),
                f2ms(cap10), f2ms(cap50), f2ms(cap90),
                f2ms(p10), f2ms(p50), f2ms(p90),
                o2ms(rdy10), o2ms(rdy50), o2ms(rdy90),
                if trim_to_keep { trim_tag } else { "-" }
            );

            if trim_to_keep {
                floor_strikes = 0;
                // ★ Do NOT disarm after a single trim. Dropping the floor is provably
                // productive, but ONE drop only removes one window's worth of never-consumed
                // backlog — a deeper ratchet needs several. Disarming after the first left the
                // path stuck (measured, plan section 28b: it fired once, dropped nothing, and
                // could never re-arm because `low` stays 0% while backlogged). Bound the burst
                // instead: up to 3 consecutive trims, then insist on a window that genuinely
                // drains before arming again, so a pathological source cannot skip forever.
                trim_bursts += 1;
                if trim_bursts >= 3 {
                    floor_armed = false;
                }
            }

            // ── THE DRIFT REGULATOR. Holds the resampler FIFO's TROUGH at one output period. ──
            // See READY_BAND for the measurement and for why the ring trims above cannot do this
            // job: they watch a compartment the drift no longer reaches.
            //
            // Acts on `rdy10`, so it only ever moves the STANDING level. The peaks are the DFT
            // block in flight and trimming those would starve the writei loop — the same p10-never-
            // max rule the bitstream regulator and the NDI transmitter's regulators both landed on.
            //
            // ⚠ Deliberately NOT gated on `!trim_to_keep`'s outcome the way the block it sits in
            // is: it is an independent fault in an independent compartment. But it does sit INSIDE
            // that block, so a window that fires a ring trim skips this one — never double-splice
            // two faults in one window.
            // The trough is a WINDOW statistic and this fires at one instant on the sawtooth,
            // which may be below it — hence the cap at what is actually there.
            let drift_drop = drift_drop_frames(
                rdy10, ready_target, READY_BAND, rs.num_samples_ready() / channels);
            if race_reset_done && drift_drop > 0 {
                // Whole frames only — a partial frame rotates the channel mapping, exactly as on
                // the ring's drop path.
                let mut left = drift_drop * channels;
                let mut dropped = 0usize;
                // `out_f` is dead between the window close and the write below, so it doubles as
                // the discard buffer rather than carrying a second 200 kB allocation for life.
                while left > 0 {
                    match rs.read_samples(&mut out_f[..left.min(out_cap)]) {
                        Some(0) | None => break,
                        Some(n) => {
                            dropped += n;
                            left -= n.min(left);
                        }
                    }
                }
                // Conservation: `rsm~` is derived from in-minus-out, so a discard that skips this
                // counter shows up forever after as phantom occupancy inside the transform.
                rs_out_samples.fetch_add(dropped as u64, Ordering::Relaxed);
                eprintln!(
                    "ardftsrc-bridge[diag drift]: ready trough {rdy10}f({:.1}ms) over setpoint \
                     {ready_target}f({:.1}ms) — shed {}f({:.1}ms) of standing drift",
                    o2ms(rdy10),
                    o2ms(ready_target),
                    dropped / channels,
                    o2ms(dropped / channels),
                );
            }

            floor_min = usize::MAX;
            depth_hist = [Duration::ZERO; DEPTH_BUCKETS + 1];
            ring_hist = [Duration::ZERO; RING_HIST_BUCKETS + 1];
            ready_hist = [Duration::ZERO; READY_HIST_BUCKETS + 1];
            cap_hist = [Duration::ZERO; CAP_HIST_BUCKETS + 1];
            window_time = Duration::ZERO;
            floor_since = Instant::now();
        }

        if trim_to_keep {
            floor_min = usize::MAX;
            floor_since = Instant::now();
            // ⛔⛔ THE TARGET IS NOT ALWAYS `keep`. Trimming down to `keep` is right for the
            // startup and deep-stall cases, where the ring is far above it. It is USELESS for
            // the dead-band case, and this cost a run: the ratcheted ring oscillates between
            // `keep` and 2*keep, so a trim landing on the low phase computes `avail - keep`
            // = 0 and drops nothing — logged live as `dropped 0f (0.0ms) of deadband-p10`.
            // What is actually backlogged there is the FLOOR: the part the ring provably never
            // consumed all window. So the dead-band trim drops the floor, which is exactly the
            // quantity its statistic identified, and leaves the oscillation intact.
            // saturating_sub because `avail` is instantaneous and can sit BELOW `keep` at the
            // moment the window closes — plain subtraction wraps in release and drains the
            // whole ring.
            let mut drop_left = if trim_floor > 0 {
                // `trim_floor` is the PERSISTENT depth (whole periods held for all but
                // LOW_FRAC_MAX of the window), not the floor and not the instantaneous
                // excess. Capped at `avail` because the trim can land on the low phase of
                // the oscillation — that cap is why this drops something at all.
                trim_floor.min(avail)
            } else {
                avail.saturating_sub(keep)
            };
            // Whole frames only — a partial frame would rotate the channel mapping.
            drop_left -= drop_left % channels;
            let dropped = drop_left;
            while drop_left > 0 {
                let got = cons.pop_slice(&mut in_buf[..drop_left.min(pop_cap)]);
                if got == 0 {
                    break;
                }
                drop_left -= got;
            }
            eprintln!(
                "ardftsrc-bridge[diag trim]: dropped {}f ({:.1}ms) of {trim_tag} backlog",
                dropped / channels,
                (dropped / channels) as f64 * 1000.0 / rate as f64
            );
        }

        // Ingest captured samples into the resampler, BOUNDED by the FIFO's remaining headroom.
        // Anything that does not fit stays in the ring, where the trims above can reach it. See the
        // loop header before touching this, and `ingest_high_water` for why the bound is not just
        // the gate: a boolean gate with an unbounded pop overshoots the ceiling by a whole
        // `pop_cap` (4096 input frames = 8192 output frames at 48k -> 96k), which is most of the
        // reservoir the ceiling was chosen to prevent.
        let ring_avail = cons.occupied_len();
        let headroom_out = ingest_high_water.saturating_sub(ready_frames_now);
        // Output-frame headroom -> input elements. Rounded DOWN, and to whole frames: a partial
        // frame would rotate the channel mapping on the trim's drop path.
        let mut take = (headroom_out as u64 * rate as u64 / TARGET_RATE as u64) as usize * channels;
        take = take.min(pop_cap).min(ring_avail);
        take -= take % channels;
        if take > 0 {
            let n = cons.pop_slice(&mut in_buf[..take]);
            if n > 0 {
                rs_in_samples.fetch_add(n as u64, Ordering::Relaxed);
                if let Err(e) = rs.write_samples(&in_buf[..n]) {
                    eprintln!("ardftsrc-bridge: write_samples failed: {e:?}");
                    running.store(false, Ordering::SeqCst);
                    break 'process;
                }
            }
        }

        // If resampler has no output frames ready, yield briefly and wait for more capture input.
        let ready_samples = rs.num_samples_ready();
        if ready_samples < channels {
            thread::sleep(Duration::from_micros(500));
            continue 'process;
        }

        // Query available playback buffer space in the output PCM.
        let avail_frames = match out_pcm.avail_update() {
            Ok(f) => f.max(0) as usize,
            Err(e) => {
                if out_pcm.try_recover(e, true).is_err() {
                    eprintln!("ardftsrc-bridge: output PCM unrecoverable: {e}");
                    running.store(false, Ordering::SeqCst);
                    break 'process;
                }
                0
            }
        };

        if avail_frames < min_out_write {
            // ⚠ `PCM::wait` takes MILLISECONDS (alsa 0.9 `Option<u32>`), not a Duration.
            // The timeout is a liveness backstop only — a stalled graph must not wedge the loop,
            // because the trims above run at the top of it.
            if let Err(e) = out_pcm.wait(Some(10)) {
                let _ = out_pcm.try_recover(e, true);
            }
            continue 'process;
        }

        let ready_frames = ready_samples / channels;
        let target_frames = out_chunk_frames(avail_frames, out_period_frames, ready_frames);
        // Unreachable as the guards above stand (`avail_frames >= min_out_write >= 1` and
        // `ready_frames >= 1`), and kept only so a future change to either guard degrades into a
        // sleep rather than a zero-length `writei`. Every other path through this loop either
        // writes, sleeps, or waits on the PCM — there is no busy spin.
        if target_frames == 0 {
            thread::sleep(Duration::from_micros(500));
            continue 'process;
        }

        let target_samples = target_frames * channels;
        let w = match rs.read_samples(&mut out_f[..target_samples]) {
            Some(w) => w,
            // None = finalized, which this loop never causes (`finalize` is never called). Treating
            // it as "nothing to write" would spin this branch forever with no sleep and no wait.
            None => {
                eprintln!("ardftsrc-bridge: resampler finalized unexpectedly — stopping");
                running.store(false, Ordering::SeqCst);
                break 'process;
            }
        };
        rs_out_samples.fetch_add(w as u64, Ordering::Relaxed);
        if w == 0 {
            thread::sleep(Duration::from_micros(500));
            continue 'process;
        }

        let mut wrote_any = false;
        // Complete a pending partial frame first, then write the whole-frame body.
        let mut start = 0usize;
        if !frame_carry.is_empty() {
            let need = channels - frame_carry.len();
            let take = need.min(w);
            frame_carry.extend_from_slice(&out_f[..take]);
            start = take;
            if frame_carry.len() == channels {
                for (i, &x) in frame_carry.iter().enumerate() {
                    out_i[i] = f64_to_i32(x);
                }
                if let Err(e) = write_all(&out_io, &out_pcm, &out_i[..channels], channels) {
                    eprintln!("ardftsrc-bridge: output write failed: {e}");
                    running.store(false, Ordering::SeqCst);
                    break 'process;
                }
                wrote_any = true;
                out_frames_written += 1;
                frame_carry.clear();
            }
        }
        let body = &out_f[start..w];
        let whole = (body.len() / channels) * channels;
        for i in 0..whole {
            out_i[i] = f64_to_i32(body[i]);
        }
        if whole > 0 {
            if let Err(e) = write_all(&out_io, &out_pcm, &out_i[..whole], channels) {
                eprintln!("ardftsrc-bridge: output write failed: {e}");
                running.store(false, Ordering::SeqCst);
                break 'process;
            }
            wrote_any = true;
            out_frames_written += (whole / channels) as i64;
        }
        frame_carry.extend_from_slice(&body[whole..]);

        // Watermark AFTER the writes, so the stamp says "this much audio is in the
        // pipe as of NOW". Disabled permanently on any write failure (one warning).
        if wrote_any {
            if let Some(st) = stamp.as_mut() {
                if !st.update(out_frames_written) {
                    eprintln!("ardftsrc-bridge: stamp write failed — b2tx watermark disabled");
                    stamp = None;
                }
            }
        }

        if wrote_any && !first_write_done {
            first_write_done = true;
            eprintln!(
                "ardftsrc-bridge[diag startup]: first block written at +{:.0}ms after exec; \
                 block={block_out_frames}f gate={ingest_high_water}f({:.1}ms) \
                 drift setpoint={ready_target}f({:.1}ms) band={READY_BAND}f",
                t_start.elapsed().as_secs_f64() * 1000.0,
                ingest_high_water as f64 * 1000.0 / TARGET_RATE as f64,
                ready_target as f64 * 1000.0 / TARGET_RATE as f64,
            );
            diag(
                "startup",
                cap_delay_frames.load(Ordering::Relaxed),
                cons.occupied_len(),
                rs.num_samples_ready(),
                out_pcm.delay().unwrap_or(-1),
                floor_min,
                if window_time.is_zero() { 1.0 } else { depth_hist[0].as_secs_f64() / window_time.as_secs_f64() },
            );
        } else if first_write_done && last_diag.elapsed() >= Duration::from_secs(5) {
            last_diag = Instant::now();
            diag(
                "5s",
                cap_delay_frames.load(Ordering::Relaxed),
                cons.occupied_len(),
                rs.num_samples_ready(),
                out_pcm.delay().unwrap_or(-1),
                floor_min,
                if window_time.is_zero() { 1.0 } else { depth_hist[0].as_secs_f64() / window_time.as_secs_f64() },
            );
        }

        // ── THE RACE RESET. See the declaration of `race_reset_done` for why this is a
        //    simultaneous clear rather than a trim of any single compartment. ──
        //
        // TRIGGER: the consumer must be PROVEN to be draining, not assumed. Frames merely
        // handed to the output PCM are still sitting in its buffer, so `out_frames_written`
        // only means something once it exceeds what that buffer holds by itself. Same shape
        // as `pcm_backlog_trim.py`'s `written > 2 * pipe_capacity` test, and for the same
        // reason: before that point a "successful write" proves nothing about the far end.
        if !race_reset_done && first_write_done && out_frames_written > 2 * OUT_BUFFER {
            race_reset_done = true;
            let pre_ring = cons.occupied_len();
            let pre_ready = rs.num_samples_ready();
            // ⚠ Sampled, NOT cleared. This is the whole open question: the reset zeroes every
            // other reachable compartment at one instant, and this one is left holding whatever
            // it holds. One point sample is not the answer — `capp10` on the window lines either
            // side of this is — but printing it here at least dates the sample to the reset.
            let pre_cap = cap_delay_frames.load(Ordering::Relaxed);

            // 1. ring -> 0.
            loop {
                let got = cons.pop_slice(&mut in_buf[..pop_cap]);
                if got == 0 {
                    break;
                }
            }
            // 2. resampler FIFO + its half-accumulated chunk -> 0. There is no shed API on
            //    `samples_pending_output`, so the whole resampler is rebuilt. This discards
            //    its priming, which costs one chunk (~39 ms @48k) of audio at the reset —
            //    on a path that is already discontinuous at start-up.
            // The rebuild discards the resampler's contents, so the conservation counters have to
            // go with it — otherwise every subsequent `rsm~` is offset by the priming just dropped.
            rs_in_samples.store(0, Ordering::Relaxed);
            rs_out_samples.store(0, Ordering::Relaxed);
            rs = match RealtimeResampler::<f64>::new(make_config()) {
                Ok(r) => r,
                Err(e) => {
                    eprintln!("ardftsrc-bridge: resampler rebuild at race reset failed: {e:?}");
                    running.store(false, Ordering::SeqCst);
                    break 'process;
                }
            };
            // 3. any partial frame straddling the reset would rotate the channel mapping.
            frame_carry.clear();
            // ⛔ `out` is deliberately NOT cleared. It is bounded by OUT_BUFFER and the
            //    blocking write keeps it near full BY DESIGN — that is the working buffer
            //    feeding the consumer, not race residue, and dropping it would underrun
            //    immediately. It is also the `pipewire` ALSA plugin, i.e. this bridge's
            //    graph node: `snd_pcm_drop`/`prepare` there risks tearing the node down and
            //    making `source_router` re-link, which is the very race being closed.

            // Post-reset the window statistics describe a pipeline that no longer exists,
            // and the dead-band trim must not act on them. Restart every accumulator.
            floor_min = usize::MAX;
            floor_since = Instant::now();
            depth_hist = [Duration::ZERO; DEPTH_BUCKETS + 1];
            ring_hist = [Duration::ZERO; RING_HIST_BUCKETS + 1];
            ready_hist = [Duration::ZERO; READY_HIST_BUCKETS + 1];
            cap_hist = [Duration::ZERO; CAP_HIST_BUCKETS + 1];
            window_time = Duration::ZERO;
            last_poll = Instant::now();
            floor_armed = true;
            floor_strikes = 0;
            trim_bursts = 0;

            eprintln!(
                "ardftsrc-bridge[diag race-reset]: consumer proven draining at {}f written; \
                 shed ring={}f({:.1}ms) ready={}f({:.1}ms) out=KEPT {}f cap=NOT-CLEARED {}f({:.1}ms)",
                out_frames_written,
                pre_ring / channels,
                (pre_ring / channels) as f64 * 1000.0 / rate as f64,
                pre_ready / channels,
                (pre_ready / channels) as f64 * 1000.0 / TARGET_RATE as f64,
                out_pcm.delay().unwrap_or(-1),
                pre_cap,
                if pre_cap < 0 { 0.0 } else { pre_cap as f64 * 1000.0 / rate as f64 },
            );
        }
    }

    running.store(false, Ordering::SeqCst);
    // Do NOT join the capture thread: on a rate change the source is still ALSA-active and
    // snd_pcm_readi can block uninterruptibly, so join() could hang forever. Detach both
    // handles and exit directly; the OS reaps the threads and closes the PCMs. exit 0 =
    // watchdog-clean stop (disconnect/rate-change) — systemd + source_router restart on the
    // next ALSA-active poll, mirroring the v2 bash.
    drop((capture, watchdog));
    std::process::exit(0);
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The distinction the fine histogram exists to make: a ring PARKED at one depth and a ring
    /// OSCILLATING around it have the same mean, and `ring=` on the 5 s line cannot tell them
    /// apart because it samples one point of the sawtooth. p10/p90 separate them.
    #[test]
    fn ring_pct_separates_a_parked_ring_from_an_oscillating_one() {
        let unit = RING_HIST_UNIT; // 128 frames
        let mut parked = [Duration::ZERO; RING_HIST_BUCKETS + 1];
        parked[8] = Duration::from_secs(10); // all 10 s between 1024 and 1151 frames
        let mut swinging = [Duration::ZERO; RING_HIST_BUCKETS + 1];
        swinging[0] = Duration::from_secs(5); // half the window empty
        swinging[16] = Duration::from_secs(5); // half at ~2048

        // Same p50 bucket region is NOT the point — the spread is.
        assert_eq!(ring_pct(&parked, unit, 0.10), 8 * unit + unit / 2);
        assert_eq!(ring_pct(&parked, unit, 0.90), 8 * unit + unit / 2);
        assert_eq!(ring_pct(&swinging, unit, 0.10), unit / 2);
        assert_eq!(ring_pct(&swinging, unit, 0.90), 16 * unit + unit / 2);
    }

    /// The `@tv` case the trim is blind to: parked at 1792 frames = 1.75 TRIM_UNITs. The fine
    /// statistic must report it plainly, which is the whole reason it exists.
    #[test]
    fn ring_pct_sees_the_tv_parked_depth_the_trim_misses() {
        let mut tv = [Duration::ZERO; RING_HIST_BUCKETS + 1];
        tv[1792 / RING_HIST_UNIT] = Duration::from_secs(10);
        let p50 = ring_pct(&tv, RING_HIST_UNIT, 0.50);
        assert!((1792..1792 + RING_HIST_UNIT).contains(&p50), "p50 was {p50}");
        // …while the trim's own bucketing rounds it down to 1 whole TRIM_UNIT, i.e. "not deep".
        assert_eq!(1792 / TRIM_UNIT as usize, 1);
    }

    /// The regulator must be silent in the band. A per-window splicer is the failure mode that
    /// makes this unshippable on an appliance someone is listening to.
    #[test]
    fn drift_regulator_is_silent_inside_the_band() {
        let (target, band) = (1024, READY_BAND);
        for trough in [0, 64, 192, 1024, 1088, 1152] {
            assert_eq!(drift_drop_frames(trough, target, band, 8192), 0, "trough {trough}");
        }
    }

    /// Downward only — a trough BELOW setpoint must never provoke a re-prime. `pcm_backlog_trim`
    /// learned this the expensive way (the 2026-08-25 dry/re-prime oscillation).
    #[test]
    fn drift_regulator_never_raises() {
        assert_eq!(drift_drop_frames(64, 1024, READY_BAND, 8192), 0);
    }

    /// The quantization IS the hysteresis: `ring_pct` reports bucket midpoints, so the first
    /// trough that can trigger is one bucket above the band, and shedding `trough - target` lands
    /// the next window's p10 back inside it. Without this the regulator fires every window.
    #[test]
    fn drift_regulator_lands_back_inside_the_band() {
        let (target, band) = (1024, READY_BAND); // band = 128 f = one READY_HIST_UNIT bucket
        // Trigger threshold is 1152; midpoints run 64, 192, ... so 1216 (bucket 9) is the first.
        assert_eq!(drift_drop_frames(1152, target, band, 8192), 0, "at the threshold, silent");
        let drop = drift_drop_frames(1216, target, band, 8192);
        assert_eq!(drop, 192);
        // Post-trim the trough sits at ~target, which reports as bucket 8 (midpoint 1088).
        assert_eq!(drift_drop_frames(1216 - drop + 64, target, band, 8192), 0);
    }

    /// The measured live case: `@usb` at 42 min, trough 3904 f against a 1024 f setpoint.
    #[test]
    fn drift_regulator_sheds_the_measured_ratchet() {
        assert_eq!(drift_drop_frames(3904, 1024, READY_BAND, 8192), 2880);
    }

    /// The window statistic and the instant of the trim are different points on the sawtooth, so
    /// the FIFO can hold less than the trough says. Never ask for more than is there.
    #[test]
    fn drift_regulator_is_capped_by_what_the_fifo_holds() {
        assert_eq!(drift_drop_frames(3904, 1024, READY_BAND, 500), 500);
    }

    #[test]
    fn ring_pct_is_empty_safe() {
        let empty = [Duration::ZERO; RING_HIST_BUCKETS + 1];
        assert_eq!(ring_pct(&empty, RING_HIST_UNIT, 0.5), 0);
    }

    #[test]
    fn snap_accepts_standard_rates_and_small_error() {
        assert_eq!(snap_rate(48_003.0), Some(48_000));   // real measurement, live hardware
        assert_eq!(snap_rate(96_007.0), Some(96_000));   // real measurement, live hardware
        assert_eq!(snap_rate(44_100.0), Some(44_100));
        assert_eq!(snap_rate(192_000.0), Some(192_000));
    }

    #[test]
    fn snap_rejects_non_standard_and_degenerate() {
        // The documented startup-bias reading: timing from process spawn made 48k measure
        // ~41.8k (~13% low) and source_router refused a healthy stream on 2026-07-28.
        // It must land as "unknown", never snap onto 44.1k.
        assert_eq!(snap_rate(41_769.0), None);
        assert_eq!(snap_rate(0.0), None);
        assert_eq!(snap_rate(-1.0), None);
        assert_eq!(snap_rate(f64::NAN), None);
        assert_eq!(snap_rate(60_000.0), None);
    }

    #[test]
    fn debounce_ignores_steady_state() {
        let mut d = RateChangeDebounce::new(48_000);
        for _ in 0..20 {
            assert_eq!(d.observe(Some(48_000)), None);
        }
    }

    #[test]
    fn debounce_fires_on_sustained_change() {
        let mut d = RateChangeDebounce::new(48_000);
        assert_eq!(d.observe(Some(96_000)), None);
        assert_eq!(d.observe(Some(96_000)), None);
        assert_eq!(d.observe(Some(96_000)), Some(96_000));   // 3rd agreeing window
    }

    #[test]
    fn debounce_survives_a_stall_storm() {
        // The failure this guards: a flapping link (bad HDMI cable) making windows read
        // low/unusable must NEVER accumulate into a rate change.
        let mut d = RateChangeDebounce::new(48_000);
        for _ in 0..50 {
            assert_eq!(d.observe(None), None);
        }
        // Interleaved stalls and a plausible-but-wrong snap also must not fire.
        for _ in 0..20 {
            assert_eq!(d.observe(Some(44_100)), None);
            assert_eq!(d.observe(None), None);
        }
    }

    #[test]
    fn debounce_requires_the_same_target() {
        // Two windows agreeing, then a DIFFERENT rate, must restart the count rather
        // than count as a third — otherwise noise walks us onto an arbitrary rate.
        let mut d = RateChangeDebounce::new(48_000);
        assert_eq!(d.observe(Some(96_000)), None);
        assert_eq!(d.observe(Some(96_000)), None);
        assert_eq!(d.observe(Some(44_100)), None);   // resets to a run of 1
        assert_eq!(d.observe(Some(96_000)), None);
        assert_eq!(d.observe(Some(96_000)), None);
        assert_eq!(d.observe(Some(96_000)), Some(96_000));
    }

    #[test]
    fn debounce_resets_when_the_rate_returns() {
        let mut d = RateChangeDebounce::new(48_000);
        assert_eq!(d.observe(Some(96_000)), None);
        assert_eq!(d.observe(Some(96_000)), None);
        assert_eq!(d.observe(Some(48_000)), None);   // back to normal, evidence cleared
        assert_eq!(d.observe(Some(96_000)), None);
        assert_eq!(d.observe(Some(96_000)), None);
        assert_eq!(d.observe(Some(96_000)), Some(96_000));
    }

    /// The write must never exceed the space ALSA already reports, whichever of the three limits
    /// is tightest. Exceeding `avail` is what makes `writei` block for a whole chunk, and that
    /// block is the ring depth (ledger section 5b) — so this bound IS the fix, not a detail.
    #[test]
    fn out_chunk_never_exceeds_available_period_or_ready() {
        let period = OUT_PERIOD as usize; // 1024
        // A whole resampler chunk is ready and the buffer is empty: still one period.
        assert_eq!(out_chunk_frames(2048, period, 3756), period);
        // The resampler is the tightest bound.
        assert_eq!(out_chunk_frames(2048, period, 512), 512);
        // ALSA space is the tightest bound — the case that must never be rounded up.
        assert_eq!(out_chunk_frames(512, period, 3756), 512);
        // Nothing anywhere: the caller must not be handed a write to make.
        assert_eq!(out_chunk_frames(0, period, 3756), 0);
        assert_eq!(out_chunk_frames(2048, period, 0), 0);
    }

    /// The gate exists so an output stall leaves backlog in the RING, where the trims can drop it,
    /// instead of in the resampler FIFO, which has no drop path. Two properties, and they pull
    /// against each other: it must clear one whole emitted block (or it throttles steady state and
    /// gives back the saving), and it must NOT clear the startup residue the one-shot trim exists
    /// to drop (2-5 capture periods, per that trim's own comment).
    #[test]
    fn ingest_high_water_clears_one_block_but_not_the_startup_residue() {
        let block_out_frames = 3756usize;      // one 48k -> 96k block
        let high_water = block_out_frames + INGEST_SLACK;

        assert!(high_water > block_out_frames, "must clear one whole emitted block");
        assert!(high_water < block_out_frames * 2, "must not bank a second block of latency");

        // Startup residue in OUTPUT frames: 2-5 capture periods at 48k -> 96k is 2x the input
        // count, i.e. 1024-2560. Headroom above one block is exactly one output period (1024 f,
        // 10.7 ms), so the FIFO absorbs the very smallest end of that range and the trim still
        // reaches the rest. ⌀ That overlap is the accepted price of not throttling steady state —
        // the gate is sized to the BLOCK, not to the residue. Stated so the next reader does not
        // "discover" it as a bug and re-open the ceiling.
        let residue_out_min = CAP_PERIOD as usize * 2 * 2;
        let residue_out_max = CAP_PERIOD as usize * 5 * 2;
        assert_eq!(high_water - block_out_frames, INGEST_SLACK);
        assert!(high_water - block_out_frames <= residue_out_min);
        assert!(high_water - block_out_frames < residue_out_max);
    }
}
