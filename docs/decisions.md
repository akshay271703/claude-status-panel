# Design Decisions

Why things are the way they are. Most entries here exist because the obvious
alternative was tried first and failed — so changing them back will reintroduce
a specific, known bug.

If you are an agent modifying this project, read this before "simplifying"
anything below.

---

## `IDLE` comes from `PermissionRequest`

**Not** from `Notification` with matcher `permission_prompt`, which is the
intuitive choice and was the original implementation.

`Notification`/`permission_prompt` is a *delayed nudge*. Measured four times on
real sessions, it fires **~6.2 seconds after** a permission prompt goes
unanswered — it exists to alert you when you are not looking at the terminal.
Prompts answered promptly never produce it at all, so `IDLE` never appeared in
practice.

`PermissionRequest` fires immediately. Both hooks are wired (they map to the
same state, so the late one is harmless), but `PermissionRequest` is the one
doing the work.

**Don't** drop `PermissionRequest` on the grounds that `Notification` looks
like it covers the same ground.

## LEDs are dimmed by software PWM

**Not** `analogWrite`. Only D3, D5, D6, D9 and D10 of the nine module pins
support hardware PWM on an Uno. Using it would leave module 2 dimmable and
modules 1 and 3 mostly not — visibly inconsistent.

`driveLeds()` records intent into `wantR/wantG/wantB`; `handleLedPwm()` is the
sole output stage and gates every channel on one shared duty cycle.

### The PWM only writes pins when the phase flips

A first attempt wrote all nine pins every loop iteration. Nine `digitalWrite`
calls cost roughly 45 µs, which made `loop()` too slow to sample a short
on-window cleanly — at 2% duty the on-window is 80 µs, so iterations randomly
missed it and the LEDs **flickered visibly**.

The fix is the `pwmDirty` / `lastPwmOn` check: pins are touched only when the
duty phase changes or a state change dirties them.

**Don't** remove that early return "for clarity". The flicker comes straight
back at low brightness.

## Staleness suppresses output only

When the board loses contact it drives the LEDs and buzzer off but leaves
`modules[].state`, `needInputSince` and `silenced` **untouched**.

That is what lets it repaint itself from its own memory when contact returns,
with no resync protocol and no cooperation from the host — and why a
still-overdue module correctly resumes chirping instead of being silently
cleared.

Blanking uses `driveLeds(i, OFF)`, never `applyState(i, OFF)`. The latter would
overwrite the retained state and permanently destroy the information recovery
depends on.

**Don't** "tidy up" state when going stale.

## The board starts stale

`setup()` initialises `lastContactMs` to a value already past the timeout.

Previously it started at zero, which — since `millis()` also starts near zero —
meant the board considered itself *fresh* for its first 10 seconds. A board on
a USB charger with no host attached would pulse D13 and show a dark panel: the
exact composite that means "healthy and idle". It was lying.

## Repeated `NEED_INPUT` does not restart the alarm

Only a transition *into* `NEED_INPUT` from another state starts the countdown.

This is what makes the protocol safe to resend into. The host repaints modules
on reconnect and on restart; if resending restarted timers, a session that had
been waiting ten minutes would look freshly idle every time the bridge blinked.

## The bridge is write-only, except for `STATUS`

For most of its life the bridge never read from the serial port at all — it
drains replies to stop them filling the host's input buffer. That was a
deliberate simplification: parsing `OK`/`ERR:` adds failure modes for
information nothing consumed.

That changed only when the dashboard needed real telemetry. The read path is
confined to `_query_status()`, on its own thread, holding the lock across
send-and-read so a concurrent drain cannot eat the reply.

**Don't** add general reply parsing to the write path. The write path stays
fire-and-forget.

## Liveness is checked by PID, not by trusting `SessionEnd`

`SessionEnd` does not fire on Ctrl+C, a closed terminal, or a crash — verified
against Claude Code's hook documentation. Relying on it alone leaves modules lit
for sessions that ended.

So every event carries the session's Claude Code PID and the bridge polls
whether that process still exists.

### `is_pid_alive(None)` must return `False`

`find_claude_pid()` returns `None` by design when the ancestry walk fails.
Passing that to `psutil.pid_exists()` raises `TypeError`, which killed the
liveness thread **silently** — disabling the whole fallback with no symptom but
modules that never went dark — and persisted a `null` pid that then crashed
every subsequent startup until the state file was deleted by hand.

The guard is one line. It is load-bearing.

## `report_event.py` must never fail visibly

It runs inside every hook invocation of every session, on the blocking path of
real work. It wraps everything — **including its own imports** — in a
try/except, uses a 0.5 s HTTP timeout, and always exits 0.

The import guard matters: a broken `process_utils.py` would otherwise produce an
unhandled traceback on every tool call in every session.

A missed LED update is an acceptable failure. Disrupting someone's actual
coding session is not.

## `state_manager.py` has no I/O

