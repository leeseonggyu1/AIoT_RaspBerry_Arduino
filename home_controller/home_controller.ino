#include <DHT.h>
#include <Servo.h>
#include "ir_commands.h"

#define DHTPIN 2
#define DHTTYPE DHT11

#define AIRCON_SERVO_PIN 9
#define HUMIDIFIER_SERVO_PIN 10
#define IR_SEND_PIN 3
#define IR_RECV_PIN 4
#define LIGHT_SENSOR_PIN A0

DHT dht(DHTPIN, DHTTYPE);
Servo airconServo;
Servo humidifierServo;

const int AIRCON_READY_ANGLE = 0;
const int AIRCON_PUSH_ANGLE = 50;
const int AIRCON_PUSH_TIME_MS = 500;

// Adjust these two values after mounting the servo to the humidifier dial.
const int HUMIDIFIER_OFF_ANGLE = 0;
const int HUMIDIFIER_ON_ANGLE = 150;
const int HUMIDIFIER_STEP_DELAY_MS = 12;

bool humidifierOn = false;
int humidifierCurrentAngle = HUMIDIFIER_OFF_ANGLE;

unsigned long lastSensorPrintAt = 0;
const unsigned long SENSOR_PRINT_INTERVAL = 2000;
const unsigned long IR_RECEIVE_TEST_MS = 5000;
const unsigned int IR_CARRIER_FREQUENCY = 38000;
const bool IR_SEND_ACTIVE_LOW = false;
const unsigned int IR_COMMAND_REPEAT_COUNT = 3;
const unsigned int IR_COMMAND_REPEAT_GAP_MS = 90;
unsigned int irTimingScalePercent = 95;
const unsigned int IR_RAW_MAX_EDGES = 240;
const unsigned int IR_RAW_MIN_START_LOW_US = 2500;
const unsigned int IR_RAW_MIN_VALID_EDGES = 20;
const unsigned long IR_RAW_START_TIMEOUT_MS = 10000;
const unsigned long IR_RAW_GAP_US = 30000;
const unsigned long IR_RAW_MAX_CAPTURE_US = 500000;

unsigned int irRawDurations[IR_RAW_MAX_EDGES];
unsigned int irRawCount = 0;
bool irRawOverflow = false;

struct IrReceiveResult {
  unsigned int fallingEdges;
  unsigned long lowSamples;
};

void setIrLed(bool on) {
  digitalWrite(IR_SEND_PIN, IR_SEND_ACTIVE_LOW ? (on ? LOW : HIGH) : (on ? HIGH : LOW));
}

void setupIrCarrierPwm() {
  pinMode(IR_SEND_PIN, OUTPUT);
  setIrLed(false);

#if IR_SEND_PIN == 3
  // D3 is OC2B on Arduino Uno. Timer2 PWM gives a steadier 38 kHz carrier than tone()/noTone().
  TCCR2A = _BV(WGM20) | _BV(WGM21);
  TCCR2B = _BV(WGM22) | _BV(CS21);
  OCR2A = 51;  // 16 MHz / (8 * (51 + 1)) = 38.46 kHz
  OCR2B = 17;  // roughly 33% duty cycle
  TCCR2A &= ~(_BV(COM2B1) | _BV(COM2B0));
#endif
}

void startIrCarrier() {
#if IR_SEND_PIN == 3
  TCNT2 = 0;
  TCCR2A &= ~(_BV(COM2B1) | _BV(COM2B0));
  TCCR2A |= IR_SEND_ACTIVE_LOW ? (_BV(COM2B1) | _BV(COM2B0)) : _BV(COM2B1);
#else
  tone(IR_SEND_PIN, IR_CARRIER_FREQUENCY);
#endif
}

void stopIrCarrier() {
#if IR_SEND_PIN == 3
  TCCR2A &= ~(_BV(COM2B1) | _BV(COM2B0));
#else
  noTone(IR_SEND_PIN);
#endif
  setIrLed(false);
}

unsigned int scaledIrDuration(unsigned int duration) {
  unsigned long scaled = (unsigned long)duration * irTimingScalePercent / 100;

  if (scaled < 40) {
    return 40;
  }
  if (scaled > 65535) {
    return 65535;
  }

  return (unsigned int)scaled;
}

