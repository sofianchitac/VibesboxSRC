# producers/ — Now Playing metadata adapters

Every audio source reports "what is playing" differently, and some do not report it at
all. Each producer here wraps one source and turns whatever it offers — a JSON-RPC API,
a D-Bus property, a metadata pipe — into the single shape the rest of the system uses.

`base.py` defines that shape and the polling contract. The others implement it:

- **`lms.py`** — Lyrion Music Server, over its JSON-RPC API. The richest source by far:
  title, artist, album, artwork, and accurate elapsed time.
- **`shairport.py`** — AirPlay, from shairport-sync's metadata pipe.
- **`tidal.py`** — Tidal Connect, read out of the container's logs.
- **`bluealsa.py`** — Bluetooth, via AVRCP metadata when the phone bothers to send it.
- **`generic.py`** — the fallback for sources with no metadata channel at all (USB, TV).
  Reports that something is playing without claiming to know what.

`metadata_orchestrator.py` in the parent directory owns arbitration: multiple sources can
be audible at once, so something has to decide whose metadata the UI should show. Adding
a source means adding a producer here and registering it there — no other file needs to
know.
