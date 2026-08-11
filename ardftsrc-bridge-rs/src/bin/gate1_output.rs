// Gate 1 — Output spike.
//
// Confirms the load-bearing assumption of the v3 plan: a plain Rust process that
// opens the ALSA "pipewire" PCM (the pipewire-alsa plugin) with PIPEWIRE_PROPS set
// creates a `source.<name>.ardftsrc` PipeWire node with the right channels/position
// and node.autoconnect=false — exactly like ardftsrc_bridge.sh does today via ffmpeg
// `-f alsa pipewire`. If this passes, the whole PipeWire/WirePlumber/source_router
// side stays untouched and the bridge rewrite is purely an input-engine swap.
//
// Run ON THE PI, with the same env the systemd unit gives the bridge:
//
//   cd /opt/vibesbox-src/ardftsrc-bridge-rs
//   sudo XDG_RUNTIME_DIR=/run/pipewire PIPEWIRE_REMOTE=pipewire-0 \
//        ./target/release/gate1_output
//
// Then, in another shell:
//
//   XDG_RUNTIME_DIR=/run/pipewire pw-link -l
//
// PASS  = a node `source.gate1test.ardftsrc` appears with 2 ports FL/FR, unlinked
//         (autoconnect=false → it does not auto-attach to anything).
// FAIL  = no node, wrong name, wrong ports, or it auto-links → fall back to
//         pipewire-rs native output (bigger lift; see the plan's Gate 1 fallback).

use std::env;
use std::f64::consts::PI;

use alsa::pcm::{Access, Format, HwParams, PCM};
use alsa::{Direction, ValueOr};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let node = "source.gate1test.ardftsrc";
    let channels: u32 = 2;
    let rate: u32 = 96000;
    let position = "[ FL FR ]";
    let seconds: u32 = 60;

    // Mirror ardftsrc_bridge.sh's PIPEWIRE_PROPS byte-for-byte (modulo the test node
    // name): media.class Stream/Output/Audio, autoconnect off, channels + position.
    // The pipewire-alsa plugin reads PIPEWIRE_PROPS via getenv at PCM open, so setting
    // it in-process before PCM::new is equivalent to the bash `export`.
    env::set_var(
        "PIPEWIRE_PROPS",
        format!(
            "{{ node.name={node} node.autoconnect=false \
             media.class=Stream/Output/Audio audio.channels={channels} audio.position={position} }}"
        ),
    );

    let pcm = PCM::new("pipewire", Direction::Playback, false)?;
    {
        let hwp = HwParams::any(&pcm)?;
        hwp.set_channels(channels)?;
        hwp.set_rate(rate, ValueOr::Nearest)?;
        hwp.set_format(Format::s32())?; // native-endian S32 == S32_LE on the Pi (ARM LE)
        hwp.set_access(Access::RWInterleaved)?;
        pcm.hw_params(&hwp)?;
    }
    let io = pcm.io_i32()?;

    eprintln!("gate1: opened ALSA 'pipewire' PCM as {node} ({channels}ch {rate}Hz S32LE).");
    eprintln!("gate1: inspect now ->  XDG_RUNTIME_DIR=/run/pipewire pw-link -l");
    eprintln!("gate1: writing a quiet 440 Hz tone for {seconds}s, then exiting.");

    // A very quiet tone so the node is obviously live; autoconnect=false means it
    // links to nothing, so this is harmless even on the production graph.
    let amp = 0.01_f64 * i32::MAX as f64;
    let freq = 440.0_f64;
    let frames_per_write = 1024usize;
    let dphase = 2.0 * PI * freq / rate as f64;
    let mut phase = 0.0_f64;
    let mut buf = vec![0i32; frames_per_write * channels as usize];

    let total_writes = (rate * seconds) as usize / frames_per_write;
    for _ in 0..total_writes {
        for f in 0..frames_per_write {
            let s = (amp * phase.sin()) as i32;
            for c in 0..channels as usize {
                buf[f * channels as usize + c] = s;
            }
            phase += dphase;
            if phase > 2.0 * PI {
                phase -= 2.0 * PI;
            }
        }
        if let Err(e) = io.writei(&buf) {
            pcm.try_recover(e, false)?;
        }
    }
    pcm.drain()?;
    Ok(())
}
