// Diagnostic: scans the I2C bus and reports every address that responds.
// Used to check whether the OLED module is actually reachable on the bus
// before debugging the display library/address further.

#include <Wire.h>

void setup() {
  Wire.begin();
  Serial.begin(9600);
  while (!Serial) {}
}

void loop() {
  Serial.println("Scanning I2C bus...");
  int found = 0;

  for (byte address = 1; address < 127; address++) {
    Wire.beginTransmission(address);
    byte error = Wire.endTransmission();

    if (error == 0) {
      Serial.print("Device found at address 0x");
      if (address < 16) Serial.print("0");
      Serial.println(address, HEX);
      found++;
    }
  }

  if (found == 0) {
    Serial.println("No I2C devices found.");
  } else {
    Serial.print(found);
    Serial.println(" device(s) found.");
  }

  Serial.println();
  delay(3000);
}
