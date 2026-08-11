# Tidal Connect source (VibesboxSRC)

Tidal Connect runs as a 5th Vibesbox source, plugged into the existing
source → snd-aloop → ardftsrc bridge → PipeWire sum-bus pattern (identical to
Lyrion/AirPlay).

```
tidal_connect_application (Docker, armhf)
  └─ PortAudio/ALSA → hw:Tidal,0,0 (snd-aloop card 15, playback side; format follows the track)
        └─ ardftsrc-bridge@tidal reads plughw:Tidal,1,0 → source.tidal.ardftsrc (96k, 2ch)
              └─ source_router links FL/FR into dsp-in → sum bus → CamillaDSP → NDI
```

## Why Docker

The Tidal Connect engine is iFi's **closed-source 32-bit ARM (armhf) binaries**, built for
Raspbian Stretch (2017-era glibc/openssl1.0/libcurl3). They cannot run on the Pi's 64-bit
userland, so they run inside [TonyTromp/tidal-connect-docker](https://github.com/TonyTromp/tidal-connect-docker)'s
armhf image. The Pi's Cortex-A72/A76 executes AArch32 natively, so no qemu is involved.

`install.sh` builds the image locally from a **pinned commit** and tags it
`vibesbox-tidal-connect:latest`. The closed binaries live only in the upstream repo (not
vendored here). `services/tidal-connect.service` runs it always-on (like squeezelite) so the
device stays discoverable in the Tidal app; `source_router` starts `ardftsrc-bridge@tidal`
on loopback activity and the touchscreen "Tidal" button is a per-source mute toggle.

## Files
- `tidal.env.example` — template; `install.sh` seeds it to `tidal.env` (live, git-ignored)
  once and never overwrites, so the deploy-time `PLAYBACK_DEVICE` survives re-installs. Edit
  the **live** `/opt/vibesbox-src/tidal-connect/tidal.env`.

## Deploy / first-run (on the Pi)
1. `systemctl start tidal-connect` and confirm **"VibesboxSRC"** appears in the Tidal app.
   - **mDNS:** the container uses host networking and its own mDNS responder, which can
     collide with the host `avahi-daemon` on UDP 5353. If the device never appears or flaps,
     apply the upstream mitigation (`fix-name-collision.sh` / `docs/MDNS_COLLISION_FIX.md`).
2. Discover the output device name and set it in `tidal.env`:
   ```
   docker exec tidal_connect /app/ifi-tidal-release/bin/ifi-pa-devs-get
   ```
   Pick the entry for the **Tidal** loopback (card 15 / `hw:15,0`), set `PLAYBACK_DEVICE`,
   then `systemctl restart tidal-connect`.
3. Play a track and confirm the loopback is carrying audio:
   ```
   cat /proc/asound/Tidal/pcm0p/sub0/hw_params
   ```
   `format` shows the track's native format (S16_LE for 16-bit, S24/S32 for hi-res) — any of
   them is fine: the bridge reads `plughw:Tidal,1,0`, so the ALSA plug layer converts to the
   S32 it requests transparently (bit-exact).
4. Confirm `source.tidal.ardftsrc` exists (`pw-link -l`), `journalctl -u source-router` logs
   `Tidal: active`, the UI meters move, and audio reaches the output.
