#!/bin/bash
# ══════════════════════════════════════════════════════════════════════════════
# Vibesbox SRC — Deploy (the light path)
# Usage:  sudo bash deploy.sh              # from a repo checkout ON THE PI
#
# WHY THIS EXISTS: install.sh is the only thing that syncs the checkout to
# /opt/vibesbox-src, and it is far too heavy to run for a routine change — it
# reinstalls packages, re-downloads CamillaDSP, rebuilds the venv and prompts for
# a user. So in practice files got hand-copied to /opt instead, and the checkout
# drifted behind both the repo and the running system. This script is the missing
# middle: it deploys CODE and the configs that change during development, and
# nothing else.
#
# SCOPE — what this does NOT do (still install.sh's job, on a fresh box or when
# any of these change): apt packages, the pipewire/vibesbox system users, the
# CamillaDSP binary, the Python venv, docker/tidal-connect setup, and seeding
# ui/remote/config.json. If you changed one of those, run install.sh.
# ══════════════════════════════════════════════════════════════════════════════

INSTALL_DIR="/opt/vibesbox-src"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

FAILED_STEPS=()
WARNINGS=()
step_ok()   { echo "  ✓ $1"; }
step_fail() { echo "  ✗ $1"; FAILED_STEPS+=("$1"); }
step_warn() { echo "  ⚠ $1"; WARNINGS+=("$1"); }

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║        Vibesbox SRC — Deploy             ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── 0. Preflight ────────────────────────────────────────────────────────────
echo "[0/6] Preflight…"

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: must be run as root (sudo bash deploy.sh)."
    exit 1
fi

# Guard against running this from the wrong directory and copying junk into /opt.
if [[ ! -f "$SCRIPT_DIR/install.sh" || ! -d "$SCRIPT_DIR/camilladsp" ]]; then
    echo "ERROR: $SCRIPT_DIR is not a VibesboxSRC checkout."
    exit 1
fi
if [[ ! -d "$INSTALL_DIR" ]]; then
    echo "ERROR: $INSTALL_DIR missing — this box has never been installed."
    echo "       Run install.sh first; deploy.sh only updates an existing install."
    exit 1
fi

# The service account is whoever owns the existing install — no prompt, so this
# stays runnable unattended. install.sh is where a user is chosen or created.
INSTALL_USER="$(stat -c '%U' "$INSTALL_DIR")"
id -u "$INSTALL_USER" &>/dev/null || { echo "ERROR: owner '$INSTALL_USER' of $INSTALL_DIR is not a user."; exit 1; }
step_ok "Service user: $INSTALL_USER"

# The drift this script exists to stop: say so rather than silently deploying a
# stale tree. Not fatal — deploying uncommitted work in progress is normal.
if git -C "$SCRIPT_DIR" rev-parse --git-dir &>/dev/null; then
    HEAD_SHA="$(git -C "$SCRIPT_DIR" rev-parse --short HEAD)"
    DIRTY="$(git -C "$SCRIPT_DIR" status --porcelain | grep -c '^ M\|^M')"
    step_ok "Checkout at $HEAD_SHA (${DIRTY} modified file(s))"
    if timeout 15 git -C "$SCRIPT_DIR" fetch -q 2>/dev/null; then
        BEHIND="$(git -C "$SCRIPT_DIR" rev-list --count HEAD..@{u} 2>/dev/null || echo 0)"
        [[ "$BEHIND" -gt 0 ]] && step_warn "Checkout is $BEHIND commit(s) behind origin — 'git pull' first if that was not deliberate."
    fi
fi

