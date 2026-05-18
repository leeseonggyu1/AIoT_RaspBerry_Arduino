import json
import os
import platform
import re
import subprocess
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

try:
    import serial
except ImportError:
    serial = None

SERIAL_READ_ERRORS = (OSError,)
if serial is not None:
    SERIAL_READ_ERRORS = (OSError, serial.SerialException)

ARDUINO_PORT = os.getenv("ARDUINO_PORT", "/dev/ttyACM0")
ARDUINO_BAUD = int(os.getenv("ARDUINO_BAUD", "9600"))
MOCK_ARDUINO = os.getenv("MOCK_ARDUINO", "").lower() in ("1", "true", "yes", "on")
CONFIG_PATH = os.getenv(
    "CONFIG_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "home_control_config.json"),
)

# Tailscale Serve를 쓰면 127.0.0.1 권장:
#   tailscale serve --bg 8000
# Tailscale IP로 직접 접속하려면 HOST를 0.0.0.0으로 실행:
#   HOST=0.0.0.0 python3 home_tailscale_server.py
HOST = os.getenv("HOST", "127.0.0.1")
WEB_PORT = int(os.getenv("WEB_PORT", "8000"))

# 원격 제어 보호용 토큰입니다. 비워두면 Tailscale 접속만으로 접근됩니다.
# 예: CONTROL_TOKEN=12345678 python3 home_tailscale_server.py
CONTROL_TOKEN = os.getenv("CONTROL_TOKEN", "")


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as file:
            return json.load(file)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_config(updates):
    config = load_config()
    config.update(updates)

    with open(CONFIG_PATH, "w", encoding="utf-8") as file:
        json.dump(config, file, ensure_ascii=False, indent=2)


def env_or_config_float(env_name, config, config_name, default):
    if env_name in os.environ:
        return float(os.environ[env_name])

    return float(config.get(config_name, default))


def env_or_config_int(env_name, config, config_name, default):
    if env_name in os.environ:
        return int(float(os.environ[env_name]))

    return int(float(config.get(config_name, default)))


def parse_bool(value, default=False):
    if value is None:
        return default

    return str(value).strip().lower() in ("1", "true", "yes", "on")


def env_or_config_bool(env_name, config, config_name, default=False):
    if env_name in os.environ:
        return parse_bool(os.environ[env_name], default)

    return parse_bool(config.get(config_name), default)


def env_or_config_str(env_name, config, config_name, default=""):
    if env_name in os.environ:
        return os.environ[env_name].strip()

    return str(config.get(config_name, default)).strip()


def normalize_mac(value):
    return value.strip().replace("-", ":").lower()


def has_real_mac(value):
    return bool(value) and value != "aa:bb:cc:dd:ee:ff"


config = load_config()

TEMP_ON = env_or_config_float("TEMP_ON", config, "temp_on", "26.0")
TEMP_OFF = env_or_config_float("TEMP_OFF", config, "temp_off", "25.0")
AUTO_CONTROL_DEFAULT = os.getenv("AUTO_CONTROL", "1").lower() in ("1", "true", "yes", "on")

PHONE_BLUETOOTH_MAC = normalize_mac(env_or_config_str("PHONE_BLUETOOTH_MAC", config, "phone_bluetooth_mac"))
PHONE_NAME_KEYWORD = env_or_config_str("PHONE_NAME_KEYWORD", config, "phone_name_keyword")
BLUETOOTH_SCAN_INTERVAL = env_or_config_int("BLUETOOTH_SCAN_INTERVAL", config, "bluetooth_scan_interval", 30)
BLUETOOTH_SCAN_SECONDS = env_or_config_int("BLUETOOTH_SCAN_SECONDS", config, "bluetooth_scan_seconds", 8)
AWAY_AFTER_SECONDS = env_or_config_int("AWAY_AFTER_SECONDS", config, "away_after_seconds", 300)
PRESENCE_TARGET_CONFIGURED = has_real_mac(PHONE_BLUETOOTH_MAC) or bool(PHONE_NAME_KEYWORD)
PRESENCE_DEFAULT_ENABLED = platform.system() == "Linux" and PRESENCE_TARGET_CONFIGURED
PRESENCE_ENABLED = env_or_config_bool("PRESENCE_ENABLED", config, "presence_enabled", PRESENCE_DEFAULT_ENABLED)

