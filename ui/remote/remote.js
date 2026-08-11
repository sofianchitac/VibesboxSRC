/**
 * Vibesbox Remote Dashboard
 *
 * Wires three independent panels:
 *   - SRC mirror      ← ws://<this-host>:8080      (auto_router state frame)
 *   - Now Playing     ← ws://<this-host>:8090/ws   (nowplaying_server)
 *   - DSP controls    → /control/*                 (Pi nginx → :8090, which
 *                                                   sends OSC on to REAPER)
 *   - Break-Glass     → /breakglass/*              (Pi nginx reverse-proxies
 *                                                   to the LattePanda's
 *                                                   :8091/api/breakglass/*)
 *
 * Run-time config lives in /remote/config.json (deployed copy of
 * config.example.json). The page fetches it on load. Only the shared-secret
 * token lives there — the LattePanda's address is the Pi's problem.
 */

const HOST     = window.location.hostname || 'localhost';
const SRC_WS   = `ws://${HOST}:8080`;
const NP_WS    = `ws://${HOST}:8090/ws`;
const BG_BASE  = '/breakglass';   // same origin; Pi nginx proxies to LattePanda
const RECONNECT_DELAY = 3000;
const ARM_TIMEOUT_MS  = 4000;

let bgToken = null;   // populated from config.json
let srcWs   = null;   // live SRC WebSocket, re-assigned on every (re)connect
let srcMuted = {};    // mirrors daemon state.sources_muted (for Unmute All)

// ─── Connection-status helper ────────────────────────────────────────────
function setDot(id, state, title) {
    const el = document.getElementById(id);
    if (!el) return;
    el.classList.remove('connected', 'error');
    if (state === 'connected') el.classList.add('connected');
    else if (state === 'error') el.classList.add('error');
    if (title) el.title = title;
}

// ════════════════════════════════════════════════════════════════════════
//  SRC panel — auto_router :8080
// ════════════════════════════════════════════════════════════════════════

const SOURCE_DISPLAY = { Lyrion: 'Squeeze' };
const IDLE_LABELS    = { USB: 'WAITING', AirPlay: 'LISTENING', Tidal: 'READY' };
// Cosmetic sort hint only — the pill LIST comes from the daemon's state frame
// (sources_muted keys), so a source missing here still renders (appended last).
const PILL_ORDER     = ['USB', 'Lyrion', 'AirPlay', 'Bluetooth', 'Tidal', 'TV'];

// (Re)build the pill row when the daemon's source set changes (first frame, or a
// new source added server-side). Click handlers are wired at creation.
function ensureSourcePills(names) {
    const row = document.getElementById('src-sources-row');
    if (!row) return;
    const have = [...row.querySelectorAll('.source-pill')].map(p => p.dataset.source);
    if (have.length === names.length && names.every((n, i) => n === have[i])) return;
    row.innerHTML = '';
    names.forEach(name => {
        const pill = document.createElement('div');
        pill.className = 'source-pill';
        pill.dataset.source = name;
        pill.innerHTML =
            `<span class="pill-title">${esc(SOURCE_DISPLAY[name] || name)}</span>` +
            `<span class="pill-status">—</span>`;
        pill.addEventListener('click', () =>
            srcSend({ command: 'toggle_mute', source: name }));
        row.appendChild(pill);
    });
}

function connectSrcWs() {
    setDot('src-status', 'pending', 'connecting');
    const ws = new WebSocket(SRC_WS);
    srcWs = ws;   // re-point on every (re)connect so pill clicks send on the live socket

    ws.onopen  = () => setDot('src-status', 'connected', 'connected');
    ws.onerror = () => setDot('src-status', 'error',    'error');
    ws.onclose = () => {
        setDot('src-status', 'error', 'reconnecting');
        setTimeout(connectSrcWs, RECONNECT_DELAY);
    };

    ws.onmessage = (ev) => {
        let msg; try { msg = JSON.parse(ev.data); } catch { return; }
        if (msg.type === 'state') renderSrc(msg);
        // levels frames are ignored — no meter UI in remote dashboard v1
    };
}

