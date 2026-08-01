// status_ring/status_ring.ino
//
// Drives a 16-LED WS2812B/NeoPixel ring + a shared buzzer/silence button
// from a single serial protocol -- the v2 successor to status_panel.ino,
// which drove 3 discrete RGB modules. See docs/protocol.md and
// docs/hardware.md. status_panel.ino is kept, untouched, as a legacy
// reference; this is a new sketch, not a patch of the old one.
//
// Serial protocol (9600 baud, line-based text):
//   <slot>:<STATE>\n
//   slot:  1-16
//   STATE: WORKING | DISPATCHED | BLOCKED | IDLE | RUNNING | OFF
//   Response: "OK <line>" on success, "ERR: <line>" on malformed input
//   (unknown slot number, unrecognized state, bad format, or an
//   overlong line). A slot with no command received yet is OFF.
//
//   PING\n
//   No slot prefix -- concerns the device, not a slot. Refreshes the
//   bridge-contact timestamp and answers "OK PING".
//
//   DIM:<0-100>\n
//   Sets ring brightness as a percentage of full (0-255), clamped to
//   0-100, answering "OK DIM:<n>". Native to Adafruit_NeoPixel's
//   setBrightness() -- no software-PWM duty-cycle trick needed here,
//   unlike status_panel.ino.
//
// One LED slot maps to one Claude Code session OR one of its subagents --
// the bridge treats both as claimants on the same 16-slot pool. Two roles
// share the green channel, distinguished by blink, not colour: a working
// main session is solid green, a running subagent blinks green. This is
// the "role" signal so a glance tells "how many are active" (green count)
// apart from "which of those are subagents" (which ones blink).
//
// State/colour/blink table:
//   WORKING     green,  solid    -- a session composing or using a tool
//   DISPATCHED  purple, solid    -- a session waiting on subagents it dispatched
//   BLOCKED     blue,   blinking -- a session needs a decision from you now
//   IDLE        red,    blinking -- a session's turn ended, idle, no urgency
//   RUNNING     green,  blinking -- a subagent currently running
//   OFF         dark             -- unclaimed
//
// Buzzer rule: only BLOCKED alarms (not IDLE) -- it's the one state that
// truly can't proceed without you. Same shape as status_panel.ino's old
// NEED_INPUT alarm: a slot stuck in BLOCKED for RED_TIMEOUT_MS unsilenced
// starts a chirp, silenced by the button, re-armed only by a fresh
// transition into BLOCKED.
//
// Blink timing: one global phase computed each loop() against absolute
// millis() (same pattern as status_panel.ino's buzzer chirp/heartbeat),
// applied uniformly to every slot currently BLOCKED/IDLE/RUNNING. Unlike
// the old per-pin software PWM, strip.show() always retransmits the
// entire strip regardless of how many pixels changed, so the equivalent
// of the old pwmDirty optimization is a single ringDirty bool: show() is
// called only when a slot's state changed this loop, or the blink phase
// just flipped while at least one slot is blinking. This bounds show()
// to a few Hz instead of every iteration.
//
// show() briefly disables interrupts (~16 LEDs * 24 bits * 1.25us =~
// 480us). At 9600 baud (~1.04ms/byte) this could in the worst case drop
// one in-flight serial byte; since show() only runs a few times a second
// per the dirty-flag gating above, exposure is small. Verified at
// bring-up by driving all 16 slots into alternating blink states and
// checking for ERR: replies -- see docs/hardware.md.
//
// Bridge-staleness rule: lastContactMs is refreshed by any successfully
// parsed line (PING or a state command). Once BRIDGE_TIMEOUT_MS passes
// without contact, the ring goes dark and D13 goes dark, and the buzzer
// is silenced -- but slots[].state (and each slot's blockedSince and
// silenced flags) is left untouched, so the panel repaints itself from
// retained state the moment contact resumes.

#include <Adafruit_NeoPixel.h>

enum State { OFF, WORKING, DISPATCHED, BLOCKED, IDLE, RUNNING };

const int NUM_SLOTS = 16;
const int RING_PIN = 6;

Adafruit_NeoPixel ring(NUM_SLOTS, RING_PIN, NEO_GRB + NEO_KHZ800);

struct Slot {
  State state;
  unsigned long blockedSince;
  bool silenced;
};

Slot slots[NUM_SLOTS];

// --- Config constants ---
const int MAX_LINE_LEN = 32;

const int BUZZER_PIN = 11;
const unsigned long RED_TIMEOUT_MS = 5000;
const unsigned long CHIRP_ON_MS = 150;
const unsigned long CHIRP_PERIOD_MS = 3000;
const int BUZZER_FREQ = 1500;

const int BUTTON_PIN = 12;
const unsigned long DEBOUNCE_MS = 50;

const int HEARTBEAT_PIN = 13;          // Uno built-in LED; no external wiring
const unsigned long BRIDGE_TIMEOUT_MS = 10000;
const unsigned long HEARTBEAT_ON_MS = 80;
const unsigned long HEARTBEAT_PERIOD_MS = 2000;

