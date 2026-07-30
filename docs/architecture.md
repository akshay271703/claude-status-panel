# Bridge Architecture

How Claude Code session activity becomes light on a desk.

```
Claude Code session
      │  hook fires (SessionStart, PreToolUse, Stop, …)
      ▼
report_event.py          short-lived, one per hook invocation
      │  POST /event  {session_id, event, claude_pid}
      ▼
bridge.py                long-lived daemon
      │  "2:WORKING\n"
      ▼
Arduino firmware  ──────► LEDs, buzzer
      │  STATUS reply
      ▼
dashboard  (GET / and /api/status)
```

## Files

| File | Responsibility |
|---|---|
| `bridge/bridge.py` | The daemon. Owns the serial port and the HTTP server; runs the background threads. All I/O lives here. |
| `bridge/state_manager.py` | Which session owns which module, the waiting queue, persistence. **Pure logic, no I/O** — this is what most tests cover. |
| `bridge/process_utils.py` | Finds a session's Claude Code PID by process ancestry; resolves its project name from that process's working directory. |
| `bridge/usage.py` | Per-session token totals, read incrementally from Claude Code transcripts. |
| `bridge/serial_port.py` | Picks the serial device by USB vendor id. Pure function, testable without hardware. |
| `bridge/report_event.py` | Invoked by every hook. POSTs one event and exits. |
| `bridge/install_hooks.py` | One-time setup: writes hook config into `~/.claude/settings.json`. |
| `bridge/dashboard.html` | The status page. Edit directly; no build step. |
| `bridge/debug_hook.py` | Diagnostic. Logs any hook's full payload. Not wired by default. |

The split between `state_manager.py` and `bridge.py` is deliberate and worth
preserving: it is what makes the tricky logic (queueing, liveness, persistence)
testable without a board or a serial port.

## Hook → state mapping

Configured in `~/.claude/settings.json` by `install_hooks.py`.

| Hook event | Internal name | Resulting state |
|---|---|---|
| `SessionStart` | `session_start` | `THINKING` |
| `UserPromptSubmit` | `user_prompt_submit` | `THINKING` |
| `PreToolUse` | `pre_tool_use` | `WORKING` |
| `PermissionRequest` | `notification` | `IDLE` |
| `Notification` (matcher `permission_prompt`) | `notification` | `IDLE` |
| `Stop` | `stop` | `NEED_INPUT` |
| `SessionEnd` | `session_end` | releases the module |

`PostToolUse` is intentionally not hooked — nothing needs to change when a tool
finishes; the module stays `WORKING` until the next event supersedes it.