void moveServoSmooth(Servo &servo, int fromAngle, int toAngle) {
  fromAngle = constrain(fromAngle, 0, 180);
  toAngle = constrain(toAngle, 0, 180);

  if (fromAngle == toAngle) {
    servo.write(toAngle);
    delay(200);
    return;
  }

  int step = fromAngle < toAngle ? 1 : -1;
  for (int angle = fromAngle; angle != toAngle; angle += step) {
    servo.write(angle);
    delay(HUMIDIFIER_STEP_DELAY_MS);
  }
  servo.write(toAngle);
  delay(300);
}

void pressAirconButton() {
  Serial.println(F("에어컨 버튼 누르기 시작"));

  airconServo.write(AIRCON_PUSH_ANGLE);
  delay(AIRCON_PUSH_TIME_MS);

  airconServo.write(AIRCON_READY_ANGLE);
  delay(500);

  Serial.println(F("에어컨 버튼 누르기 완료"));
}

void setHumidifierDial(bool on) {
  int targetAngle = on ? HUMIDIFIER_ON_ANGLE : HUMIDIFIER_OFF_ANGLE;

  moveServoSmooth(humidifierServo, humidifierCurrentAngle, targetAngle);
  humidifierCurrentAngle = targetAngle;
  humidifierOn = on;

  Serial.print(F("가습기: "));
  Serial.println(humidifierOn ? F("ON") : F("OFF"));
}

IrReceiveResult listenForIr(unsigned long durationMs) {
  IrReceiveResult result = {0, 0};
  int previousLevel = digitalRead(IR_RECV_PIN);
  unsigned long startAt = millis();

  while (millis() - startAt < durationMs) {
    int currentLevel = digitalRead(IR_RECV_PIN);
    if (previousLevel == HIGH && currentLevel == LOW) {
      result.fallingEdges++;
    }
    if (currentLevel == LOW) {
      result.lowSamples++;
    }
    previousLevel = currentLevel;
    delayMicroseconds(80);
  }

  return result;
}

void printIrRawLevel(const char *label, IrReceiveResult result) {
  Serial.print(label);
  Serial.print(F(" pulses="));
  Serial.print(result.fallingEdges);
  Serial.print(F(", low_samples="));
  Serial.print(result.lowSamples);
  Serial.print(F(", idle_low="));
  Serial.println(result.lowSamples > 0 ? F("YES") : F("NO"));
}

void printIrResult(const char *label, IrReceiveResult result) {
  Serial.print(label);
  Serial.print(F(" pulses="));
  Serial.print(result.fallingEdges);
  Serial.print(F(", low_samples="));
  Serial.print(result.lowSamples);
  Serial.print(F(", detected="));
  Serial.println(result.fallingEdges > 0 || result.lowSamples > 0 ? F("YES") : F("NO"));
}

void runIrReceiveTest() {
  Serial.println(F("IR_RECEIVE_TEST start"));
  IrReceiveResult result = listenForIr(IR_RECEIVE_TEST_MS);
  printIrResult("IR_RECEIVE_TEST", result);
}

