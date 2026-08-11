# Third-party components

VibesboxSRC itself is MIT-licensed (see `LICENSE`). It is an integration project:
most of the heavy lifting is done by software it *orchestrates* rather than
contains. Nothing in the list below is vendored into this repository — `install.sh`
fetches each one from its own upstream, under its own licence.

## Bundled in this repository

| Component | Where | Licence |
|---|---|---|
| Instrument Sans (font) | `ui/Instrument_Sans/`, `ui-qml/fonts/` | SIL Open Font License 1.1 (`OFL.txt` alongside each copy) |
| Power / reboot icons | `ui-qml/icons/` | Derived from [Feather Icons](https://feathericons.com) — MIT, © Cole Bemis |

## Fetched at install time, not distributed here

| Component | Role | Licence |
|---|---|---|
| [NDI SDK 6](https://ndi.video) (Vizrt NDI AB) | 6ch audio transport to the DSP unit | Proprietary SDK EULA — see below |
| [CamillaDSP](https://github.com/HEnquist/camilladsp) | 6ch sum-bus processing | GPL-3.0 |
| [PipeWire](https://pipewire.org) / WirePlumber | audio graph, summing, drift correction | MIT |
| [squeezelite](https://github.com/ralph-irving/squeezelite) | Lyrion/LMS player | GPL-3.0 |
| [shairport-sync](https://github.com/mikebrady/shairport-sync) | AirPlay receiver | MIT |
| [ardftsrc](https://github.com/phayes/ardftsrc-rs) (Rust crate) | the resampler in `ardftsrc-bridge-rs` | MIT OR Apache-2.0 |
| [librempeg](https://github.com/librempeg/librempeg) | legacy rollback resampler, opt-in build only | GPL-3.0 / AGPL-3.0 |
| [tidal-connect-docker](https://github.com/TonyTromp/tidal-connect-docker) | Tidal Connect container | see upstream; wraps **closed-source iFi binaries** that are not redistributable |

## NDI

> NDI® is a registered trademark of Vizrt NDI AB.

This project is **not** sponsored by, endorsed by, or affiliated with Vizrt NDI AB.
The NDI trademark is used here solely to identify compatibility.

The NDI SDK is **not included in this repository** and must not be redistributed —
its EULA (§2.d) prohibits distributing SDK files. `scripts/ndi_transmitter.py` loads
`libndi.so` at runtime via `ctypes`; it does not link or embed any NDI code. To run
the NDI transmitter you must download the SDK yourself from
[ndi.video](https://ndi.video/for-developers/ndi-sdk/) and accept Vizrt's licence.
`install.sh` detects whether `libndi.so` is already present and otherwise prompts you
for the installer's path.

If you redistribute a *binary* built against the NDI SDK, additional EULA obligations
apply (see §3.d and §3.g of the SDK licence). Publishing source, as this repository
does, does not trigger them.

## Tidal Connect

The Tidal Connect engine consists of closed-source 32-bit ARM binaries owned by iFi.
They are neither included here nor redistributable. `install.sh` builds the upstream
container from a pinned commit, which fetches them from their original source. If you
do not use Tidal, skip that step entirely — nothing else depends on it.
