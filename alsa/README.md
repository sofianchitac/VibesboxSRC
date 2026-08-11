# alsa/ — ALSA configuration

One file, `asound.conf`, installed system-wide.

Its most important line is `defaults.pcm.rate_converter "none"`.

ALSA will silently resample for you whenever a device's rate does not match what an
application asks for. That is a reasonable default for a desktop and completely wrong
here: this project's entire premise is that sample-rate conversion happens once, in a
known place, using a resampler chosen for quality. Hidden conversions would sit in the
path invisibly, undoing that. Turning the automatic converter off means a rate mismatch
becomes a loud, obvious failure instead of a quiet quality loss — which is what you want
when quality is the point.

The rest of the file defines the loopback devices that carry audio from the source
applications into the resampler bridges. Each software source (Lyrion, AirPlay, Tidal)
writes into a loopback card's playback side; the matching `ardftsrc-bridge@` instance
reads the capture side. This is what lets sources that only know how to open an ALSA
device participate in the PipeWire graph without any of them being modified.

Two inputs bypass this entirely because they are real capture hardware: the USB gadget
and the eARC I2S tap.