void runIrRawCapture() {
  Serial.println(F("IR_RAW_CAPTURE start: press and hold one remote button."));

  unsigned long waitStart = millis();
  unsigned int ignoredShortBursts = 0;

  while (millis() - waitStart < IR_RAW_START_TIMEOUT_MS) {
    while (digitalRead(IR_RECV_PIN) == HIGH) {
      if (millis() - waitStart >= IR_RAW_START_TIMEOUT_MS) {
        Serial.print(F("IR_RAW_CAPTURE timeout: no valid signal, ignored_short="));
        Serial.println(ignoredShortBursts);
        return;
      }
    }

    unsigned long lowStartAt = micros();
    while (digitalRead(IR_RECV_PIN) == LOW) {
      if (micros() - lowStartAt > 65535) {
        break;
      }
    }

    unsigned long firstLowDuration = micros() - lowStartAt;
    if (firstLowDuration < IR_RAW_MIN_START_LOW_US) {
      ignoredShortBursts++;
      continue;
    }

    unsigned int count = 0;
    bool overflow = false;

    if (firstLowDuration > 65535) {
      firstLowDuration = 65535;
    }
    irRawDurations[count++] = (unsigned int)firstLowDuration;

    int currentLevel = HIGH;
    unsigned long captureStart = micros();
    unsigned long lastChangeAt = captureStart;

    while (micros() - captureStart < IR_RAW_MAX_CAPTURE_US) {
      int nextLevel = digitalRead(IR_RECV_PIN);
      unsigned long now = micros();

      if (nextLevel != currentLevel) {
        unsigned long duration = now - lastChangeAt;
        if (duration > 65535) {
          duration = 65535;
        }

        if (count < IR_RAW_MAX_EDGES) {
          irRawDurations[count++] = (unsigned int)duration;
        } else {
          overflow = true;
        }

        currentLevel = nextLevel;
        lastChangeAt = now;
      } else if (currentLevel == HIGH && count > 0 && now - lastChangeAt >= IR_RAW_GAP_US) {
        break;
      }
    }

    if (count < IR_RAW_MIN_VALID_EDGES && !overflow) {
      ignoredShortBursts++;
      continue;
    }

    irRawCount = count;
    irRawOverflow = overflow;

    Serial.print(F("IR_RAW_CAPTURE count="));
    Serial.print(count);
    Serial.print(F(", overflow="));
    Serial.println(overflow ? F("YES") : F("NO"));

    Serial.print(F("IR_RAW_DATA: "));
    for (unsigned int i = 0; i < count; i++) {
      if (i > 0) {
        Serial.print(',');
      }
      Serial.print(i % 2 == 0 ? '+' : '-');
      Serial.print(irRawDurations[i]);
    }
    Serial.println();
    return;
  }

  Serial.print(F("IR_RAW_CAPTURE timeout: no valid signal, ignored_short="));
  Serial.println(ignoredShortBursts);
}

void replayLastIrRaw() {
  if (irRawCount == 0) {
    Serial.println(F("IR_REPLAY_LAST no captured raw data"));
    return;
  }

  Serial.print(F("IR_REPLAY_LAST count="));
  Serial.print(irRawCount);
  Serial.print(F(", overflow="));
  Serial.println(irRawOverflow ? F("YES") : F("NO"));

  for (unsigned int i = 0; i < irRawCount; i++) {
    if (i % 2 == 0) {
      sendIrCarrier(irRawDurations[i], 0);
    } else {
      delayMicroseconds(irRawDurations[i]);
    }
  }

  setIrLed(false);
  Serial.println(F("IR_REPLAY_LAST done"));
}

void runIrPolarityTest() {
  Serial.println(F("IR_POLARITY_TEST start"));

  pinMode(IR_SEND_PIN, OUTPUT);
  digitalWrite(IR_SEND_PIN, LOW);
  delay(300);
  printIrRawLevel("IR_POLARITY_TEST D3_LOW", listenForIr(1000));

  digitalWrite(IR_SEND_PIN, HIGH);
  delay(300);
  printIrRawLevel("IR_POLARITY_TEST D3_HIGH", listenForIr(1000));

  pinMode(IR_SEND_PIN, INPUT);
  delay(300);
  printIrRawLevel("IR_POLARITY_TEST D3_INPUT", listenForIr(1000));

  pinMode(IR_SEND_PIN, OUTPUT);
  setIrLed(false);
}

void sendIrCarrier(unsigned long durationMicros, IrReceiveResult *loopbackResult) {
  unsigned long startAt = micros();
  int previousLevel = digitalRead(IR_RECV_PIN);
  startIrCarrier();

  if (loopbackResult == 0) {
    delayMicroseconds(durationMicros);
    stopIrCarrier();
    return;
  }

  while (micros() - startAt < durationMicros) {
    int currentLevel = digitalRead(IR_RECV_PIN);
    if (previousLevel == HIGH && currentLevel == LOW) {
      loopbackResult->fallingEdges++;
    }
    if (currentLevel == LOW) {
      loopbackResult->lowSamples++;
    }
    previousLevel = currentLevel;
    delayMicroseconds(50);
  }

  stopIrCarrier();
}

