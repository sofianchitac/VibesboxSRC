#!/bin/bash
# ══════════════════════════════════════════════════════════════════════════════
# Vibesbox SRC — Master Installation Script
# Target: Raspberry Pi 5 on Raspberry Pi OS Lite 64-bit (Trixie, kernel 6.12+).
#         Still runs on a Pi 4B / Bookworm (the pre-2026-07 hardware) — the
#         distro- and model-specific steps below branch on os-release / device-tree.
# Usage:  sudo bash install.sh
#         VIBESBOX_BUILD_LIBREMPEG=1 sudo bash install.sh   # also build librempeg
#         DSP_HOST=my-dsp-box.local sudo bash install.sh    # set the DSP unit's hostname
# ══════════════════════════════════════════════════════════════════════════════

INSTALL_DIR="/opt/vibesbox-src"
VENV_DIR="$INSTALL_DIR/venv"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Hostname of the DSP unit (VibesboxDSP), used by the nginx break-glass reverse
# proxy and lattepanda-watcher. Only matters if you run the DSP half of the
# system; harmless otherwise (the break-glass panel just reports unreachable).
DSP_HOST="${DSP_HOST:-vibesbox-dsp.local}"

# ── Resilient error tracking ────────────────────────────────────────────────
# Instead of set -e (which aborts on first failure), we track failures
# and print a summary at the end. Critical steps still exit on failure.
FAILED_STEPS=()
WARNINGS=()

step_ok()   { echo "  ✓ $1"; }
step_fail() { echo "  ✗ $1"; FAILED_STEPS+=("$1"); }
step_warn() { echo "  ⚠ $1"; WARNINGS+=("$1"); }

# ══════════════════════════════════════════════════════════════════════════════
echo ""
echo "╔══════════════════════════════════════════╗"
echo "║       Vibesbox SRC — Installer           ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# ─────────────────────────────────────────────
# 0. Preflight: root check + username prompt
# ─────────────────────────────────────────────
echo "[0/13] Running preflight checks…"

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: This script must be run as root (sudo bash install.sh)."
    exit 1
fi

# Prompt for the system user
read -rp "  System user to run services [default: vibesbox]: " INSTALL_USER
INSTALL_USER="${INSTALL_USER:-vibesbox}"

# Validate user exists or offer to create
if ! id -u "$INSTALL_USER" &>/dev/null; then
    read -rp "  User '$INSTALL_USER' does not exist. Create it? [Y/n]: " CREATE_USER
    CREATE_USER="${CREATE_USER:-Y}"
    if [[ "$CREATE_USER" =~ ^[Yy] ]]; then
        adduser --disabled-password --gecos "Vibesbox SRC" "$INSTALL_USER"
        step_ok "Created user '$INSTALL_USER'"
    else
        echo "ERROR: User '$INSTALL_USER' does not exist. Cannot continue."
        exit 1
    fi
fi

echo "  Using user: $INSTALL_USER"
echo "  Install dir: $INSTALL_DIR"

# Distro + model detection, used by the branching steps below.
. /etc/os-release
PI_MODEL=$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo "unknown")
echo "  Distro: ${PRETTY_NAME:-unknown} | Model: $PI_MODEL"
echo ""

# ─────────────────────────────────────────────
# 1. System update
# ─────────────────────────────────────────────
echo "[1/13] Updating system packages…"
if apt-get update && apt-get upgrade -y; then
    step_ok "System packages updated"
else
    step_warn "System update had issues (non-fatal, continuing)"
fi

# ─────────────────────────────────────────────
# 2. System dependencies
# ─────────────────────────────────────────────
echo "[2/13] Installing system dependencies…"
if apt-get install -y \
    build-essential \
    git \
    pkg-config nasm yasm \
    python3 python3-pip python3-venv python3-pyalsa \
    alsa-utils \
    device-tree-compiler \
    libasound2-dev \
    avahi-daemon \
    nginx \
    squeezelite \
    shairport-sync \
    bluez \
    bluez-tools \
    ffmpeg \
    libwebsockets-dev \
    qml-qt6 \
    qml6-module-qtquick \
    qml6-module-qtquick-window \
    qml6-module-qtwebsockets \
    qml6-module-qtqml \
    qml6-module-qtqml-workerscript \
    qml6-module-qt5compat-graphicaleffects \
    libqt6svg6 \
    qt6-wayland \
    greetd \
    libffi-dev \
    libyaml-dev \
    libasound2-plugins \
    seatd \
    plymouth \
    plymouth-themes \
    docker.io \
    wget curl unzip; then
    step_ok "System dependencies installed"
else
    step_fail "Some system dependencies failed to install"
fi
# greetd: vt1 session manager — launches scripts/kiosk.sh as the configured user,
#   which runs the native QML dashboard (ui-qml/) straight on KMS/DRM via Qt
#   eglfs. No compositor (sway removed 2026-07-22).
# qml-qt6 + qml6-module-*: runtime + QML modules for the touchscreen dashboard
#   (Qt 6.4 on bookworm / 6.8 on trixie — the app targets the 6.4 baseline).
#   libqt6svg6 = SVG image plugin (logos/icons); qt6-wayland = Wayland platform
#   (kept for remote-session dev runs).
# bluez-tools: provides /usr/bin/bt-agent required by bt-agent.service.
#   (bluez-alsa-utils was dropped: PipeWire's bluez5 stack owns A2DP; installing
#   bluealsa only meant disabling it again in step 12.)
# ffmpeg: decoder for the TV IEC 61937 bitstream path (earc-bitstream-bridge.sh).
#   The production resampler is the native Rust ardftsrc bridge [3b]; the librempeg
#   source build [3a] is only the legacy rollback engine and is skipped by default.
# libwebsockets-dev: required to compile camilladsp-setrate from source.
# nginx: serves the UI on port 80 for both touchscreen and remote LAN access.
# docker.io: runs the Dockerised Tidal Connect armhf binaries (tidal-connect.service);
#   the Pi's CPU executes the armhf image natively (no qemu) — NEEDS the 4K-page
#   kernel on a Pi 5, see step 13. See [3c] and tidal-connect/.

