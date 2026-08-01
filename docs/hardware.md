# Hardware Reference

What the panel is made of and how it is wired.

This describes the current build: a single 16-LED WS2812B/NeoPixel ring,
driven by `status_ring/status_ring.ino`. The original 3-module board and
`status_panel/status_panel.ino` are kept as a legacy reference — see
[Legacy: the 3-module board](#legacy-the-3-module-board) at the bottom — not
removed, per [decisions.md](decisions.md#v2-the-16-led-ring-status_ringino).

## Parts

| Part | Qty | Notes |
|---|---|---|
| Arduino Uno | 1 | Built and tested on a CH340-based board |
| WS2812B/NeoPixel RGB LED ring, 16 pixels | 1 | Single data-in pin drives all 16 |
| Passive buzzer module | 1 | 3-pin: VCC, GND, I/O. Must be *passive* — it needs a driven tone, not DC |
| Tactile push button | 1 | Silences an active alarm |
| Electrolytic capacitor, ~470–1000 µF | 1 | Across the ring's 5V/GND, at the ring itself |
| Resistor, ~300–500 Ω | 1 | Inline on the data line, near the Arduino pin |
| Breadboard + jumpers | 1 | For a shared ground rail |

The button uses the ATmega's internal pull-up, so it needs no resistor of its
own.

## Pin map

| Connection | Arduino pin |
|---|---|
| Ring — data in | D6 |
| Buzzer — I/O | D11 |
| Button — leg 1 | D12 |
| Heartbeat indicator | D13 (built-in LED, no wiring) |
| Ring GND, buzzer GND, button leg 2 | Shared ground rail |
| Ring 5V, buzzer VCC | 5V |

D0 and D1 are reserved for the USB serial link and must stay clear.

**Only 3 digital pins are used** (D6, D11, D12), plus D13 for the built-in
heartbeat LED — a big drop from v1, where D2–D13 were all allocated. This was
the point of moving to an addressable ring: one data pin drives any number of
pixels. D2–D5, D7–D10, and the analog pins (A0–A5, usable as digital pins) are
all free for whatever comes next.

## Wiring notes

- All grounds share one breadboard rail fed from a single Arduino GND pin.
- The button needs no resistor; the firmware uses `INPUT_PULLUP`, so a press
  reads `LOW`.
- Put the capacitor across the ring's own 5V/GND, not back at the Arduino —
  it's there to smooth the ring's own current draw.
- The inline resistor on the data line goes close to the Arduino pin, to tame
  reflections into the first pixel.
- **No level shifter is needed.** The Uno's native 5V logic already clears
  WS2812B's data threshold (~0.7×Vdd) — an advantage over 3.3V boards, which
  typically do need one.

## Power

Powered from the Uno's 5V pin — no external supply needed. At this project's
established low-brightness convention (5% by default), 16 lit pixels draw
only a few dozen mA total, far under a USB port's ~500 mA budget. Only an
all-white, full-brightness scenario (16 × 60 mA ≈ 960 mA) would approach a
real limit, and this design never drives white or runs anywhere near full
brightness.

## Brightness

The ring runs at **5% brightness** by default, which is dim enough for a desk
in a normal room. Change it live with `DIM:<0-100>` (see
[protocol.md](protocol.md#dim0-100--set-led-brightness)) or change the
compiled default in `status_ring/status_ring.ino`.

This is native `Adafruit_NeoPixel::setBrightness()` — a straight simplification
over v1's software-PWM duty-cycle trick, which existed only because most of
the old 9 module pins lacked hardware PWM. A single addressable strip has no
such per-pin inconsistency to work around.

## Flashing

```bash
arduino-cli core install arduino:avr
arduino-cli lib install "Adafruit NeoPixel"
arduino-cli compile --fqbn arduino:avr:uno status_ring
arduino-cli upload -p <PORT> --fqbn arduino:avr:uno status_ring
```

`arduino-cli board list` will name the port: `COM3`-style on Windows,
`/dev/ttyUSB0` or `/dev/ttyACM0` on Linux.

**Stop the bridge first.** It holds the port open exclusively and the upload
will fail with the port busy.

## Resource use

Roughly 25% of flash and 26% of SRAM on an ATmega328P, leaving comfortable
headroom — close to v1's ~22%/21% despite the added NeoPixel library and
16-slot state. `STATUS` reports free SRAM live if you want to watch it.

## Bring-up verification

`ring.show()` briefly disables interrupts (~16 LEDs × 24 bits × 1.25 µs ≈
480 µs). At 9600 baud (~1.04 ms/byte) this could in the worst case drop one
in-flight serial byte. Since `show()` only runs a few times a second (the
firmware only calls it when a slot's state changed or the blink phase
flipped), exposure is small, but verify it directly after flashing rather
than assuming: with the bridge stopped, drive all 16 slots into a mix of
`BLOCKED`/`IDLE`/`RUNNING` (maximum blink churn) over a raw serial terminal
and confirm no `ERR:` replies appear. See
[protocol.md](protocol.md#talking-to-the-board-directly) for the manual
commands.

## Known board behaviours

**A long-lived parent process can hold a stale `dialout` group after you've
already logged back in.** Adding your user to `dialout` (see the README's
Linux setup step) only takes effect for shells started after the change. If
the bridge is launched from inside a process that was already running before
you logged out/in — e.g. an existing Claude Code session — its child shells
still inherit the old group list even though your own login session is fresh.
Check with `groups`; if `dialout` is missing, either restart that parent
process or run the bridge with `sg dialout -c 'python3 bridge/bridge.py'`,
which reads current membership from `/etc/group` directly.

**Opening the serial port resets the board.** Standard USB-serial DTR
behaviour. Every bridge start therefore blanks the panel briefly, and any
`DIM:` setting reverts. The bridge repaints from persisted state immediately
afterwards.

---

## Legacy: the 3-module board

`status_panel/status_panel.ino`, kept as a reference, not removed. It used 3
individually-wired common-cathode RGB LED modules (4-pin: R, G, B, −) instead
of an addressable ring:

| Connection | Arduino pin |
|---|---|
| Module 1 — R / G / B | D2 / D3 / D4 |
| Module 2 — R / G / B | D5 / D6 / D7 |
| Module 3 — R / G / B | D8 / D9 / D10 |
| Buzzer — I/O | D11 |
| Button — leg 1 | D12 |
| Heartbeat indicator | D13 |

Every digital pin was in use — the reason a fourth module had nowhere to go,
and ultimately why the ring replaced it; see
[decisions.md](decisions.md#the-panel-is-capped-at-three-modules). Brightness
was dimmed by software PWM rather than a native brightness call, because only
some of those 9 module pins had hardware PWM — see
[decisions.md](decisions.md#leds-are-dimmed-by-software-pwm).

Flash it the same way, targeting the old sketch name:

```bash
arduino-cli compile --fqbn arduino:avr:uno status_panel
arduino-cli upload -p <PORT> --fqbn arduino:avr:uno status_panel
```

**A yellow `THINKING` was tried and rejected** (v1 only — v2 has no
`THINKING` state). Yellow (R+G) reads as green-dominant to the eye and was
too easily confused with `WORKING`. Magenta (R+B) shares no channel with
green and is unmistakable.
