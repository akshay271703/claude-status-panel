// status_panel/status_panel.ino
//
// Drives 3 RGB status modules + a shared buzzer/silence button from a
// single serial protocol. See docs/protocol.md and docs/hardware.md.
//
// Serial protocol (9600 baud, line-based text):
//   <module>:<STATE>\n
//   module: 1 | 2 | 3
//   STATE:  THINKING | WORKING | IDLE | NEED_INPUT | OFF
//   Response: "OK <line>" on success, "ERR: <line>" on malformed input
//   (unknown module number, unrecognized state, bad format, or an
//   overlong line). A module with no command received yet is OFF.
//
//   PING\n
//   No module prefix -- concerns the device, not a module. Refreshes
//   the bridge-contact timestamp and answers "OK PING". Used by the
//   bridge to prove liveness during otherwise-quiet periods.
//
//   DIM:<0-100>\n
//   Sets module LED brightness as a PWM duty percentage, clamped to
//   0-100, answering "OK DIM:<n>". Device-level like PING, and also
//   refreshes contact. Not persisted -- resets to the compiled default
//   on reboot. Intended for tuning by eye and, later, for an LDR to
//   drive brightness from ambient light.
//
// Pin map: see the modules[] array below for each module's R/G/B pins.
// Shared buzzer is on BUZZER_PIN, the silence button is on BUTTON_PIN,
// and the bridge-heartbeat pulse is on HEARTBEAT_PIN (D13, the Uno's
// built-in LED) -- all defined in the constants block below.
//
// Red-timer rule: only a transition INTO NEED_INPUT (from any other
// state) starts the RED_TIMEOUT_MS countdown; repeated NEED_INPUT
// messages while already in NEED_INPUT do not restart it.
//
// Silence-button rule: a press silences every module that is currently
// alarming (in NEED_INPUT, past its timeout, not yet silenced) at that
// moment; a silenced module re-arms only on its next fresh transition
// into NEED_INPUT.
//
// Brightness rule: module LEDs are dimmed by software PWM, not
// analogWrite(), because only D3/D5/D6/D9/D10 of the nine module pins
// support hardware PWM and mixing the two would leave the modules at
// visibly different brightness. driveLeds() records intent; handleLedPwm()
// is the sole output stage and writes pins only when the duty phase flips
// or a state change dirties them -- writing all nine every iteration made
// loop() too slow to sample a short on-window, which showed as flicker.
//
// Bridge-staleness rule: lastContactMs is refreshed by any successfully
// parsed line (PING or a state command). Once BRIDGE_TIMEOUT_MS passes
// without contact, all modules go dark, D13 goes dark, and the buzzer
// is silenced -- but modules[].state (and each module's needInputSince
// and silenced flags) is left untouched, so the panel repaints itself
// from retained state the moment contact resumes.

enum State { OFF, THINKING, WORKING, IDLE, NEED_INPUT };

struct Module {
  int pinR;
  int pinG;
  int pinB;
  State state;
  unsigned long needInputSince;
  bool silenced;
  // Desired channel output. driveLeds() sets these; handleLedPwm() is what
  // actually drives the pins, gating them by the software-PWM duty cycle.
  bool wantR;
  bool wantG;
  bool wantB;
};

Module modules[3] = {
  {2, 3, 4, OFF, 0, false, false, false, false},
  {5, 6, 7, OFF, 0, false, false, false, false},
  {8, 9, 10, OFF, 0, false, false, false, false}
};

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

// Module LEDs are dimmed by software PWM rather than analogWrite(), because
// only D3/D5/D6/D9/D10 of the nine module pins support hardware PWM -- using
// it would leave the three modules at visibly different brightness. Gating
// every channel on a shared duty cycle dims them uniformly.
// 30 = run at 30% duty, i.e. a 70% reduction. Swap this constant for an
// analogRead() of an LDR to make it ambient-adaptive.
const unsigned long PWM_PERIOD_US = 4000;  // 250Hz, above visible flicker

// Runtime-adjustable so brightness can be dialled in over serial (DIM:<0-100>)
// without a re-flash, and so an LDR reading can drive it later.
int ledBrightnessPct = 5;
unsigned long pwmOnUs = PWM_PERIOD_US * 5 / 100;

// handleLedPwm only touches the pins when something actually changes -- either
// the duty phase flipped or a state change dirtied the desired output. Writing
// all nine pins every iteration cost ~45us, which made loop() too slow to
// sample a short on-window cleanly and showed up as flicker at low brightness.
bool lastPwmOn = false;
bool pwmDirty = true;

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