# ─────────────────────────────────────────────
# 2a. PipeWire + WirePlumber (v2 audio backbone, from bookworm-backports)
# ─────────────────────────────────────────────
# v2 routes every source through a system-wide PipeWire graph (96 kHz, summed by
# WirePlumber), replacing the v1 CamillaDSP hot-swap. The v2 rules need
# PipeWire >= 1.4 / WirePlumber >= 0.5 (validated set: 1.4.2 / 0.5.8):
#   Trixie (Debian 13): ships that natively — plain install, no pin, no hold.
#   Bookworm: the Pi-OS/rpt 1.2.x packages are too old, so pin from
#     bookworm-backports and apt-mark hold every resulting package so a future
#     rpt-flavoured 1.2.x (higher apt priority) can't silently downgrade them.
# --no-install-recommends keeps pipewire-pulse out (we use the pipewire-alsa shim only).
echo "[2a] Installing PipeWire/WirePlumber…"
PW_PKGS="pipewire pipewire-bin pipewire-alsa wireplumber libspa-0.2-bluetooth"
if [ "${VERSION_CODENAME:-}" = "bookworm" ]; then
    if ! grep -rqs "bookworm-backports" /etc/apt/sources.list /etc/apt/sources.list.d/; then
        echo "deb http://deb.debian.org/debian bookworm-backports main" \
            > /etc/apt/sources.list.d/bookworm-backports.list
        apt-get update
    fi
    if apt-get install -y -t bookworm-backports --no-install-recommends $PW_PKGS; then
        # Hold every installed PipeWire/WirePlumber/SPA package (robust vs. a hardcoded list).
        HOLD_PKGS=$(dpkg-query -W -f='${Package}\n' 2>/dev/null \
            | grep -E '^(pipewire|wireplumber|libwireplumber|libpipewire|libspa)' || true)
        [ -n "$HOLD_PKGS" ] && apt-mark hold $HOLD_PKGS >/dev/null
        step_ok "PipeWire/WirePlumber installed from bookworm-backports and held"
    else
        step_fail "PipeWire/WirePlumber install (bookworm-backports) — v2 audio graph will not start"
    fi
else
    if apt-get install -y --no-install-recommends $PW_PKGS; then
        step_ok "PipeWire/WirePlumber installed ($(pipewire --version 2>/dev/null | head -1))"
    else
        step_fail "PipeWire/WirePlumber install — v2 audio graph will not start"
    fi
fi

# ─────────────────────────────────────────────
# 3. CamillaDSP v4.1.3 (PipeWire-enabled build)
# ─────────────────────────────────────────────
# v2 uses CamillaDSP's NATIVE PipeWire backend (dsp_6ch/dsp_2ch.yml: type: PipeWire),
# so we install the `-pipewire` release asset, not the plain ALSA-only one.
echo "[3/13] Installing CamillaDSP v4.1.3 (PipeWire build)…"
CDSP_VERSION="4.1.3"
CDSP_ARCH="aarch64"
if wget -q \
    "https://github.com/HEnquist/camilladsp/releases/download/v${CDSP_VERSION}/camilladsp-linux-pipewire-${CDSP_ARCH}.tar.gz" \
    -O /tmp/camilladsp.tar.gz && \
    tar -xzf /tmp/camilladsp.tar.gz -C /usr/local/bin/ && \
    chmod +x /usr/local/bin/camilladsp; then
    rm -f /tmp/camilladsp.tar.gz
    step_ok "CamillaDSP $(camilladsp --version 2>/dev/null || echo 'v4.1.3')"
else
    rm -f /tmp/camilladsp.tar.gz
    step_fail "CamillaDSP installation"
fi

# ─────────────────────────────────────────────
# 3a. librempeg (FFmpeg fork with ARDFTSRC filter — LEGACY rollback engine)
# ─────────────────────────────────────────────
# librempeg is Paul B Mahol's FFmpeg fork containing the ardftsrc audio filter
# (DFT-based sample-rate converter). It was the v2 production resampler; since
# the v3 cutover the native Rust bridge [3b] is production and librempeg is only
# the instant-rollback engine for scripts/ardftsrc_bridge.sh (retired). The TV
# bitstream decode path uses distro ffmpeg (step 2). The ~45 min source build is
# therefore SKIPPED by default — opt in with VIBESBOX_BUILD_LIBREMPEG=1.
echo "[3a] librempeg (FFmpeg fork w/ ARDFTSRC — legacy rollback engine)…"
NEED_LIBREMPEG=0
if [ "${VIBESBOX_BUILD_LIBREMPEG:-0}" = "1" ]; then
    NEED_LIBREMPEG=1
    if [ -x /usr/local/bin/ffmpeg ] && \
       /usr/local/bin/ffmpeg -hide_banner -filters 2>/dev/null | grep -q '\bardftsrc\b'; then
        step_ok "librempeg already installed with ardftsrc — skipping build"
        NEED_LIBREMPEG=0
    fi
else
    step_ok "librempeg build skipped (v3 Rust bridge is production; set VIBESBOX_BUILD_LIBREMPEG=1 to build)"
fi

if [ "$NEED_LIBREMPEG" = "1" ]; then
    echo "  Source-building librempeg — this takes ~45 min on a Pi 4B."
    LIBREMPEG_SRC="/tmp/librempeg-build"
    rm -rf "$LIBREMPEG_SRC"
    # --enable-agpl is required for the ffmpeg CLI binary in librempeg
    # (the ardftsrc filter is in libavfilter under GPL, but the ffmpeg
    # binary's deps include 'agpl' per librempeg's configure script).
    if git clone --depth=1 https://github.com/librempeg/librempeg.git "$LIBREMPEG_SRC" && \
       ( cd "$LIBREMPEG_SRC" && \
         ./configure --prefix=/usr/local --enable-gpl --enable-agpl \
             --disable-doc --disable-htmlpages --disable-manpages \
             --disable-podpages --disable-txtpages --disable-ffplay \
             > /tmp/librempeg-configure.log 2>&1 && \
         make -j"$(nproc)" > /tmp/librempeg-make.log 2>&1 && \
         make install > /tmp/librempeg-install.log 2>&1 ); then
        rm -rf "$LIBREMPEG_SRC"
        # Re-verify ardftsrc presence
        if /usr/local/bin/ffmpeg -hide_banner -filters 2>/dev/null | grep -q '\bardftsrc\b'; then
            step_ok "librempeg built and installed (ardftsrc available)"
        else
            step_warn "librempeg built but ardftsrc filter not found — rollback engine unavailable"
        fi
    else
        step_warn "librempeg build failed — see /tmp/librempeg-{configure,make,install}.log (rollback engine unavailable)"
    fi