# ── 1. Rust bridge ──────────────────────────────────────────────────────────
# MUST come before the restart: the per-source channel counts and rate handling
# are compiled in, so copying main.rs alone changes nothing at runtime.
# cargo is incremental, so this is nearly free when the source has not changed.
echo "[1/6] Building ardftsrc-bridge…"
CARGO="/home/$INSTALL_USER/.cargo/bin/cargo"
BUILD_OK=1
if [[ -x "$CARGO" ]]; then
    if sudo -u "$INSTALL_USER" -H "$CARGO" build --release --quiet \
            --manifest-path "$SCRIPT_DIR/ardftsrc-bridge-rs/Cargo.toml" 2>/tmp/ardftsrc-deploy-build.log; then
        mkdir -p "$INSTALL_DIR/bin"
        # Keep the outgoing binary — the only artefact here that cannot be
        # regenerated from the repo if the new one misbehaves.
        [[ -f "$INSTALL_DIR/bin/ardftsrc-bridge" ]] && \
            cp "$INSTALL_DIR/bin/ardftsrc-bridge" "$INSTALL_DIR/bin/ardftsrc-bridge.previous"
        # ⚠ Write-then-rename, NOT a plain cp. A bridge instance is usually running, and
        # writing to a busy executable fails with ETXTBSY ("Text file busy") — rename(2)
        # only swaps the directory entry, so it succeeds and the running process keeps
        # the old inode until it restarts. Found the hard way on the first real deploy
        # (2026-08-13): the cp failed, its status was never checked, and the deploy
        # reported success while still running the OLD binary.
        if cp "$SCRIPT_DIR/ardftsrc-bridge-rs/target/release/ardftsrc-bridge" \
              "$INSTALL_DIR/bin/.ardftsrc-bridge.new" \
           && chmod 755 "$INSTALL_DIR/bin/.ardftsrc-bridge.new" \
           && mv -f "$INSTALL_DIR/bin/.ardftsrc-bridge.new" "$INSTALL_DIR/bin/ardftsrc-bridge"; then
            step_ok "ardftsrc-bridge installed (previous kept as bin/ardftsrc-bridge.previous)"
        else
            rm -f "$INSTALL_DIR/bin/.ardftsrc-bridge.new"
            BUILD_OK=0
            step_fail "ardftsrc-bridge built but could NOT be installed to $INSTALL_DIR/bin/"
        fi
    else
        BUILD_OK=0
        step_fail "ardftsrc-bridge build FAILED — see /tmp/ardftsrc-deploy-build.log"
    fi
else
    BUILD_OK=0
    step_fail "cargo not found at $CARGO — cannot rebuild the bridge"
fi

# ── 2. Project files → /opt ─────────────────────────────────────────────────
echo "[2/6] Deploying project files to $INSTALL_DIR…"
for d in scripts camilladsp alsa services ui ui-qml config tidal-connect tools; do
    [[ -d "$SCRIPT_DIR/$d" ]] && cp -r "$SCRIPT_DIR/$d" "$INSTALL_DIR/"
