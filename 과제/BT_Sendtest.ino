#include <SoftwareSerial.h>

SoftwareSerial BTSerial(3, 4);

int cnt;

void setup() {
  Serial.begin(9600);
  BTSerial.begin(9600);
  cnt = 0;
}

void loop() {
  Serial.write("Receive : ");
  while (BTSerial.available()) {
    Serial.write(BTSerial.read());
  }
  Serial.println();

  BTSerial.println(cnt);
  Serial.print("Send : ");
  Serial.println(cnt);
  cnt++;
  delay(5000);
}
