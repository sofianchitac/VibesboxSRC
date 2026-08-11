# services/ — systemd units

One unit per daemon in [`../scripts/`](../scripts/), plus config files for the third-party
source services. `install.sh` copies these to `/etc/systemd/system/` and enables them.

## Boot order matters

The units declare dependencies, but the intent is easier to read as a sequence:

1. `alsa-loopback` — creates the loopback cards other services write into
2. `usb-gadget` — brings up the UAC2 USB audio gadget
3. `pipewire` + `wireplumber` — the 96 kHz audio graph and its session manager
4. `camilladsp` — starts *without* a config (`-w`), so it locks no devices
5. `source-router` — wires the graph up, pushes CamillaDSP its config, opens `:8080`
6. `ndi-output` — the always-on NDI transmitter
7. source services (`squeezelite`, `shairport-sync`, `tidal-connect`) and the on-demand
   `ardftsrc-bridge@` instances
8. `greetd` — the touchscreen kiosk session

The unusual step is 4→5. CamillaDSP deliberately boots with no configuration at all, and
`source-router` pushes it one once PipeWire is confirmed up. That push is what creates
CamillaDSP's graph nodes, so a CamillaDSP that has never been pushed to is inert rather
than broken. It also means restarting CamillaDSP on its own is safe — the router notices
the reconnect and re-pushes.

## Templated units

`ardftsrc-bridge@.service` and `earc-bitstream-bridge@.service` are instantiated per
source (`ardftsrc-bridge@lyrion`, `earc-bitstream-bridge@eac3`, …). `source_router.py`
starts and stops them; nothing enables them at boot, because whether a source needs a
bridge is a runtime question.

Both use `Restart=on-failure` and exit 0 on a clean stop, which is what lets a format
change tear a bridge down without systemd fighting the router to bring it back.

## Third-party config

`squeezelite.conf`, `shairport-sync.conf` and `tidal-connect.service` configure software
this project does not ship. `nowplaying.env.example` is the template for
`/etc/vibesbox/nowplaying.env`; `install.sh` seeds it once and never overwrites it, so
deployment-specific values survive reinstallation.