fi

# ─────────────────────────────────────────────
# 3b. v3 native Rust ardftsrc bridge
# ─────────────────────────────────────────────
# v3 replaces the ffmpeg/librempeg per-source bridge with a native Rust binary
# (ardftsrc crate). Once ardftsrc-bridge@.service ExecStart points here, THIS is the
# production resampler (no binary = silent box, hence step_fail). librempeg above is
# kept on disk as a rollback engine. Build happens in a user-owned temp copy so the
# crate's target/ doesn't need a writable repo checkout.
echo "[3b] Building the v3 native Rust ardftsrc bridge…"
BRIDGE_SRC="$SCRIPT_DIR/ardftsrc-bridge-rs"
BRIDGE_BUILD="/tmp/ardftsrc-bridge-build"
USER_CARGO="/home/$INSTALL_USER/.cargo/bin/cargo"

if [ ! -x "$USER_CARGO" ]; then
    echo "  Installing Rust toolchain (rustup, minimal) for $INSTALL_USER…"
    sudo -u "$INSTALL_USER" -H bash -c \
        "curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain stable" \
        > /tmp/rustup-install.log 2>&1 \
        && step_ok "Rust toolchain installed" \
        || step_warn "Rust toolchain install had issues — see /tmp/rustup-install.log"
fi

if [ -x "$USER_CARGO" ]; then
    rm -rf "$BRIDGE_BUILD"
    cp -r "$BRIDGE_SRC" "$BRIDGE_BUILD"
    chown -R "$INSTALL_USER:$INSTALL_USER" "$BRIDGE_BUILD"
    if sudo -u "$INSTALL_USER" -H bash -c \
        "cd '$BRIDGE_BUILD' && '$USER_CARGO' build --release --bin ardftsrc-bridge" \
        > /tmp/ardftsrc-bridge-build.log 2>&1; then
        mkdir -p "$INSTALL_DIR/bin"
        cp "$BRIDGE_BUILD/target/release/ardftsrc-bridge" "$INSTALL_DIR/bin/ardftsrc-bridge"
        chmod 755 "$INSTALL_DIR/bin/ardftsrc-bridge"
        rm -rf "$BRIDGE_BUILD"
        step_ok "ardftsrc-bridge (Rust v3) built and installed to $INSTALL_DIR/bin/"
    else
        step_fail "ardftsrc-bridge (Rust) build failed — see /tmp/ardftsrc-bridge-build.log (v3 resampling unavailable)"
    fi
else
    step_fail "cargo not found for $INSTALL_USER — cannot build ardftsrc-bridge (v3 resampling unavailable)"
fi

# ─────────────────────────────────────────────
# 3c. Tidal Connect Docker image (iFi closed-source armhf binaries)
# ─────────────────────────────────────────────
# Tidal Connect ships as iFi's closed-source 32-bit ARM binaries built for Raspbian Stretch;
# they can't run on the Pi's 64-bit userland, so they run inside TonyTromp/tidal-connect-docker's
# armhf image (the Pi's Cortex-A72 runs AArch32 natively — no qemu). We build it locally from a
# PINNED commit and tag it vibesbox-tidal-connect:latest; tidal-connect.service runs it always-on
# as a source feeding the Tidal snd-aloop. The build needs internet (the Dockerfile pulls old debs).
echo "[3c] Building the Tidal Connect Docker image…"
TIDAL_IMAGE="vibesbox-tidal-connect:latest"
TIDAL_REPO="https://github.com/TonyTromp/tidal-connect-docker.git"
TIDAL_REF="690b76ff8c6596f2e66b347b875544e5607ca645"   # pinned 2025-12-21
if ! command -v docker >/dev/null 2>&1; then
    step_fail "docker not installed — cannot build Tidal Connect image (Tidal source unavailable)"
elif docker image inspect "$TIDAL_IMAGE" >/dev/null 2>&1; then
    step_ok "Tidal Connect image already present ($TIDAL_IMAGE) — skipping build"
else
    systemctl enable --now docker.service >/dev/null 2>&1 || true
    TIDAL_BUILD="/tmp/tidal-connect-docker"
    rm -rf "$TIDAL_BUILD"
    # Full clone (not --depth=1) so the pinned sha is checkoutable even after upstream moves.
    if git clone "$TIDAL_REPO" "$TIDAL_BUILD" \
       && ( cd "$TIDAL_BUILD" && git checkout -q "$TIDAL_REF" \
            && docker build -f Docker/Dockerfile -t "$TIDAL_IMAGE" . ) \
            > /tmp/tidal-connect-build.log 2>&1; then
        rm -rf "$TIDAL_BUILD"
        step_ok "Tidal Connect image built ($TIDAL_IMAGE)"
    else
        step_fail "Tidal Connect image build failed — see /tmp/tidal-connect-build.log (Tidal source unavailable)"
    fi
fi

# ─────────────────────────────────────────────
# 5. Python virtualenv + dependencies
# ─────────────────────────────────────────────
echo "[5/13] Creating Python venv and installing packages…"
python3 -m venv --system-site-packages "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip

