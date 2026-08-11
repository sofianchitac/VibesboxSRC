# LMS-side: VibesboxTranscode

Lives on the **Lyrion (LMS) server**, not on the Pi — it is kept here because it fixes the
Lyrion source path and there is nowhere better to version it.

The commands below assume LMS runs in Docker and use `$LMS_HOST` for the server and
`$LMS_APPDATA` for its persistent data directory. Substitute your own; the paths shown are
what an Unraid-style Docker deployment looks like.

## What it fixes

Tidal serves MP4 for releases with no stereo master (Atmos-only). The TIDAL plugin relabels
that MP4 as `aac`, LMS direct-streams it to squeezelite, squeezelite can't decode it, and the
player wedges at the end of the *previous* track in a fake `play` state. The Pi looks broken
and isn't — it faithfully transports the resulting silence.

This makes LMS decode those streams with ffmpeg instead, so they play.

## Two halves — neither works alone

1. **`aac-aac-*-*` disabled** in Settings → Advanced → File Types. Without this LMS
   direct-streams the MP4 and never consults a conversion rule at all.
2. **This plugin**, which exists only to carry `custom-convert.conf`. LMS scans the plugin
   directory for it; a plain directory is not scanned.

## Install

```bash
LMS_HOST=root@your-lms-server.local
LMS_APPDATA=/mnt/user/appdata/LyrionMusicServer

# 1. static ffmpeg (LMS ships faad, sox, flac — no ffmpeg)
ssh "$LMS_HOST" "mkdir -p $LMS_APPDATA/bin"
# download a static ffmpeg for the server's architecture, place it at
# $LMS_APPDATA/bin/ffmpeg, chmod 755

# 2. the plugin — note the directory, see the warning below
scp -r lms/VibesboxTranscode "$LMS_HOST:$LMS_APPDATA/cache/Plugins/"
ssh "$LMS_HOST" "chown -R nobody:users $LMS_APPDATA/cache/Plugins"

# 3. disable native AAC in the LMS web UI, then restart the container
ssh "$LMS_HOST" 'docker restart LyrionMusicServer'
```

> **Install to `cache/Plugins/`, NOT `cache/InstalledPlugins/Plugins/`.** The latter belongs to
> LMS's extension manager, which deletes anything it did not install — it silently removed this
> plugin on the first attempt. `cache/Plugins` is a second plugin directory that `Docker.pm`
> adds on the Docker image, and nothing prunes it.

## Verify

Both `aac-flc-*-*` **and** `aac-flc-*-*-1` should appear on the File Types page: the custom
rule claimed the base profile name and the built-in faad rule was demoted. Conversion tables
load only at startup, so every change to `custom-convert.conf` needs an LMS restart.

Gain is handled by `-target_level`, a decoder option — see the comments in
`custom-convert.conf` for why it must not be an `-af volume` filter.