done
# cp -r never deletes, so per-deployment state that is not in the repo survives:
# ui/remote/config.json (LattePanda host + break-glass token) and state/.
chmod +x "$INSTALL_DIR"/scripts/*.sh 2>/dev/null
chown -R "$INSTALL_USER:$INSTALL_USER" "$INSTALL_DIR"
chmod -R a+rX "$INSTALL_DIR/ui"          # nginx runs as www-data
find "$INSTALL_DIR/services" "$INSTALL_DIR/config" -type f \
    -exec sed -i "s|__USER__|${INSTALL_USER}|g" {} +
step_ok "Project files deployed"

# ── 3. System config ────────────────────────────────────────────────────────
echo "[3/6] Installing system configuration…"
cp "$INSTALL_DIR/alsa/asound.conf" /etc/asound.conf
mkdir -p /etc/pipewire/pipewire.conf.d /etc/wireplumber/wireplumber.conf.d
cp "$INSTALL_DIR"/config/pipewire/pipewire.conf.d/*.conf     /etc/pipewire/pipewire.conf.d/
cp "$INSTALL_DIR"/config/wireplumber/wireplumber.conf.d/*.conf /etc/wireplumber/wireplumber.conf.d/
# *.timer as well as *.service — lattepanda-watcher is timer-driven, and a glob of
# just .service silently drops it. The other files in services/ (README, *.conf,
# nowplaying.env.example) are deliberately NOT systemd units and must not go here.
cp "$INSTALL_DIR"/services/*.service /etc/systemd/system/
cp "$INSTALL_DIR"/services/*.timer   /etc/systemd/system/ 2>/dev/null
# Source daemon configs that live outside /opt.
cp "$INSTALL_DIR/services/squeezelite.conf"    /etc/default/squeezelite
cp "$INSTALL_DIR/services/shairport-sync.conf" /etc/shairport-sync.conf
# NOT nowplaying.env — it holds per-deployment secrets and install.sh seeds it once
# from the .example. Overwriting it here would wipe the break-glass token.
[[ -f "$INSTALL_DIR/config/greetd/config.toml" ]] && cp "$INSTALL_DIR/config/greetd/config.toml" /etc/greetd/config.toml
step_ok "ALSA / PipeWire / WirePlumber / systemd units + timer / source configs / greetd"

# nginx carries the DSP hostname BAKED IN, unlike lattepanda-watcher and
# nowplaying_server which read /etc/vibesbox/dsp-host at process start. The
# tracked conf holds a `vibesbox-dsp.local` placeholder that does not resolve, so
# re-deploying it verbatim would silently break the break-glass proxy. Substitute
# from the file, which install.sh established as the single source of truth.
if [[ -f /etc/vibesbox/dsp-host ]]; then
    DSP_HOST="$(cat /etc/vibesbox/dsp-host)"
    sed "s/vibesbox-dsp\.local/$DSP_HOST/g" \
        "$INSTALL_DIR/config/nginx/nginx-ui.conf" > /etc/nginx/sites-available/vibesbox-ui
    if nginx -t 2>/dev/null; then
        systemctl reload nginx 2>/dev/null
        step_ok "nginx conf regenerated for DSP host '$DSP_HOST'"
    else
        step_fail "nginx config validation failed — left running on the previous conf"
    fi
else
    step_warn "/etc/vibesbox/dsp-host missing — nginx left untouched. Run install.sh with DSP_HOST set."
fi

systemctl daemon-reload
step_ok "systemd daemon-reload"

# ── 4. Restart ──────────────────────────────────────────────────────────────
# Order matters and mirrors the boot order: PipeWire/WirePlumber own the node
# definitions (source node names and channel maps) and must settle before
# CamillaDSP opens them; source_router pushes the CamillaDSP config on connect, so
# it follows CamillaDSP; the transmitter drains the loopback CamillaDSP feeds.
# Restarting out of order leaves the graph built from the OLD node definitions.
echo "[4/6] Restarting the audio chain…"
if [[ $BUILD_OK -eq 0 ]]; then
    step_warn "Skipping restart — the bridge build failed, so the running system is left alone."
else
    read -rp "  Restart audio services now? Audio will drop for a few seconds. [Y/n]: " DO_RESTART
    if [[ "${DO_RESTART:-Y}" =~ ^[Yy] ]]; then
        systemctl restart pipewire wireplumber
        sleep 2
        systemctl restart camilladsp
        sleep 1
        systemctl restart source-router
        systemctl restart ndi-output
        step_ok "pipewire → wireplumber → camilladsp → source-router → ndi-output"
        # QML has no live-reload; the kiosk runs under the greetd session.
        systemctl restart greetd
        step_ok "greetd (QML kiosk)"
    else
        step_warn "Restart skipped — deployed files are NOT live yet."
        echo "     sudo systemctl restart pipewire wireplumber camilladsp source-router ndi-output greetd"
    fi
fi

# ── 5. Verify ───────────────────────────────────────────────────────────────
echo "[5/6] Service status…"
sleep 2
for u in pipewire wireplumber camilladsp source-router ndi-output; do
    STATE="$(systemctl is-active "$u" 2>/dev/null)"
    if [[ "$STATE" == "active" ]]; then step_ok "$(printf '%-16s %s' "$u" "$STATE")"
    else step_fail "$(printf '%-16s %s' "$u" "$STATE")"; fi
done

# ── 6. Summary ──────────────────────────────────────────────────────────────
echo ""
echo "[6/6] Summary"
if [[ ${#WARNINGS[@]} -gt 0 ]]; then
    echo "  Warnings:"
    for w in "${WARNINGS[@]}"; do echo "    ⚠ $w"; done
fi
if [[ ${#FAILED_STEPS[@]} -gt 0 ]]; then
    echo "  FAILED:"
    for f in "${FAILED_STEPS[@]}"; do echo "    ✗ $f"; done
    echo ""
    echo "  Deploy completed WITH FAILURES."
    exit 1
fi
echo "  Deploy OK."
echo ""
