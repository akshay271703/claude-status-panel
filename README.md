# Claude Status Panel

A physical desk panel that shows what your Claude Code sessions are doing.
Three RGB modules track up to three concurrent sessions; a shared buzzer nags
you when one has been waiting too long; a browser dashboard fills in what the
LEDs can't say — which project each module belongs to, and what it's cost.

Two halves:

- **`status_panel/`** — Arduino firmware. Drives the LEDs, buzzer and silence
  button from a small line-based serial protocol.
- **`bridge/`** — a Python daemon. Listens for Claude Code hook events, decides
  which session owns which module, drives the board over serial, and serves the
  dashboard.
- **`diagnostics/`** — two throwaway sketches kept for bring-up: `blink_test`
  proves the chip is executing sketches at all, `i2c_scanner` lists devices on
  the I2C bus. Neither is part of the panel.

Runs on Windows, Linux and macOS. The only machine-specific thing is the serial
port, and that's auto-detected.

## Hardware

| Part | Notes |
|---|---|
| Arduino Uno | Built and tested on a CH340-based board |
| 3× common-cathode RGB LED module | 4-pin (R, G, B, −) |
| 1× passive buzzer module | 3-pin (VCC, GND, I/O) |
| 1× tactile push button | Silences an active alarm |
| Breadboard + jumpers | For a shared ground rail |

Full pin map and wiring notes: [`docs/hardware.md`](docs/hardware.md). D13 uses
the Uno's built-in LED as a heartbeat, so it needs no wiring.

## Quick start

Assuming the panel is wired and `arduino-cli` is installed:

```bash
git clone git@github.com:akshay271703/claude-status-panel.git
cd claude-status-panel
python3 -m pip install -r bridge/requirements.txt

# Linux only, then log out and back in
sudo usermod -a -G dialout $USER

arduino-cli compile --fqbn arduino:avr:uno status_panel
arduino-cli upload -p /dev/ttyUSB0 --fqbn arduino:avr:uno status_panel

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
arduino-cli compile --fqbn arduino:avr:uno status_panel
arduino-cli upload -p <PORT> --fqbn arduino:avr:uno status_panel
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

| State | Colour | Means |
|---|---|---|
| `THINKING` | Magenta | Session started, or you just sent a prompt |
| `WORKING` | Green | A tool call is running |
| `IDLE` | Red | Waiting on a permission decision |
| `NEED_INPUT` | Blue | Claude is done and waiting on you — buzzes after 5s |
| `OFF` | Dark | Module unclaimed |

The Uno's built-in LED (D13) pulses while the bridge is connected. If it stops
and the panel goes dark, the bridge is gone — the firmware blanks itself rather
than leave stale colours that look like real status.

A fourth concurrent session waits in a queue until a module frees up.

## Tests

```bash
python3 -m pytest bridge/tests/ -v
```

Covers the bridge's pure logic — module assignment, queueing, liveness,
persistence, token accounting, serial-port selection, telemetry parsing. The
firmware and the serial/HTTP glue are verified against real hardware; there's
no emulator for a buzzer.

## Docs

| Document | Covers |
|---|---|
| [`docs/protocol.md`](docs/protocol.md) | The serial protocol — every command, response and behaviour rule. The contract between the two halves. |
| [`docs/hardware.md`](docs/hardware.md) | Parts, pin map, wiring, flashing, brightness |
| [`docs/architecture.md`](docs/architecture.md) | How the bridge works: files, threads, session handling, failure behaviour |
| [`docs/decisions.md`](docs/decisions.md) | Why things are the way they are, and what breaks if you change them back |
| [`bridge/README.md`](bridge/README.md) | Day-to-day operation and troubleshooting |

If you are modifying this project — human or agent — read
[`docs/decisions.md`](docs/decisions.md) first. Most entries exist because the
obvious alternative was tried and failed.
