// Gate 2 — Throughput spike (existential).
//
// librempeg's ardftsrc FFT is hand-tuned; this crate uses realfft (RustFFT). The v3
// premise dies if the crate can't sustain realtime on the Pi 4B (Cortex-A72/NEON) at
// the quality needed to match v2's fidelity. This measures the realtime factor (rtf =
// audio seconds processed / wall seconds) for a sweep of channel counts and quality
// values, using the *same* chunked path the bridge will use.
//
// rtf is the ceiling under a steady producer; the real bridge also pays per-block
// burstiness, so treat anything under ~2x as suspect for the bursty 6ch USB case.
//
// Build + run ON THE PI in release:
//
//   cd /opt/vibesbox-src/ardftsrc-bridge-rs
//   cargo build --release --bin gate2_throughput
//   ./target/release/gate2_throughput
//
// No ALSA / PipeWire involved — pure CPU. Safe to run alongside the live v2 services
// (it will compete for CPU, so ideally run during a quiet moment).

use std::time::Instant;

use ardftsrc::{Config, InterleavedResampler};

/// Deterministic full-band pseudo-noise in roughly [-0.5, 0.5], interleaved.
/// White-ish input exercises every FFT bin (worst case), unlike a single sine.
fn gen_noise(n: usize) -> Vec<f64> {
    let mut state: u64 = 0x2545_F491_4F6C_DD1D;
    let mut v = Vec::with_capacity(n);
    for _ in 0..n {
        state = state
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        let x = ((state >> 33) as f64 / (1u64 << 31) as f64) - 1.0; // ~[-1, 1)
        v.push(x * 0.5);
    }
    v
}

/// Returns the realtime factor for one (channels, in_rate, quality) case.
fn measure(in_rate: usize, channels: usize, quality: usize, seconds: usize) -> f64 {
    let frames = in_rate * seconds;
    let input = gen_noise(frames * channels); // interleaved

    // Mirror the intended bridge config: 96 kHz out, v2's bandwidth/phase as a starting
    // point. (Gate 3 re-tunes these for real; here they only need to be representative.)
    let config = Config::new(in_rate, 96_000, channels)
        .with_quality(quality)
        .with_bandwidth(0.993)
        .with_phase(-0.3);

    let mut rs = InterleavedResampler::<f64>::new(config).expect("resampler init");
    let in_chunk = rs.input_buffer_size(); // interleaved-sample chunk size
    let out_chunk = rs.output_buffer_size();
    let mut out = vec![0.0_f64; out_chunk];

    let t = Instant::now();
    let mut off = 0usize;
    while off + in_chunk <= input.len() {
        let _ = rs
            .process_chunk(&input[off..off + in_chunk], &mut out)
            .expect("process_chunk");
        off += in_chunk;
    }
    let wall = t.elapsed().as_secs_f64();
    seconds as f64 / wall
}

fn main() {
    let seconds = 10usize;
    println!("Gate 2 — ardftsrc throughput (release). rtf = audio_secs / wall_secs; higher is better.");
    println!("v2 reference: 2ch ran quality=65536, 6ch ran quality=16384 (xrun ceiling, not CPU).\n");
    println!("{:>4}  {:>8}  {:>9}  {:>9}  {}", "ch", "in_rate", "quality", "rtf(x)", "verdict");
    println!("{}", "-".repeat(48));

    // 2ch loopback sources (Lyrion/AirPlay): commonly 44.1k; sweep up to and past v2's 65536.
    // 6ch USB gadget: often 48k/96k; sweep around v2's 16384 ceiling and above (the v3
    // ring-buffer decoupling is meant to let us raise it — Gate 3 confirms the real ceiling).
    let cases: &[(usize, usize, &[usize])] = &[
        (2, 44_100, &[1878, 16384, 65536, 73622, 131072]),
        (6, 48_000, &[1878, 16384, 32768, 65536, 73622]),
    ];

    for &(ch, in_rate, qualities) in cases {
        for &q in qualities {
            let rtf = measure(in_rate, ch, q, seconds);
            let verdict = if rtf >= 2.0 {
                "OK"
            } else if rtf >= 1.2 {
                "tight"
            } else {
                "FAIL (< realtime headroom)"
            };
            println!("{:>4}  {:>8}  {:>9}  {:>9.2}  {}", ch, in_rate, q, rtf, verdict);
        }
        println!();
    }
}