Module assignment, queueing, liveness bookkeeping and persistence formatting
are pure functions over in-memory state, with the clock and the liveness check
injected. All the genuinely tricky logic is therefore testable without a board,
a serial port, or a live session — which is why most of the test suite lives
there.

`bridge.py` is the only file that touches serial, HTTP or the filesystem.

**Don't** reach for `psutil` or `serial` inside `state_manager.py`.

## Serial writes have a `write_timeout`

pyserial's `timeout` bounds *reads* only; `write_timeout` defaults to `None`,
meaning writes block forever.

A port that is present but not draining — a wedged driver, a device that
stopped accepting without disappearing — would hang `write()` instead of
raising. The existing handler never fires, the reconnect never starts, and the
ping thread parks **inside the lock**, stalling every other thread.

`SerialTimeoutException` subclasses `SerialException`, so the existing handler
catches it unchanged.

## Port detection refuses to guess

When several candidate devices are present, `choose_port()` raises with a list
rather than picking one. Opening the wrong device is worse than not starting:
it can drive unrelated hardware, and the failure is confusing rather than
obvious.

## The bridge is started manually

Auto-start on login was considered and rejected. It is a desk toy; having it
launch itself was more surprising than useful, and it would need to solve a
startup race between concurrent sessions for no real gain.

The cost is real and documented: if the bridge is not running, nothing happens
and the hooks fail silently. That is the first thing to check when the panel
seems dead.

## The panel is capped at three modules

Not an architectural limit — a pin one. D2–D13 are fully allocated, so a fourth
RGB module has nowhere to go.

A fourth concurrent session queues rather than being dropped. Raising the cap
means either using A0–A5 as digital pins (good for two more modules), or moving
to addressable LEDs like WS2812B, which need one pin for any number of LEDs and
would free eight.

## No cost estimate on token usage

The dashboard shows raw token counts and no dollar figure. Pricing varies by
model and changes over time; a hardcoded rate would drift out of date silently
and be believed anyway.

The four counts are also not comparable to each other — `cache_read` dominates
by orders of magnitude but is billed at a fraction of normal input. Summing them
produces a number that looks alarming and means nothing.

## Subagent tokens are counted, and reported separately

A session's transcript is not one file. Subagents each write their own under
`<session_id>/subagents/agent-<id>.jsonl`, with an `agent-<id>.meta.json`
sidecar naming the type, model and purpose. Nested subagents land in that same
flat directory, so one glob catches every depth.

The first implementation read only `<session_id>.jsonl`. Measured on the two
sessions that built this project, that missed:

| Session | Main output | Subagent output | Missed |
|---|---|---|---|
| Phase 1 + 2 | 898,526 | 166,291 | 18.5% |
| Heartbeat + dashboard | 523,816 | 100,818 | 19.2% |

Cache-read was worse in absolute terms — 25.7M and 10.4M tokens invisible.

They are **not** merged into one number. Main and subagent output answer
different questions: a session at 900k output because you had a long
conversation and a session at 900k because it fanned out 29 agents are not the
same session, and only the split distinguishes them.

**Don't** collapse this back into a single total.

## Token totals are read incrementally

Transcripts reach several megabytes and are appended to *while being read*.
`usage.py` keeps a byte offset per file and consumes only new bytes,
stopping at the last complete newline so a half-written trailing line is left
for the next poll.

Re-reading from the top every 5 seconds would be wasteful; mishandling the torn
line would double-count or drop a message and corrupt the totals silently. Both
behaviours are pinned by tests.

## `THINKING` is magenta, not yellow

Yellow (R+G) reads as green-dominant to the eye and was too easily confused
with `WORKING` (green) at a glance. Magenta (R+B) shares no channel with green.

**Superseded by the ring redesign below** — v2 (`status_ring.ino`) collapses
`THINKING` into `WORKING` and no longer has an equivalent state. Kept here for
history; the reasoning (yellow reads as green-dominant) is still true of any
future colour choice on this hardware.

---

# v2: the 16-LED ring (`status_ring.ino`)

The entries below describe the redesign that replaced the 3-module board with
a single WS2812B/NeoPixel ring. See [protocol.md](protocol.md) and
[architecture.md](architecture.md) for the full current behaviour; these are
just the decisions worth not re-litigating.

## The panel is no longer capped at three modules

