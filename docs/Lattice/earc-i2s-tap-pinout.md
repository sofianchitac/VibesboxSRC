# SiI9437 I2S Tap Pinout — Lindy 38368 eARC Extractor

Physical tap-point research for capturing multichannel LPCM off the SiI9437CNUC (`U1007`)
inside the Lindy 38368 4K60 HDMI eARC Extractor. Feeds the Pi 5 direct 8ch I2S slave
capture plan in [[project_earc_i2s_tap_research]].

## SiI9437 pin diagram (QFN32, 0.4mm pitch)

Source: `SiI-DB-02013-B.pdf` (Lattice Data Brief — pin diagram only; full Pin Description
table is NOT in the public Data Brief, only in the NDA-gated full datasheet).

```
                    GPIO  RSVD/HPD 1.2V  3.3V  Diff_Audio Diff_Audio  GND  1.2V
                     32     31      30    29       28         27      26   25
                 ┌────────────────────────────────────────────────────────────┐
   RSVD        1 │                                                            │ 24  CTRL_SIG_I
CI2CA/GPIO     2 │                                                            │ 23  CTRL_SIG_O
   GPIO        3 │                    SiI9437/SiI9438                         │ 22  I2C_BUS
   GPIO        4 │                                                            │ 21  I2C_BUS
   GPIO        5 │                    ePad (GND)                              │ 20  1.2V
   1.2V        6 │                                                            │ 19  GND
   GND         7 │                                                            │ 18  AUDIO_CTRL_IO (MUTE)
AUDIO_CTRL_IO  8 │                                                            │ 17  AUDIO_IF_IO (SPDIF/DSDR2)
                 └────────────────────────────────────────────────────────────┘
                     9     10     11    12    13    14    15    16
                   3.3V  AUDIO  AUDIO  AUDIO AUDIO AUDIO AUDIO  3.3V
                        CTRL_IO IF_IO  IF_IO IF_IO IF_IO IF_IO
```

## Confirmed pin → signal map

Ground-truthed against a Denon AVR schematic using the identical `SiI9437CNUC` part
(same chip, different product — reference-design pinout, not Lindy's own schematic).
Cross-checked against the Data Brief's generic pin-type groups (`AUDIO_CTRL_IO` /
`AUDIO_IF_IO`) — every pin lands in the correct type group, high confidence.

| Pin | Signal | Notes | Color |
|---|---|---|---|
| 8  | MCLK | not needed for Pi 5 slave-mode capture |
| 9  | 3.3V | power — landmark pin, brackets the audio cluster |
| 10 | **SCK** (BCLK) | → Pi 5 GPIO 18 | Orange |
| 11 | **WS** (LRCK) | → Pi 5 GPIO 19 | Green |
| 12 | **SD0** | → Pi 5 GPIO 20 | Yellow |
| 13 | **SD1** | → Pi 5 GPIO 22 | Blue |
| 14 | **SD2** | → Pi 5 GPIO 24 | Purple |
| 15 | **SD3** | → Pi 5 GPIO 26 | Red |
| 16 | 3.3V | power — landmark pin |
| 17 | SPDIF / DSDR2 (muxed) | likely not needed for 5.1 LPCM capture |
| 18 | MUTE | avoid — don't want to trigger this |

Logic level: 2.8–3.6V TTL-tolerant (same audio-pin voltage class as the GSCoolink
GSV-series family) — matches Pi 5 3.3V GPIO domain directly, no level shifting needed.

## Pi 5 capture wiring (6 signals + 2 grounds)

| SiI9437 pin | Signal | Pi 5 GPIO (RP1 `i2s1` slave) | Header pin | Color |
|---|---|---|---|---|
| 10 | SCK  | 18 | **12** | Orange |
| 11 | WS   | 19 | **35** | Green |
| 12 | SD0  | 20 | **38** | Yellow |
| 13 | SD1  | 22 | **15** | Blue |
| 14 | SD2  | 24 | **18** | Purple |
| 15 | SD3  | 26 | **37** | Red |
| 7 or 19 (GND) | ground return A | — | **14** | (pick a spare) |
| 7 or 19 (GND) | ground return B | — | **39** | (pick a spare) |

Overlay pattern: clone `hifiberry-studio-dac8x-pro` — pins 18–27 function `i2s1`,
target `i2s_clk_consumer`, external device (SiI9437) is bitclock/frame master,
64-bit frames (`set_bclk_ratio 64`).

### Ground return — required even though both boxes share a supply

The Pi and the Lindy run off one 5 V rail inside the same case, so they already share a
DC ground **bond**. That is not the same thing as a signal **return path**, and it does
not remove the need for these wires.

Without dedicated grounds, the return current for every I2S edge has to travel
SiI9437 → Pi GPIO → Pi ground plane → Pi's power-GND wire → distribution point →
Lindy's power-GND wire → Lindy ground plane → SiI9437. Two problems with that:

- **Common-impedance coupling.** The Pi's own supply return — amps, with fast DVFS
  current steps — flows through those same power-GND wires. Even 20 mΩ of wire turns
  that into tens of millivolts of difference between the two ground planes, and wire
  *inductance* makes it much worse at exactly the fast edges you care about. That
  difference lands directly on your I2S reference.
- **Loop area.** A return path routed around through the power wiring is a large loop
  antenna at 3.072 MHz.

