# camilladsp/ — the metering tap

A single static config, `dsp_8ch.yml`: eight channels in, eight channels out, 96 kHz to
96 kHz, no filters, no resampler.

**It is not in the audio path.** It captures the sum bus (`sink.dsp-sum`'s monitor, the same
signal the NDI transmitter reads) and its output is discarded into `sink.dsp-void`. The one
thing it still provides is the RMS its WebSocket serves to the touchscreen meters and to
`source_router.py`'s auto-upmix detector.

That is a demotion, and a deliberate one. CamillaDSP used to be the sum node, back when it did
the source-native → 96 kHz conversion and held the routing matrix. Both jobs left — resampling
to the per-source bridges, all channel and mix decisions to REAPER — years apart, and what
remained was a 1:1 mixer at gain 0 sitting in the middle of the only audio path. A PipeWire
null sink sums just as well and does not process.

> ⛔ Adding filters here would no longer do anything to the sound. The audio does not pass
> through this stage any more; putting DSP back on the Pi means restructuring the graph, not
> editing this file.

## Why it boots without this file

CamillaDSP is started with `-w` and no config at all. `source_router.py` pushes `dsp_8ch.yml`
over the WebSocket once PipeWire is confirmed up, and re-pushes on every reconnect. A
CamillaDSP that loaded a config immediately would grab devices before the graph beneath it
existed. Booting inert and being configured later is strictly more robust, and it means
`systemctl restart camilladsp` recovers on its own.

> ⚠ Safe, but not silent: its nodes leaving and rejoining forces a PipeWire graph
> re-negotiation, and the bridge sheds ~220 ms of backlog as one audible skip. Measured and
> user-confirmed 2026-09-08. Don't bounce it while someone is listening.

## No upmixing here

The Pi never upmixes. A stereo source occupies FL/FR and the other channels carry silence all
the way to the DSP unit, where Penteo 360 does the actual upmix. Channel routing is likewise
not this file's business — REAPER owns it.