The [old cap](#the-panel-is-capped-at-three-modules) was a pin limit, not an
architectural one, and the fix anticipated there — addressable LEDs needing
one pin for any number of LEDs — is exactly what shipped: a 16-LED WS2812B
ring on a single data pin (D6). `status_panel.ino` and its 3-module wiring are
kept as a legacy reference, not removed; `status_ring.ino` is the new primary
sketch. `MODULE_NUMBERS` widened from `(1, 2, 3)` to `range(1, 17)` in
`state_manager.py` with **no other structural change** — the assign/release/
queue/persist logic was already generic over an opaque claimant key, which is
exactly what let subagents reuse it too (see below).

## Subagents are claimants on the same pool as sessions, not a token-usage-only concern

Before this, `usage.py` tracked subagent token totals purely for the
dashboard — the physical board had no idea a subagent existed. Now a
subagent claims its own slot on the same 16-slot pool a session does, via a
synthetic key `f"{session_id}#agent:{agent_id}"`. This reuses
`_assign_module`/`_release_module`/the FIFO queue **unmodified** — a
subagent is, as far as that machinery is concerned, just another claimant.

**Don't** special-case subagent assignment logic. If it needs to behave
differently from a session claiming a slot, that's a sign the generic
machinery is the wrong layer for the change.

## `SubagentStart`/`SubagentStop` are used directly — not file-activity polling

An earlier pass of this design deliberately avoided registering new hooks for
subagent lifecycle, planning instead to infer "is this subagent still active"
from whether its transcript file (`usage.py` already polls these) grew in the
last poll — because this project has a real history of Claude Code hook
behaviour not matching casual assumptions (see "`IDLE` comes from
`PermissionRequest`" above), and the existence/reliability of subagent-specific
hook events hadn't been confirmed.

Checking https://code.claude.com/docs/en/hooks.md directly confirmed
`SubagentStart` and `SubagentStop` are real, documented events, both carrying
a stable `agent_id` correlating start and stop precisely. That's strictly
better than a polling heuristic — real-time, exact, no debounce window, no
lingering state — so the design switched to using them directly.
**`usage.py` is completely untouched by this feature**; it remains solely a
token-accounting concern, unrelated to the LEDs.

**Don't** reintroduce file-activity polling for subagent presence "to avoid
depending on a hook" — the hook is the better mechanism *because* it was
actually verified, not assumed.

## `agent_id` is a noise guard, not just an identifier

The same official-docs check surfaced something that would have broken the
`DISPATCHED` (purple) state if missed: `PreToolUse`/`PostToolUse` fire for
tool calls made **inside** a dispatched subagent too, carrying the **parent's**
`session_id` — not a subagent-specific one. Without a guard, a subagent's own
`Bash`/`Read`/etc. calls would flip the parent session's slot from
`DISPATCHED` back to `WORKING` every time, because they arrive as ordinary
`pre_tool_use` events against the parent's session_id.

The fix: `agent_id` is present *only* when a hook fires inside a subagent
call. `state_manager.handle_event()` ignores any event carrying an `agent_id`
unless that event is itself `subagent_start`/`subagent_stop` — subagent-
internal tool calls are noise as far as either slot (the parent's or the
subagent's own) is concerned; the subagent's slot is driven exclusively by
its own start/stop events, never by its internal tool use.

**Don't** remove this guard "to simplify" — `DISPATCHED` cannot hold for more
than an instant without it, on any session whose dispatched subagent makes
more than zero tool calls (i.e. essentially all of them).

## Solid vs. blinking green tells sessions and subagents apart

A working session and a running subagent share the same green — no separate
colour was spent on it. The distinction is solid (session, `WORKING`) vs.
blinking (subagent, `RUNNING`), a deliberate choice so a glance answers both
"how many are active" (green count) and "which of those are subagents"
(which ones blink), without adding a colour that then has to be told apart
from the other four under normal desk lighting.

This is why `RUNNING` is a distinct *state* name from `WORKING` even though
they render the same colour — the firmware's per-state `{colour, blinks}`
table needs two entries to blink one and not the other; a single `WORKING`
state with a side-channel "role" flag would work too, but the state-name
split keeps the wire protocol and `state_manager.py` boring (a state fully
determines its rendering, no second field to keep in sync).

## The alarm moved from the turn-ended state to the blocked state

v1's alarm was tied to `NEED_INPUT`, fired on `Stop` (the session's turn
ending — "done, waiting for whatever you send next"). v2 splits that
former single state into two: `IDLE` (turn ended, calm, no urgency) and
`BLOCKED` (a permission prompt — genuinely can't proceed without a decision
from you). The alarm now attaches only to `BLOCKED`.

**Don't** attach the alarm to `IDLE` — it's deliberately the calm state, and
alarming on it would make every ordinary end-of-turn as loud as a stuck
permission prompt.

## A finished subagent's slot releases immediately, with no lingering colour

An earlier draft of this design had subagents linger in a `DONE` (red-blink)
state for a grace period before auto-releasing, modeled on the uncertainty
around whether `SubagentStop` would fire reliably/exactly once. Once
`SubagentStop` was confirmed as a real, correlated event (see above), the
grace period became unnecessary complexity: the subagent's slot now goes
straight to `OFF` the moment `SubagentStop` arrives. There is no `DONE` state
in the v2 vocabulary at all.

**Don't** reintroduce a lingering post-completion state for subagents "to make
it easier to see what just finished" — that was explicitly not wanted: a
subagent should "do its work and close," full stop.