# Core dependencies (required):
#   websockets + pyyaml            — source_router.py (:8080 WS server, config push)
#   aiohttp + shazamio             — nowplaying_server.py (HTTP/WS API, fingerprinting)
#   dbus-next                      — producers/bluealsa.py (AVRCP metadata via org.bluez)
#   numpy + pyalsaaudio            — ndi_transmitter.py / fingerprint_capture.py
# (ndi-python and sounddevice were dropped 2026-07-13: nothing imports them —
#  ndi_transmitter.py drives libndi.so via ctypes — and ndi-python has no aarch64
#  wheel, so it source-built or failed on every fresh install.)
#
# audioop-lts (Python >= 3.13 only): PEP 594 removed the stdlib `audioop` module in
# 3.13, which Trixie ships. shazamio -> pydub falls back to `import pyaudioop`, so
# nowplaying_server crash-loops on ModuleNotFoundError. audioop-lts supplies both
# names. Bookworm/py3.11 never hit this. (Found on the Pi 5 fresh flash 2026-07-27.)
PY_MINOR=$("$VENV_DIR/bin/python" -c 'import sys; print(sys.version_info[1])')
if [ "$PY_MINOR" -ge 13 ]; then
    EXTRA_PY_PKGS="audioop-lts"
else
    EXTRA_PY_PKGS=""
fi
if "$VENV_DIR/bin/pip" install \
    ${EXTRA_PY_PKGS} \
    git+https://github.com/HEnquist/pycamilladsp.git \
    websockets pyyaml \
    numpy pyalsaaudio \
    aiohttp aiohttp-cors jsonschema \
    shazamio \
    dbus-next \
    "camilladsp-plot[plot] @ git+https://github.com/HEnquist/pycamilladsp-plot.git"; then
    step_ok "Core Python packages installed"
else
    step_fail "Core Python packages"
fi

# ─────────────────────────────────────────────
# 5a. NDI SDK (optional)
# ─────────────────────────────────────────────
echo ""
echo "  NDI SDK is REQUIRED — NDI is the only output transport (no audio"
echo "  leaves the box without it). Download from"
echo "  https://ndi.video/for-developers/ndi-sdk/ and install to"
echo "  /usr/local/lib/libndi.so"
echo ""

if [ -f /usr/local/lib/libndi.so ]; then
    step_ok "NDI SDK found at /usr/local/lib/libndi.so"
else
    read -rp "  NDI SDK not found. Do you have the NDI SDK installer (.tar.gz)? [y/N]: " HAS_NDI
    if [[ "$HAS_NDI" =~ ^[Yy] ]]; then
        read -rp "  Enter full path to the NDI SDK .tar.gz: " NDI_PATH
        if [ -f "$NDI_PATH" ]; then
            TMPNDI=$(mktemp -d)
            if tar -xzf "$NDI_PATH" -C "$TMPNDI" 2>/dev/null; then
                # The official Linux SDK tarball does NOT contain lib/ — it contains a
                # single self-extracting installer (EULA via $PAGER, then `read` for y).
                # Run it non-interactively; PAGER=cat stops `more` blocking on no TTY.
                # (Before 2026-07-27 this step only did the find below, which could
                #  never match, so the SDK "installed" as a warning and the box stayed
                #  silent.) Older/hand-rolled tarballs that DO ship lib/ still work —
                #  the find runs either way.
                NDI_SFX=$(find "$TMPNDI" -maxdepth 2 -name "Install_NDI_SDK*.sh" -type f | head -1)
                if [ -n "$NDI_SFX" ]; then
                    ( cd "$(dirname "$NDI_SFX")" && printf 'y\n' | PAGER=cat sh "$NDI_SFX" ) >/dev/null 2>&1
                fi
                # Prefer the real versioned object over the symlink, and pick the arch
                # matching this machine (the SDK ships x86_64 + several ARM variants).
                NDI_ARCH=$(uname -m)
                NDI_LIB=$(find "$TMPNDI" -type f -name "libndi.so.*" -path "*${NDI_ARCH}*" | head -1)
                [ -n "$NDI_LIB" ] || NDI_LIB=$(find "$TMPNDI" -type f -name "libndi.so.*" | head -1)
                if [ -n "$NDI_LIB" ]; then
                    NDI_SOName=$(basename "$NDI_LIB")
                    cp "$NDI_LIB" "/usr/local/lib/$NDI_SOName"
                    ln -sf "$NDI_SOName" /usr/local/lib/libndi.so
                    ln -sf "$NDI_SOName" /usr/local/lib/libndi.so.6
                    ldconfig
                    # ctypes load is the real test — ndi_transmitter.py opens it this way.
                    if python3 -c "import ctypes,sys; ctypes.CDLL('/usr/local/lib/libndi.so')" 2>/dev/null; then
                        step_ok "NDI SDK installed ($NDI_SOName, $NDI_ARCH) and loads"
                    else
                        step_fail "libndi.so copied but will not load (wrong arch/libc?)"
                    fi
                else
                    step_warn "Could not find libndi.so in archive — install manually"
                fi
            else
                step_warn "Failed to extract NDI SDK archive"
            fi
            rm -rf "$TMPNDI"
        else
            step_fail "NDI SDK file not found at: $NDI_PATH"
        fi
    else
        step_fail "NDI SDK not installed — NDI is the ONLY output transport, the box is silent without it"
    fi
fi

# ─────────────────────────────────────────────
# 6. Deploy project files
# ─────────────────────────────────────────────
echo "[6/13] Deploying project files to ${INSTALL_DIR}…"
mkdir -p "$INSTALL_DIR"
cp -r "$SCRIPT_DIR"/scripts    "$INSTALL_DIR/"
cp -r "$SCRIPT_DIR"/camilladsp "$INSTALL_DIR/"
cp -r "$SCRIPT_DIR"/alsa       "$INSTALL_DIR/"
cp -r "$SCRIPT_DIR"/services   "$INSTALL_DIR/"
cp -r "$SCRIPT_DIR"/ui         "$INSTALL_DIR/"
cp -r "$SCRIPT_DIR"/ui-qml     "$INSTALL_DIR/"
cp -r "$SCRIPT_DIR"/config     "$INSTALL_DIR/"
cp -r "$SCRIPT_DIR"/tidal-connect "$INSTALL_DIR/"
# Diagnostic probes (latency, eARC). Not needed at runtime, but they must survive a
# reboot: they were previously hand-copied to /tmp for every investigation. They import
# numpy, so run them with $VENV_DIR/bin/python3 — the system interpreter has none.
cp -r "$SCRIPT_DIR"/tools      "$INSTALL_DIR/"