const unsigned long BLINK_PERIOD_MS = 900;
const unsigned long BLINK_ON_MS = 450;

// Runtime-adjustable so brightness can be dialled in over serial (DIM:<0-100>)
// without a re-flash.
int ledBrightnessPct = 5;

// Set whenever a slot's state changes, or the blink phase flips while any
// slot is currently blinking. handleRingOutput() is the only place that
// calls ring.show(), and only when this is true -- see the header comment
// on why that matters at 9600 baud.
bool ringDirty = true;
bool lastBlinkOn = false;

// Refreshed by any successfully parsed line: PING during quiet periods, and
// ordinary state commands the rest of the time. Initialised in setup() to
// already be stale, so a board powered with no bridge attached never shows
// a false "healthy" heartbeat during the startup window.
unsigned long lastContactMs = 0;
bool bridgeStale = false;

// Whether the buzzer is currently sounding an alarm, so STATUS can report it.
bool buzzerActive = false;

void noteContact() {
  lastContactMs = millis();
}

bool isBridgeStale() {
  return (millis() - lastContactMs) > BRIDGE_TIMEOUT_MS;
}

bool stateBlinks(State s) {
  return s == BLOCKED || s == IDLE || s == RUNNING;
}

uint32_t stateColor(State s) {
  switch (s) {
    case WORKING:    return ring.Color(0, 255, 0);      // green
    case DISPATCHED: return ring.Color(160, 0, 200);    // purple
    case BLOCKED:    return ring.Color(0, 0, 255);       // blue
    case IDLE:       return ring.Color(255, 0, 0);       // red
    case RUNNING:    return ring.Color(0, 255, 0);       // green
    case OFF:        return 0;
  }
  return 0;
}

void applyState(int index, State newState) {
  Slot &s = slots[index];
  if (newState == BLOCKED && s.state != BLOCKED) {
    s.blockedSince = millis();
    s.silenced = false;
  }
  s.state = newState;
  ringDirty = true;
}

void handleRingOutput() {
  bool blinkOn = (millis() % BLINK_PERIOD_MS) < BLINK_ON_MS;
  if (!ringDirty && blinkOn == lastBlinkOn) {
    return;  // nothing changed; leave the ring alone and keep loop() fast
  }
  lastBlinkOn = blinkOn;
  ringDirty = false;

  for (int i = 0; i < NUM_SLOTS; i++) {
    State st = slots[i].state;
    bool visible = !stateBlinks(st) || blinkOn;
    ring.setPixelColor(i, visible ? stateColor(st) : 0);
  }
  ring.show();
}

String lineBuffer = "";

const char *stateName(State s) {
  switch (s) {
    case WORKING:    return "WORKING";
    case DISPATCHED: return "DISPATCHED";
    case BLOCKED:    return "BLOCKED";
    case IDLE:       return "IDLE";
    case RUNNING:    return "RUNNING";
    default:         return "OFF";
  }
}

char stateCode(State s) {
  switch (s) {
    case WORKING:    return 'W';
    case DISPATCHED: return 'D';
    case BLOCKED:    return 'B';
    case IDLE:       return 'I';
    case RUNNING:    return 'R';
    default:         return 'O';
  }
}

void setBrightness(int pct) {
  if (pct < 0) pct = 0;
  if (pct > 100) pct = 100;
  ledBrightnessPct = pct;
  ring.setBrightness((uint8_t)(255L * pct / 100));
  ringDirty = true;
}

void processLine(String line) {
  if (line == "PING") {
    noteContact();
    Serial.println("OK PING");
    return;
  }

  if (line == "STATUS") {
    noteContact();
    printStatus();
    return;
  }

  if (line.startsWith("DIM:")) {
    setBrightness(line.substring(4).toInt());
    noteContact();
    Serial.print("OK ");
    Serial.println(line);
    return;
  }

  int colonIndex = line.indexOf(':');
  if (colonIndex == -1) {
    Serial.print("ERR: ");
    Serial.println(line);
    return;
  }

  String slotStr = line.substring(0, colonIndex);
  String stateStr = line.substring(colonIndex + 1);
  int slotNum = slotStr.toInt();

  if (slotNum < 1 || slotNum > NUM_SLOTS) {
    Serial.print("ERR: ");
    Serial.println(line);
    return;
  }

  State newState;
  if (stateStr == "WORKING") newState = WORKING;
  else if (stateStr == "DISPATCHED") newState = DISPATCHED;
  else if (stateStr == "BLOCKED") newState = BLOCKED;
  else if (stateStr == "IDLE") newState = IDLE;
  else if (stateStr == "RUNNING") newState = RUNNING;
  else if (stateStr == "OFF") newState = OFF;
  else {
    Serial.print("ERR: ");
    Serial.println(line);
    return;
  }

  noteContact();
  applyState(slotNum - 1, newState);
  Serial.print("OK ");
  Serial.println(line);
}

int freeRam() {
  extern int __heap_start, *__brkval;
  int here;
  return (int) &here - (__brkval == 0 ? (int) &__heap_start : (int) __brkval);
}

