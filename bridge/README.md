# Claude Status Panel — Host Bridge

Connects live Claude Code sessions to the Arduino status panel
(`../status_panel/status_panel.ino`). Up to 3 concurrent sessions each claim a
physical LED module; a 4th waits in a FIFO queue until one frees up.

## Running it

The bridge is **started manually** — this is deliberate, not an oversight.

```
python3 bridge/bridge.py
```

Expected output:

```
Opening serial port /dev/ttyACM0@9600...
Bridge listening on http://127.0.0.1:8765/event
```

Leave it running. Opening the serial port resets the Arduino, so all three
modules blank at startup and then repaint from any recovered session state.

**If the bridge isn't running, nothing happens.** Hooks fail silently by
design — a missed LED update must never disrupt an actual coding session — so
"no lights" almost always means "the bridge isn't up." Check that first.

## Dashboard

With the bridge running, open **http://127.0.0.1:8765** for a live view that
the LEDs can't give you — chiefly *which project* each module belongs to.

Per session: project name, state, how long it's been in that state, and token
totals (input / output / cache-write / cache-read; hover for exact figures).
Below that, a Hardware section reads the board's own telemetry — its uptime,
brightness, staleness flag, buzzer state, free SRAM, and the module states as
the *firmware* reports them.

That last one matters: the bridge compares the firmware's view against what it
believes it sent, and raises a desync banner if they disagree. A dropped or
rejected command was previously undetectable.

The page polls once a second (token totals refresh every 5s) and says so
plainly when the bridge goes away, rather than freezing on stale values.

Two notes on the token figures: `cache_read` dominates by orders of magnitude
but is re-sent context billed at a fraction of normal input, so the four
numbers aren't comparable and summing them isn't meaningful — `output` is the
closest thing to "work done". And totals come from Claude Code's own session
transcripts, found by session id, so no hook changes were needed.

## States

| State | Colour | Fires on |
|---|---|---|
| `THINKING` | Magenta | session start / prompt submitted |
| `WORKING` | Green | tool call starting |
| `IDLE` | Red | waiting on a permission decision |
| `NEED_INPUT` | Blue | Claude finished, waiting on you (buzzes after 5s) |
| `OFF` | Dark | module unclaimed |
| — | D13 pulse | bridge alive; stops when contact goes stale |

The firmware also accepts three device-level verbs with no module prefix:
`PING` (liveness), `STATUS` (returns its own telemetry, used by the dashboard),
and `DIM:<0-100>` (LED brightness as a PWM duty percentage; not persisted, so
it reverts to the compiled default on reboot).

`IDLE` is driven by the **`PermissionRequest`** hook, not `Notification`.
`Notification`/`permission_prompt` fires ~6s *after* a prompt goes unanswered
(it's a nudge, not a signal), so it misses every prompt answered promptly.

## Pieces

- `bridge.py` — daemon: owns the serial port, serves the dashboard and
  `/api/status` on `127.0.0.1:8765`, and runs four background threads —
  session liveness (5s), firmware ping (3s), firmware `STATUS` telemetry (2s),
  and token usage (5s). All serial and file I/O happens on those threads, never
  on an HTTP request path, so a wedged board can't stall the page.
- `state_manager.py` — pure module-assignment / queue / persistence logic
  (no I/O; this is what most of the tests cover)
- `usage.py` — per-session token totals, read incrementally from Claude Code
  transcripts; tolerates the file being appended to mid-read
- `process_utils.py` — finds the Claude Code PID by walking process ancestry,
  and resolves a session's project name from that PID's working directory
- `report_event.py` — invoked by every hook; POSTs one event and exits
- `dashboard.html` — the status page; edit it directly, no rebuild step
- `debug_hook.py` — diagnostic; logs any hook's full payload to
  `hook_debug.log`. Not wired by default — add it as an extra `hooks` entry in
  `~/.claude/settings.json` when you need to see what a hook actually sends.

Hook wiring lives in `~/.claude/settings.json` (outside this repo, so it isn't
version-controlled here).

## Troubleshooting

| Symptom | Cause |
|---|---|
| No LEDs change at all | Bridge not running, or Arduino unplugged |
| One module stuck lit | Session died; the liveness poll clears it within ~5s |
| Events rejected | Check the bridge's stderr — every rejection is logged |
| Board dark, bridge alive | Serial dropped; it reconnects automatically and repaints |
| Panel dark, D13 not pulsing | Bridge stopped or serial dropped — the firmware blanks rather than show stale colours. Restart the bridge (`python3 bridge/bridge.py`); the panel repaints once contact resumes |

State lives in `.bridge_state.json` (gitignored). It's written atomically and a
corrupt or unreadable file is discarded on startup rather than blocking it, so
deleting it is safe but shouldn't be necessary.

## Uploading new firmware

**The bridge holds the serial port open.** Stop it before running any `arduino-cli
upload` (or opening the Arduino IDE's serial monitor) against the board, or
the upload will fail to acquire the port. Restart the bridge afterwards —
it isn't auto-started.

## Tests

```
python3 -m pytest bridge/tests/ -v
```

Covers `state_manager` and `process_utils`. `bridge.py` (serial + HTTP glue) is
verified against real hardware rather than unit tests.