chmod +x "$INSTALL_DIR"/scripts/*.sh

# State directory for persisted daemon values (e.g. output_channels)
mkdir -p "$INSTALL_DIR/state"
chown -R "$INSTALL_USER:$INSTALL_USER" "$INSTALL_DIR"

# UI files must be world-readable for nginx (runs as www-data)
chmod -R a+rX "$INSTALL_DIR/ui"

# Remote dashboard: copy the example config to the deployed config on first
# install only. Subsequent installs preserve whatever the operator wrote
# (LattePanda host + break-glass token), since this is per-deployment state.
if [ -f "$INSTALL_DIR/ui/remote/config.example.json" ] \
        && [ ! -f "$INSTALL_DIR/ui/remote/config.json" ]; then
    cp "$INSTALL_DIR/ui/remote/config.example.json" "$INSTALL_DIR/ui/remote/config.json"
    chmod a+r "$INSTALL_DIR/ui/remote/config.json"
    step_warn "Remote dashboard config.json seeded from example — edit /opt/vibesbox-src/ui/remote/config.json with the LattePanda host and break-glass token."
fi

# ── Replace __USER__ placeholder in service files and configs ────────────
echo "  Applying user '$INSTALL_USER' to service files…"
find "$INSTALL_DIR/services" "$INSTALL_DIR/config" -type f \
    -exec sed -i "s|__USER__|${INSTALL_USER}|g" {} +

step_ok "Project files deployed"

# ─────────────────────────────────────────────
# 7. ALSA configuration
# ─────────────────────────────────────────────
echo "[7/13] Installing ALSA configuration…"
cp "$INSTALL_DIR/alsa/asound.conf" /etc/asound.conf
step_ok "ALSA config installed"

# ─────────────────────────────────────────────
# 7a. PipeWire/WirePlumber system config + user (v2)
# ─────────────────────────────────────────────
echo "[7a] Installing PipeWire/WirePlumber system config…"

# Dedicated unprivileged account the system-mode pipewire.service / wireplumber.service
# run as (headless appliance: PipeWire runs system-wide, not per-login-session).
getent group pipewire >/dev/null || groupadd --system pipewire
if ! id -u pipewire &>/dev/null; then
    useradd --system --gid pipewire --no-create-home --shell /usr/sbin/nologin \
        -G audio,bluetooth pipewire
    step_ok "Created system user 'pipewire'"
else
    usermod -a -G audio,bluetooth pipewire
    step_ok "System user 'pipewire' already present"
fi

# 96 kHz clock pin + resampler quality drop-ins → /etc/pipewire/pipewire.conf.d/
mkdir -p /etc/pipewire/pipewire.conf.d
cp "$INSTALL_DIR"/config/pipewire/pipewire.conf.d/*.conf /etc/pipewire/pipewire.conf.d/

# Vibesbox node rules (sink.ndi-feed, source.usb) → WirePlumber
mkdir -p /etc/wireplumber/wireplumber.conf.d
cp "$INSTALL_DIR"/config/wireplumber/wireplumber.conf.d/*.conf /etc/wireplumber/wireplumber.conf.d/

# Mask the distro's PER-USER PipeWire/WirePlumber units so the kiosk login session can't
# auto-start a second WirePlumber that grabs the audio devices from the system-mode
# daemons (enable-linger on the kiosk user makes this a real boot-time race).
systemctl --global mask \
    pipewire.socket pipewire.service \
    pipewire-pulse.socket pipewire-pulse.service \
    wireplumber.service wireplumber@.service \
    filter-chain.service 2>/dev/null || true

step_ok "PipeWire/WirePlumber system config installed"

# ─────────────────────────────────────────────
# 8. Audio service configurations
# ─────────────────────────────────────────────
echo "[8/13] Configuring audio services…"
cp "$INSTALL_DIR/services/squeezelite.conf"    /etc/default/squeezelite
cp "$INSTALL_DIR/services/shairport-sync.conf" /etc/shairport-sync.conf

# Now Playing env file — installed once, never overwritten so user edits survive
mkdir -p /etc/vibesbox
if [ ! -f /etc/vibesbox/nowplaying.env ]; then
    cp "$INSTALL_DIR/services/nowplaying.env.example" /etc/vibesbox/nowplaying.env
    chmod 644 /etc/vibesbox/nowplaying.env
    step_ok "Now Playing env file installed at /etc/vibesbox/nowplaying.env"
    echo "    → Edit it to set LMS_HOST and LMS_PLAYER_ID for the Lyrion producer."
else
    step_ok "Now Playing env file already present at /etc/vibesbox/nowplaying.env (kept)"
fi

# Tidal Connect env — seeded once from the example, never overwritten so the deploy-time
# PLAYBACK_DEVICE survives re-installs (same rule as nowplaying.env).
if [ ! -f "$INSTALL_DIR/tidal-connect/tidal.env" ]; then
    cp "$INSTALL_DIR/tidal-connect/tidal.env.example" "$INSTALL_DIR/tidal-connect/tidal.env"
    chown "$INSTALL_USER:$INSTALL_USER" "$INSTALL_DIR/tidal-connect/tidal.env"
    step_ok "Tidal env seeded at $INSTALL_DIR/tidal-connect/tidal.env"
    echo "    → Set PLAYBACK_DEVICE (see tidal-connect/README.md) after first container start."
else
    step_ok "Tidal env already present at $INSTALL_DIR/tidal-connect/tidal.env (kept)"
fi

step_ok "Audio services configured"

# ─────────────────────────────────────────────
# 9. Nginx — UI static file server
# ─────────────────────────────────────────────
echo "[9/13] Configuring nginx…"
rm -f /etc/nginx/sites-enabled/default
# The tracked conf carries a `vibesbox-dsp.local` placeholder; substitute the
# deployment's real DSP hostname on the way in. /etc/vibesbox/dsp-host is the
# single source of truth — lattepanda-watcher.sh and nowplaying_server.py both
# read it at runtime.
#
# NOTE the asymmetry: those two read the FILE at process start, but nginx gets
# the hostname baked into its deployed conf by the sed below. Editing the file
# alone therefore moves OSC control and the watcher without moving nginx's
# break-glass proxy, and neither side errors — the stale one just stops working.
# Change the hostname by re-running install.sh with DSP_HOST set, not by hand.
mkdir -p /etc/vibesbox
echo "$DSP_HOST" > /etc/vibesbox/dsp-host
sed "s/vibesbox-dsp\.local/$DSP_HOST/g" \
    "$INSTALL_DIR/config/nginx/nginx-ui.conf" > /etc/nginx/sites-available/vibesbox-ui
ln -sf /etc/nginx/sites-available/vibesbox-ui /etc/nginx/sites-enabled/vibesbox-ui
if nginx -t 2>/dev/null; then
    step_ok "Nginx configured"
else
    step_fail "Nginx config validation"
fi

# ─────────────────────────────────────────────
# 10. greetd — Wayland kiosk session
# ─────────────────────────────────────────────
echo "[10/13] Configuring greetd kiosk session…"
mkdir -p /etc/greetd
cp "$INSTALL_DIR/config/greetd/config.toml" /etc/greetd/config.toml
mkdir -p /etc/systemd/system/greetd.service.d
cp "$INSTALL_DIR/config/greetd.service.d/override.conf" \
   /etc/systemd/system/greetd.service.d/override.conf

# greetd's [Install] section is only "Alias=display-manager.service" — it has no
# WantedBy, so it is pulled in by graphical.target via that alias. RPi OS Lite
# defaults to multi-user.target, which never reaches graphical.target, so greetd
# stays enabled-but-dead and the touchscreen never lights. (Fresh-flash gap found
# 2026-07-27; the Pi 4B had been switched over by hand long ago.)
systemctl set-default graphical.target >/dev/null 2>&1
step_ok "greetd kiosk configured (default target = graphical)"

# ─────────────────────────────────────────────
# 10a. Backlight udev rule
# ─────────────────────────────────────────────
echo "[10a] Installing udev rules…"
cp "$INSTALL_DIR"/config/udev/*.rules /etc/udev/rules.d/
udevadm control --reload-rules
udevadm trigger --subsystem-match=backlight
udevadm trigger --subsystem-match=sound
step_ok "Udev rules installed"

# ─────────────────────────────────────────────
# 10b. Plymouth Boot Splash
# ─────────────────────────────────────────────
echo "[10b] Configuring Plymouth boot splash…"
THEME_DIR="/usr/share/plymouth/themes/vibesbox"
mkdir -p "$THEME_DIR"
cp "$INSTALL_DIR/config/plymouth/vibesbox.plymouth" "$THEME_DIR/"
cp "$INSTALL_DIR/config/plymouth/vibesbox.script" "$THEME_DIR/"
cp "$INSTALL_DIR/ui/splashscreen.png" "$THEME_DIR/"
chmod 644 "$THEME_DIR"/*
plymouth-set-default-theme -R vibesbox

# Disable rainbow splash
sed -i 's/^#disable_splash=1/disable_splash=1/' /boot/firmware/config.txt
if ! grep -q "disable_splash=1" /boot/firmware/config.txt; then
    echo "disable_splash=1" >> /boot/firmware/config.txt
fi

# Modify cmdline.txt
CMDLINE_FILE="/boot/firmware/cmdline.txt"
if [ ! -f "$CMDLINE_FILE" ]; then
    CMDLINE_FILE="/boot/cmdline.txt"
fi
if [ -f "$CMDLINE_FILE" ]; then
    CMD=$(cat "$CMDLINE_FILE")
    CMD=$(echo "$CMD" | sed -E 's/\b(console=tty1|splash|quiet|logo.nologo|consoleblank=[0-9]+|loglevel=[0-9]+|vt.global_cursor_default=[0-9]+|plymouth.ignore-serial-consoles)\b//g')
    CMD="$CMD quiet splash plymouth.ignore-serial-consoles logo.nologo consoleblank=0 loglevel=1 vt.global_cursor_default=0"
    CMD=$(echo "$CMD" | tr -s ' ' | sed 's/^ //;s/ $//')
    echo "$CMD" > "$CMDLINE_FILE"
fi
step_ok "Plymouth boot splash configured"

# ─────────────────────────────────────────────
# 11. Install systemd services
# ─────────────────────────────────────────────
echo "[11/13] Installing systemd services…"
SERVICES=(
    pipewire.service
    wireplumber.service
    source-router.service
    usb-gadget.service
    alsa-loopback.service
    camilladsp.service
    ndi-output.service
    bt-agent.service
    ardftsrc-bridge@.service
    earc-bitstream-bridge@.service
    nowplaying-server.service
    metadata-orchestrator.service
    fingerprint-capture.service
    lattepanda-watcher.service
    lattepanda-watcher.timer
    tidal-connect.service
)

for svc in "${SERVICES[@]}"; do
    cp "$INSTALL_DIR/services/$svc" /etc/systemd/system/
done
systemctl daemon-reload
step_ok "Systemd services installed"

# ─────────────────────────────────────────────
# 12. Enable services
# ─────────────────────────────────────────────
echo "[12/13] Enabling services…"
systemctl enable \
    pipewire.service \
    wireplumber.service \
    source-router.service \
    usb-gadget.service \
    alsa-loopback.service \
    camilladsp.service \
    bt-agent.service \
    squeezelite.service \
    shairport-sync.service \
    avahi-daemon.service \
    nginx.service \
    greetd.service \
    nowplaying-server.service \
    metadata-orchestrator.service \
    fingerprint-capture.service \
    lattepanda-watcher.timer

# NDI output: only enable if NDI SDK is present
if [ -f /usr/local/lib/libndi.so ]; then
    systemctl enable ndi-output.service
    step_ok "NDI output service enabled"
else
    systemctl disable ndi-output.service 2>/dev/null || true
    step_warn "NDI output service NOT enabled (libndi.so not found)"
fi

# Tidal Connect: only enable if the Docker image built successfully (mirrors ndi-output).
# Avoids a crash-looping `docker run` when the image is missing.
if command -v docker >/dev/null 2>&1 && docker image inspect "$TIDAL_IMAGE" >/dev/null 2>&1; then
    systemctl enable docker.service 2>/dev/null || true
    systemctl enable tidal-connect.service
    step_ok "Tidal Connect service enabled"
else
    systemctl disable tidal-connect.service 2>/dev/null || true
    step_warn "Tidal Connect service NOT enabled (image $TIDAL_IMAGE not found)"
fi

# v2: bluealsa (the v1 BT audio path) is retired — PipeWire's bluez5 stack owns A2DP now
# (config/wireplumber/wireplumber.conf.d/53-vibesbox-bluez.conf). bluez-alsa-utils is no
# longer installed (dropped from step 2), but if it is present from an earlier install it
# auto-enables bluealsa[-aplay] via bluetooth.target.wants, which re-grabs the A2DP
# MediaEndpoint at boot and PipeWire's bluez monitor gets org.bluez.Error.NotAuthorized —
# so keep the belt-and-braces disable. AVRCP metadata (BluealsaProducer) reads
# org.bluez.MediaPlayer1 directly and does not need the bluealsa daemon.
systemctl disable --now bluealsa.service bluealsa-aplay.service 2>/dev/null || true

step_ok "Services enabled"

# ─────────────────────────────────────────────
# 13. Hardware overlays, user groups + Bluetooth
# ─────────────────────────────────────────────
echo "[13/13] Configuring hardware overlays, user groups and Bluetooth…"

CONFIG_TXT="/boot/firmware/config.txt"

# Onboard audio off — keeps stray vc4/HDMI ALSA cards out of the graph.
# (The v2-era HiFiBerry Digi2 Pro overlay is gone: the S/PDIF transport was
# retired with the Pi 5 migration — NDI is the only output.)
sed -i 's/^dtparam=audio=on/#dtparam=audio=on/' "$CONFIG_TXT"

# Match the peripheral line specifically, and re-assert [all]. The Trixie stock
# config.txt ships "dtoverlay=dwc2,dr_mode=host" inside a [cm5] block — inert on a
# Pi 5, but a bare "dtoverlay=dwc2" guard matches it and silently skips the gadget
# overlay, leaving the UAC2 USB source permanently absent. (Bit us 2026-07-27.)
if ! grep -q "^dtoverlay=dwc2,dr_mode=peripheral" "$CONFIG_TXT"; then
    printf '\n# USB Gadget Mode (UAC2)\n[all]\ndtoverlay=dwc2,dr_mode=peripheral\n' >> "$CONFIG_TXT"
fi

# Pi 5: force the 4K-page kernel. Raspberry Pi OS defaults the Pi 5 to
# kernel_2712.img (16K pages), and a 16K-page arm64 kernel cannot exec 32-bit
# ARM binaries — which the Tidal Connect armhf Docker image is. kernel8.img
# (4K pages) ships in the same image; this line just selects it.
if echo "$PI_MODEL" | grep -q "Raspberry Pi 5"; then
    if ! grep -q "^kernel=kernel8.img" "$CONFIG_TXT"; then
        printf '\n# 4K-page kernel: required for the armhf Tidal Connect container\nkernel=kernel8.img\n' >> "$CONFIG_TXT"
    fi
    step_ok "Pi 5 detected — kernel8.img (4K pages) pinned for armhf Docker support"

    # The Pi 5 is powered from the GPIO header (the USB-C port is the UAC2 gadget
    # data link, VBUS cut in the cable), so no USB-PD negotiation ever happens and
    # the firmware assumes a 3A supply — which caps TOTAL downstream USB current at
    # 600mA and nags about low power. Nothing currently hangs off the USB-A ports, so
    # this is pre-emptive; the failure it prevents is not a clean one (a peripheral
    # that intermittently refuses to enumerate). See docs/pi5-migration.md §0a.
    if ! grep -q "^usb_max_current_enable=1" "$CONFIG_TXT"; then
        printf '\n# GPIO-powered: no PD negotiation, so lift the 600mA downstream USB cap\nusb_max_current_enable=1\n' >> "$CONFIG_TXT"
    fi
    step_ok "Pi 5 detected — 600mA downstream USB cap lifted (GPIO-powered, no PD)"

    # eARC I2S tap overlay — Pi 5 ONLY (targets the RP1's i2s1 slave instance,
    # which does not exist on the Pi 4B). Harmless with no tap soldered: it just
    # publishes an "eARC" capture card that never sees a bit clock.
    # Details + bring-up: config/overlays/README.md
    if dtc -@ -I dts -O dtb \
           -o /boot/firmware/overlays/vibesbox-earc-tap.dtbo \
           "$INSTALL_DIR/config/overlays/vibesbox-earc-tap-overlay.dts" 2>/dev/null; then
        if ! grep -q "^dtoverlay=vibesbox-earc-tap" "$CONFIG_TXT"; then
            printf '\n# eARC I2S tap (SiI9437 in the Lindy 38368) -> RP1 i2s1 slave, 8ch capture\ndtoverlay=vibesbox-earc-tap\n' >> "$CONFIG_TXT"
        fi
        step_ok "eARC I2S tap overlay compiled and enabled"
    else
        step_warn "eARC I2S tap overlay failed to compile — TV eARC capture unavailable"
    fi
fi

grep -q "^dwc2"         /etc/modules || echo "dwc2"         >> /etc/modules
grep -q "^libcomposite" /etc/modules || echo "libcomposite"  >> /etc/modules

# User needs:
#   audio  — ALSA device access (loopbacks/UAC2 gadget/USB capture cards)
#   video  — KMS/DRM display access (Qt eglfs)
#   render — GPU acceleration (V3D)
#   input  — touchscreen input events (Qt libinput)
#   bluetooth — Manage Bluetooth adapters/pairing
usermod -a -G audio,video,render,input,bluetooth "$INSTALL_USER"

# Enable user lingering to allow services to stay active after logout/reboot
loginctl enable-linger "$INSTALL_USER"

if ! grep -q "AutoEnable=true" /etc/bluetooth/main.conf 2>/dev/null; then
    sed -i '/^\[Policy\]/a AutoEnable=true' /etc/bluetooth/main.conf 2>/dev/null || \
        printf '\n[Policy]\nAutoEnable=true\n' >> /etc/bluetooth/main.conf
fi

# Advertise as a Bluetooth LOUDSPEAKER (CoD 0x240414: Audio/Video major + Loudspeaker
# minor + Audio/Rendering service). The default RPi class is "uncategorized" so source
# devices that filter their "pair a speaker" list by device class — TVs especially —
# never show the Vibesbox. Phones are permissive and don't need this. BlueZ keeps the
# device-class (major/minor) from this value and recomputes the service bits itself.
if grep -qE '^#?Class = ' /etc/bluetooth/main.conf 2>/dev/null; then
    sed -i -E 's|^#?Class = .*|Class = 0x240414|' /etc/bluetooth/main.conf
else
    sed -i '/^\[General\]/a Class = 0x240414' /etc/bluetooth/main.conf
fi

# Allow headless "Just Works" RE-pairing. TVs (LG webOS, Google/Android TV) tear down
# their side of the bond after a connect and then re-pair from scratch on every reconnect;
# with BlueZ's default JustWorksRepairing=never the box still holds the old link key, so the
# re-pair is rejected and the TV shows "Couldn't pair" indefinitely. Phones don't re-pair so
# they were unaffected. "always" lets the re-pair through — acceptable for a fixed-location
# appliance speaker. (Pairing still only succeeds inside the UI's discoverable window.)
if grep -qE '^#?JustWorksRepairing = ' /etc/bluetooth/main.conf 2>/dev/null; then
    sed -i -E 's|^#?JustWorksRepairing = .*|JustWorksRepairing = always|' /etc/bluetooth/main.conf
else
    sed -i '/^\[General\]/a JustWorksRepairing = always' /etc/bluetooth/main.conf
fi

# Ensure Bluetooth is not soft-blocked by the kernel
rfkill unblock bluetooth 2>/dev/null || true

# Invisible Pixel Overwrite: Replace system-wide arrows with transparent 1x1 cursors
# This is a binary 68-byte valid Xcursor file that renders nothing.
TRANS_CURSOR_HEX="586375721000000000000100010000000200FDFF010000001C000000240000000200FDFF0100000000000100000000000000000001000000010000000000000000000000"
printf "$(echo $TRANS_CURSOR_HEX | sed 's/../\\x&/g')" > /tmp/trans_cursor

# Overwrite 'left_ptr' (the arrow) in common theme paths
for theme in PiXflat Adwaita hicolor default; do
    mkdir -p /usr/share/icons/$theme/cursors
    cp /tmp/trans_cursor /usr/share/icons/$theme/cursors/left_ptr
    # Also overwrite some other common pointers
    for ptr in hand2 xterm cross watch default; do
        cp /tmp/trans_cursor /usr/share/icons/$theme/cursors/$ptr 2>/dev/null || true
    done
done

# Force Touch-Only Mode: Strip pointer/mouse capabilities from touchscreens
# When LabWC sees zero mice connected, it automatically hides the cursor.
printf 'ACTION=="add|change", KERNEL=="event*", ENV{ID_INPUT_TOUCHSCREEN}=="1", ENV{ID_INPUT_MOUSE}="0", ENV{ID_INPUT_PTR}="0"\n' > /etc/udev/rules.d/99-force-touch-only.rules

# Kernel Console Fix: Disable the text cursor at boot
CMDLINE_PATH="/boot/cmdline.txt"
[ -f /boot/firmware/cmdline.txt ] && CMDLINE_PATH="/boot/firmware/cmdline.txt"
if ! grep -q "vt.global_cursor_default=0" "$CMDLINE_PATH"; then
    sed -i '$ s/$/ vt.global_cursor_default=0/' "$CMDLINE_PATH"
fi

udevadm control --reload-rules && udevadm trigger

# Hub/Peripheral handling: Ensure seatd and greetd are configured correctly
# We do this after the udev rules to ensure permissions are caught.
step_ok "Hardware, kernel, and input rules configured"

# Set hostname to vibesbox-src
if [ "$(hostname)" != "vibesbox-src" ]; then
    hostnamectl set-hostname vibesbox-src 2>/dev/null || \
        echo "vibesbox-src" > /etc/hostname
    step_ok "Hostname set to vibesbox-src"
fi

# ══════════════════════════════════════════════════════════════════════════════
# Summary
# ══════════════════════════════════════════════════════════════════════════════
echo ""
echo "╔══════════════════════════════════════════╗"
echo "║       Installation Summary                ║"
echo "╚══════════════════════════════════════════╝"
echo ""

if [ ${#FAILED_STEPS[@]} -eq 0 ] && [ ${#WARNINGS[@]} -eq 0 ]; then
    echo "  ✓ All steps completed successfully!"
else
    if [ ${#FAILED_STEPS[@]} -gt 0 ]; then
        echo "  ✗ FAILED steps (require attention):"
        for f in "${FAILED_STEPS[@]}"; do
            echo "      - $f"
        done
        echo ""
    fi
    if [ ${#WARNINGS[@]} -gt 0 ]; then
        echo "  ⚠ WARNINGS (non-critical):"
        for w in "${WARNINGS[@]}"; do
            echo "      - $w"
        done
        echo ""
    fi
fi

echo "  User:      $INSTALL_USER"
echo "  Install:   $INSTALL_DIR"
echo "  Hostname:  vibesbox-src"
echo ""
echo "  Please reboot for all changes to take effect:"
echo "    sudo reboot"
echo ""
echo "  After reboot, verify core services:"
echo "    systemctl status pipewire wireplumber source-router camilladsp usb-gadget alsa-loopback nginx greetd"
echo ""
echo "  Dashboard access:"
echo "    Touchscreen : launches automatically via greetd (Qt eglfs, no compositor)"
echo "    LAN browser : http://vibesbox-src.local"
echo ""

if [ ! -f /usr/local/lib/libndi.so ]; then
    echo "  ✗ NDI SDK still needs to be installed — it is the ONLY output transport."
    echo "    Download from https://ndi.video/for-developers/ndi-sdk/"
    echo "    Then copy libndi.so to /usr/local/lib/, run: sudo ldconfig,"
    echo "    and enable the transmitter: sudo systemctl enable ndi-output.service"
    echo ""
fi