void printStatus() {
  // Printed field-by-field rather than built into a String: this is polled
  // every couple of seconds and heap churn on a 2KB part is worth avoiding.
  Serial.print("STATUS up=");
  Serial.print(millis());
  Serial.print(" dim=");
  Serial.print(ledBrightnessPct);
  Serial.print(" stale=");
  Serial.print(bridgeStale ? 1 : 0);
  Serial.print(" buzz=");
  Serial.print(buzzerActive ? 1 : 0);
  Serial.print(" ram=");
  Serial.print(freeRam());
  Serial.print(" ver=2 ring=");
  for (int i = 0; i < NUM_SLOTS; i++) {
    Serial.print(stateCode(slots[i].state));
  }
  Serial.println();
}

bool lineOverflow = false;

void handleSerial() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n') {
      if (lineOverflow) {
        Serial.println("ERR: <line too long>");
      } else {
        processLine(lineBuffer);
      }
      lineBuffer = "";
      lineOverflow = false;
    } else if (c != '\r') {
      if (lineBuffer.length() < MAX_LINE_LEN) {
        lineBuffer += c;
      } else {
        // Overlong line: stop accumulating and latch overflow so the
        // whole line (including its tail) is discarded at the next
        // newline, instead of letting the tail be parsed as a new command.
        lineOverflow = true;
      }
    }
  }
}

bool isSlotAlarming(int index) {
  Slot &s = slots[index];
  if (s.state != BLOCKED) return false;
  if (s.silenced) return false;
  return (millis() - s.blockedSince) >= RED_TIMEOUT_MS;
}

void handleBuzzer() {
  if (bridgeStale) {
    buzzerActive = false;
    noTone(BUZZER_PIN);
    return;
  }

  bool anyAlarming = false;
  for (int i = 0; i < NUM_SLOTS; i++) {
    // There is only one physical buzzer, so all alarming slots share a
    // single global chirp phase -- the first alarming slot found is all
    // we need; we don't layer or distinguish multiple simultaneous alarms.
    if (isSlotAlarming(i)) { anyAlarming = true; break; }
  }

  buzzerActive = anyAlarming;

  if (!anyAlarming) {
    noTone(BUZZER_PIN);
    return;
  }

  // Phased against absolute uptime (millis()), not against when this
  // particular alarm began, so the first chirp after crossing the
  // timeout can lag by up to one CHIRP_PERIOD_MS (~3s). Fine for a
  // human-attention alert; a deliberate simplicity tradeoff.
  if ((millis() % CHIRP_PERIOD_MS) < CHIRP_ON_MS) {
    tone(BUZZER_PIN, BUZZER_FREQ);
  } else {
    noTone(BUZZER_PIN);
  }
}

int lastButtonReading = HIGH;
int buttonState = HIGH;
unsigned long lastDebounceTime = 0;

void handleButton() {
  int reading = digitalRead(BUTTON_PIN);

  if (reading != lastButtonReading) {
    lastDebounceTime = millis();
  }

  if ((millis() - lastDebounceTime) > DEBOUNCE_MS) {
    if (reading != buttonState) {
      buttonState = reading;
      if (buttonState == LOW && !bridgeStale) { // pressed (INPUT_PULLUP: LOW = pressed)
        for (int i = 0; i < NUM_SLOTS; i++) {
          if (isSlotAlarming(i)) {
            slots[i].silenced = true;
          }
        }
      }
    }
  }

  lastButtonReading = reading;
}

void handleHeartbeat() {
  bool stale = isBridgeStale();

  if (stale != bridgeStale) {
    bridgeStale = stale;
    if (stale) {
      // Suppress output only. slots[].state, blockedSince and silenced are
      // all left intact, so recovery can repaint from what we already know
      // without the bridge having to resend anything.
      ring.clear();
      ring.show();
      digitalWrite(HEARTBEAT_PIN, LOW);
    } else {
      ringDirty = true;
    }
  }

  if (stale) {
    return;
  }

  // Phased against absolute uptime, same approach as the buzzer chirp.
  bool on = (millis() % HEARTBEAT_PERIOD_MS) < HEARTBEAT_ON_MS;
  digitalWrite(HEARTBEAT_PIN, on ? HIGH : LOW);
}

void setup() {
  ring.begin();
  setBrightness(ledBrightnessPct);
  ring.clear();
  ring.show();

  pinMode(BUZZER_PIN, OUTPUT);
  pinMode(BUTTON_PIN, INPUT_PULLUP);
  pinMode(HEARTBEAT_PIN, OUTPUT);
  Serial.begin(9600);

  // Start already stale: without this, lastContactMs == 0 and an early
  // millis() would read as "fresh" for the first BRIDGE_TIMEOUT_MS, showing
  // a false-healthy heartbeat on a board with no bridge attached. Safe under
  // unsigned modular arithmetic; D13 is already LOW from pinMode above.
  lastContactMs = millis() - BRIDGE_TIMEOUT_MS - 1;
  bridgeStale = true;
}

void loop() {
  handleSerial();
  handleHeartbeat();
  handleBuzzer();
  handleButton();
  handleRingOutput();  // must run every iteration: it is the LED output stage
}