# 기존 아두이노 스케치와 새 home_controller.ino가 둘 다 받을 수 있는 명령입니다.
AIRCON_TOGGLE_COMMAND = "PUSH"
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
    "auto_control": AUTO_CONTROL_DEFAULT,
    "temp_on": TEMP_ON,
    "temp_off": TEMP_OFF,
    "presence_enabled": PRESENCE_ENABLED,
    "presence_state": "unknown" if PRESENCE_ENABLED else "disabled",
    "is_home": False,
    "phone_detected": False,
    "last_phone_seen_at": None,
    "last_presence_check_at": None,
    "presence_missing_since": None,
    "last_presence_error": "",
    "away_after_seconds": AWAY_AFTER_SECONDS,
}

arduino = None


class MockArduino:
    def __init__(self):
        self.started_at = time.monotonic()
        self.humidifier_on = False
        self.last_command = "없음"

    def write(self, data):
        command = data.decode("utf-8", errors="ignore").strip()
        self.last_command = command

        if command == HUMIDIFIER_ON_COMMAND:
            self.humidifier_on = True
        elif command == HUMIDIFIER_OFF_COMMAND:
            self.humidifier_on = False

        print(f"Mock Arduino command: {command}")

    def flush(self):
        pass

    def readline(self):
        elapsed = int(time.monotonic() - self.started_at)
        temperature = 24.0 + (elapsed % 8) * 0.2
        humidity = 45.0 + (elapsed % 6) * 0.5
        humidifier = "ON" if self.humidifier_on else "OFF"
        time.sleep(1)
        return f"온도: {temperature:.2f} C, 습도: {humidity:.2f} %, 가습기: {humidifier}\n".encode("utf-8")

    def close(self):
        pass


