// Asks ALSA what it would actually GRANT for a requested period/buffer, without running the
// bridge. `set_*_near` is a request; the grant can differ wildly and silently — the USB
// gadget answers 170/2720 to a 1024/4096 ask, which is why the CAP_PERIOD reasoning in
// main.rs was arguing about a number the driver never honoured.
//
//   hw_probe <device> <capture|playback> <rate> <channels> <period> <buffer>
//
// The playback probe creates a NON-autoconnecting PipeWire node and writes nothing, so it is
// safe to run against a live graph: it negotiates hw_params and exits.

use alsa::pcm::{Access, Format, HwParams, PCM};
use alsa::{Direction, ValueOr};

fn main() {
    let a: Vec<String> = std::env::args().collect();
    if a.len() < 7 {
        eprintln!("usage: hw_probe <device> <capture|playback> <rate> <channels> <period> <buffer>");
        std::process::exit(2);
    }
    let device = &a[1];
    let dir = if a[2] == "capture" { Direction::Capture } else { Direction::Playback };
    let rate: u32 = a[3].parse().unwrap();
    let channels: u32 = a[4].parse().unwrap();
    let period: i64 = a[5].parse().unwrap();
    let buffer: i64 = a[6].parse().unwrap();

    if dir == Direction::Playback {
        std::env::set_var(
            "PIPEWIRE_PROPS",
            format!("{{ node.name=hw-probe node.autoconnect=false \
                     media.class=Stream/Output/Audio audio.channels={channels} }}"),
        );
    }

    let pcm = match PCM::new(device, dir, false) {
        Ok(p) => p,
        Err(e) => {
            println!("{device} {}: OPEN FAILED: {e}", a[2]);
            std::process::exit(1);
        }
    };
    {
        let hwp = HwParams::any(&pcm).expect("hwparams");
        hwp.set_channels(channels).expect("channels");
        hwp.set_rate(rate, ValueOr::Nearest).expect("rate");
        hwp.set_format(Format::s32()).expect("format");
        hwp.set_access(Access::RWInterleaved).expect("access");
        // Report the legal RANGE too — that is what says whether a tighter ask is even
        // reachable, as opposed to being silently rounded back up.
        let (pmin, pmax) = (hwp.get_period_size_min(), hwp.get_period_size_max());
        let (bmin, bmax) = (hwp.get_buffer_size_min(), hwp.get_buffer_size_max());
        let _ = hwp.set_buffer_size_near(buffer);
        let _ = hwp.set_period_size_near(period, ValueOr::Nearest);
        pcm.hw_params(&hwp).expect("apply");
        println!(
            "{device} {} {rate}Hz x{channels}ch: requested {period}/{buffer}  \
             LEGAL period [{}..{}] buffer [{}..{}]",
            a[2],
            pmin.map(|v| v.to_string()).unwrap_or_else(|_| "?".into()),
            pmax.map(|v| v.to_string()).unwrap_or_else(|_| "?".into()),
            bmin.map(|v| v.to_string()).unwrap_or_else(|_| "?".into()),
            bmax.map(|v| v.to_string()).unwrap_or_else(|_| "?".into()),
        );
    }
    let cur = pcm.hw_params_current().expect("current");
    let p = cur.get_period_size().unwrap_or(-1);
    let b = cur.get_buffer_size().unwrap_or(-1);
    println!(
        "  => GRANTED period={p} buffer={b}  ({:.2} ms period, {:.2} ms buffer)",
        p as f64 * 1000.0 / rate as f64,
        b as f64 * 1000.0 / rate as f64
    );
}
