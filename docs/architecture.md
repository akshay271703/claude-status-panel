# Bridge Architecture

How Claude Code session (and subagent) activity becomes light on a desk.

This describes the current (v2, ring) design. `status_panel/status_panel.ino`
and its 3-module wiring still exist as a legacy reference — see
[decisions.md](decisions.md#v2-the-16-led-ring-status_ringino) for why they
were replaced rather than edited in place.

```
Claude Code session
      │  hook fires (SessionStart, PreToolUse, Stop, SubagentStart, …)
      ▼
report_event.py          short-lived, one per hook invocation
      │  POST /event  {session_id, event, claude_pid, tool_name, agent_id, agent_type}
      ▼
bridge.py                long-lived daemon
      │  "2:DISPATCHED\n"
      ▼
Arduino firmware  ──────► ring LEDs, buzzer
      │  STATUS reply
      ▼
dashboard  (GET / and /api/status)
```

## Files

| File | Responsibility |
|---|---|
| `bridge/bridge.py` | The daemon. Owns the serial port and the HTTP server; runs the background threads. All I/O lives here. |
| `bridge/state_manager.py` | Which claimant (session or subagent) owns which slot, the waiting queue, persistence. **Pure logic, no I/O** — this is what most tests cover. |
| `bridge/process_utils.py` | Finds a session's Claude Code PID by process ancestry; resolves its project name from that process's working directory. |
| `bridge/usage.py` | Per-session token totals, read incrementally from Claude Code transcripts — main thread and subagents, counted separately. Purely a token-accounting feature; unrelated to the LEDs. |
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
| `SessionStart` | `session_start` | `WORKING` |
| `UserPromptSubmit` | `user_prompt_submit` | `WORKING` |
| `PreToolUse` (`tool_name != "Task"`) | `pre_tool_use` | `WORKING` |
| `PreToolUse` (`tool_name == "Task"`) | `pre_tool_use` | `DISPATCHED` |
| `PermissionRequest` | `notification` | `BLOCKED` |
| `Notification` (matcher `permission_prompt`) | `notification` | `BLOCKED` |
| `Stop` | `stop` | `IDLE` |
| `SessionEnd` | `session_end` | releases the slot |
| `SubagentStart` | `subagent_start` | `RUNNING`, on a **new** slot keyed to the subagent |
| `SubagentStop` | `subagent_stop` | releases the subagent's slot immediately |

`PostToolUse` is intentionally not hooked — nothing needs to change when a tool
finishes; the slot stays `WORKING`/`DISPATCHED` until the next event supersedes it.

**`BLOCKED` is driven by `PermissionRequest`, not `Notification`.** Both are
wired, but `Notification`/`permission_prompt` fires roughly six seconds
*after* a prompt goes unanswered — it is a nudge, not a signal, and misses
every prompt answered promptly. `PermissionRequest` fires immediately. See
[decisions.md](decisions.md#idle-comes-from-permissionrequest) (the entry
predates the `BLOCKED` rename but the reasoning is unchanged).

**Detecting a `Task` dispatch reuses the existing, already-wired generic
`PreToolUse` hook** rather than a second, `Task`-matched registration —
`report_event.py` forwards the `tool_name` field it used to discard, and
`state_manager.handle_event()` branches on it. No changes to
`install_hooks.py` were needed for this part.

## `agent_id`: telling a subagent's own activity apart from its parent's

`PreToolUse`/`PostToolUse` fire for tool calls made **inside** a subagent too,
carrying the **parent's** `session_id` — tagged with `agent_id` (and
`agent_type`) so they can be told apart. `state_manager.handle_event()`
ignores any event carrying an `agent_id` unless the event itself is
`subagent_start`/`subagent_stop` — otherwise a subagent's own tool calls would
flip its parent's slot back to `WORKING` mid-`DISPATCHED`. See
[decisions.md](decisions.md#agent_id-is-a-noise-guard-not-just-an-identifier).

## Slot assignment

Both sessions and subagents are **claimants** on one shared pool of 16 slots.
A subagent's claimant key is synthetic — `f"{session_id}#agent:{agent_id}"` —
so it can hold a slot independently of its parent session's own slot. The
assignment rule itself doesn't know or care which kind of claimant it's
handling:

- **A slot is free** → claim it, apply the state.
- **All 16 claimed** → the claimant joins a FIFO queue. Nothing is sent to
  the board for it, but its state is still tracked, so it shows its *current*
  state when it eventually gets a slot, not a stale one.
- **A slot is released** → if the queue is non-empty, the next waiting
  claimant takes it immediately; otherwise the slot goes `OFF`.

A session's slot is released on `SessionEnd`, or when liveness detection
notices its process is gone. A subagent's slot is released the instant
`SubagentStop` arrives — no grace period, no lingering "done" colour (an
earlier draft had one; see
[decisions.md](decisions.md#a-finished-subagents-slot-releases-immediately-with-no-lingering-colour)
for why it was dropped).

**Solid vs. blinking green is the session-vs-subagent signal**, not a
separate colour: `WORKING` (session) is solid, `RUNNING` (subagent) blinks.
See [decisions.md](decisions.md#solid-vs-blinking-green-tells-sessions-and-subagents-apart).

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
releases the slot exactly as if the hook had fired.

A subagent claimant stores its **parent's** `claude_pid`, so a dead parent
frees its subagents' slots too, with no extra liveness code.

A Ctrl+C'd session's slot therefore goes dark within a few seconds rather
than freezing forever.

## Threads

Four daemon threads, plus the HTTP server. **No serial or file I/O ever happens
on an HTTP request path** — a wedged board cannot stall the dashboard.

| Thread | Every | Does |
|---|---|---|
| `liveness_loop` | 5 s | Releases slots whose claimant's process died |
| `ping_loop` | 3 s | Sends `PING`, drains the board's replies |
| `status_loop` | 2 s | Sends `STATUS`, caches the parsed telemetry |
| `usage_loop` | 5 s | Re-reads session transcripts for token totals (real sessions only — see below) |

A fifth thread is spawned on demand to reconnect a dropped serial port.

All shared state is guarded by one lock. Each loop wraps its body in a broad
`except` that logs and continues — a thread dying silently would disable its
feature with no symptom, which is exactly the failure this system exists to
prevent elsewhere.

The reconnect loop deliberately does **not** hold the lock while sleeping,
otherwise an unplugged board would park every HTTP thread behind it.

## Persistence and recovery

The bridge writes `claimant key → {module, last_known_state, claude_pid}` to
`bridge/.bridge_state.json` on every change, atomically (write-then-replace, so
a Ctrl+C mid-write cannot truncate it). A subagent's synthetic key round-trips
through this exactly like a real session_id — no schema change was needed.

On startup it loads that file, discards entries whose process is gone, and
repaints the rest — so restarting the bridge mid-session is a brief blink
rather than a loss of tracking. A corrupt or unreadable file is discarded with
a log line rather than treated as fatal.

## The dashboard

Served by the bridge itself at `http://127.0.0.1:8765`.

- `GET /` — the page
- `GET /api/status` — JSON, polled every 5s

Per slot it shows the project name, state, time in that state, and (for a
real session, not a subagent's synthetic slot) token totals. Below that,
hardware telemetry straight from `STATUS`, including a compact 16-cell strip
mirroring the physical ring.

**Project names** come from reading each claimant's process working directory
via its `claude_pid` — which is why a subagent's card shows the same project
as its parent (it shares the parent's pid) with no extra lookup needed.

**Token totals** come from Claude Code's own transcripts, located by session id
— **entirely unrelated to slot assignment**. A session writes two kinds of file:

```
~/.claude/projects/<dir>/
  <session_id>.jsonl                        main thread
  <session_id>/subagents/
    agent-<id>.jsonl                        one per subagent, any nesting depth
    agent-<id>.meta.json                    agentType, description, model, spawnDepth
```

Both are read, and the results are kept apart — the dashboard shows a main vs.
subagent split with the subagent count, and a per-subagent breakdown on hover.
Reading only the main transcript undercounts: see
[decisions.md](decisions.md#subagent-tokens-are-counted-and-reported-separately).
`usage_loop` only polls real session_ids — a subagent's synthetic claimant key
has no transcript of its own to look up by.

Everything is read incrementally: the tracker keeps a byte offset **per file**
and consumes only what was appended, leaving a partially-written trailing line
alone until it is complete. Transcripts reach megabytes and are being written
while read, so re-parsing from the top or mis-handling a torn line would be
both wasteful and wrong. The subagents directory is re-globbed every poll,
since subagents appear while the session runs.

The four token figures are not comparable to each other — `cache_read`
dominates by orders of magnitude but is re-sent context billed at a fraction of
normal input. `output_tokens` is the closest thing to "work done"; summing all
four is meaningless.

**Desync detection.** The bridge compares the firmware's reported slot states
(decoded from the packed `ring=` STATUS field) against what it believes it
sent, and raises a banner when they disagree. This is the only way a dropped
or rejected command becomes visible.

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
| Bridge dies while running | Board stops receiving pings, goes stale after 10 s: ring dark, D13 pulse stops, buzzer silenced. |
| Board unplugged | Write fails, port marked dead, background reconnect starts. Session tracking continues; the panel repaints on reconnect. |
| 17th claimant (session or subagent) starts | Queued. No slot until one frees. |
| Malformed hook payload | Rejected with a log line; other sessions unaffected. |
| Corrupt state file | Discarded on startup with a log line. |

`report_event.py` is the highest-stakes file in the project: it runs inside
every hook invocation of every session (and every subagent). It wraps
everything — including its own imports — in a try/except, uses a 0.5 s
timeout, and always exits 0. A missed LED update is acceptable; disrupting a
real coding session is not.
