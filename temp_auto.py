import platform
import re
import serial
import subprocess
import time

PORT = "COM5"
BAUD = 9600

# 라즈베리파이에서는 보통 "/dev/ttyACM0" 또는 "/dev/ttyUSB0" 입니다.
# PORT = "/dev/ttyACM0"

# 내 휴대폰 블루투스 MAC 주소로 바꿔 주세요.
# 라즈베리파이에서 `bluetoothctl devices` 또는 `bluetoothctl scan on`으로 확인할 수 있습니다.
PHONE_BLUETOOTH_MAC = "AA:BB:CC:DD:EE:FF"

# MAC 주소를 모를 때 보조로 사용할 휴대폰 이름 일부입니다. MAC 주소 사용을 권장합니다.
PHONE_NAME_KEYWORD = ""

BLUETOOTH_SCAN_INTERVAL = 10  # 몇 초마다 휴대폰을 찾을지
BLUETOOTH_SCAN_SECONDS = 5    # 한 번 검색할 때 몇 초 동안 검색할지
BLUETOOTH_MISSED_LIMIT = 2    # 몇 번 연속 안 잡히면 외출로 볼지
BLUETOOTH_ENABLED = platform.system() == "Linux"

# 온도 기준값
TEMP_ON = 26.0      # 이 온도 이상이면 에어컨 ON
TEMP_OFF = 25.0     # 이 온도 이하이면 에어컨 OFF

# 아두이노에 보낼 명령입니다.
AIRCON_TOGGLE_COMMAND = "PUSH"
HUMIDIFIER_OFF_COMMAND = "HUMIDIFIER_OFF"

# 현재 장치 상태
# 실제 시작 상태에 맞게 바꿔 주세요.
aircon_on = False
humidifier_on = False

# 현재 재실 상태
is_home = False
missed_phone_count = 0
next_bluetooth_check = 0.0


def send_arduino_command(command):
    arduino.write(f"{command}\n".encode("utf-8"))
    arduino.flush()


def scan_bluetooth_devices():
    if not BLUETOOTH_ENABLED:
        return ""

    try:
        process = subprocess.Popen(
            ["bluetoothctl"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError:
        print("bluetoothctl을 찾을 수 없습니다. 라즈베리파이에서 BlueZ 설치를 확인하세요.")
        return ""

    try:
        process.stdin.write("scan on\n")
        process.stdin.flush()
        time.sleep(BLUETOOTH_SCAN_SECONDS)

        process.stdin.write("scan off\nquit\n")
        process.stdin.flush()

        stdout, stderr = process.communicate(timeout=5)
    except (BrokenPipeError, subprocess.TimeoutExpired):
        process.kill()
        stdout, stderr = process.communicate()

    return f"{stdout}\n{stderr}"


def is_phone_connected():
    if not BLUETOOTH_ENABLED or not PHONE_BLUETOOTH_MAC:
        return False

    try:
        result = subprocess.run(
            ["bluetoothctl", "info", PHONE_BLUETOOTH_MAC],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except FileNotFoundError:
        return False

    return "Connected: yes" in result.stdout


def is_phone_detected():
    mac = PHONE_BLUETOOTH_MAC.lower()
    name = PHONE_NAME_KEYWORD.lower().strip()

    if is_phone_connected():
        return True

    scan_result = scan_bluetooth_devices().lower()

    if mac and mac != "aa:bb:cc:dd:ee:ff" and mac in scan_result:
        return True

    if name and name in scan_result:
        return True

    return False


def turn_aircon_off():
    global aircon_on

    if not aircon_on:
        return

    print("외출 감지: 에어컨 OFF 명령 전송")
    send_arduino_command(AIRCON_TOGGLE_COMMAND)
    aircon_on = False
    time.sleep(3)


def turn_humidifier_off():
    global humidifier_on

    print("외출 감지: 가습기 OFF 명령 전송")
    send_arduino_command(HUMIDIFIER_OFF_COMMAND)
    humidifier_on = False
    time.sleep(1)


def turn_devices_off_when_away():
    turn_aircon_off()
    turn_humidifier_off()


def update_home_state():
    global is_home, missed_phone_count, next_bluetooth_check

    now = time.monotonic()
    if now < next_bluetooth_check:
        return

    next_bluetooth_check = now + BLUETOOTH_SCAN_INTERVAL

    if is_phone_detected():
        missed_phone_count = 0
        if not is_home:
            print("휴대폰 감지됨: 재실 상태 ON")
        is_home = True
        return

    missed_phone_count += 1
    print(f"휴대폰 미감지 {missed_phone_count}/{BLUETOOTH_MISSED_LIMIT}")

    if is_home and missed_phone_count >= BLUETOOTH_MISSED_LIMIT:
        print("휴대폰 연결/검색 끊김: 외출 상태로 변경")
        is_home = False
        turn_devices_off_when_away()


arduino = serial.Serial(PORT, BAUD, timeout=1)

print("아두이노 연결됨, 리셋 대기 중...")
time.sleep(4)

print("자동 온도 제어 시작")
print(f"온도 {TEMP_ON}℃ 이상 → 에어컨 ON")
print(f"온도 {TEMP_OFF}℃ 이하 → 에어컨 OFF")
print("휴대폰이 감지될 때만 에어컨 자동 ON 동작")
print("휴대폰 연결/검색이 끊기면 에어컨, 가습기 상태 OFF")
print("종료하려면 Ctrl + C")

try:
    while True:
        update_home_state()

        if arduino.in_waiting > 0:
            line = arduino.readline().decode("utf-8", errors="ignore").strip()

            # 예: 온도: 24.00 C, 습도: 44.00 %
            match = re.search(r"온도:\s*([0-9.]+)\s*C,\s*습도:\s*([0-9.]+)", line)

            if match:
                temperature = float(match.group(1))
                humidity = float(match.group(2))

                # 집에 있고 온도가 높고 에어컨이 꺼져 있으면 ON
                if is_home and temperature >= TEMP_ON and not aircon_on:
                    print("온도가 높고 재실 중입니다. 에어컨 ON 명령 전송")
                    send_arduino_command(AIRCON_TOGGLE_COMMAND)
                    aircon_on = True
                    time.sleep(3)

                # 온도가 낮고 에어컨이 켜져 있으면 OFF
                elif temperature <= TEMP_OFF and aircon_on:
                    print("온도가 낮아졌습니다. 에어컨 OFF 명령 전송")
                    send_arduino_command(AIRCON_TOGGLE_COMMAND)
                    aircon_on = False
                    time.sleep(3)

                home_state = "재실" if is_home else "외출"
                aircon_state = "ON" if aircon_on else "OFF"
                humidifier_state = "ON" if humidifier_on else "OFF"

                print(
                    f"온도: {temperature:.2f} C, "
                    f"습도: {humidity:.2f} %, "
                    f"재실 상태: {home_state}, "
                    f"에어컨 상태: {aircon_state}, "
                    f"가습기 상태: {humidifier_state}"
                )

            elif line:
                # 버튼 누르기 동작 실행 / 완료 같은 기타 메시지 출력
                print("Arduino:", line)

        time.sleep(0.1)

except KeyboardInterrupt:
    print("\n자동 제어 종료")

finally:
    arduino.close()
    print("시리얼 연결 종료")