INDEX_HTML = """<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Home Control</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f5f7fb;
      --panel: #ffffff;
      --text: #17202a;
      --muted: #667085;
      --line: #d7dde8;
      --blue: #2563eb;
      --green: #16803c;
      --red: #b42318;
      --amber: #b54708;
    }

    * {
      box-sizing: border-box;
    }

    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }

    main {
      width: min(920px, 100%);
      margin: 0 auto;
      padding: 20px;
    }

    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 16px;
    }

    h1 {
      margin: 0;
      font-size: 24px;
      letter-spacing: 0;
    }

    .updated {
      color: var(--muted);
      font-size: 14px;
      white-space: nowrap;
    }

    .grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }

    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
    }

    .wide {
      grid-column: 1 / -1;
    }

    .label {
      color: var(--muted);
      font-size: 13px;
      margin-bottom: 6px;
    }

    .value {
      font-size: 30px;
      font-weight: 700;
      line-height: 1.1;
      letter-spacing: 0;
    }

    .status-row {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 12px;
    }

    .pill {
      display: inline-flex;
      align-items: center;
      min-height: 30px;
      padding: 5px 9px;
      border-radius: 8px;
      border: 1px solid var(--line);
      font-size: 14px;
      background: #fff;
    }

    .on {
      color: var(--green);
      border-color: #a8dab5;
      background: #f0fbf3;
    }

    .off {
      color: var(--red);
      border-color: #f0b8b2;
      background: #fff5f4;
    }

    .auto {
      color: var(--blue);
      border-color: #b8cafc;
      background: #f4f7ff;
    }

    .controls {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }

    .thresholds {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
      align-items: end;
    }

    .field span {
      display: block;
      color: var(--muted);
      font-size: 13px;
      margin-bottom: 6px;
    }

    button, input {
      width: 100%;
      min-height: 44px;
      border-radius: 8px;
      border: 1px solid var(--line);
      font: inherit;
      letter-spacing: 0;
    }

    button {
      cursor: pointer;
      color: #fff;
      background: var(--blue);
      font-weight: 650;
    }

    button.secondary {
      color: var(--text);
      background: #fff;
    }

    button.warn {
      background: var(--amber);
    }

    button.danger {
      background: var(--red);
    }

    input {
      padding: 0 12px;
      background: #fff;
    }

    .message {
      margin-top: 12px;
      color: var(--muted);
      font-size: 14px;
      min-height: 20px;
    }

    .token-panel {
      display: none;
      grid-template-columns: 1fr auto;
      gap: 8px;
      margin-bottom: 12px;
    }

    .token-panel.visible {
      display: grid;
    }

    pre {
      white-space: pre-wrap;
      word-break: break-word;
      margin: 0;
      color: var(--muted);
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 13px;
    }

    @media (max-width: 680px) {
      main {
        padding: 14px;
      }

      header {
        align-items: flex-start;
        flex-direction: column;
      }

      .grid, .controls, .thresholds {
        grid-template-columns: 1fr;
      }

      .value {
        font-size: 26px;
      }

      .token-panel {
        grid-template-columns: 1fr;
      }
    }
  </style>
</head>
<body>
  <main>
    <header>
      <h1>Home Control</h1>
      <div class="updated" id="updated">연결 중</div>
    </header>

    <section class="token-panel" id="tokenPanel">
      <input id="tokenInput" type="password" autocomplete="current-password" placeholder="CONTROL_TOKEN">
      <button class="secondary" id="saveToken">저장</button>
    </section>

    <section class="grid">
      <article class="panel">
        <div class="label">온도</div>
        <div class="value" id="temperature">--.- C</div>
      </article>

      <article class="panel">
        <div class="label">습도</div>
        <div class="value" id="humidity">--.- %</div>
      </article>

      <article class="panel wide">
        <div class="label">장치 상태</div>
        <div class="status-row">
          <span class="pill" id="presence">재실 --</span>
          <span class="pill" id="aircon">에어컨 --</span>
          <span class="pill" id="humidifier">가습기 --</span>
          <span class="pill" id="autoControl">자동 제어 --</span>
          <span class="pill auto" id="threshold">기준 --</span>
        </div>
      </article>

      <article class="panel wide">
        <div class="label">원격 제어</div>
        <div class="controls">
          <button data-command="aircon_on">에어컨 ON</button>
          <button class="danger" data-command="aircon_off">에어컨 OFF</button>
          <button class="secondary" data-command="aircon_toggle">에어컨 버튼 누르기</button>
          <button data-command="humidifier_on">가습기 ON</button>
          <button class="danger" data-command="humidifier_off">가습기 OFF</button>
          <button class="secondary" data-command="auto_on">자동 제어 ON</button>
          <button class="warn" data-command="auto_off">자동 제어 OFF</button>
        </div>
        <div class="message" id="message"></div>
      </article>

      <article class="panel wide">
        <div class="label">자동 제어 온도</div>
        <div class="thresholds">
          <label class="field">
            <span>ON 기준</span>
            <input id="tempOnInput" type="number" step="0.5" min="0" max="50">
          </label>
          <label class="field">
            <span>OFF 기준</span>
            <input id="tempOffInput" type="number" step="0.5" min="0" max="50">
          </label>
          <button class="secondary" id="saveThresholds">저장</button>
        </div>
      </article>

      <article class="panel wide">
        <div class="label">마지막 아두이노 메시지</div>
        <pre id="lastLine">없음</pre>
      </article>
    </section>
  </main>

  <script>
    const tokenFromUrl = new URLSearchParams(location.search).get("token");
    if (tokenFromUrl) {
      localStorage.setItem("homeControlToken", tokenFromUrl);
      history.replaceState(null, "", location.pathname);
    }

    let controlToken = localStorage.getItem("homeControlToken") || "";
    const tokenPanel = document.getElementById("tokenPanel");
    const tokenInput = document.getElementById("tokenInput");
    const saveToken = document.getElementById("saveToken");
    const message = document.getElementById("message");
    const tempOnInput = document.getElementById("tempOnInput");
    const tempOffInput = document.getElementById("tempOffInput");
    const saveThresholds = document.getElementById("saveThresholds");

    tokenInput.value = controlToken;
    saveToken.addEventListener("click", () => {
      controlToken = tokenInput.value.trim();
      localStorage.setItem("homeControlToken", controlToken);
      refreshStatus();
    });

    function headers() {
      return controlToken ? {"X-Control-Token": controlToken} : {};
    }

    async function requestJson(path, options = {}) {
      const response = await fetch(path, {
        ...options,
        headers: {
          ...(options.headers || {}),
          ...headers(),
        },
      });

      if (response.status === 401) {
        tokenPanel.classList.add("visible");
        throw new Error("토큰 확인 필요");
      }

      if (!response.ok) {
        throw new Error(`요청 실패: ${response.status}`);
      }

      tokenPanel.classList.remove("visible");
      return response.json();
    }

    function setPill(element, label, isOn, extraClass = "") {
      element.textContent = `${label} ${isOn ? "ON" : "OFF"}`;
      element.className = `pill ${isOn ? "on" : "off"} ${extraClass}`.trim();
    }

    function renderPresence(status) {
      const element = document.getElementById("presence");

      if (!status.presence_enabled) {
        element.textContent = "재실 감지 OFF";
        element.className = "pill";
        return;
      }

      if (status.is_home) {
        element.textContent = "재실 감지 ON";
        element.className = "pill on";
        return;
      }

      if (status.presence_state === "away") {
        element.textContent = "외출";
        element.className = "pill off";
        return;
      }

      if (status.presence_missing_seconds !== null) {
        element.textContent =
          `미감지 ${status.presence_missing_seconds}/${status.away_after_seconds}초`;
      } else {
        element.textContent = "재실 확인 중";
      }
      element.className = "pill auto";
    }

    function render(status) {
      document.getElementById("temperature").textContent =
        status.temperature === null ? "--.- C" : `${status.temperature.toFixed(1)} C`;
      document.getElementById("humidity").textContent =
        status.humidity === null ? "--.- %" : `${status.humidity.toFixed(1)} %`;

      renderPresence(status);
      setPill(document.getElementById("aircon"), "에어컨", status.aircon_on);
      setPill(document.getElementById("humidifier"), "가습기", status.humidifier_on);
      setPill(document.getElementById("autoControl"), "자동 제어", status.auto_control, "auto");
      document.getElementById("threshold").textContent =
        `기준 ON ${status.temp_on.toFixed(1)} C / OFF ${status.temp_off.toFixed(1)} C`;

      if (document.activeElement !== tempOnInput) {
        tempOnInput.value = status.temp_on.toFixed(1);
      }
      if (document.activeElement !== tempOffInput) {
        tempOffInput.value = status.temp_off.toFixed(1);
      }

      document.getElementById("lastLine").textContent = status.last_arduino_line || "없음";
      document.getElementById("updated").textContent =
        status.last_sensor_age === null ? "온습도 수신 전" : `${status.last_sensor_age}초 전 업데이트`;
    }

    async function refreshStatus() {
      try {
        const data = await requestJson("/api/status");
        render(data);
      } catch (error) {
        document.getElementById("updated").textContent = error.message;
      }
    }

    async function sendCommand(command) {
      message.textContent = "명령 전송 중";
      try {
        const data = await requestJson("/api/command", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({command}),
        });
        render(data.status);
        message.textContent = data.message;
      } catch (error) {
        message.textContent = error.message;
      }
    }

    async function saveAutoThresholds() {
      message.textContent = "온도 기준 저장 중";
      try {
        const data = await requestJson("/api/command", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({
            command: "set_thresholds",
            temp_on: Number(tempOnInput.value),
            temp_off: Number(tempOffInput.value),
          }),
        });
        render(data.status);
        message.textContent = data.message;
      } catch (error) {
        message.textContent = error.message;
      }
    }

    document.querySelectorAll("[data-command]").forEach((button) => {
      button.addEventListener("click", () => sendCommand(button.dataset.command));
    });
    saveThresholds.addEventListener("click", saveAutoThresholds);

    refreshStatus();
    setInterval(refreshStatus, 3000);
  </script>
</body>
</html>
"""


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


