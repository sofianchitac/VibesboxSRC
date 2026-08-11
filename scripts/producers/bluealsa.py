"""BluealsaProducer — Bluetooth AVRCP metadata via BlueZ's MediaPlayer1.

The producer name is "bluealsa" (matches the auto-router source) but the
DBus interface we actually consume is `org.bluez.MediaPlayer1`. BlueZ
exposes AVRCP track/status/position; bluealsa itself handles the audio
PCM channel.

Cover art (AVRCP BIP) is best-effort: BlueZ does not surface it on a
property the same way it surfaces Track. We skip artwork in v1.

dbus-next is the async DBus client. It must be installed
(`pip install dbus-next`) — install.sh adds it.
"""

from __future__ import annotations

import asyncio
import logging
import time

from .base import Producer

log = logging.getLogger(__name__)


def _unwrap(v):
    return v.value if hasattr(v, "value") else v


class BluealsaProducer(Producer):
    name = "bluealsa"

    def __init__(self, sink, device_name: str | None = None):
        super().__init__(sink)
        self._device_name  = device_name
        self._baseline_uid = f"bluealsa-active:{device_name or 'unknown'}"
        try:
            from dbus_next import BusType
            from dbus_next.aio import MessageBus
            self._BusType    = BusType
            self._MessageBus = MessageBus
        except ImportError:
            self._BusType = None
            self._MessageBus = None

        self._bus = None
        self._task: asyncio.Task | None = None
        self._players: dict[str, dict] = {}     # path → {"interface", "props", "device_name"}
        self._active_path: str | None = None
        self._last_emitted_uid: str | None = None
        self._last_state_change: float = 0.0

    async def start(self) -> None:
        # Baseline "Bluetooth active" so the Now Playing card shows the device the moment
        # BT is the focused source. Sources without an AVRCP media session (TVs on a menu,
        # games, apps with no media metadata) never populate Title/Artist, so without this
        # the card reads "No source active" while audio is plainly playing. A real AVRCP
        # track (phones, the TV's YouTube app, etc.) upgrades it in _emit_current().
        await self._emit_baseline()
        if self._MessageBus is None:
            log.warning("dbus-next not installed — BluealsaProducer baseline only")
            return
        try:
            self._bus = await self._MessageBus(bus_type=self._BusType.SYSTEM).connect()
        except Exception as e:
            log.warning(f"DBus connect failed ({e}); BluealsaProducer baseline only")
            return
        self._task = asyncio.create_task(self._run())
        log.info("BluealsaProducer started (org.bluez.MediaPlayer1)")

    async def _emit_baseline(self) -> None:
        """Minimal 'Bluetooth active' envelope (device name, no track) for sources that
        expose no AVRCP metadata. Mirrors GenericProducer's start-time emit."""
        dev = self._device_name
        await self.sink.push(self._envelope(
            event="track_change",
            playing=True,
            playback_rate=1.0,
            track={
                "unique_id":          self._baseline_uid,
                "title":              "Bluetooth",
                "artist":             dev,
                "album":              None,
                "duration_seconds":   None,
                "elapsed_seconds":    0.0,
                "elapsed_captured_at": time.time(),
            },
            source_app={"bundle_id": None,
                        "display_name": f"Bluetooth: {dev}" if dev else "Bluetooth"},
            artwork=None,
            confidence=None,
            transport=None,
        ))
        self._last_emitted_uid = self._baseline_uid

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._bus:
            self._bus.disconnect()
            self._bus = None
        try:
            await self.sink.push(self._envelope(event="cleared"))
        except Exception as e:
            log.warning(f"Final cleared push failed: {e}")

    async def _run(self) -> None:
        try:
            await self._scan_existing()
            await self._subscribe_object_manager()
            # Keep the task alive forever; signals drive everything else.
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning(f"BluealsaProducer run loop error: {e}")

    async def _introspect(self, path: str):
        return await self._bus.introspect("org.bluez", path)

    async def _scan_existing(self) -> None:
        try:
            intro = await self._introspect("/")
            obj   = self._bus.get_proxy_object("org.bluez", "/", intro)
            om    = obj.get_interface("org.freedesktop.DBus.ObjectManager")
            objects = await om.call_get_managed_objects()
        except Exception as e:
            log.warning(f"GetManagedObjects failed: {e}")
            return

        for path, ifaces in objects.items():
            if "org.bluez.MediaPlayer1" in ifaces:
                await self._add_player(path)

    async def _subscribe_object_manager(self) -> None:
        intro = await self._introspect("/")
        obj   = self._bus.get_proxy_object("org.bluez", "/", intro)
        om    = obj.get_interface("org.freedesktop.DBus.ObjectManager")

        def on_added(path, ifaces):
            if "org.bluez.MediaPlayer1" in ifaces:
                asyncio.create_task(self._add_player(path))

        def on_removed(path, ifaces):
            if "org.bluez.MediaPlayer1" in ifaces:
                asyncio.create_task(self._remove_player(path))

        om.on_interfaces_added(on_added)
        om.on_interfaces_removed(on_removed)

    async def _add_player(self, path: str) -> None:
        if path in self._players:
            return
        try:
            intro = await self._introspect(path)
            obj   = self._bus.get_proxy_object("org.bluez", path, intro)
            mp    = obj.get_interface("org.bluez.MediaPlayer1")
            props = obj.get_interface("org.freedesktop.DBus.Properties")
        except Exception as e:
            log.warning(f"Cannot bind MediaPlayer1 at {path}: {e}")
            return

        device_name = await self._lookup_device_name(path)

        def on_changed(iface_name, changed, invalidated):
            if iface_name != "org.bluez.MediaPlayer1":
                return
            asyncio.create_task(self._on_changed(path, changed))

        props.on_properties_changed(on_changed)

        self._players[path] = {
            "interface": mp,
            "props":     props,
            "device":    device_name,
            "on_changed": on_changed,
        }
        self._active_path = path
        log.info(f"MediaPlayer1 added at {path} (device={device_name!r})")
        await self._emit_current(path, fresh=True)

    async def _remove_player(self, path: str) -> None:
        rec = self._players.pop(path, None)
        if rec is None:
            return
        try:
            rec["props"].off_properties_changed(rec["on_changed"])
        except Exception:
            pass
        if self._active_path == path:
            self._active_path = None
            await self.sink.push(self._envelope(event="cleared"))
            self._last_emitted_uid = None
        log.info(f"MediaPlayer1 removed at {path}")

    async def _lookup_device_name(self, player_path: str) -> str | None:
        # Player path is .../dev_XX_XX_XX_XX_XX_XX/playerN — device is the parent
        device_path = player_path.rsplit("/", 1)[0]
        try:
            intro = await self._introspect(device_path)
            obj   = self._bus.get_proxy_object("org.bluez", device_path, intro)
            dev   = obj.get_interface("org.bluez.Device1")
            name  = await dev.get_alias()
            return name
        except Exception:
            return None

    async def _on_changed(self, path: str, changed: dict) -> None:
        if path != self._active_path:
            self._active_path = path

        if "Track" in changed:
            await self._emit_current(path, fresh=True)
            return

        if "Status" in changed or "Position" in changed:
            # Throttle state_change to ≤ 1 per 2s
            now = time.time()
            if now - self._last_state_change < 2.0 and "Status" not in changed:
                return
            self._last_state_change = now
            await self._emit_current(path, fresh=False)

    async def _emit_current(self, path: str, fresh: bool) -> None:
        rec = self._players.get(path)
        if rec is None:
            return
        mp = rec["interface"]
        try:
            track    = await mp.get_track()
            status   = await mp.get_status()
            position = await mp.get_position()
        except Exception as e:
            log.debug(f"MediaPlayer1 read failed at {path}: {e}")
            return

        track    = {k: _unwrap(v) for k, v in (track or {}).items()}
        status   = _unwrap(status)
        position = _unwrap(position)

        title    = track.get("Title")
        artist   = track.get("Artist")
        album    = track.get("Album")
        duration_ms = track.get("Duration")
        if not title and not artist:
            # No real AVRCP metadata (player present but empty — TV stopped to a menu,
            # app with no media session). Fall back to the "Bluetooth active" baseline
            # rather than leaving a stale track on the card.
            if self._last_emitted_uid != self._baseline_uid:
                await self._emit_baseline()
            return

        playing = (status == "playing")
        rate    = 1.0 if playing else 0.0
        elapsed = (position or 0) / 1000.0
        duration_sec = (duration_ms / 1000.0) if duration_ms else None
        uid = f"bluealsa:{rec['device'] or 'unknown'}:{title}|{artist}|{album}"

        if fresh or uid != self._last_emitted_uid:
            await self.sink.push(self._envelope(
                event="track_change",
                playing=playing,
                playback_rate=rate,
                track={
                    "unique_id":          uid,
                    "title":              title,
                    "artist":             artist,
                    "album":              album,
                    "duration_seconds":   duration_sec,
                    "elapsed_seconds":    elapsed,
                    "elapsed_captured_at": time.time(),
                },
                source_app={"bundle_id": None,
                            "display_name": f"Bluetooth: {rec['device'] or 'unknown'}"},
                artwork=None,
                confidence=1.0,
                transport=None,
            ))
            self._last_emitted_uid = uid
        else:
            await self.sink.push(self._envelope(
                event="state_change",
                playing=playing,
                playback_rate=rate,
                track_unique_id=uid,
                elapsed_seconds=elapsed,
                elapsed_captured_at=time.time(),
            ))
