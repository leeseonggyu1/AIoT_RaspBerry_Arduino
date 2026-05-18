import json
import os
import re
import serial
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

PORT = os.getenv("ARDUINO_PORT", "/dev/ttyACM0")
BAUD = int(os.getenv("ARDUINO_BAUD", "9600"))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

TEMP_ON = float(os.getenv("TEMP_ON", "26.0"))
TEMP_OFF = float(os.getenv("TEMP_OFF", "25.0"))

AIRCON_TOGGLE_COMMAND = "AIRCON_TOGGLE"
HUMIDIFIER_ON_COMMAND = "HUMIDIFIER_ON"
HUMIDIFIER_OFF_COMMAND = "HUMIDIFIER_OFF"

state_lock = threading.Lock()
serial_lock = threading.Lock()
stop_event = threading.Event()

state = {
    "temperature": None,
    "humidity": None,
    "last_sensor_at": None,
    "last_arduino_line": "",
    "aircon_on": False,
    "humidifier_on": False,
    "auto_control": True,
}

arduino = None


def telegram_api(method, params=None, timeout=35):
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN 환경변수가 필요합니다.")

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    data = urllib.parse.urlencode(params or {}).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST")

    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def send_message(chat_id, text, show_keyboard=True):
    params = {
        "chat_id": chat_id,
        "text": text,
    }

    if show_keyboard:
        params["reply_markup"] = json.dumps(
            {
                "keyboard": [
                    ["/status"],
                    ["/aircon_on", "/aircon_off"],
                    ["/humidifier_on", "/humidifier_off"],
                    ["/auto_on", "/auto_off"],
                ],
                "resize_keyboard": True,
            },
            ensure_ascii=False,
        )

    telegram_api("sendMessage", params, timeout=10)


def is_authorized(chat_id):
    if not TELEGRAM_CHAT_ID:
        send_message(
            chat_id,
            f"초기 설정 필요\n\n이 휴대폰 chat_id는 {chat_id} 입니다.\n"
            "라즈베리파이에서 TELEGRAM_CHAT_ID에 이 값을 넣고 다시 실행하세요.",
            show_keyboard=False,
        )
        return False

    if str(chat_id) != str(TELEGRAM_CHAT_ID):
        send_message(chat_id, "허용되지 않은 사용자입니다.", show_keyboard=False)
        return False

    return True


def send_arduino_command(command):
    with serial_lock:
        arduino.write(f"{command}\n".encode("utf-8"))
        arduino.flush()


def set_aircon(target_on):
    with state_lock:
        current_on = state["aircon_on"]

    if current_on == target_on:
        return f"에어컨은 이미 {'ON' if target_on else 'OFF'} 상태로 기억되어 있습니다."

    send_arduino_command(AIRCON_TOGGLE_COMMAND)

    with state_lock:
        state["aircon_on"] = target_on

    return f"에어컨 {'ON' if target_on else 'OFF'} 명령을 보냈습니다."


def toggle_aircon():
    with state_lock:
        target_on = not state["aircon_on"]

    return set_aircon(target_on)


def set_humidifier(target_on):
    command = HUMIDIFIER_ON_COMMAND if target_on else HUMIDIFIER_OFF_COMMAND
    send_arduino_command(command)

    with state_lock:
        state["humidifier_on"] = target_on

    return f"가습기 {'ON' if target_on else 'OFF'} 명령을 보냈습니다."


def maybe_auto_control(temperature):
    with state_lock:
        auto_control = state["auto_control"]
        aircon_on = state["aircon_on"]

    if not auto_control:
        return

    if temperature >= TEMP_ON and not aircon_on:
        print("자동 제어: 온도가 높아 에어컨 ON")
        set_aircon(True)
    elif temperature <= TEMP_OFF and aircon_on:
        print("자동 제어: 온도가 낮아져 에어컨 OFF")
        set_aircon(False)


def handle_arduino_line(line):
    print("Arduino:", line)

    temperature = None
    humidity = None

    match = re.search(
        r"온도:\s*(-?[0-9]+(?:\.[0-9]+)?)\s*C,\s*습도:\s*([0-9]+(?:\.[0-9]+)?)",
        line,
    )
    if match:
        temperature = float(match.group(1))
        humidity = float(match.group(2))

    humidifier_match = re.search(r"가습기:\s*(ON|OFF)", line)

    with state_lock:
        state["last_arduino_line"] = line

        if temperature is not None and humidity is not None:
            state["temperature"] = temperature
            state["humidity"] = humidity
            state["last_sensor_at"] = time.time()

        if humidifier_match:
            state["humidifier_on"] = humidifier_match.group(1) == "ON"

    if temperature is not None:
        maybe_auto_control(temperature)


