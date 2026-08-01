# Ring Simulator

Try the whole system — real Claude Code hooks, real `bridge.py`,
real `state_manager.py` — without any hardware. Only the firmware leg is
replaced, by `virtual_board.py`, a faithful software port of
`status_ring/status_ring.ino`'s protocol.

```
Claude Code session (real)
      │  hooks fire (real)
      ▼
report_event.py (real)  ──POST /event──►  bridge.py (real, unmodified logic)
                                                 │
                                     serial_open (the only fake part)
                                                 ▼
                                        VirtualBoard (this folder)
```

## Running it

```bash
python3 simulator/virtual_board.py
```

Then:

1. Open **http://127.0.0.1:8765/simulator** — a big visual ring, one LED per
   slot, coloured and blinking exactly like the real firmware would.
2. Make sure hooks are installed (`python3 bridge/install_hooks.py`) and
   start a **new** Claude Code session — sessions started before the hooks
   were wired won't have them.
3. Watch the ring react: your session's slot goes solid green while it
   works, purple while it's waiting on subagents it dispatched, blue and
   blinking if it needs a permission decision, red and blinking once its
   turn ends. Dispatch a subagent (or run `/loop`, a `Task`-based workflow,
   etc.) and watch it claim its own blinking-green slot, then go dark the
   instant it finishes.

The regular dashboard (**http://127.0.0.1:8765/**) works exactly as it does
against real hardware too — same data, same per-session cards, same token
totals. `/simulator` is just a bigger, dedicated view of the ring itself;
it's equally useful later against a real board, not simulator-only code.

**Stop any real `bridge/bridge.py` first** — both bind port 8765, so only
one can run at a time. `virtual_board.py` uses its own state file
(`simulator/.simulated_state.json`, gitignored) precisely so a stray
simulator run never races writes against a real bridge's
`bridge/.bridge_state.json` if you *do* run one on an alternate port for
comparison (`STATUS_PANEL_HTTP_PORT=8765` is the bridge's own override, so
set a different value for whichever one you're not actively using).

## The silence button

The physical board's silence button has no serial command at all — it's a
GPIO the real firmware reads directly. The simulator can't fake a button
press over the wire either, so `virtual_board.py` runs one extra, tiny HTTP
server (default **`127.0.0.1:8766`**, override with
`STATUS_PANEL_SIM_CONTROL_PORT`) with a single `POST /button` endpoint. The
simulator page's "press silence button" calls it directly. Real hardware
needs none of this.

## Files

- `virtual_board.py` — `VirtualBoard`: pure protocol/state logic, no I/O,
  line-for-line mirroring `status_ring.ino`'s `processLine()`/`printStatus()`
  (see `simulator/tests/test_virtual_board.py`). Everything below that in the
  file is transport: a `socket.socketpair()` (not a PTY — this project
  supports Windows, and PTYs are POSIX-only) whose two ends play the role of
  "the serial cable," `SocketSerial` (just enough of pyserial's surface —
  `write`/`readline`/`reset_input_buffer`/`close` — for `bridge.py` to use
  unmodified), and the tiny button-control server.
- `tests/test_virtual_board.py` — unit tests for `VirtualBoard` alone: OK/ERR
  responses, the packed `STATUS` format, `BLOCKED` alarm timing (including
  that `buzz=` is continuous while alarming, *not* gated by the ~150ms chirp
  window — an easy thing to get wrong, since the real firmware's `tone()`
  call *is* chirp-gated, but `buzzerActive` isn't), the silence button
  (including it being ignored while stale, and re-arming only on a fresh
  `BLOCKED` transition), and staleness.

## What's genuinely simulated vs. what's real

| Real | Simulated |
|---|---|
| Claude Code hooks | The serial cable |
| `report_event.py` | The Arduino itself |
| `bridge.py` / `state_manager.py` | LEDs, buzzer speaker |
| Slot assignment, queueing, liveness | Physical button (needs the control-port workaround above) |
| Token accounting (`usage.py`) | |

If something looks right in the simulator, the only thing left to verify on
real hardware is the physical wiring itself — the entire software path
(hooks → assignment → protocol) has already been exercised for real.
