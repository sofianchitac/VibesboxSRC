// Measures the ACTUAL end-to-end delay through ardftsrc's RealtimeResampler, because the
// bridge's `rs~` diag term is an assumption, not a measurement, and the crate's own docs
// contradict it by ~2x.
//
// `rs~` is computed as `estimate_priming_samples()/2/channels`, i.e. ONE FULL INPUT CHUNK
// (46.7 ms at 44.1k). The crate documents realtime latency as `quality/2 / fs` = 939/44100
// = 21.3 ms. Those cannot both be right, and `sum~` — which fed the §29 "half the bridge
// premium is the resampler" attribution — is built on the first one.
//
// Method: an impulse at a known input index, then find where its peak lands on the output.
// Delay = t_out(peak) - t_in(impulse). Includes priming, which is real in-flight latency.
// Run per rate:  ./group_delay 44100 96000 1

use ardftsrc::{RealtimeResampler, PRESET_GOOD};

fn main() {
    let a: Vec<String> = std::env::args().collect();
    let in_sr: usize = a.get(1).map(|s| s.parse().unwrap()).unwrap_or(44100);
    let out_sr: usize = a.get(2).map(|s| s.parse().unwrap()).unwrap_or(96000);
    let ch: usize = a.get(3).map(|s| s.parse().unwrap()).unwrap_or(1);
    // 4th arg overrides quality, to map the hold against the ONE knob that controls it.
    let quality: usize = a.get(4).map(|s| s.parse().unwrap()).unwrap_or(PRESET_GOOD.quality);

    let cfg = PRESET_GOOD
        .with_input_rate(in_sr)
        .with_output_rate(out_sr)
        .with_channels(ch)
        .with_quality(quality);
    let mut rs = RealtimeResampler::<f64>::new(cfg).expect("resampler");

    let prim = rs.estimate_priming_samples();
    println!("in={in_sr} out={out_sr} ch={ch} quality={quality}");
    println!("estimate_priming_samples() = {prim} interleaved samples = {} frames = {:.2} ms",
             prim / ch, (prim / ch) as f64 * 1000.0 / in_sr as f64);
    println!("  bridge's rs~ term (prim/2/ch)          = {:.2} ms", (prim / 2 / ch) as f64 * 1000.0 / in_sr as f64);
    println!("  crate doc formula  (quality/2 / fs)    = {:.2} ms", (quality as f64 / 2.0) * 1000.0 / in_sr as f64);

    // ⛔ An impulse test measures STREAM ALIGNMENT, not latency, and reads 0.00 ms: output
    // frame N corresponds to input time N/out_sr because the priming period emits silence
    // that keeps the output timeline in step. What costs wall-clock latency is the input
    // that has been WRITTEN but whose output has not been EMITTED yet — measure that.
    //
    //   lag = (frames_written / in_sr) - (frames_read / out_sr)
    //
    let total_frames = in_sr * 5;
    let mut buf = vec![0.0f64; 8192];
    let mut written = 0usize;
    let mut read_frames = 0usize;
    let mut samples = Vec::new();

    for f in 0..total_frames {
        for _ in 0..ch {
            rs.write_sample(0.25).expect("write");
        }
        written += 1;
        loop {
            let got = match rs.read_samples(&mut buf) {
                Some(n) if n > 0 => n,
                _ => break,
            };
            read_frames += got / ch;
        }
        if f % (in_sr / 10) == 0 && f > 0 {
            let lag_ms = (written as f64 / in_sr as f64 - read_frames as f64 / out_sr as f64) * 1000.0;
            samples.push((f as f64 / in_sr as f64, lag_ms));
        }
    }

    println!();
    println!("  t(s)   in-flight hold (ms)");
    for (t, lag) in samples.iter().take(12) {
        println!("  {t:.1}    {lag:8.2}");
    }
    let settled: Vec<f64> = samples.iter().filter(|(t, _)| *t > 1.0).map(|(_, l)| *l).collect();
    let mean = settled.iter().sum::<f64>() / settled.len() as f64;
    let mn = settled.iter().cloned().fold(f64::INFINITY, f64::min);
    let mx = settled.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
    println!();
    println!(">>> STEADY-STATE in-flight hold: mean {mean:.2} ms  (min {mn:.2}, max {mx:.2}, n={})", settled.len());
    println!("    bridge `rs~` claims {:.2} ms", (prim / 2 / ch) as f64 * 1000.0 / in_sr as f64);
    println!("    crate doc claims    {:.2} ms", (quality as f64 / 2.0) * 1000.0 / in_sr as f64);
}
