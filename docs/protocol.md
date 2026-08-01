# Serial Protocol Reference

The contract between the Python bridge and the Arduino firmware. If you are
changing either side, this is the document that must stay true.

This describes **v2**, spoken by `status_ring/status_ring.ino` (the 16-LED
WS2812B ring). The original 3-module board, `status_panel/status_panel.ino`,
speaks a different, older protocol — kept as a legacy reference, not deleted
— documented in [Legacy: v1](#legacy-v1-status_panelino-3-module-board) at
the bottom of this page.

- **Transport:** USB serial, **9600 baud**, 8N1
- **Framing:** one command per line, terminated `\n` (a leading `\r` is ignored)
- **Direction:** the host sends commands; the board answers one line per command
- **Encoding:** ASCII

Commands are case-sensitive. The board never sends unsolicited output.

## Commands

### `<slot>:<STATE>` — set a slot's colour

```
2:WORKING
```

`<slot>` is `1`-`16`. `<STATE>` is one of:

| State | Colour | Blinks? | Meaning |
|---|---|---|---|
| `WORKING` | Green | No | A session actively working (composing, or using a tool directly) |
| `DISPATCHED` | Purple | No | A session waiting on subagents it dispatched |
| `BLOCKED` | Blue | Yes | A session needs a decision from you right now — the only state that alarms |
| `IDLE` | Red | Yes | A session's turn ended, idle, no urgency |
| `RUNNING` | Green | Yes | A subagent currently running |
| `OFF` | Dark | — | Slot unclaimed |

`WORKING` and `RUNNING` share a colour on purpose: solid vs. blinking is the
*role* signal (session vs. subagent), not the colour — see
[decisions.md](decisions.md#solid-vs-blinking-green-tells-sessions-and-subagents-apart).

Answers `OK 2:WORKING`.

Setting a slot to the state it is already in is harmless and explicitly
supported — see [Idempotence](#idempotence).

### `PING` — prove the host is alive

```
PING
```

Answers `OK PING`. Carries no other meaning; it exists so the board can tell
the difference between "the host has nothing to say" and "the host is gone".
See [Staleness](#staleness).

### `STATUS` — read the board's own view of itself

```
STATUS
```

Answers a single line:

```
STATUS up=15897 dim=5 stale=0 buzz=0 ram=1561 ver=2 ring=WWDBIROOOOOOOOOO
```

| Field | Meaning |
|---|---|
| `up` | Board uptime, milliseconds since reset |
| `dim` | LED brightness, percent (0–100) |
| `stale` | `1` if host contact has timed out, else `0` |
| `buzz` | `1` if the buzzer is currently alarming, else `0` |
| `ram` | Free SRAM in bytes |
| `ver` | Protocol version (`2`) — lets the bridge detect a v1 board plugged in by mistake and refuse to talk to it, rather than silently misreading it |
| `ring` | One character per slot, 1–16 in order: `W`/`D`/`B`/`I`/`R`/`O` for `WORKING`/`DISPATCHED`/`BLOCKED`/`IDLE`/`RUNNING`/`OFF` |

Sixteen individual `mN=` tokens (v1's approach) would run to ~130 characters;
the packed `ring` field says the same thing in 16. The bridge decodes it back
into full state names before it reaches the dashboard — the packed form is a
wire-only concern.

Fields may be added in future. Parsers must ignore unknown keys rather than
fail.

### `DIM:<0-100>` — set LED brightness

```
DIM:5
```

Percentage of full brightness, clamped to 0–100, applied via
`Adafruit_NeoPixel::setBrightness()`. Answers `OK DIM:5`.

**Not persisted** — reverts to the compiled default (5) on reset, and opening
the serial port resets the board. If you need a specific brightness, send it
after connecting.

## Responses

| Response | Meaning |
|---|---|
| `OK <line>` | Command accepted and applied |
| `ERR: <line>` | Command rejected; **no state changed** |
| `ERR: <line too long>` | Input exceeded 32 characters; whole line discarded |
| `STATUS ...` | Reply to `STATUS` (note: not prefixed `OK`) |

Rejection causes: unknown slot number, unrecognised state, missing colon,
unknown verb, oversized line.

The bridge does not read `OK`/`ERR:` replies during normal operation — it
drains them to stop them accumulating in the host's input buffer. It reads
only when it issues `STATUS`. This is deliberate; see
[decisions.md](decisions.md).

## Behaviour rules

These are the parts most easily broken by a well-meaning change.

### Idempotence

Re-sending a slot's current state does nothing beyond repainting it. In
particular, re-sending `BLOCKED` to a slot already in `BLOCKED` does **not**
restart its alarm countdown, and does not un-silence it.

Only a transition *into* `BLOCKED` from another state starts the countdown.
This is what lets the host resend state freely without resetting timers.

### The alarm

Only `BLOCKED` alarms — it's the one state that truly can't proceed without
you. A slot that stays in `BLOCKED` for `RED_TIMEOUT_MS` (5000 ms) without
being silenced starts the buzzer chirping — 150 ms every 3 s. `IDLE` never
alarms; it's meant to read as calm, not urgent.

The chirp is phased against absolute uptime rather than when the alarm began,
so the first chirp can lag the timeout by up to one full period. Acceptable for
a human-attention signal.

### The silence button

A press silences every slot *currently alarming at that moment* — in
`BLOCKED`, past the timeout, not already silenced. Slots still inside their
timeout are unaffected.

A silenced slot re-arms only when it leaves `BLOCKED` and enters it again.
A press is ignored entirely while contact is stale, since the user cannot see
or hear what they would be silencing.

### Blink timing

One global phase, computed each firmware loop against absolute `millis()`
(same pattern as the buzzer chirp), applied to every slot currently
`BLOCKED`/`IDLE`/`RUNNING` — every blinking slot flashes in sync. Because
`ring.show()` retransmits the *entire* strip regardless of how many pixels
changed, the firmware only calls it when a slot's state changed or the blink
phase just flipped while something is blinking — bounding it to a few Hz
rather than every loop iteration.

### Staleness

The board tracks the time of the last **successfully parsed** line — any
command, not just `PING`. Malformed input does not count, because garbage on
the wire is not evidence of a healthy sender.

If `BRIDGE_TIMEOUT_MS` (10 000 ms) passes without contact:

- the entire ring goes dark
- the D13 heartbeat pulse stops
- the buzzer is silenced

Crucially this suppresses **output only**. Slot states, alarm timestamps and
silence flags are all retained, so when contact resumes the board repaints
itself from what it already knows. The host does not need to resend anything,
and a still-overdue slot resumes chirping.

The board starts in the stale state, so one powered with no host attached shows
nothing rather than a misleading "healthy and idle" heartbeat.

### Line buffer

Input lines are capped at 32 characters. An oversized line is discarded in full
— including the portion after the cap — and answered `ERR: <line too long>`.
The tail is never parsed as a fresh command.

## Talking to the board directly

Any serial terminal works. The bridge holds the port exclusively, so stop it
first.

```
PING                  -> OK PING
1:WORKING             -> OK 1:WORKING
1:DISPATCHED          -> OK 1:DISPATCHED
STATUS                -> STATUS up=... dim=5 stale=0 ... ver=2 ring=W...
DIM:40                -> OK DIM:40
nonsense              -> ERR: nonsense
```

Remember that opening the port resets the board on most USB-serial adapters,
so it will start dark, stale, and at default brightness.

---

## Legacy: v1 (`status_panel.ino`, 3-module board)

Kept as a reference, not deleted — see
[decisions.md](decisions.md#the-panel-is-capped-at-three-modules) for why the
ring replaced it. The full protocol is documented in `status_panel.ino`'s own
header comment. Summary of what differs from v2:

- 3 discrete RGB modules (`<module>` was `1`-`3`), not a 16-LED ring.
- States were `THINKING` (magenta), `WORKING` (green), `IDLE` (red),
  `NEED_INPUT` (blue), `OFF` — mapped 1:1 to hook events, with no concept of
  subagents or a dispatch-vs-working distinction.
- `STATUS` reported three individual `m1=`/`m2=`/`m3=` tokens rather than a
  packed `ring=` field, and had no `ver=` field.
- Brightness was software PWM (only some of the 9 module pins had hardware
  PWM), not `setBrightness()`.
- The alarm was tied to `NEED_INPUT` (the equivalent of today's turn-ended
  state); v2 moves the alarm to `BLOCKED` instead, since that's the state
  that actually can't proceed without you.
