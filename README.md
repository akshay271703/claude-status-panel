# Claude Status Panel

A physical desk panel that shows what your Claude Code sessions — and their
subagents — are doing. A 16-LED ring gives every session and every dispatched
subagent its own light; a shared buzzer nags you when one has been waiting too
long; a browser dashboard fills in what the LEDs can't say — which project
each slot belongs to, and what it's cost.

Three pieces:

- **`status_ring/`** — Arduino firmware for the current (v2) build. Drives the
  16-LED ring, buzzer and silence button from a small line-based serial
  protocol. (`status_panel/` is the original 3-module board, kept as a legacy
  reference — see [`docs/decisions.md`](docs/decisions.md).)
- **`bridge/`** — a Python daemon. Listens for Claude Code hook events, decides
  which session or subagent owns which slot, drives the board over serial, and
  serves the dashboard.
- **`simulator/`** — a software stand-in for the firmware, so the whole real
  path (hooks → bridge → protocol) can be watched live in a browser before any
  hardware exists. See [Try it without hardware](#try-it-without-hardware).
- **`diagnostics/`** — two throwaway sketches kept for bring-up: `blink_test`
  proves the chip is executing sketches at all, `i2c_scanner` lists devices on
  the I2C bus. Neither is part of the panel.

Runs on Windows, Linux and macOS. The only machine-specific thing is the serial
port, and that's auto-detected.

## Hardware

| Part | Notes |
|---|---|
| Arduino Uno | Built and tested on a CH340-based board |
| 1× WS2812B/NeoPixel RGB LED ring, 16 pixels | Single data pin drives all 16 |
| 1× passive buzzer module | 3-pin (VCC, GND, I/O) |
| 1× tactile push button | Silences an active alarm |
| 1× electrolytic capacitor (~470–1000 µF) | Across the ring's 5V/GND |
| 1× resistor (~300–500 Ω) | Inline on the data line |
| Breadboard + jumpers | For a shared ground rail |

Full pin map and wiring notes: [`docs/hardware.md`](docs/hardware.md). D13 uses
the Uno's built-in LED as a heartbeat, so it needs no wiring. Only 3 digital
pins are used in total (ring data, buzzer, button) — a big drop from the
original 3-module board, which used every pin the Uno had.

## Quick start

Assuming the panel is wired and `arduino-cli` is installed:

```bash
git clone git@github.com:akshay271703/claude-status-panel.git
cd claude-status-panel
python3 -m pip install -r bridge/requirements.txt

# Linux only, then log out and back in
sudo usermod -a -G dialout $USER

arduino-cli lib install "Adafruit NeoPixel"
arduino-cli compile --fqbn arduino:avr:uno status_ring
arduino-cli upload -p /dev/ttyUSB0 --fqbn arduino:avr:uno status_ring

python3 bridge/install_hooks.py
python3 bridge/bridge.py
```

Then open **http://127.0.0.1:8765** and start a *new* Claude Code session so
it picks up the hooks.

Substitute your own serial port — `arduino-cli board list` will name it
(`COM3`-style on Windows, `/dev/ttyUSB0` or `/dev/ttyACM0` on Linux). The
bridge itself auto-detects the port; only the upload needs it spelled out.

The steps below are the same thing with the reasoning attached.

## Setup

### 1. Flash the firmware

Install [`arduino-cli`](https://arduino.github.io/arduino-cli/), then:

```bash
arduino-cli core install arduino:avr
arduino-cli lib install "Adafruit NeoPixel"
arduino-cli compile --fqbn arduino:avr:uno status_ring
arduino-cli upload -p <PORT> --fqbn arduino:avr:uno status_ring
```

`<PORT>` is `COM3`-style on Windows, `/dev/ttyUSB0` or `/dev/ttyACM0` on Linux.
`arduino-cli board list` will tell you.

### 2. Install Python dependencies

```bash
python3 -m pip install -r bridge/requirements.txt
```

### 3. Linux only — serial permissions

Your user needs access to the serial device, or the bridge can't open it:

```bash
sudo usermod -a -G dialout $USER
```

**Log out and back in** for it to take effect. This is the single most common
reason the bridge won't start on a fresh Linux machine.

If the bridge still gets `Permission denied` on the serial port after logging
back in, check whether the shell running it is actually new — a long-lived
parent process (e.g. an already-running Claude Code session) started before
the group change keeps its old group list in any shell it spawns, even after
you personally log out and back in elsewhere. Confirm with `groups` (does it
list `dialout`?); if not, either restart that parent process or run the
bridge via `sg dialout -c 'python3 bridge/bridge.py'`, which picks up current
group membership from `/etc/group` without needing a fresh login shell.

### 4. Wire up the Claude Code hooks

```bash
python3 bridge/install_hooks.py
```

Hook commands need absolute paths and the right interpreter, so they're
machine-specific and can't live in the repo. This script writes them into
`~/.claude/settings.json`, leaves your other settings untouched, and backs the
file up first. `--remove` undoes it; `--dry-run` shows the result without
writing.

Start a **new** Claude Code session afterwards to pick them up.

### 5. Run the bridge

```bash
python3 bridge/bridge.py
```

Then open **http://127.0.0.1:8765**.

The bridge is started manually, by design — it's a desk toy, and having it
launch itself on login was more surprising than useful.

## Configuration

All optional — the defaults are right on a single-board setup.

| Variable | Default | Purpose |
|---|---|---|
| `STATUS_PANEL_PORT` | auto-detected | Force a serial port, e.g. `/dev/ttyACM0` |
| `STATUS_PANEL_HOST` | `127.0.0.1` | Bind address for the dashboard |
| `STATUS_PANEL_HTTP_PORT` | `8765` | Dashboard port |

Port auto-detection looks for known USB-serial vendor ids (Arduino, CH340,
FTDI, CP210x). If it finds several candidates it refuses to guess and asks you
to set `STATUS_PANEL_PORT` — opening the wrong device is worse than not
starting.

## What the panel shows

Every session gets its own LED, and so does every subagent it dispatches —
both draw from the same 16-slot ring. A working session is **solid**; a
running subagent **blinks** in the same colour, so a glance answers both "how
many things are active" and "which of those are subagents."

| State | Colour | Blinks? | Who | Means |
|---|---|---|---|---|
| `WORKING` | Green | No | Session | Composing, or using a tool directly |
| `DISPATCHED` | Purple | No | Session | Waiting on subagents it just dispatched |
| `BLOCKED` | Blue | Yes | Session | Needs a decision from you right now — buzzes after 5s |
| `IDLE` | Red | Yes | Session | Turn ended, idle, no urgency |
| `RUNNING` | Green | Yes | Subagent | Currently running |
| `OFF` | Dark | — | — | Slot unclaimed |

A subagent's LED goes dark the instant it finishes — no lingering "done"
colour.

The Uno's built-in LED (D13) pulses while the bridge is connected. If it stops
and the panel goes dark, the bridge is gone — the firmware blanks itself rather
than leave stale colours that look like real status.

A 17th concurrent claimant (a session or a subagent) waits in a queue until a
slot frees up.

## Try it without hardware

**`simulator/`** runs the whole real path — hooks, `report_event.py`,
`bridge.py`, `state_manager.py` — against a faithful software stand-in for
the firmware, visualized as a live ring in the browser at `/simulator`. See
[`simulator/README.md`](simulator/README.md).

```bash
python3 simulator/virtual_board.py
```

## Tests

```bash
python3 -m pytest bridge/tests/ simulator/tests/ -v
```

Covers the bridge's pure logic — slot assignment, queueing, liveness,
persistence, token accounting, serial-port selection, telemetry parsing. The
firmware and the serial/HTTP glue are verified against real hardware; there's
no emulator for a buzzer.

## Docs

| Document | Covers |
|---|---|
| [`docs/protocol.md`](docs/protocol.md) | The serial protocol — every command, response and behaviour rule. The contract the bridge, firmware, and simulator all speak. |
| [`docs/hardware.md`](docs/hardware.md) | Parts, pin map, wiring, flashing, brightness |
| [`docs/architecture.md`](docs/architecture.md) | How the bridge works: files, threads, session/subagent handling, failure behaviour |
| [`docs/decisions.md`](docs/decisions.md) | Why things are the way they are, and what breaks if you change them back |
| [`bridge/README.md`](bridge/README.md) | Day-to-day operation and troubleshooting |
| [`simulator/README.md`](simulator/README.md) | Running the whole system without hardware |

If you are modifying this project — human or agent — read
[`docs/decisions.md`](docs/decisions.md) first. Most entries exist because the
obvious alternative was tried and failed.
