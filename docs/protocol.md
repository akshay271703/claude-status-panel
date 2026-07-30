# Serial Protocol Reference

The contract between the Python bridge and the Arduino firmware. If you are
changing either side, this is the document that must stay true.

- **Transport:** USB serial, **9600 baud**, 8N1
- **Framing:** one command per line, terminated `\n` (a leading `\r` is ignored)
- **Direction:** the host sends commands; the board answers one line per command
- **Encoding:** ASCII

Commands are case-sensitive. The board never sends unsolicited output.

## Commands

### `<module>:<STATE>` — set a module's colour

```
2:WORKING
```

`<module>` is `1`, `2` or `3`. `<STATE>` is one of:

| State | Colour | Channels driven |
|---|---|---|
| `THINKING` | Magenta | R + B |
| `WORKING` | Green | G |
| `IDLE` | Red | R |
| `NEED_INPUT` | Blue | B |
| `OFF` | Dark | none |

Answers `OK 2:WORKING`.

Setting a module to the state it is already in is harmless and explicitly
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
STATUS up=15897 dim=5 stale=0 buzz=0 ram=1561 m1=OFF m2=NEED_INPUT m3=OFF
```

| Field | Meaning |
|---|---|
| `up` | Board uptime, milliseconds since reset |
| `dim` | LED brightness, PWM duty percent (0–100) |
| `stale` | `1` if host contact has timed out, else `0` |
| `buzz` | `1` if the buzzer is currently alarming, else `0` |
| `ram` | Free SRAM in bytes |
| `m1`,`m2`,`m3` | Each module's state **as the firmware believes it** |

The `m1`–`m3` fields exist so the host can compare the board's view against
what it believes it sent. A mismatch means a command was lost or rejected —
the only way to detect that, since the host does not otherwise read replies.

Fields may be added in future. Parsers must ignore unknown keys rather than
fail.

### `DIM:<0-100>` — set LED brightness

```
DIM:5
```

PWM duty percentage, clamped to 0–100. Answers `OK DIM:5`.

**Not persisted** — reverts to the compiled default (5) on reset, and opening
the serial port resets the board. If you need a specific brightness, send it
after connecting.

Perceived brightness is roughly the 1/2.2 power of duty, so 30% duty looks
about 58% as bright, not 30%. Low values are less dramatic than they sound.

## Responses

| Response | Meaning |
|---|---|
| `OK <line>` | Command accepted and applied |
| `ERR: <line>` | Command rejected; **no state changed** |
| `ERR: <line too long>` | Input exceeded 32 characters; whole line discarded |
| `STATUS ...` | Reply to `STATUS` (note: not prefixed `OK`) |

Rejection causes: unknown module number, unrecognised state, missing colon,
unknown verb, oversized line.

The bridge does not read `OK`/`ERR:` replies during normal operation — it
drains them to stop them accumulating in the host's input buffer. It reads
only when it issues `STATUS`. This is deliberate; see
[decisions.md](decisions.md).

## Behaviour rules

These are the parts most easily broken by a well-meaning change.

### Idempotence

Re-sending a module's current state does nothing beyond repainting it. In
particular, re-sending `NEED_INPUT` to a module already in `NEED_INPUT` does
**not** restart its alarm countdown, and does not un-silence it.

Only a transition *into* `NEED_INPUT` from another state starts the countdown.
This is what lets the host resend state freely without resetting timers.

### The alarm

A module that stays in `NEED_INPUT` for `RED_TIMEOUT_MS` (5000 ms) without
being silenced starts the buzzer chirping — 150 ms every 3 s.

The chirp is phased against absolute uptime rather than when the alarm began,
so the first chirp can lag the timeout by up to one full period. Acceptable for
a human-attention signal.

### The silence button

A press silences every module *currently alarming at that moment* — in
`NEED_INPUT`, past the timeout, not already silenced. Modules still inside
their timeout are unaffected.

A silenced module re-arms only when it leaves `NEED_INPUT` and enters it again.
A press is ignored entirely while contact is stale, since the user cannot see
or hear what they would be silencing.

### Staleness

The board tracks the time of the last **successfully parsed** line — any
command, not just `PING`. Malformed input does not count, because garbage on
the wire is not evidence of a healthy sender.

If `BRIDGE_TIMEOUT_MS` (10 000 ms) passes without contact:

- all three modules go dark
- the D13 heartbeat pulse stops
- the buzzer is silenced

Crucially this suppresses **output only**. Module states, alarm timestamps and
silence flags are all retained, so when contact resumes the board repaints
itself from what it already knows. The host does not need to resend anything,
and a still-overdue module resumes chirping.

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
1:THINKING            -> OK 1:THINKING
STATUS                -> STATUS up=... dim=5 stale=0 ...
DIM:40                -> OK DIM:40
nonsense              -> ERR: nonsense
```

Remember that opening the port resets the board on most USB-serial adapters,
so it will start dark, stale, and at default brightness.
