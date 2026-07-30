# Hardware Reference

What the panel is made of and how it is wired.

## Parts

| Part | Qty | Notes |
|---|---|---|
| Arduino Uno | 1 | Built and tested on a CH340-based board |
| Common-cathode RGB LED module | 3 | 4-pin: R, G, B, − |
| Passive buzzer module | 1 | 3-pin: VCC, GND, I/O. Must be *passive* — it needs a driven tone, not DC |
| Tactile push button | 1 | Silences an active alarm |
| Breadboard + jumpers | 1 | For a shared ground rail |

No resistors are needed: the LED modules include their own, and the button uses
the ATmega's internal pull-up.

## Pin map

| Connection | Arduino pin |
|---|---|
| Module 1 — R / G / B | D2 / D3 / D4 |
| Module 2 — R / G / B | D5 / D6 / D7 |
| Module 3 — R / G / B | D8 / D9 / D10 |
| Buzzer — I/O | D11 |
| Button — leg 1 | D12 |
| Heartbeat indicator | D13 (built-in LED, no wiring) |
| All module `−`, buzzer GND, button leg 2 | Shared ground rail |
| Buzzer VCC | 5V |

D0 and D1 are reserved for the USB serial link and must stay clear.

**Every digital pin is now in use.** D2–D13 are all allocated. A fourth module,
or anything else needing digital I/O, requires either the analog pins (A0–A5
work as ordinary digital pins) or a different approach — see
[decisions.md](decisions.md#the-panel-is-capped-at-three-modules).

## Wiring notes

- All grounds share one breadboard rail fed from a single Arduino GND pin.
- The button needs no resistor; the firmware uses `INPUT_PULLUP`, so a press
  reads `LOW`.
- All three colour channels are wired on every module. Blue is not optional —
  `NEED_INPUT` and `THINKING` both use it.

## Brightness

The LEDs run at **5% PWM duty** by default, which is dim enough for a desk in a
normal room. Change it live with `DIM:<0-100>` (see
[protocol.md](protocol.md#dim0-100--set-led-brightness)) or change the compiled
default in `status_panel/status_panel.ino`.

This is *software* PWM, not `analogWrite`. Only D3, D5, D6, D9 and D10 of the
nine module pins support hardware PWM on an Uno, so hardware dimming would
leave the three modules at visibly different brightness. See
[decisions.md](decisions.md#leds-are-dimmed-by-software-pwm) — including why
the loop only touches the pins when the duty phase flips.

## Flashing

```bash
arduino-cli core install arduino:avr
arduino-cli compile --fqbn arduino:avr:uno status_panel
arduino-cli upload -p <PORT> --fqbn arduino:avr:uno status_panel
```

`arduino-cli board list` will name the port: `COM3`-style on Windows,
`/dev/ttyUSB0` or `/dev/ttyACM0` on Linux.

**Stop the bridge first.** It holds the port open exclusively and the upload
will fail with the port busy.

## Resource use

Roughly 22% of flash and 21% of SRAM on an ATmega328P, leaving comfortable
headroom. `STATUS` reports free SRAM live if you want to watch it.

## Known board behaviours

**Opening the serial port resets the board.** Standard USB-serial DTR
behaviour. Every bridge start therefore blanks the panel briefly, and any
`DIM:` setting reverts. The bridge repaints from persisted state immediately
afterwards.

**A yellow `THINKING` was tried and rejected.** Yellow (R+G) reads as
green-dominant to the eye and was too easily confused with `WORKING`. Magenta
(R+B) shares no channel with green and is unmistakable.
