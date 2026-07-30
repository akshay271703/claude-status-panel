// Diagnostic: blinks the board's own built-in LED (pin 13), no external
// wiring or peripherals involved at all. Used to confirm the chip is
// actually executing sketches right now, independent of Serial/I2C/OLED.

void setup() {
  pinMode(LED_BUILTIN, OUTPUT);
}

void loop() {
  digitalWrite(LED_BUILTIN, HIGH);
  delay(200);
  digitalWrite(LED_BUILTIN, LOW);
  delay(200);
}