function renderSrc(s) {
    srcMuted = s.sources_muted || {};

    // Input — the focused source's native rate (e.g. 44.1 kHz); the summed graph is 96k.
    const rateEl = document.getElementById('src-input-rate');
    if (rateEl) rateEl.textContent = s.input_rate
        ? parseFloat((s.input_rate / 1000).toFixed(1))
        : '—';
    document.getElementById('src-source').textContent =
        SOURCE_DISPLAY[s.focused_source] || s.focused_source || '—';

    // Resampler / engine — all active sources use the per-source ARDFTSRC bridge (toggle retired).
    document.getElementById('src-engine').textContent = 'ARDFTSRC';
    document.getElementById('src-engine-profile').textContent = 'DFT · 50% OL';

    // Output — NDI is the only transport (the S/PDIF selector is retired).
    document.getElementById('src-channels').textContent = 'NDI';
    const outPathEl = document.getElementById('src-output-path');
    outPathEl.textContent = s.focused_source ? 'NDI' : '—';
    // Pi-side DSP + PipeWire graph latency (the native backend's buffer_level is dead).
    const bufEl = document.getElementById('src-buffer');
    if (bufEl) bufEl.textContent = (s.latency_ms != null) ? `${s.latency_ms} ms` : '—';

    // Sources — each pill is a per-source MUTE toggle (mirrors the touchscreen):
    //   unmuted + playing = lit (.active), muted = .muted, else idle.
    // The list is the daemon's (sources_muted covers every source incl. Bluetooth).
    let anyMuted = false;
    const pos = n => { const i = PILL_ORDER.indexOf(n); return i === -1 ? PILL_ORDER.length : i; };
    const sources = Object.keys(srcMuted).sort((a, b) => pos(a) - pos(b));
    ensureSourcePills(sources);
    sources.forEach(name => {
        const pill = document.querySelector(`.source-pill[data-source="${name}"]`);
        const statusEl = pill ? pill.querySelector('.pill-status') : null;
        if (!pill || !statusEl) return;

        const isMuted   = srcMuted[name] === true;
        const isPlaying = s.sources_playing?.[name] === true;
        const lit       = isPlaying && !isMuted;   // live in the mix
        if (isMuted) anyMuted = true;

        pill.classList.toggle('muted', isMuted);
        pill.classList.toggle('active', lit);
        pill.classList.toggle('bluetooth', name === 'Bluetooth' && lit);

        if (name === 'Bluetooth') {
            statusEl.textContent = isMuted ? 'MUTED' : (s.bt_state || 'off').toUpperCase();
        } else if (isMuted)      statusEl.textContent = 'MUTED';
        else if (isPlaying)      statusEl.textContent = 'PLAYING';
        else                     statusEl.textContent = IDLE_LABELS[name] || 'IDLE';
    });

    // Footer "Unmute All" button — lit when there is something to unmute.
    const unmuteBtn = document.getElementById('src-unmute-all');
    if (unmuteBtn) unmuteBtn.classList.toggle('active', anyMuted);
}

// ── SRC controls ──────────────────────────────────────────────────────────
function srcSend(obj) {
    if (srcWs && srcWs.readyState === WebSocket.OPEN) srcWs.send(JSON.stringify(obj));
}

function wireSrcButtons() {
    // Source pills are wired at creation in ensureSourcePills().
    const unmuteBtn = document.getElementById('src-unmute-all');
    if (unmuteBtn) unmuteBtn.addEventListener('click', () => {
        Object.keys(srcMuted).forEach(src => {
            if (srcMuted[src]) srcSend({ command: 'set_mute', source: src, muted: false });
        });
    });
}

// ════════════════════════════════════════════════════════════════════════
//  Now Playing panel — nowplaying_server :8090/ws
// ════════════════════════════════════════════════════════════════════════

let npLastTrack = null;
let npElapsedTimer = null;
let npBackoff = 2000;

function connectNpWs() {
    setDot('np-status', 'pending', 'connecting');
    const ws = new WebSocket(NP_WS);

    ws.onopen = () => {
        setDot('np-status', 'connected', 'connected');
        npBackoff = 2000;
    };
    ws.onerror = () => setDot('np-status', 'error', 'error');
    ws.onclose = () => {
        setDot('np-status', 'error', 'reconnecting');
        setTimeout(connectNpWs, npBackoff);
        npBackoff = Math.min(npBackoff * 2, 30000);
    };

    ws.onmessage = (ev) => {
        let msg; try { msg = JSON.parse(ev.data); } catch { return; }
        handleNpEvent(msg);
    };
}

