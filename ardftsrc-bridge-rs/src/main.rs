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
// source-vs-graph clock drift accumulates IN THE RING (just as v2's drift accumulated in
// ffmpeg's 131072-frame capture buffer and produced the accepted "occasional flush"). The
// ring is therefore the drift reservoir as well as the compute-burst buffer: its size sets
// the flush interval under drift — smaller = lower latency but more frequent brief flushes.
// The soak test must confirm that flush rate is inaudible-enough.
//
// 2026-06-10: moved from the InterleavedResampler chunk API to RealtimeResampler
// (push/pull, priming introspection) + PRESET_GOOD, and added a sustained-backlog
// trim guard — together these removed the chunk-gathering and stall-residue latency
// the chunk-API first cut carried (see the process-loop comments).

use std::fs;
use std::process::Command;
use std::sync::atomic::{AtomicBool, AtomicI64, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

use alsa::pcm::{Access, Format, HwParams, IO, PCM};
use alsa::{Direction, ValueOr};
use ardftsrc::{RealtimeResampler, PRESET_GOOD};
use ringbuf::traits::{Consumer, Observer, Producer, Split};
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
const CAP_BUFFER: i64 = 4096;
// ⚠ `OUT_PERIOD` is not a latency term. The blocking `writei` keeps the output PCM near
// full, so standing latency tracks OUT_BUFFER, not the write granularity. Its only leverage
// is that ALSA requires buffer >= 2 periods, so 1024 here is what makes 2048 the floor for
// OUT_BUFFER. Cutting it to 512 would unlock 1536 (3 quanta) — see the note below before
// trying that.
const OUT_PERIOD: i64 = 1024;
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
            // 2026-06-17 with the channel-ID test files. Do NOT try to correct it here:
            // PipeWire audio.position is cosmetic to the NDI wire (which is index-ordered),
            // so relabeling does NOT reorder the bytes REAPER receives (verified live —
            // the relabel changed nothing downstream). The correction is done in REAPER.
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
        // config, the NDITX loopback width, the transmitter, the stream identity and
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
        // ⛔ `position` is NOMINAL: it exists so the node has an 8ch layout. Lanes are
        // passed through exactly as captured and REAPER maps them. Do not "fix" it here.
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
    // ⚠ `set_*_near` is a REQUEST, not a setting, and nothing has ever checked what came
    // back. That matters: `out=` peaks at 3578 against a nominal 3072, which is either a
    // grant that differs from the ask or `delay()` including graph-side buffering — and
    // until it is known, every "OUT_BUFFER is worth N ms" claim rests on the requested
    // number rather than the real one. Printed once per open, so it costs nothing.
    if let Ok(cur) = pcm.hw_params_current() {
        eprintln!(
            "ardftsrc-bridge: {device} {} hw granted period={} buffer={} (requested {period}/{buffer})",
            match dir { Direction::Capture => "capture", Direction::Playback => "playback" },
            cur.get_period_size().map(|v| v.to_string()).unwrap_or_else(|_| "?".into()),
            cur.get_buffer_size().map(|v| v.to_string()).unwrap_or_else(|_| "?".into()),
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

    // ── Resampler (2026-06-10: RealtimeResampler push/pull API, replacing the chunk API).
    //    The chunk API forced the process loop to hoard a full quality-sized chunk (~43 ms
    //    @ 44.1k) in the ring before every process call — measured as the dominant
    //    reducible term in the 145 ms bridge decomposition. RealtimeResampler accepts
    //    writes of any size and is drained as output becomes ready, so the ring holds only
    //    scheduling slack. ──
    let config = PRESET_GOOD
        .with_input_rate(rate as usize)
        .with_output_rate(TARGET_RATE as usize)
        .with_channels(channels);
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

    // ── Process loop: drain whatever capture has produced into the resampler, then write
    //    out everything it has ready. The blocking writei into the "pipewire" PCM paces
    //    the loop to the 96k graph clock. Unlike the chunk API there is no input hoarding:
    //    the ring holds only scheduling slack (one writei-block worth). ──
    let pop_cap = TRIM_UNIT as usize * channels * 4; // unchanged by the CAP_PERIOD A/B
    let mut in_buf = vec![0.0f64; pop_cap];
    let out_cap = OUT_PERIOD as usize * channels * 4;
    let mut out_f = vec![0.0f64; out_cap];
    let mut out_i = vec![0i32; out_cap];
    // Partial-frame remainder, in case read_samples() ever returns a count that is not a
    // whole number of frames (writei needs whole frames). Normally stays empty.
    let mut frame_carry: Vec<f64> = Vec::with_capacity(channels);

    // LATENCY DIAGNOSTIC (temporary, 2026-06-10): decompose the bridge's in-flight audio.
    // One line after the first block lands, then a report every 5 s.
    let group_ms = (prim_samples / 2 / channels) as f64 * 1000.0 / rate as f64;
    let t_start = Instant::now();
    let mut first_write_done = false;
    let mut startup_trimmed = false;
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
    let mut window_time = Duration::ZERO;
    let mut last_poll = Instant::now();
    // One source of truth for the trim threshold: the in-loop `keep` binds to this, and the
    // low-time accumulator above the early-continue needs it before that binding exists.
    let keep_elems = TRIM_UNIT as usize * channels;
    let mut last_diag = Instant::now();
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
        eprintln!(
            "ardftsrc-bridge[diag {tag}]: cap={cap_f}f({cap_ms:.1}ms) readq={rq_n}f({rq_ms:.1}ms,blocked {rq_us}us) \
             ring={ring_frames}f({ring_ms:.1}ms) \
             floor={floor_frames}f low={low_pct:.0}% \
             rs~{group_ms:.1}ms ready={ready_frames}f({ready_ms:.1}ms) out={out_f64}f({out_ms:.1}ms) sum~{:.1}ms",
            cap_ms + rq_ms / 2.0 + ring_ms + group_ms + ready_ms + out_ms
        );
    };

    'process: while running.load(Ordering::Relaxed) {
        let avail = cons.occupied_len();
        // ⚠ BEFORE the empty-ring early-continue, on purpose: a healthy ring alternates
        // 0 <-> one period, and its floor IS the zero. Sampling only the non-empty passes
        // would make every state look permanently backlogged.
        floor_min = floor_min.min(avail);
        {
            let now = Instant::now();
            let dt = now.duration_since(last_poll);
            last_poll = now;
            window_time += dt;
            depth_hist[(avail / keep_elems).min(DEPTH_BUCKETS)] += dt;
        }
        if avail == 0 {
            thread::sleep(Duration::from_micros(500));
            continue;
        }

        // ── Backlog trims. In-flight audio is CONSERVED in this pipeline (input and
        //    output both run at real-time rate), so backlog accumulated during ANY output
        //    stall is PERMANENT latency once flow resumes. Each trim costs one brief
        //    audible skip instead of a constant extra delay. Two mechanisms:
        //    (1) startup one-shot: ~2 s after the first write, drop the residue that
        //        accumulated while the output node waited for its PipeWire graph link
        //        (measured: 2-5 capture periods — too close to normal slack for a
        //        threshold to separate);
        //    (2) sustained stall guard at 8x keep: catches big stalls — a pipewire/stack
        //        restart pinned the ring at ~557 ms live 2026-06-10. Normal post-trim
        //        slack re-locks at 2-4 periods (resampler chunk quantization), so 8x
        //        with the sustained-1s filter never trips in steady state. ──
        let keep = keep_elems;
        let mut trim_to_keep = false;
        let mut trim_tag = "startup";
        // The window's floor, captured for the p10 trim's ACTION (see the trim block).
        let mut trim_floor = 0usize;
        if !startup_trimmed && first_write_done && t_start.elapsed() >= Duration::from_secs(2) {
            startup_trimmed = true;
            trim_to_keep = avail > keep;
        } else if first_write_done && avail > keep * 8 {
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
            eprintln!(
                "ardftsrc-bridge[diag window]: low={:.1}% deep={:.1}% persistent={persistent}                  strikes={floor_strikes} armed={floor_armed} bursts={trim_bursts}                  hist%=[{}] trim={}",
                if total > 0.0 { depth_hist[0].as_secs_f64() / total * 100.0 } else { 100.0 },
                deep_frac * 100.0,
                hist_pct.join(","),
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
            floor_min = usize::MAX;
            depth_hist = [Duration::ZERO; DEPTH_BUCKETS + 1];
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

        let n = cons.pop_slice(&mut in_buf);
        if n == 0 {
            continue;
        }
        if let Err(e) = rs.write_samples(&in_buf[..n]) {
            eprintln!("ardftsrc-bridge: write_samples failed: {e:?}");
            running.store(false, Ordering::SeqCst);
            break;
        }

        // Drain everything the resampler has ready (Some(0) = unprimed or starved).
        let mut wrote_any = false;
        loop {
            let w = match rs.read_samples(&mut out_f) {
                Some(w) => w,
                None => 0, // finalized — cannot happen in this loop
            };
            if w == 0 {
                break;
            }
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
            }
            frame_carry.extend_from_slice(&body[whole..]);
        }

        if wrote_any && !first_write_done {
            first_write_done = true;
            eprintln!(
                "ardftsrc-bridge[diag startup]: first block written at +{:.0}ms after exec",
                t_start.elapsed().as_secs_f64() * 1000.0
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
}
