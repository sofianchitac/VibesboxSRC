# scripts/ — the daemons

Everything that *runs* on the Pi, as opposed to configuring something else that runs.
Each script here is paired with a systemd unit in [`../services/`](../services/).

## The core three

**`source_router.py`** is the heart of the system and the right place to start reading.
It is the only component that knows the whole picture: which sources exist, which are
currently playing, and how they should be wired together. It detects source activity at
the ALSA layer, starts and stops the per-source resampler bridges, and links or unlinks
each source into CamillaDSP over PipeWire. Per-source mute is implemented as *unlink* —
there is no gain stage involved — and any number of sources can be unmuted at once,
because PipeWire sums whatever is connected. It also pushes the CamillaDSP config on
connect, owns the Bluetooth pairing state machine, and serves the WebSocket on `:8080`
that the touchscreen and remote dashboards subscribe to.

**`ndi_transmitter.py`** takes CamillaDSP's 6-channel output off an ALSA loopback and
transmits it as an NDI stream via the NDI SDK (loaded through `ctypes`). It is
deliberately dumb and always running — it does no format decisions and has no modes, so
there is nothing in it to get out of sync with the rest of the system.

**`earc-bitstream-bridge.sh`** handles the one source that carries compressed audio. When
the TV sends DD+/DD/DTS rather than LPCM, this decodes the IEC 61937 stream to 6 channels
and feeds it into the same sum bus everything else uses. `tv_ac3_extract.py` is the
demuxer it drives, and `pcm_backlog_trim.py` sits between the decoder and PipeWire to
keep start-up backlog from turning into permanent latency.

## Now Playing

`nowplaying_server.py` exposes track metadata over HTTP and WebSocket on `:8090`.
`metadata_orchestrator.py` decides *which* source's metadata is authoritative at any
moment and feeds it there, drawing on the per-source adapters in
[`producers/`](producers/). `fingerprint_capture.py` covers the sources that report no
metadata at all by listening to the sum bus and identifying tracks acoustically.

## Supporting cast

`load_aloop.sh` and `usb_audio_gadget.sh` run once at boot to create the ALSA loopback
cards and the USB audio gadget. `bt_agent.sh` keeps a Bluetooth pairing agent alive.
`kiosk.sh` launches the QML touchscreen UI, `setup_splash.sh` installs the boot splash,
and `lattepanda-watcher.sh` keeps nginx's reverse proxy pointed at the DSP unit as its
DHCP lease moves.

`bitstream_bridge_latency.py` is a read-only diagnostic that reports where latency is
accumulating in the bitstream pipeline. It is safe to run against the live system.
