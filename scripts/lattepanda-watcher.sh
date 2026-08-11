#!/bin/bash
# lattepanda-watcher.sh
# -----------------------------------------------------------------------------
# Keeps nginx's literal-hostname proxy_pass to the DSP unit fresh.
#
# nginx resolves literal hostnames in `proxy_pass` once, at config-load time,
# via NSS (which includes libnss-mdns for .local names). It does NOT re-resolve
# on subsequent requests — so if the DSP unit's DHCP lease changes its IP
# after a reboot, nginx happily keeps proxying to the dead address until it
# is reloaded.
#
# This script is fired every minute by lattepanda-watcher.timer. It resolves
# the DSP hostname via getent (mDNS), compares the result to the last seen
# value, and `systemctl reload nginx` on change. nginx reload is a soft
# reload — no dropped connections — and re-resolves the hostname on the way.
#
# The hostname comes from /etc/vibesbox/dsp-host, written by install.sh from
# $DSP_HOST. Keep it in sync with the proxy_pass in config/nginx/nginx-ui.conf.
# -----------------------------------------------------------------------------

set -u

HOST="$(cat /etc/vibesbox/dsp-host 2>/dev/null || echo 'vibesbox-dsp.local')"
STATE_DIR="/opt/vibesbox-src/state"
STATE_FILE="$STATE_DIR/lattepanda.ip"

mkdir -p "$STATE_DIR"

NEW="$(getent hosts "$HOST" 2>/dev/null | awk '{print $1}' | head -1)"
if [ -z "$NEW" ]; then
    # mDNS lookup failed (LattePanda off, network down, etc.). Don't churn
    # nginx — just exit quietly. Next run will pick it back up.
    exit 0
fi

OLD="$(cat "$STATE_FILE" 2>/dev/null || true)"
if [ "$NEW" != "$OLD" ]; then
    echo "$NEW" > "$STATE_FILE"
    logger -t lattepanda-watcher "$HOST resolved: ${OLD:-<none>} -> $NEW; reloading nginx"
    systemctl reload nginx
fi
