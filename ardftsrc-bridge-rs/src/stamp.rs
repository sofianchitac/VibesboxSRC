//! Phase-2 latency side-channel (ledger §7.1 / agents-exchange 2026-08-24).
//!
//! Every occupancy instrument on the forward leg reads empty across the per-run
//! draw, and snd_pcm_delay() at the transmitter proved flat too (discriminator
//! session 2026-08-24). What remains invisible is TIMING, not occupancy: when the
//! newest output block actually entered the chain. This module publishes exactly
//! that — a small seqlock record in /dev/shm, overwritten in place on every
//! completed output write:
//!
//!   u64 seq        increments per update (reader detects tears/torn reads)
//!   u64 instance   per-process id, so the transmitter detects a bridge RESTART
//!                  (a restart redraws the per-run draw and resets out_frames)
//!   u64 out_frames cumulative 96k frames handed to the output PCM (starts at 0
//!                  per process — the "hw_ptr-derived" position of the newest block)
//!   u64 mono_ns    CLOCK_MONOTONIC write-completion stamp — the SAME clock
//!                  Python's time.monotonic_ns() reads, so the transmitter can
//!                  difference stamp against read time with no clock-sync term.
//!
//! The transmitter anchors its own read counter against this timeline once per
//! instance and interpolates: variations of the resulting figure track the true
//! bridge-write → tx-read hold one-for-one, continuously and inaudibly. The
//! ABSOLUTE carries the hold that existed at anchor time — unknowable passively,
//! by construction (the pipe between us and the reader is always full). This is
//! a variation instrument, not an absolute probe; do not quote it as one.
//!
//! Cost: one pwrite of 32 bytes per process-loop iteration with output (~26/s at
//! 48k input) — noise against the writei itself. Any open/write failure disables
//! the stamp for the life of the process with one warning; the audio path never
//! depends on it.
//!
//! ⚠ Multiple concurrent bridges share ONE path and overwrite each other. Fine
//! for single-source probing (the variance work injects at one source); garbage
//! under multi-source playback until the transmitter filters by instance.

use std::fs::{File, OpenOptions};
use std::io::{Seek, SeekFrom, Write};
use std::time::Instant;

pub const STAMP_PATH: &str = "/dev/shm/vibesbox-ardftsrc-stamp";

fn mono_ns() -> u64 {
    let mut ts = libc::timespec { tv_sec: 0, tv_nsec: 0 };
    // CLOCK_MONOTONIC — matches Python time.monotonic_ns() on this kernel.
    unsafe { libc::clock_gettime(libc::CLOCK_MONOTONIC, &mut ts) };
    (ts.tv_sec as u64) * 1_000_000_000 + ts.tv_nsec as u64
}

pub struct StampWriter {
    f: File,
    seq: u64,
    instance: u64,
    buf: [u8; 32],
}

impl StampWriter {
    /// Open the shared record. None = stamping disabled for this process
    /// (/dev/shm missing or unwritable) — caller logs once and moves on.
    ///
    /// ⛔ NEVER truncate this file. The transmitter holds a permanent mmap of it;
    /// truncating the length under a mapped page kills the reader with SIGBUS,
    /// which Python cannot catch (ndi-output crash-looped through the whole
    /// 13:46 discriminator session because of exactly this — found 2026-08-24).
    /// A fresh file is created empty and extended to 32 bytes by the first
    /// update(); readers see seq=0 until then and treat it as "never stamped".
    pub fn open(started: &Instant) -> Option<Self> {
        let f = OpenOptions::new().create(true).write(true).open(STAMP_PATH).ok()?;
        // Instance id: process-start monotonic time mixed with pid. Two rapid
        // restarts cannot share it (pid differs), and it is stable for life.
        let instance = mono_ns() ^ (started.elapsed().as_nanos() as u64) ^ (std::process::id() as u64).wrapping_mul(0x9E37_79B9_7F4A_7C15);
        Some(Self { f, seq: 0, instance, buf: [0u8; 32] })
    }

    /// Stamp the newest written block: cumulative frame count + completion time.
    /// A failure here disables stamping (returns false) rather than risking the
    /// audio loop — the caller drops the writer.
    pub fn update(&mut self, out_frames: i64) -> bool {
        self.seq = self.seq.wrapping_add(1);
        self.buf[..8].copy_from_slice(&self.seq.to_le_bytes());
        self.buf[8..16].copy_from_slice(&self.instance.to_le_bytes());
        self.buf[16..24].copy_from_slice(&(out_frames as u64).to_le_bytes());
        self.buf[24..32].copy_from_slice(&mono_ns().to_le_bytes());
        let ok = self.f.seek(SeekFrom::Start(0)).is_ok()
            && self.f.write_all(&self.buf).is_ok();
        if !ok {
            // Leave a poisoned seq=0 record rather than a half-old one: a reader
            // treats seq 0 as "never stamped".
            self.seq = 0;
        }
        ok
    }
}