void sendIrRawFromProgmem(const unsigned int *raw, unsigned int length) {
  for (unsigned int repeat = 0; repeat < IR_COMMAND_REPEAT_COUNT; repeat++) {
    for (unsigned int i = 0; i < length; i++) {
      unsigned int duration = scaledIrDuration(pgm_read_word(&raw[i]));

      if (i % 2 == 0) {
        sendIrCarrier(duration, 0);
      } else {
        delayMicroseconds(duration);
      }
    }

    setIrLed(false);
    if (repeat + 1 < IR_COMMAND_REPEAT_COUNT) {
      delay(IR_COMMAND_REPEAT_GAP_MS);
    }
  }

  setIrLed(false);
}

bool sendNamedIrCommand(String command) {
  command.trim();
  command.toUpperCase();

  if (command.startsWith("AC_ON_COOL_") || command.startsWith("AC_SET_COOL_")) {
    command = command.substring(3);
  } else if (command == "AC_ON_DRY") {
    command = "ON_DRY";
  } else if (command == "AC_DRY_OFF") {
    command = "DRY_OFF";
  }

  #define TRY_STORED_IR_COMMAND(stem, nameLiteral, rawArray) \
    if (command == F(nameLiteral)) { \
      Serial.print(F("IR_COMMAND send: ")); \
      Serial.print(F(nameLiteral)); \
      Serial.print(F(", scale=")); \
      Serial.print(irTimingScalePercent); \
      Serial.println(F("%")); \
      sendIrRawFromProgmem(rawArray, sizeof(rawArray) / sizeof(rawArray[0])); \
      Serial.println(F("IR_COMMAND done")); \
      return true; \
    }

  FOR_EACH_IR_COMMAND(TRY_STORED_IR_COMMAND)

  #undef TRY_STORED_IR_COMMAND

  return false;
}

void sendIrTestPattern(IrReceiveResult *loopbackResult) {
  for (int repeat = 0; repeat < 3; repeat++) {
    for (int burst = 0; burst < 20; burst++) {
      sendIrCarrier(600, loopbackResult);
      delayMicroseconds(600);
    }
    delay(80);
  }
}

void runIrSendTest() {
  Serial.println(F("IR_SEND_TEST start"));
  sendIrTestPattern(0);
  setIrLed(false);
  Serial.println(F("IR_SEND_TEST done"));
}

void runIrLoopbackTest() {
  Serial.println(F("IR_LOOPBACK_TEST start"));
  IrReceiveResult result = {0, 0};
  sendIrTestPattern(&result);
  setIrLed(false);
  printIrResult("IR_LOOPBACK_TEST", result);
}

unsigned long sendIrCarrierAndCountLow(unsigned long durationMicros) {
  unsigned long lowSamples = 0;
  unsigned long startAt = micros();
  startIrCarrier();

  while (micros() - startAt < durationMicros) {
    if (digitalRead(IR_RECV_PIN) == LOW) {
      lowSamples++;
    }
    delayMicroseconds(50);
  }

  stopIrCarrier();
  return lowSamples;
}

unsigned long countLowDuringSpace(unsigned long durationMicros) {
  unsigned long lowSamples = 0;
  unsigned long startAt = micros();
  setIrLed(false);

  while (micros() - startAt < durationMicros) {
    if (digitalRead(IR_RECV_PIN) == LOW) {
      lowSamples++;
    }
    delayMicroseconds(50);
  }

  return lowSamples;
}