def serial_reader():
    while not stop_event.is_set():
        try:
            line = arduino.readline().decode("utf-8", errors="ignore").strip()
        except serial.SerialException as exc:
            print("시리얼 읽기 오류:", exc)
            time.sleep(2)
            continue

        if line:
            handle_arduino_line(line)


def format_status():
    with state_lock:
        temperature = state["temperature"]
        humidity = state["humidity"]
        last_sensor_at = state["last_sensor_at"]
        aircon_on = state["aircon_on"]
        humidifier_on = state["humidifier_on"]
        auto_control = state["auto_control"]
        last_arduino_line = state["last_arduino_line"]

    if temperature is None or humidity is None:
        sensor_text = "온습도: 아직 수신 전"
    else:
        age = int(time.time() - last_sensor_at)
        sensor_text = f"온도: {temperature:.1f} C\n습도: {humidity:.1f} %\n센서 수신: {age}초 전"

    return (
        f"{sensor_text}\n"
        f"에어컨: {'ON' if aircon_on else 'OFF'}\n"
        f"가습기: {'ON' if humidifier_on else 'OFF'}\n"
        f"자동 제어: {'ON' if auto_control else 'OFF'}\n"
        f"자동 기준: {TEMP_ON:.1f} C 이상 ON, {TEMP_OFF:.1f} C 이하 OFF\n"
        f"마지막 아두이노 메시지: {last_arduino_line or '없음'}"
    )


def handle_command(chat_id, text):
    command = text.strip().lower()

    if command in ("/start", "/help", "도움말"):
        send_message(
            chat_id,
            "사용 가능한 명령\n"
            "/status - 집안 온습도와 장치 상태 확인\n"
            "/aircon_on - 에어컨 ON\n"
            "/aircon_off - 에어컨 OFF\n"
            "/aircon_toggle - 에어컨 토글\n"
            "/humidifier_on - 가습기 ON\n"
            "/humidifier_off - 가습기 OFF\n"
            "/auto_on - 온도 자동 제어 ON\n"
            "/auto_off - 온도 자동 제어 OFF",
        )
    elif command in ("/status", "상태"):
        send_message(chat_id, format_status())
    elif command in ("/aircon_on", "에어컨켜", "에어컨 켜"):
        send_message(chat_id, set_aircon(True))
    elif command in ("/aircon_off", "에어컨꺼", "에어컨 꺼"):
        send_message(chat_id, set_aircon(False))
    elif command in ("/aircon_toggle", "에어컨토글"):
        send_message(chat_id, toggle_aircon())
    elif command in ("/humidifier_on", "가습기켜", "가습기 켜"):
        send_message(chat_id, set_humidifier(True))
    elif command in ("/humidifier_off", "가습기꺼", "가습기 꺼"):
        send_message(chat_id, set_humidifier(False))
    elif command in ("/auto_on", "자동켜", "자동 켜"):
        with state_lock:
            state["auto_control"] = True
        send_message(chat_id, "온도 자동 제어를 켰습니다.")
    elif command in ("/auto_off", "자동꺼", "자동 꺼"):
        with state_lock:
            state["auto_control"] = False
        send_message(chat_id, "온도 자동 제어를 껐습니다.")
    else:
        send_message(chat_id, "알 수 없는 명령입니다. /help 를 보내면 명령 목록을 볼 수 있습니다.")


def telegram_loop():
    offset = None

    while not stop_event.is_set():
        try:
            response = telegram_api(
                "getUpdates",
                {
                    "timeout": 30,
                    "offset": offset or "",
                    "allowed_updates": json.dumps(["message"]),
                },
                timeout=40,
            )
        except (urllib.error.URLError, TimeoutError, RuntimeError) as exc:
            print("텔레그램 연결 오류:", exc)
            time.sleep(5)
            continue

        for update in response.get("result", []):
            offset = update["update_id"] + 1
            message = update.get("message", {})
            chat = message.get("chat", {})
            chat_id = chat.get("id")
            text = message.get("text", "")

            if chat_id is None or not text:
                continue

            if not is_authorized(chat_id):
                continue

            handle_command(chat_id, text)


def main():
    global arduino

    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN 환경변수를 먼저 설정하세요.")

    arduino = serial.Serial(PORT, BAUD, timeout=1)
    print(f"아두이노 연결됨: {PORT}, {BAUD}bps")
    time.sleep(3)

    reader_thread = threading.Thread(target=serial_reader, daemon=True)
    reader_thread.start()

    print("텔레그램 원격 제어 시작")
    print("휴대폰에서 봇에게 /start 또는 /status를 보내세요.")
    telegram_loop()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n원격 제어 종료")
    finally:
        stop_event.set()
        if arduino is not None:
            arduino.close()
            print("시리얼 연결 종료")