def set_humidifier(target_on):
    command = HUMIDIFIER_ON_COMMAND if target_on else HUMIDIFIER_OFF_COMMAND
    send_arduino_command(command)

    with state_lock:
        state["humidifier_on"] = target_on

    return f"가습기 {'ON' if target_on else 'OFF'} 명령을 보냈습니다."


def set_auto_control(target_on):
    with state_lock:
        state["auto_control"] = target_on

    return f"자동 제어를 {'켰습니다' if target_on else '껐습니다'}."


def parse_threshold(value, label):
    try:
        threshold = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} 온도를 숫자로 입력하세요.")

    if threshold < 0 or threshold > 50:
        raise ValueError(f"{label} 온도는 0~50 C 범위로 입력하세요.")

    return round(threshold, 1)


def set_auto_thresholds(temp_on, temp_off):
    temp_on = parse_threshold(temp_on, "ON 기준")
    temp_off = parse_threshold(temp_off, "OFF 기준")

    if temp_on <= temp_off:
        raise ValueError("ON 기준 온도는 OFF 기준 온도보다 높아야 합니다.")

    with state_lock:
        state["temp_on"] = temp_on
        state["temp_off"] = temp_off

    save_config({"temp_on": temp_on, "temp_off": temp_off})
    return f"자동 제어 기준을 ON {temp_on:.1f} C / OFF {temp_off:.1f} C로 저장했습니다."


