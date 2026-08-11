# config/ — system configuration

Configuration for software this project does not ship. `install.sh` copies these into
place. Nothing here runs on its own.

## The audio graph

**`pipewire/`** pins the graph to 96 kHz and sets the buffer and resampler behaviour.
This is where the "everything is 96 kHz" guarantee is actually enforced — the per-source
bridges convert *to* 96 kHz precisely because the graph will not negotiate anything else.
PipeWire's adaptive resampler stays in the path deliberately, to absorb drift between the
source clocks and the graph clock.

**`wireplumber/`** names things and decides what is allowed to touch what. It gives the
sum-bus sink and the source nodes stable names (`sink.ndi-feed`, `source.usb`, …) so
`source_router.py` can link by name rather than by index, assigns clock authority, and —
just as importantly — *suppresses* nodes WirePlumber would otherwise create. The eARC
capture card is the clearest case: WirePlumber cannot read a channel map off an I2S slave
DAI, so left alone it publishes a bogus stereo fallback node that would compete for the
device.

> The USB gadget rules match the dwc2 platform address, which is SoC-specific
> (`fe980000.usb` on Pi 4B, `1000480000.usb` on Pi 5). Moving between Pi models means
> editing that string.

## The TV input

**`overlays/`** contains the device-tree overlay for the eARC I2S tap, and is the most
self-contained thing in this repository — see [its own README](overlays/README.md). It
turns four I2S data lanes from an HDMI eARC extractor into an ALSA capture card. Useful on
its own if you want multichannel HDMI audio into a Pi 5 and care nothing about the rest of
this project.

## The kiosk

**`greetd/`** and **`greetd.service.d/`** auto-start the QML touchscreen session on the
framebuffer, with no desktop or compositor in between. **`plymouth/`** is the boot splash,
**`udev/`** fixes the touchscreen backlight permissions, and **`nginx/`** serves the
remote dashboard and reverse-proxies break-glass calls to the DSP unit.

> The DSP unit's hostname appears in two places with different timing: `install.sh` bakes
> it into the deployed nginx conf, while `lattepanda-watcher.sh` and `nowplaying_server.py`
> read `/etc/vibesbox/dsp-host` at process start. Change it by re-running `install.sh` with
> `DSP_HOST` set, so both move together — editing the file alone leaves nginx pointing at
> the old host, and nothing reports an error.

> `chromium.d/` is left over from the pre-2026-07 web kiosk, which the QML app replaced.
> It is no longer referenced by anything.