Two short wires alongside the signals fix both: the low-impedance path wins and the
return current stops going through the power wiring. The tap GPIOs land at both ends of
the 40-pin header (12/15/18 near, 35/37/38 far), so run **two** and keep each short:

- Header **pin 14** (GND) — serves the pin 12 / 15 / 18 cluster (SCK, SD1, SD2)
- Header **pin 39** (GND) — serves the pin 35 / 37 / 38 cluster (WS, SD3, SD0)

Lindy side: SiI9437 pin 7 and pin 19 are both GND, but a ground pour, via, or the
shield can near the tap point is far easier to solder than another 0.4 mm QFN pin —
electrically identical.

**Twist SCK (orange) with a ground return.** At 48 kHz / 64-bit frames BCLK is
3.072 MHz — the fastest edge in the bundle and the first thing to fail on unshielded
flying leads. The data lanes are more forgiving.

No ground-loop worry on the Pi side: the Pi has no analog audio output, and NDI leaves
over transformer-isolated Ethernet. This bond cannot produce audible hum.

### Series resistors — 330 Ω, still needed

Put a resistor (220–470 Ω, 330 Ω is the pick) in series with **each** of the six signal
lines. Two reasons, of very different weight:

1. **Backfeed into a halted Pi — this is the damage risk, and a shared supply does NOT
   remove it.** Sharing one 5 V rail means the two boards power up and down together, so
   the "Lindy on, Pi at the wall switch off" window is gone. But `shutdown -h` does not
   cut the 5 V feed — the Pi 5 halts into a low-power state with the PMIC's main rails
   down (that is what the power button wakes from) while your rail, and therefore the
   Lindy, stays fully live. The SiI9437 then drives 3.3 V into six GPIOs whose I/O rail
   is off: current into the RP1's ESD clamps, and the RP1 is less forgiving than the
   BCM2711 on the Pi 4B. Unlimited, six pins can push ~100 mA into a dead rail. At
   330 Ω it is ~6 mA total. This reason does not care which end the resistor sits at.
2. **Stub damping.** A 15–20 cm flying lead is electrically long against a ~2 ns CMOS
   edge, and the tap hangs off a pin the Lindy's *own* signal path to the GSV2002
   depends on. Damping wants the resistor at the **source (Lindy) end** — prefer that
   end if you can splice one in near the joint; the Pi end still gets you reason 1.

Cost either way: ~7 ns RC against a 325 ns bit period at 48 kHz. Nothing.

Keep the harness ≤ 15–20 cm, route it away from the Pi's Ethernet magnetics and the
5 V distribution wiring, and strain-relieve the QFN end (Kapton or hot glue). A 0.4 mm
pad that tears off takes the pad with it.

## Board-level tracing (Lindy 38368, own PCB — traced 2026-07-08/09)

- Pins **10, 11, 13, 14, 15** → direct point-to-point via series resistor → GSV2002
  (R27, R26, R20, R22, R25 respectively). Clean, unconditional tap points.
- Pin **12 (SD0)** and pin **17 (SPDIF/DSDR2)** both feed a 74HC4052 dual 4:1 analog
  mux, which time-shares a *single* GSV2002 input pin between them (→ R18 → GSV2002),
  presumably switched by firmware between "discrete SD0 channel" (LPCM) and "compressed
  bitstream" (Dolby/DTS) modes.
  - **Implication: tap at the SiI9437 pins directly (12 and 17), not downstream of the
    mux.** Downstream of the mux, the signal identity is state-dependent — you could be
    reading SD0 or SPDIF depending on what the board is currently routing. Tapping
    upstream at the chip pins sidesteps this entirely.
  - Pin assignment note: standard 74HC4052 pinout (TI/ON Semi/Nexperia, industry-standard
    since the CD4000 series) has **pin 12 = COMMON Z**, **pin 13 = Z1 (independent)**.
    Field trace found pin 12 wired to SD0 as an "independent" leg and pin 13 as the
    "common" feeding R18 — the reverse of the datasheet. Possible miscount on the
    SOIC-16 package (easy to lose a pin going around a corner at that pitch). Doesn't
    change the tap plan (still tap upstream at the SiI9437), but worth re-verifying
    against the physical pin-1 dot if the mux's select-line behavior is traced later.
- Pin 17 (SPDIF/DSDR2) is likely not needed at all for a 5.1 LPCM capture — 5.1 only
  occupies 3 of the 4 SD lanes per earlier bandwidth math in
  [[project_earc_i2s_tap_research]].

## Sources

- `SiI-DB-02013-B.pdf` — SiI9437/SiI9438 Data Brief (Lattice), pin diagram only
- GSV2001 full datasheet (ACP-Tech mirror, 21pp, not redacted) — Table 6 "8 channel I2S
  Input" cross-check: 4 data lanes (AP0-AP3) + LRCLK/WS (AP5) + SCLK + MCLK, confirms the
  7-signal cluster shape matches SiI9437's 6 `AUDIO_IF_IO`/`AUDIO_CTRL_IO` pins (no
  separate MCLK data pin needed downstream in slave capture)
- GSV2002's own published datasheet excerpt is redacted (stops after General Info, jumps
  to Package Dimensions) — same commercial pattern as the SiI9437 Data Brief, no public
  pin table found for GSV2002 itself
- Denon AVR schematic (third-party product using `SiI9437CNUC`) — ground truth for the
  pin 8/10/11/12/13/14/15/17/18 → signal mapping above
- Field continuity trace on the Lindy 38368 PCB itself, 2026-07-08/09
