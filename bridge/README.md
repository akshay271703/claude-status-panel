# Claude Status Panel — Host Bridge

Connects live Claude Code sessions — and their subagents — to the Arduino
status panel (`../status_ring/status_ring.ino`, a 16-LED WS2812B/NeoPixel
ring). Sessions and subagents are both **claimants** on the same 16-slot pool;
each claims a slot when it starts, and a 17th waits in a FIFO queue until one
frees up. (`../status_panel/status_panel.ino`, the original 3-module board, is
kept as a legacy reference — see `docs/decisions.md`.)

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

Leave it running. Opening the serial port resets the Arduino, so the whole
ring blanks at startup and then repaints from any recovered session state.

**If the bridge isn't running, nothing happens.** Hooks fail silently by
design — a missed LED update must never disrupt an actual coding session — so
"no lights" almost always means "the bridge isn't up." Check that first.

## Dashboard

With the bridge running, open **http://127.0.0.1:8765** for a live view that
the LEDs can't give you — chiefly *which project* each slot belongs to, and
whether a lit slot is a session or one of its subagents.

Per session: project name, state, how long it's been in that state, and token
totals (input / output / cache-write / cache-read; hover for exact figures).
Under those, a **main vs. subagent split** — how much of the session's output
came from the main thread and how much from dispatched subagents, with the
count. Hover it for a per-subagent breakdown (type, model, purpose, output
tokens), heaviest first. A subagent's own slot card skips this block — its
tokens are already counted in its parent's split, and it has no transcript of
its own to look up separately.

Below that, a Hardware section reads the board's own telemetry — its uptime,
brightness, staleness flag, buzzer state, free SRAM, protocol version, and a
compact 16-cell strip mirroring the ring as the *firmware* currently sees it.

That last one matters: the bridge compares the firmware's view against what it
believes it sent, and raises a desync banner if they disagree. A dropped or
rejected command was previously undetectable.

The page polls every 5s (token totals refresh on the same 5s cadence) and says so
plainly when the bridge goes away, rather than freezing on stale values.

Two notes on the token figures: `cache_read` dominates by orders of magnitude
but is re-sent context billed at a fraction of normal input, so the four
numbers aren't comparable and summing them isn't meaningful — `output` is the
closest thing to "work done". And totals come from Claude Code's own session
transcripts, found by session id — entirely separate from slot assignment, so
no hook changes were needed for token accounting itself.

Subagents write their own transcripts in a sibling directory, so counting only
the main one undercuts the real figure — measured here at ~19% of output
tokens across two full sessions. Both are read; the split row is what tells
you which half of the session actually spent it.

## States

A working session is **solid**; a running subagent **blinks** in the same
green — that's the signal for "session vs. subagent," not a separate colour.

| State | Colour | Blinks? | Who | Fires on |
|---|---|---|---|---|
| `WORKING` | Green | No | Session | session start / prompt submitted / any non-`Task` tool call |
| `DISPATCHED` | Purple | No | Session | dispatching a subagent (`PreToolUse` for `Task`) |
| `BLOCKED` | Blue | Yes | Session | waiting on a permission decision — buzzes after 5s |
| `IDLE` | Red | Yes | Session | Claude finished its turn, waiting on you, no urgency |
| `RUNNING` | Green | Yes | Subagent | currently running (`SubagentStart`) |
| `OFF` | Dark | — | — | slot unclaimed, or a subagent just finished (`SubagentStop` releases it immediately) |
| — | D13 pulse | — | — | bridge alive; stops when contact goes stale |

The firmware also accepts three device-level verbs with no slot prefix:
`PING` (liveness), `STATUS` (returns its own telemetry, used by the dashboard),
and `DIM:<0-100>` (LED brightness as a percentage; not persisted, so it
reverts to the compiled default on reboot).

`BLOCKED` is driven by the **`PermissionRequest`** hook, not `Notification`.
`Notification`/`permission_prompt` fires ~6s *after* a prompt goes unanswered
(it's a nudge, not a signal), so it misses every prompt answered promptly.

`DISPATCHED` reuses the already-wired generic `PreToolUse` hook rather than a
new registration — `report_event.py` forwards `tool_name`, and the bridge
branches on `tool_name == "Task"`. A subagent's own internal tool calls also
fire `PreToolUse` against its *parent's* session_id, but are tagged with
`agent_id`, which the bridge uses to ignore them rather than let them flip the
parent back to `WORKING` mid-dispatch.

## Pieces

- `bridge.py` — daemon: owns the serial port, serves the dashboard and
  `/api/status` on `127.0.0.1:8765`, and runs four background threads —
  session liveness (5s), firmware ping (3s), firmware `STATUS` telemetry (2s),
  and token usage (5s, real sessions only). All serial and file I/O happens on
  those threads, never on an HTTP request path, so a wedged board can't stall
  the page.
- `state_manager.py` — pure slot-assignment / queue / persistence logic for
  both sessions and subagents (no I/O; this is what most of the tests cover)
- `usage.py` — per-session token totals, read incrementally from Claude Code
  transcripts (the main one *and* each subagent's, kept apart); tolerates a
  file being appended to mid-read. Purely a dashboard/token-accounting
  concern — unrelated to slot assignment.
- `process_utils.py` — finds the Claude Code PID by walking process ancestry,
  and resolves a session's project name from that PID's working directory
- `report_event.py` — invoked by every hook; POSTs one event (plus
  `tool_name`/`agent_id`/`agent_type` where present) and exits
- `install_hooks.py` — one-time setup: writes hook config (including
  `SubagentStart`/`SubagentStop`) into `~/.claude/settings.json`
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
| One slot stuck lit | Session or subagent's parent died; the liveness poll clears it within ~5s |
| A subagent's slot never appears | Confirm `SubagentStart`/`SubagentStop` are wired — run `python3 bridge/install_hooks.py --dry-run` and check both are present |
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
python3 -m pytest bridge/tests/ simulator/tests/ -v
```

Covers `state_manager` (including subagent claim/release and the `agent_id`
guard), `process_utils`, `usage`, `serial_port`, `STATUS` parsing, and (in
`simulator/tests/`) the virtual board's own protocol handling and alarm
timing. The serial and HTTP glue in `bridge.py` — and real firmware — are
verified against `simulator/virtual_board.py` and real hardware rather than
unit tests; see [`simulator/README.md`](../simulator/README.md).