**`IDLE` is driven by `PermissionRequest`, not `Notification`.** Both are wired,
but `Notification`/`permission_prompt` fires roughly six seconds *after* a
prompt goes unanswered — it is a nudge, not a signal, and misses every prompt
answered promptly. `PermissionRequest` fires immediately. See
[decisions.md](decisions.md#idle-comes-from-permissionrequest).

## Session-to-module assignment

Sessions are assigned lazily: the first event for an unknown `session_id`
claims a module.

- **A module is free** → claim it, apply the state.
- **All three claimed** → the session joins a FIFO queue. Nothing is sent to
  the board for it, but its state is still tracked, so it shows its *current*
  state when it eventually gets a module, not a stale one.
- **A module is released** → if the queue is non-empty, the next waiting
  session takes it immediately; otherwise the module goes `OFF`.

A module is released on `SessionEnd`, or when liveness detection notices the
session's process is gone.

## Liveness detection

`SessionEnd` is not guaranteed. Per Claude Code's hook documentation:

| How the session ended | `SessionEnd` fires? |
|---|---|
| `/exit`, `/clear`, `/logout`, Ctrl+D | Yes |
| Ctrl+C | Unreliable — may be interrupted mid-hook |
| Terminal closed | Unreliable |
| Killed or crashed | No |

So every event carries the session's Claude Code PID, found by walking the hook
script's own process ancestry looking for a process named `claude`. The bridge
polls those PIDs every 5 s; when one is gone and no `SessionEnd` arrived, it
releases the module exactly as if the hook had fired.

A Ctrl+C'd session's module therefore goes dark within a few seconds rather
than freezing forever.

## Threads

Four daemon threads, plus the HTTP server. **No serial or file I/O ever happens
on an HTTP request path** — a wedged board cannot stall the dashboard.

| Thread | Every | Does |
|---|---|---|
| `liveness_loop` | 5 s | Releases modules whose session process died |
| `ping_loop` | 3 s | Sends `PING`, drains the board's replies |
| `status_loop` | 2 s | Sends `STATUS`, caches the parsed telemetry |
| `usage_loop` | 5 s | Re-reads session transcripts for token totals |

A fifth thread is spawned on demand to reconnect a dropped serial port.

All shared state is guarded by one lock. Each loop wraps its body in a broad
`except` that logs and continues — a thread dying silently would disable its
feature with no symptom, which is exactly the failure this system exists to
prevent elsewhere.

The reconnect loop deliberately does **not** hold the lock while sleeping,
otherwise an unplugged board would park every HTTP thread behind it.

## Persistence and recovery

The bridge writes `session_id → {module, last_known_state, claude_pid}` to
`bridge/.bridge_state.json` on every change, atomically (write-then-replace, so
a Ctrl+C mid-write cannot truncate it).

On startup it loads that file, discards entries whose process is gone, and
repaints the rest — so restarting the bridge mid-session is a brief blink
rather than a loss of tracking. A corrupt or unreadable file is discarded with
a log line rather than treated as fatal.

## The dashboard

Served by the bridge itself at `http://127.0.0.1:8765`.

- `GET /` — the page
- `GET /api/status` — JSON, polled once a second

Per session it shows the project name, state, time in that state, and token
totals. Below that, hardware telemetry straight from `STATUS`.

**Project names** come from reading each session's process working directory
via its PID — which is why no hook changes were needed to add them.

**Token totals** come from Claude Code's own transcripts at
`~/.claude/projects/<dir>/<session_id>.jsonl`, located by session id. They are
read incrementally: the tracker keeps a byte offset per session and consumes
only what was appended, leaving a partially-written trailing line alone until
it is complete. Transcripts reach megabytes and are being written while read,
so re-parsing from the top or mis-handling a torn line would be both wasteful
and wrong.

The four token figures are not comparable to each other — `cache_read`
dominates by orders of magnitude but is re-sent context billed at a fraction of
normal input. `output_tokens` is the closest thing to "work done"; summing all
four is meaningless.

**Desync detection.** The bridge compares the firmware's reported module states
against what it believes it sent, and raises a banner when they disagree. This
is the only way a dropped or rejected command becomes visible.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `STATUS_PANEL_PORT` | auto-detected | Force a serial device |
| `STATUS_PANEL_HOST` | `127.0.0.1` | Dashboard bind address |
| `STATUS_PANEL_HTTP_PORT` | `8765` | Dashboard port |

Auto-detection matches known USB-serial vendor ids (Arduino, CH340, FTDI,
CP210x). With several candidates it refuses to guess and lists them — opening
the wrong device is worse than not starting.

## Failure behaviour

| Situation | What happens |
|---|---|
| Bridge not running | Hooks fail silently and exit 0. No LEDs change. The usual cause of "nothing happens". |
| Bridge dies while running | Board stops receiving pings, goes stale after 10 s: panel dark, D13 pulse stops, buzzer silenced. |
| Board unplugged | Write fails, port marked dead, background reconnect starts. Session tracking continues; the panel repaints on reconnect. |
| Fourth session starts | Queued. No module until one frees. |
| Malformed hook payload | Rejected with a log line; other sessions unaffected. |
| Corrupt state file | Discarded on startup with a log line. |

`report_event.py` is the highest-stakes file in the project: it runs inside
every hook invocation of every session. It wraps everything — including its own
imports — in a try/except, uses a 0.5 s timeout, and always exits 0. A missed
LED update is acceptable; disrupting a real coding session is not.
