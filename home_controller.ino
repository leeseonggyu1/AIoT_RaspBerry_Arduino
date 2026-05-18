#include <DHT.h>
#include <Servo.h>

#define DHTPIN 2
#define DHTTYPE DHT11

#define AIRCON_SERVO_PIN 9
#define HUMIDIFIER_RELAY_PIN 7

// 릴레이 모듈이 LOW일 때 켜지는 타입이면 true, HIGH일 때 켜지는 타입이면 false
const bool RELAY_ACTIVE_LOW = true;

DHT dht(DHTPIN, DHTTYPE);
Servo airconServo;

int readyAngle = 0;   // 버튼을 누르지 않는 위치
int pushAngle = 50;   // 버튼이 눌리는 각도
int pushTime = 500;   // 버튼을 누르고 있는 시간(ms)

bool humidifierOn = false;
unsigned long lastSensorPrintAt = 0;
const unsigned long SENSOR_PRINT_INTERVAL = 2000;

void writeHumidifierRelay(bool on) {
  humidifierOn = on;

  if (RELAY_ACTIVE_LOW) {
    digitalWrite(HUMIDIFIER_RELAY_PIN, on ? LOW : HIGH);
  } else {
    digitalWrite(HUMIDIFIER_RELAY_PIN, on ? HIGH : LOW);
  }
}

void pressAirconButton() {
  Serial.println("에어컨 버튼 누르기 동작 실행");

  airconServo.write(pushAngle);
  delay(pushTime);

  airconServo.write(readyAngle);
  delay(500);

  Serial.println("에어컨 버튼 누르기 완료");
}

void printStatus() {
  float humidity = dht.readHumidity();
  float temperature = dht.readTemperature();

  if (!isnan(humidity) && !isnan(temperature)) {
    Serial.print("온도: ");
    Serial.print(temperature);
    Serial.print(" C, 습도: ");
    Serial.print(humidity);
    Serial.print(" %, 가습기: ");
    Serial.println(humidifierOn ? "ON" : "OFF");
  } else {
    Serial.println("센서 값을 읽을 수 없습니다.");
  }
}

void handleCommand(String command) {
  command.trim();

  if (command == "PUSH" || command == "AIRCON_TOGGLE") {
    pressAirconButton();
  } else if (command == "HUMIDIFIER_ON") {
    writeHumidifierRelay(true);
    Serial.println("가습기: ON");
  } else if (command == "HUMIDIFIER_OFF") {
    writeHumidifierRelay(false);
    Serial.println("가습기: OFF");
  } else if (command == "STATUS") {
    printStatus();
  } else if (command.length() > 0) {
    Serial.print("알 수 없는 명령: ");
    Serial.println(command);
  }
}

void setup() {
  Serial.begin(9600);

  dht.begin();

  pinMode(HUMIDIFIER_RELAY_PIN, OUTPUT);
  writeHumidifierRelay(false);

  airconServo.attach(AIRCON_SERVO_PIN);
  airconServo.write(readyAngle);

  Serial.println("집안 원격 제어 시스템 시작");
  Serial.println("명령: AIRCON_TOGGLE, HUMIDIFIER_ON, HUMIDIFIER_OFF, STATUS");
}

void loop() {
  if (Serial.available() > 0) {
    String command = Serial.readStringUntil('\n');
    handleCommand(command);
  }

  if (millis() - lastSensorPrintAt >= SENSOR_PRINT_INTERVAL) {
    lastSensorPrintAt = millis();
    printStatus();
  }
}
