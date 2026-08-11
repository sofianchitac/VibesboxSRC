// Gate 3 (pre-cutover slice) — buffer sizes & latency floor.
//
// Settles the chunk-API-vs-RealtimeResampler architecture question with a free, no-audio
// measurement. Two things ride on InterleavedResampler::input_buffer_size():
//
//   1. Latency floor: with process_chunk you can't emit output until a full
//      input_buffer_size() chunk is accumulated → that chunk is the minimum input-side
//      latency. Compare it to RealtimeResampler::estimate_priming_samples() (streaming /
//      overlap) at the same config. If the chunk floor ≫ the priming estimate at the real
//      qualities, switch the bridge to RealtimeResampler before the live session.
//
//   2. 6ch correctness: the bridge slices `input_buffer_size()` f64 off an interleaved
//      ring. That's only correct if the value is an interleaved-sample count. The 2ch-vs-6ch
//      comparison at fixed quality disambiguates: if 6ch == 3 × 2ch it's interleaved
//      samples (bridge correct); if equal it's frames (bridge would misalign 6ch channels).
//
// Run on the Pi: cargo build --release --bin gate3_bufsizes && ./target/release/gate3_bufsizes

use ardftsrc::{Config, InterleavedResampler, RealtimeResampler};

fn cfg(in_rate: usize, ch: usize, q: usize) -> Config {
    Config::new(in_rate, 96_000, ch)
        .with_quality(q)
        .with_bandwidth(0.993)
        .with_phase(-0.3)
}

fn report(in_rate: usize, ch: usize, q: usize) {
    let ir = InterleavedResampler::<f64>::new(cfg(in_rate, ch, q)).expect("interleaved init");
    let ib = ir.input_buffer_size();
    let ob = ir.output_buffer_size();
    let rr = RealtimeResampler::<f64>::new(cfg(in_rate, ch, q)).expect("realtime init");
    let prime = rr.estimate_priming_samples();

    // Interpret ib/prime as interleaved-sample counts (the bridge's assumption).
    let ib_frames = ib as f64 / ch as f64;
    let in_ms = ib_frames / in_rate as f64 * 1000.0;
    let prime_frames = prime as f64 / ch as f64;
    let prime_ms = prime_frames / in_rate as f64 * 1000.0;

    println!(
        "ch={ch} in={in_rate} q={q:>6}: in_buf={ib:>7} (~{ib_frames:>7.0} fr, {in_ms:>6.1} ms@in)  \
         out_buf={ob:>7}  prime={prime:>7} (~{prime_ms:>6.1} ms@in)  chunk_floor/prime={:.1}x",
        if prime > 0 { ib as f64 / prime as f64 } else { f64::NAN }
    );
}

fn main() {
    println!("Gate 3 — buffer sizes & latency floor (chunk vs realtime). All values assume");
    println!("input_buffer_size()/estimate_priming_samples() are INTERLEAVED-sample counts.\n");

    println!("-- production candidates --");
    for &(r, ch, q) in &[
        (44_100usize, 2usize, 65_536usize), // Lyrion/AirPlay @ v2 quality
        (48_000, 6, 16_384),                // USB @ v2 quality
        (48_000, 6, 65_536),                // USB pushed (Gate 2 said CPU allows it)
        (96_000, 6, 16_384),                // USB native-96k case (96k->96k)
    ] {
        report(r, ch, q);
    }

    println!("\n-- frames-vs-interleaved-samples disambiguation (fixed q=16384, in=48000) --");
    report(48_000, 2, 16_384);
    report(48_000, 6, 16_384);
    println!("(if 6ch in_buf == 3x the 2ch in_buf -> interleaved samples, bridge slicing is correct)");
}