void runIrAccuracyTest() {
  const unsigned int burstDurations[] = {600, 1200, 600, 1800, 600, 2400, 600, 1200};
  const unsigned int spaceDurations[] = {700, 900, 1100, 1300, 1500, 1700, 1900, 2100};
  const int patternCount = sizeof(burstDurations) / sizeof(burstDurations[0]);
  int passedBursts = 0;
  int passedSpaces = 0;

  Serial.println(F("IR_ACCURACY_TEST start"));

  for (int i = 0; i < patternCount; i++) {
    unsigned long burstLow = sendIrCarrierAndCountLow(burstDurations[i]);
    unsigned long spaceLow = countLowDuringSpace(spaceDurations[i]);
    bool burstOk = burstLow > 0;
    bool spaceOk = spaceLow == 0;

    if (burstOk) {
      passedBursts++;
    }
    if (spaceOk) {
      passedSpaces++;
    }

    Serial.print(F("IR_STEP "));
    Serial.print(i + 1);
    Serial.print(F(" burst_us="));
    Serial.print(burstDurations[i]);
    Serial.print(F(" burst_low="));
    Serial.print(burstLow);
    Serial.print(F(" burst_ok="));
    Serial.print(burstOk ? F("YES") : F("NO"));
    Serial.print(F(" space_us="));
    Serial.print(spaceDurations[i]);
    Serial.print(F(" space_low="));
    Serial.print(spaceLow);
    Serial.print(F(" space_ok="));
    Serial.println(spaceOk ? F("YES") : F("NO"));

    delay(30);
  }

  setIrLed(false);
  Serial.print(F("IR_ACCURACY_TEST passed_bursts="));
  Serial.print(passedBursts);
  Serial.print(F("/"));
  Serial.print(patternCount);
  Serial.print(F(", passed_spaces="));
  Serial.print(passedSpaces);
  Serial.print(F("/"));
  Serial.print(patternCount);
  Serial.print(F(", result="));
  Serial.println(passedBursts == patternCount && passedSpaces >= patternCount - 1 ? F("PASS") : F("CHECK"));
}

void printStatus() {
  float humidity = dht.readHumidity();
  float temperature = dht.readTemperature();
  int lightRaw = analogRead(LIGHT_SENSOR_PIN);

  if (!isnan(humidity) && !isnan(temperature)) {
    Serial.print(F("온도: "));
    Serial.print(temperature);
    Serial.print(F(" C, 습도: "));
    Serial.print(humidity);
    Serial.print(F(" %, 조도: "));
    Serial.print(lightRaw);
    Serial.print(F(", 가습기: "));
    Serial.println(humidifierOn ? F("ON") : F("OFF"));
  } else {
    Serial.println(F("센서 값을 읽을 수 없습니다."));
  }
}

void handleCommand(String command) {
  command.trim();

  if (sendNamedIrCommand(command)) {
    return;
  } else if (command.startsWith("IR_SCALE_")) {
    int scale = command.substring(9).toInt();
    if (scale >= 70 && scale <= 130) {
      irTimingScalePercent = scale;
      Serial.print(F("IR_SCALE set: "));
      Serial.print(irTimingScalePercent);
      Serial.println(F("%"));
    } else {
      Serial.println(F("IR_SCALE range: 70-130"));
    }
  } else if (command == "PUSH" || command == "AIRCON_TOGGLE") {
    pressAirconButton();
  } else if (command == "HUMIDIFIER_ON") {
    setHumidifierDial(true);
  } else if (command == "HUMIDIFIER_OFF") {
    setHumidifierDial(false);
  } else if (command == "IR_RECEIVE_TEST") {
    runIrReceiveTest();
  } else if (command == "IR_RAW_CAPTURE") {
    runIrRawCapture();
  } else if (command == "IR_REPLAY_LAST") {
    replayLastIrRaw();
  } else if (command == "IR_SEND_TEST") {
    runIrSendTest();
  } else if (command == "IR_LOOPBACK_TEST") {
    runIrLoopbackTest();
  } else if (command == "IR_ACCURACY_TEST") {
    runIrAccuracyTest();
  } else if (command == "IR_POLARITY_TEST") {
    runIrPolarityTest();
  } else if (command == "STATUS") {
    printStatus();
  } else if (command.length() > 0) {
    Serial.print(F("알 수 없는 명령: "));
    Serial.println(command);
  }
}

void setup() {
  Serial.begin(9600);

  dht.begin();

  setupIrCarrierPwm();
  pinMode(IR_RECV_PIN, INPUT_PULLUP);

  airconServo.attach(AIRCON_SERVO_PIN);
  airconServo.write(AIRCON_READY_ANGLE);

  humidifierServo.attach(HUMIDIFIER_SERVO_PIN);
  humidifierServo.write(HUMIDIFIER_OFF_ANGLE);
  humidifierCurrentAngle = HUMIDIFIER_OFF_ANGLE;
  humidifierOn = false;

  Serial.println(F("집안 원격 제어 시스템 시작"));
  Serial.println(F("Commands ready"));
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