void driveLeds(int index, State state) {
  Module &m = modules[index];
  bool r = false, g = false, b = false;

  switch (state) {
    case THINKING:   r = true; b = true; break;
    case WORKING:    g = true; break;
    case IDLE:       r = true; break;
    case NEED_INPUT: b = true; break;
    case OFF:        break;
  }

  // Record intent only. handleLedPwm() drives the pins, so brightness is
  // applied uniformly and this function stays the single place that maps
  // a State to its colour channels.
  m.wantR = r;
  m.wantG = g;
  m.wantB = b;
  pwmDirty = true;
}

void handleLedPwm() {
  bool on = (micros() % PWM_PERIOD_US) < pwmOnUs;
  if (!pwmDirty && on == lastPwmOn) {
    return;  // nothing changed; leave the pins alone and keep loop() fast
  }
  lastPwmOn = on;
  pwmDirty = false;

  for (int i = 0; i < 3; i++) {
    Module &m = modules[i];
    digitalWrite(m.pinR, (on && m.wantR) ? HIGH : LOW);
    digitalWrite(m.pinG, (on && m.wantG) ? HIGH : LOW);
    digitalWrite(m.pinB, (on && m.wantB) ? HIGH : LOW);
  }
}

void setBrightness(int pct) {
  if (pct < 0) pct = 0;
  if (pct > 100) pct = 100;
  ledBrightnessPct = pct;
  pwmOnUs = PWM_PERIOD_US * (unsigned long)pct / 100;
  pwmDirty = true;
}

void applyState(int index, State newState) {
  Module &m = modules[index];
  if (newState == NEED_INPUT && m.state != NEED_INPUT) {
    m.needInputSince = millis();
    m.silenced = false;
  }
  m.state = newState;
  driveLeds(index, newState);
}

String lineBuffer = "";

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

  String moduleStr = line.substring(0, colonIndex);
  String stateStr = line.substring(colonIndex + 1);
  int moduleNum = moduleStr.toInt();

  if (moduleNum < 1 || moduleNum > 3) {
    Serial.print("ERR: ");
    Serial.println(line);
    return;
  }

  State newState;
  if (stateStr == "THINKING") newState = THINKING;
  else if (stateStr == "WORKING") newState = WORKING;
  else if (stateStr == "IDLE") newState = IDLE;
  else if (stateStr == "NEED_INPUT") newState = NEED_INPUT;
  else if (stateStr == "OFF") newState = OFF;
  else {
    Serial.print("ERR: ");
    Serial.println(line);
    return;
  }

  noteContact();
  applyState(moduleNum - 1, newState);
  Serial.print("OK ");
  Serial.println(line);
}

const char *stateName(State s) {
  switch (s) {
    case THINKING:   return "THINKING";
    case WORKING:    return "WORKING";
    case IDLE:       return "IDLE";
    case NEED_INPUT: return "NEED_INPUT";
    default:         return "OFF";
  }
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
  for (int i = 0; i < 3; i++) {
    Serial.print(" m");
    Serial.print(i + 1);
    Serial.print('=');
    Serial.print(stateName(modules[i].state));
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

bool isModuleAlarming(int index) {
  Module &m = modules[index];
  if (m.state != NEED_INPUT) return false;
  if (m.silenced) return false;
  return (millis() - m.needInputSince) >= RED_TIMEOUT_MS;
}

void handleBuzzer() {
  if (bridgeStale) {
    buzzerActive = false;
    noTone(BUZZER_PIN);
    return;
  }

  bool anyAlarming = false;
  for (int i = 0; i < 3; i++) {
    // There is only one physical buzzer, so all alarming modules share a
    // single global chirp phase — the first alarming module found is all
    // we need; we don't layer or distinguish multiple simultaneous alarms.
    if (isModuleAlarming(i)) { anyAlarming = true; break; }
  }

  buzzerActive = anyAlarming;

  if (!anyAlarming) {
    noTone(BUZZER_PIN);
    return;
  }

  // Phased against absolute uptime (millis()), not against when this
  // particular alarm started, so the first chirp after crossing the
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
        for (int i = 0; i < 3; i++) {
          if (isModuleAlarming(i)) {
            modules[i].silenced = true;
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
      // Suppress output only. modules[].state, needInputSince and silenced
      // are all left intact, so recovery can repaint from what we already
      // know without the bridge having to resend anything.
      for (int i = 0; i < 3; i++) {
        driveLeds(i, OFF);
      }
      digitalWrite(HEARTBEAT_PIN, LOW);
    } else {
      for (int i = 0; i < 3; i++) {
        driveLeds(i, modules[i].state);
      }
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
  for (int i = 0; i < 3; i++) {
    pinMode(modules[i].pinR, OUTPUT);
    pinMode(modules[i].pinG, OUTPUT);
    pinMode(modules[i].pinB, OUTPUT);
  }
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
  handleLedPwm();  // must run every iteration: it is the LED output stage
}