function handleNpEvent(data) {
    const evt = data.event;
    if (evt === 'track_change') {
        npLastTrack = data;
        renderNp(data);
    } else if (evt === 'state_change' && npLastTrack) {
        npLastTrack = {
            ...npLastTrack,
            playing: data.playing,
            playback_rate: data.playback_rate,
            track: {
                ...npLastTrack.track,
                elapsed_seconds: data.elapsed_seconds,
                elapsed_captured_at: data.elapsed_captured_at,
            },
        };
        renderNp(npLastTrack);
    } else if (evt === 'cleared') {
        npLastTrack = null;
        renderNpIdle();
    }
}

function renderNp(data) {
    const card = document.getElementById('np-card');
    const track = data.track || {};
    const artwork = data.artwork;
    const transport = data.transport;
    const playing = data.playing;
    const sourceLabel = data.source_app?.display_name || '';

    const artHtml = (artwork && artwork.data_base64)
        ? `<img alt="" src="data:${artwork.mime};base64,${artwork.data_base64}">`
        : `<span>♪</span>`;

    const dur = track.duration_seconds;
    const elapsed = track.elapsed_seconds ?? 0;
    const capturedAt = track.elapsed_captured_at ?? (Date.now() / 1000);

    let progressHtml = '';
    if (dur != null && dur > 0) {
        progressHtml = `
            <div class="np-progress">
                <div class="np-progress-bar"><div class="np-progress-fill" id="np-progress-fill"></div></div>
                <div class="np-progress-times">
                    <span id="np-elapsed-text">${fmtTime(elapsed)}</span>
                    <span>${fmtTime(dur)}</span>
                </div>
            </div>`;
    } else if (track.title) {
        progressHtml = `
            <div class="np-progress">
                <div class="np-progress-times">
                    <span id="np-elapsed-text">${fmtTime(elapsed)} elapsed</span>
                    <span>—</span>
                </div>
            </div>`;
    }

    let transportHtml = '';
    if (transport) {
        const parts = [];
        if (transport.sample_rate) parts.push(`<b>${(transport.sample_rate / 1000).toFixed(1)} kHz</b>`);
        if (transport.channels)    parts.push(`<b>${transport.channels}ch</b>`);
        if (transport.config)      parts.push(`<b>${esc(transport.config)}</b>`);
        if (parts.length) transportHtml = `<div class="np-transport">${parts.join(' · ')}</div>`;
    }

    const chips = [];
    chips.push(`<span class="np-chip producer-${producerClass(data.producer)}">${esc(data.producer || '')}</span>`);
    if (sourceLabel) chips.push(`<span class="np-chip">${esc(sourceLabel)}</span>`);
    if (!playing) chips.push(`<span class="np-chip paused">PAUSED</span>`);

    card.innerHTML = `
        <div class="np-art">${artHtml}</div>
        <div class="np-info">
            <div class="np-title">${esc(track.title || '—')}</div>
            <div class="np-artist">${esc(track.artist || '—')}</div>
            <div class="np-album">${esc(track.album || '')}</div>
            <div class="np-chip-row">${chips.join('')}</div>
            ${progressHtml}
            ${transportHtml}
        </div>`;

    startNpElapsed(elapsed, capturedAt, dur, playing, data.playback_rate ?? 1.0);
}

function renderNpIdle() {
    stopNpElapsed();
    document.getElementById('np-card').innerHTML = `<div class="np-idle">No source active.</div>`;
}

function startNpElapsed(elapsedSec, capturedAtSec, durationSec, playing, rate) {
    stopNpElapsed();
    function tick() {
        const fill = document.getElementById('np-progress-fill');
        const text = document.getElementById('np-elapsed-text');
        if (!text) return;
        const nowSec = Date.now() / 1000;
        const live = playing
            ? elapsedSec + (nowSec - capturedAtSec) * rate
            : elapsedSec;
        if (fill && durationSec) {
            const pct = Math.min(100, (live / durationSec) * 100);
            fill.style.width = pct.toFixed(2) + '%';
        }
        text.textContent = durationSec
            ? fmtTime(live)
            : (fmtTime(live) + ' elapsed');
    }
    tick();
    npElapsedTimer = setInterval(tick, 250);
}
function stopNpElapsed() {
    if (npElapsedTimer) { clearInterval(npElapsedTimer); npElapsedTimer = null; }
}

function producerClass(p) {
    if (!p) return 'generic';
    const map = { lms: 1, shairport: 1, bluealsa: 1, fingerprint: 1 };
    return map[p] ? p : 'generic';
}
function fmtTime(s) {
    if (s == null || !isFinite(s)) return '—';
    s = Math.max(0, Math.floor(s));
    const m = Math.floor(s / 60);
    const ss = String(s % 60).padStart(2, '0');
    return `${m}:${ss}`;
}
function esc(s) {
    return String(s ?? '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;');
}

