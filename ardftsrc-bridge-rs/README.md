# ardftsrc-bridge — the per-source resampler

A small Rust binary. One instance runs per active audio source. It opens that source's
capture device at whatever rate the source natively produces, resamples to 96 kHz, and
publishes the result as a PipeWire node named `source.<name>.ardftsrc`.

That is the whole job, and it is the load-bearing piece of the system's central promise:
the DSP unit downstream must never see a sample-rate change, because its ASIO engine and
VST3 chain would have to reset their clock domain if it did. Sources, meanwhile, change
rate constantly — 44.1 kHz for most music, 48 kHz for video, 96 or 192 kHz for hi-res.
Absorbing that variation *here*, once per source and before anything is summed, is what
keeps everything after it stable.

## Why a dedicated resampler

Sample-rate conversion is the one place in this signal path where quality is genuinely at
stake, so it gets a component of its own rather than being left to whatever happens to be
in the chain. The [`ardftsrc`](https://github.com/phayes/ardftsrc-rs) crate implements a
DFT-based converter chosen on listening tests against the alternatives; ALSA's automatic
converter is disabled system-wide (see [`../alsa/`](../alsa/)) specifically so that no
hidden conversion can happen anywhere else.

Running one bridge per source, rather than one shared resampler after the mix, means each
source is converted from its own true native rate. Once sources are summed they share a
rate and the information about where each came from is gone.

## Per-source configuration

Sources differ in ways the bridge has to know about: some have a fixed hardware rate,
others negotiate; some are loopback devices fed by an application, others are real capture
hardware. `src/main.rs` holds a table of these per-source facts. Adding a source means
adding an entry there and a matching systemd instance.

The TV is the awkward one. Its capture rate changes with the *format* — 48 kHz for LPCM,
192 kHz for a DD+ bitstream — and I2S slave mode offers no way to detect the incoming rate.
If the measured rate is not what this bridge expects, it refuses to start and logs why,
rather than playing the stream at the wrong pitch. Silence with an explanation is easier
to diagnose than audio that is 8% sharp.

## Building

Builds **on the Pi** — it needs native `libasound`, and is not cross-compiled:

```bash
sudo apt-get install -y libasound2-dev
cargo build --release
```

`src/bin/` holds three standalone probes (`gate1_output`, `gate2_throughput`,
`gate3_bufsizes`) written to validate the approach before the bridge existed — output node
creation, resampler throughput on the target CPU, and buffer-size behaviour. They are not
part of the running system but are useful if you are porting this to different hardware and
want to know whether it will keep up.
