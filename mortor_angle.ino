#include <DHT.h>
#include <Servo.h>

#define DHTPIN 2
#define DHTTYPE DHT11  

DHT dht(DHTPIN, DHTTYPE);
Servo servo;

int readyAngle = 0;     // 버튼을 누르지 않는 위치
int pushAngle = 50;     // 실제 버튼이 눌리는 각도
int pushTime = 500;     // 버튼을 누르고 있는 시간

void setup() {
  Serial.begin(9600);

  dht.begin();

  servo.attach(9);
  servo.write(readyAngle);

  Serial.println("시스템 시작");
  Serial.println("PUSH 입력 시 서보모터가 버튼을 누릅니다.");
}

void loop() {
  float humidity = dht.readHumidity();
  float temperature = dht.readTemperature();

  if (!isnan(humidity) && !isnan(temperature)) {
    Serial.print("온도: ");
    Serial.print(temperature);
    Serial.print(" C, 습도: ");
    Serial.print(humidity);
    Serial.println(" %");
  } else {
    Serial.println("센서 값을 읽을 수 없습니다.");
  }

  if (Serial.available() > 0) {
    String command = Serial.readStringUntil('\n');
    command.trim();

    if (command == "PUSH") {
      Serial.println("버튼 누르기 동작 실행");

      servo.write(pushAngle);
      delay(pushTime);

      servo.write(readyAngle);
      delay(500);

      Serial.println("버튼 누르기 완료");
    }
  }

  delay(1000);
}