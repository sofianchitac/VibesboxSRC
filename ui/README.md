# ui/ — the remote dashboard and shared assets

Served by nginx on port 80. This is the *browser* interface, reachable from any device on
the LAN — distinct from the touchscreen attached to the Pi itself, which is a native QML
app in [`../ui-qml/`](../ui-qml/).

## `remote/`

A single-page dashboard mirroring the state the touchscreen shows: which sources are
active, per-source mute, Now Playing, and level meters. It subscribes to the same
`source_router.py` WebSocket on `:8080` that the touchscreen uses, so the two cannot
disagree about what is playing.

<img src="../screenshots/remote-dashboard.png" alt="The remote dashboard in a browser" width="560">

The rate readout across the top is the clearest view of what this box is for: source
rate in, 96 kHz out, every time, whatever the source happens to be doing.

It also carries volume, bass and treble nudges for the DSP unit, and a token-gated
break-glass panel for recovering the DSP unit's kiosk remotely. Both are proxied through
nginx rather than called directly, which keeps them same-origin — a cross-origin POST
would fire but the response would be rejected for want of a CORS header.

`config.example.json` is the template for the per-deployment `config.json`, which holds
only the break-glass shared secret. That file is deliberately git-ignored and never
committed. Without it the break-glass panel disables itself and everything else keeps
working.

> **Reviewing changes:** most of the visible surface is constructed by `remote.js` at
> runtime, so opening `index.html` directly shows almost nothing. To see it you need the
> page running against a real or stubbed WebSocket.

## Shared assets

`vibesbox-src-logo-grey.svg`, `splashscreen.png` and `Instrument_Sans/` are referenced by the
remote dashboard and the boot splash. Instrument Sans is under the SIL Open Font License;
`OFL.txt` sits alongside it.

> The QML touchscreen app keeps its own copy of the font in `../ui-qml/fonts/`, as static
> instanced TTFs rather than the variable font used here. Qt's eglfs font loading does not
> handle variable fonts, and the failure is silent — text simply renders in a fallback.