def turn_devices_off_for_away():
    with state_lock:
        aircon_on = state["aircon_on"]
        humidifier_on = state["humidifier_on"]

    if aircon_on:
        set_aircon(False)

    if humidifier_on:
        set_humidifier(False)


def maybe_auto_control(temperature):
    with state_lock:
        auto_control = state["auto_control"]
        aircon_on = state["aircon_on"]
        temp_on = state["temp_on"]
        temp_off = state["temp_off"]
        presence_enabled = state["presence_enabled"]
        is_home = state["is_home"]

    if not auto_control:
        return

    if presence_enabled and not is_home:
        return

    if temperature >= temp_on and not aircon_on:
        print("자동 제어: 온도가 높아 에어컨 ON")
        set_aircon(True)
    elif temperature <= temp_off and aircon_on:
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
        except SERIAL_READ_ERRORS as exc:
            print("시리얼 읽기 오류:", exc)
            time.sleep(2)
            continue

        if line:
            handle_arduino_line(line)


def run_bluetoothctl_info():
    if not has_real_mac(PHONE_BLUETOOTH_MAC):
        return ""

    result = subprocess.run(
        ["bluetoothctl", "info", PHONE_BLUETOOTH_MAC],
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
    )
    return f"{result.stdout}\n{result.stderr}"