// ════════════════════════════════════════════════════════════════════════
//  DSP panel — /control/* (Pi nginx → :8090 → OSC UDP → REAPER)
// ════════════════════════════════════════════════════════════════════════

// Fire-and-forget: a 204 only says the Pi sent the UDP packet. There is no
// echo back, so the LattePanda kiosk's HUD overlay is the real confirmation.
async function dspStep(btn) {
    btn.classList.remove('error');
    try {
        const r = await fetch(`/control/${btn.dataset.control}/${btn.dataset.direction}`,
                              { method: 'POST' });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        btn.classList.add('flash');
        setTimeout(() => btn.classList.remove('flash'), 150);
    } catch {
        btn.classList.add('error');
    }
}

function wireDspButtons() {
    document.querySelectorAll('.dsp-btn')
        .forEach(btn => btn.addEventListener('click', () => dspStep(btn)));
}

// ════════════════════════════════════════════════════════════════════════
//  Break-Glass panel — LattePanda HTTP service :8091
// ════════════════════════════════════════════════════════════════════════

async function loadBgConfig() {
    try {
        const resp = await fetch('config.json', { cache: 'no-store' });
        if (!resp.ok) throw new Error('config.json missing');
        const cfg = await resp.json();
        if (!cfg?.breakGlass?.token) {
            throw new Error('config.json: breakGlass.token is required');
        }
        bgToken = cfg.breakGlass.token;
        document.getElementById('bg-meta').textContent = 'proxy: /breakglass/ (LattePanda via Pi nginx)';
    } catch (err) {
        bgToken = null;
        document.getElementById('bg-meta').textContent =
            'config.json not loaded — break-glass disabled.';
        setDot('bg-status', 'error', String(err));
        document.querySelectorAll('.bg-btn').forEach(b => b.disabled = true);
    }
}

async function bgStatusPoll() {
    if (!bgToken) return;
    try {
        const r = await fetch(`${BG_BASE}/status`, {
            headers: { 'X-Vibesbox-BreakGlass': bgToken },
            cache: 'no-store',
        });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const j = await r.json();
        setDot('bg-status', 'connected', `host=${j.host} kiosk=${j.kioskRunning ? 'up' : 'down'}`);
        document.getElementById('bg-meta').textContent =
            `host ${j.host} · kiosk ${j.kioskRunning ? 'running' : 'stopped'} · uptime ${j.uptimeSec}s`;
    } catch (err) {
        setDot('bg-status', 'error', String(err));
    }
}

async function bgFire(action) {
    if (!bgToken) return;
    try {
        const r = await fetch(`${BG_BASE}/${action}`, {
            method: 'POST',
            headers: { 'X-Vibesbox-BreakGlass': bgToken },
        });
        const j = await r.json().catch(() => ({}));
        document.getElementById('bg-meta').textContent =
            `${action} → HTTP ${r.status} ${JSON.stringify(j)}`;
        // Refresh status after a destructive call
        setTimeout(bgStatusPoll, 500);
    } catch (err) {
        document.getElementById('bg-meta').textContent =
            `${action} → error: ${err}`;
    }
}

function wireBgButtons() {
    document.querySelectorAll('.bg-btn').forEach(btn => {
        const subEl = btn.querySelector('.bg-btn-sub');
        const subOriginal = subEl ? subEl.textContent : '';
        let armTimer = null;

        function disarm() {
            btn.classList.remove('armed');
            if (subEl) subEl.textContent = subOriginal;
        }

        btn.addEventListener('click', () => {
            if (btn.disabled) return;
            if (btn.classList.contains('armed')) {
                clearTimeout(armTimer);
                disarm();
                bgFire(btn.dataset.action);
            } else {
                btn.classList.add('armed');
                if (subEl) subEl.textContent = 'Tap again to confirm';
                armTimer = setTimeout(disarm, ARM_TIMEOUT_MS);
            }
        });
    });
}

// ════════════════════════════════════════════════════════════════════════
//  Boot
// ════════════════════════════════════════════════════════════════════════

(async function init() {
    connectSrcWs();
    connectNpWs();
    wireSrcButtons();
    wireDspButtons();
    wireBgButtons();
    await loadBgConfig();
    bgStatusPoll();
    setInterval(bgStatusPoll, 5000);
})();
