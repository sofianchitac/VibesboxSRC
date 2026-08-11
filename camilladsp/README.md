# camilladsp/ — the sum-bus processor

A single static config, `dsp_6ch.yml`: six channels in, six channels out, 96 kHz to
96 kHz, no filters.

That sounds like it does nothing, and audio-wise it very nearly doesn't. Its job is
structural. CamillaDSP sits at the point where the summed PipeWire bus becomes a single
fixed-format stream, using CamillaDSP's native PipeWire backend on both sides. Everything
upstream is variable — sources appear and disappear, arrive at different rates, and get
summed in changing combinations. Everything downstream is fixed: the NDI transmitter
reads one 6-channel 96 kHz stream and never has to care what produced it.

Having a real processor there rather than a plain link also means the DSP stage exists
and is addressable the moment it is ever needed — CamillaDSP's WebSocket serves the RMS
meters the touchscreen displays, and filters could be added without restructuring
anything.

## Why it boots without this file

CamillaDSP is started with `-w` and no config at all. `source_router.py` pushes
`dsp_6ch.yml` over the WebSocket once PipeWire is confirmed up, and re-pushes on every
reconnect. This avoids a boot-order race: a CamillaDSP that loads a config immediately
would grab devices before the graph beneath it existed, and fail in a way that needed
manual recovery. Booting inert and being configured later is strictly more robust, and it
makes `systemctl restart camilladsp` a safe thing to do at any time.

## No upmixing here

The Pi never upmixes. A stereo source occupies FL/FR and the other four channels carry
silence all the way to the DSP unit, where Penteo 360 does the actual upmix. Channel
routing is likewise not this file's business — REAPER owns it.