def scan_bluetooth_devices():
    process = subprocess.Popen(
        ["bluetoothctl"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        process.stdin.write("power on\nscan on\n")
        process.stdin.flush()
        stop_event.wait(BLUETOOTH_SCAN_SECONDS)

        process.stdin.write("devices\nscan off\nquit\n")
        process.stdin.flush()

        stdout, stderr = process.communicate(timeout=8)
    except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
        process.kill()
        stdout, stderr = process.communicate()

    return f"{stdout}\n{stderr}"


def is_phone_detected_by_bluetooth():
    if not PRESENCE_TARGET_CONFIGURED:
        return None, "PHONE_BLUETOOTH_MAC 또는 PHONE_NAME_KEYWORD 설정이 필요합니다."

    if platform.system() != "Linux":
        return None, "블루투스 재실 감지는 라즈베리파이 Linux에서 활성화됩니다."

    mac = PHONE_BLUETOOTH_MAC.lower()
    name = PHONE_NAME_KEYWORD.lower().strip()

    try:
        info_output = run_bluetoothctl_info().lower()
        if "connected: yes" in info_output:
            return True, ""

        scan_output = scan_bluetooth_devices().lower()
    except FileNotFoundError:
        return None, "bluetoothctl을 찾을 수 없습니다. BlueZ 설치가 필요합니다."
    except subprocess.TimeoutExpired:
        return None, "bluetoothctl 응답 시간이 초과되었습니다."
    except OSError as exc:
        return None, f"블루투스 확인 오류: {exc}"

    if has_real_mac(mac) and mac in scan_output:
        return True, ""

    if name and name in scan_output:
        return True, ""

    return False, ""


def update_presence_state(detected, error_message=""):
    now = time.time()
    should_turn_off = False
    transition_message = None

    if detected is None:
        with state_lock:
            state["last_presence_check_at"] = now
            state["last_presence_error"] = error_message
            state["phone_detected"] = False
            if state["presence_state"] == "unknown":
                state["presence_state"] = "checking"
        return

    with state_lock:
        previous_state = state["presence_state"]
        state["last_presence_check_at"] = now
        state["last_presence_error"] = error_message
        state["phone_detected"] = detected

        if detected:
            state["is_home"] = True
            state["presence_state"] = "home"
            state["last_phone_seen_at"] = now
            state["presence_missing_since"] = None

            if previous_state != "home":
                transition_message = "휴대폰 감지됨: 재실 상태"
        else:
            if state["presence_missing_since"] is None:
                state["presence_missing_since"] = now

            missing_seconds = now - state["presence_missing_since"]

            if missing_seconds >= state["away_after_seconds"]:
                if previous_state != "away":
                    should_turn_off = True
                    transition_message = "휴대폰 5분 미감지: 외출 상태, 장치 OFF"

                state["is_home"] = False
                state["presence_state"] = "away"
            elif state["is_home"]:
                state["presence_state"] = "missing"
            else:
                state["presence_state"] = "checking"

    if transition_message:
        print(transition_message)

    if should_turn_off:
        turn_devices_off_for_away()


def presence_monitor():
    if not PRESENCE_ENABLED:
        return

    print(f"블루투스 재실 감지 시작: {AWAY_AFTER_SECONDS}초 미감지 시 외출 처리")

    while not stop_event.is_set():
        detected, error_message = is_phone_detected_by_bluetooth()
        update_presence_state(detected, error_message)
        stop_event.wait(BLUETOOTH_SCAN_INTERVAL)


def status_snapshot():
    with state_lock:
        snapshot = dict(state)

    if snapshot["last_sensor_at"] is None:
        snapshot["last_sensor_age"] = None
    else:
        snapshot["last_sensor_age"] = int(time.time() - snapshot["last_sensor_at"])

    if snapshot["last_phone_seen_at"] is None:
        snapshot["last_phone_seen_age"] = None
    else:
        snapshot["last_phone_seen_age"] = int(time.time() - snapshot["last_phone_seen_at"])

    if snapshot["last_presence_check_at"] is None:
        snapshot["last_presence_check_age"] = None
    else:
        snapshot["last_presence_check_age"] = int(time.time() - snapshot["last_presence_check_at"])

    if snapshot["presence_missing_since"] is None:
        snapshot["presence_missing_seconds"] = None
    else:
        snapshot["presence_missing_seconds"] = int(time.time() - snapshot["presence_missing_since"])

    return snapshot


def run_command(payload):
    if isinstance(payload, dict):
        command = payload.get("command", "")
    else:
        command = payload

    if command == "aircon_on":
        return set_aircon(True)
    if command == "aircon_off":
        return set_aircon(False)
    if command == "aircon_toggle":
        with state_lock:
            target_on = not state["aircon_on"]
        return set_aircon(target_on)
    if command == "humidifier_on":
        return set_humidifier(True)
    if command == "humidifier_off":
        return set_humidifier(False)
    if command == "auto_on":
        return set_auto_control(True)
    if command == "auto_off":
        return set_auto_control(False)
    if command == "set_thresholds":
        if not isinstance(payload, dict):
            raise ValueError("온도 기준 데이터가 필요합니다.")

        return set_auto_thresholds(payload.get("temp_on"), payload.get("temp_off"))

    raise ValueError("알 수 없는 명령입니다.")


class HomeControlHandler(BaseHTTPRequestHandler):
    server_version = "HomeControl/1.0"

    def log_message(self, format, *args):
        tailscale_user = self.headers.get("Tailscale-User-Login", "-")
        print(f"{self.client_address[0]} {tailscale_user} - {format % args}")

    def is_authorized(self):
        if not CONTROL_TOKEN:
            return True

        parsed = urlparse(self.path)
        query_token = parse_qs(parsed.query).get("token", [""])[0]
        header_token = self.headers.get("X-Control-Token", "")
        return CONTROL_TOKEN in (query_token, header_token)

    def write_bytes(self, body, content_type, status=HTTPStatus.OK):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def write_json(self, payload, status=HTTPStatus.OK):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.write_bytes(body, "application/json; charset=utf-8", status)

    def write_unauthorized(self):
        self.write_json({"ok": False, "message": "CONTROL_TOKEN 확인이 필요합니다."}, HTTPStatus.UNAUTHORIZED)

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/":
            self.write_bytes(INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
            return

        if parsed.path == "/api/status":
            if not self.is_authorized():
                self.write_unauthorized()
                return

            self.write_json(status_snapshot())
            return

        self.write_json({"ok": False, "message": "Not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self):
        parsed = urlparse(self.path)

        if parsed.path != "/api/command":
            self.write_json({"ok": False, "message": "Not found"}, HTTPStatus.NOT_FOUND)
            return

        if not self.is_authorized():
            self.write_unauthorized()
            return

        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length).decode("utf-8")

        try:
            payload = json.loads(body or "{}")
            message = run_command(payload)
        except (json.JSONDecodeError, ValueError) as exc:
            self.write_json({"ok": False, "message": str(exc)}, HTTPStatus.BAD_REQUEST)
            return

        self.write_json({"ok": True, "message": message, "status": status_snapshot()})


def main():
    global arduino

    if MOCK_ARDUINO:
        arduino = MockArduino()
        print("모의 아두이노 모드로 실행합니다.")
    else:
        if serial is None:
            raise SystemExit("pyserial이 필요합니다. 먼저 `python -m pip install pyserial`을 실행하세요.")

        arduino = serial.Serial(ARDUINO_PORT, ARDUINO_BAUD, timeout=1)
        print(f"아두이노 연결됨: {ARDUINO_PORT}, {ARDUINO_BAUD}bps")
        time.sleep(3)

    reader_thread = threading.Thread(target=serial_reader, daemon=True)
    reader_thread.start()

    if PRESENCE_ENABLED:
        presence_thread = threading.Thread(target=presence_monitor, daemon=True)
        presence_thread.start()
    else:
        print("블루투스 재실 감지는 비활성화되어 있습니다.")

    httpd = ThreadingHTTPServer((HOST, WEB_PORT), HomeControlHandler)
    print(f"웹 서버 시작: http://{HOST}:{WEB_PORT}")
    if CONTROL_TOKEN:
        print("CONTROL_TOKEN 보호가 켜져 있습니다.")
    else:
        print("CONTROL_TOKEN이 비어 있습니다. Tailscale ACL로 접근 대상을 제한하세요.")

    httpd.serve_forever()


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
