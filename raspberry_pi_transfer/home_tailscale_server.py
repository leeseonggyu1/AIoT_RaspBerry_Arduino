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

LIGHT_SENSOR_ENABLED = env_or_config_bool("LIGHT_SENSOR_ENABLED", config, "light_sensor_enabled", True)
LIGHT_DARK_THRESHOLD = env_or_config_int("LIGHT_DARK_THRESHOLD", config, "light_dark_threshold", 250)
LIGHT_DARK_WHEN_LOW = env_or_config_bool("LIGHT_DARK_WHEN_LOW", config, "light_dark_when_low", True)
SLEEP_SUGGEST_AFTER_SECONDS = env_or_config_int(
    "SLEEP_SUGGEST_AFTER_SECONDS",
    config,
    "sleep_suggest_after_seconds",
    300,
)
SLEEP_SUGGEST_START = env_or_config_str("SLEEP_SUGGEST_START", config, "sleep_suggest_start", "22:00")
SLEEP_SUGGEST_END = env_or_config_str("SLEEP_SUGGEST_END", config, "sleep_suggest_end", "03:00")
SLEEP_SUGGEST_DISMISS_SECONDS = env_or_config_int(
    "SLEEP_SUGGEST_DISMISS_SECONDS",
    config,
    "sleep_suggest_dismiss_seconds",
    3600,
)

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
    "light_sensor_enabled": LIGHT_SENSOR_ENABLED,
    "light_raw": None,
    "light_level_percent": None,
    "is_dark": False,
    "dark_since_at": None,
    "light_dark_threshold": LIGHT_DARK_THRESHOLD,
    "light_dark_when_low": LIGHT_DARK_WHEN_LOW,
    "sleep_mode": False,
    "sleep_suggestion_pending": False,
    "sleep_suggestion_ignored_until": None,
    "sleep_suggest_after_seconds": SLEEP_SUGGEST_AFTER_SECONDS,
    "sleep_suggest_start": SLEEP_SUGGEST_START,
    "sleep_suggest_end": SLEEP_SUGGEST_END,
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
        light_raw = 180 if (elapsed // 20) % 2 else 620
        humidifier = "ON" if self.humidifier_on else "OFF"
        time.sleep(1)
        return (
            f"온도: {temperature:.2f} C, 습도: {humidity:.2f} %, "
            f"조도: {light_raw}, 가습기: {humidifier}\n"
        ).encode("utf-8")

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
      --bg: #f3f6fa;
      --panel: #ffffff;
      --panel-soft: #f8fafc;
      --text: #101828;
      --muted: #667085;
      --line: #e2e8f0;
      --blue: #2563eb;
      --blue-soft: #eff6ff;
      --green: #059669;
      --green-soft: #ecfdf5;
      --red: #dc2626;
      --red-soft: #fef2f2;
      --amber: #b7791f;
      --neutral: #94a3b8;
      --shadow: 0 18px 40px rgba(15, 23, 42, 0.1);
      --shadow-soft: 0 1px 2px rgba(15, 23, 42, 0.04), 0 10px 28px rgba(15, 23, 42, 0.06);
    }

    * {
      box-sizing: border-box;
    }

    body {
      margin: 0;
      background:
        radial-gradient(circle at 50% -20%, rgba(37, 99, 235, 0.08), transparent 34%),
        linear-gradient(180deg, #fbfdff 0%, var(--bg) 100%);
      color: var(--text);
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 15px;
    }

    main {
      width: min(680px, 100%);
      margin: 0 auto;
      padding: 16px 14px 34px;
    }

    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      padding: 0 0 12px;
    }

    .top-status {
      display: inline-flex;
      align-items: center;
      min-height: 32px;
      padding: 6px 12px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: rgba(255, 255, 255, 0.82);
      color: var(--muted);
      font-size: 13px;
      font-weight: 700;
      white-space: nowrap;
      box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
      backdrop-filter: blur(10px);
    }

    .presence-chip {
      color: #475569;
      background: var(--panel);
    }

    .presence-chip.home {
      color: var(--green);
      border-color: #b8dccb;
      background: #f0fbf6;
    }

    .presence-chip.away {
      color: var(--red);
      border-color: #efc5c5;
      background: #fff5f5;
    }

    .presence-chip.checking {
      color: var(--blue);
      border-color: #bdd0ff;
      background: #f2f6ff;
    }

    .presence-chip.disabled {
      color: var(--muted);
      background: rgba(248, 250, 252, 0.86);
    }

    .updated::before {
      content: "";
      width: 7px;
      height: 7px;
      margin-right: 7px;
      border-radius: 50%;
      background: var(--green);
    }

    .presence-chip::before {
      content: "";
      width: 7px;
      height: 7px;
      margin-right: 7px;
      border-radius: 50%;
      background: currentColor;
    }

    .grid {
      display: none;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
    }

    .simple {
      display: grid;
      gap: 12px;
    }

    .simple-readout {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }

    .readout-item {
      min-height: 184px;
      padding: 15px 10px 16px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: rgba(255, 255, 255, 0.94);
      box-shadow: var(--shadow-soft);
      display: grid;
      justify-items: center;
      align-content: center;
      gap: 10px;
    }

    .sensor-label {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      font-weight: 750;
    }

    .sensor-svg {
      width: 16px;
      height: 16px;
      color: #64748b;
      stroke: currentColor;
      stroke-width: 2;
      stroke-linecap: round;
      stroke-linejoin: round;
      fill: none;
      flex: 0 0 auto;
    }

    .sensor-svg.drop {
      color: var(--blue);
    }

    .readout-value {
      margin-top: 5px;
      font-size: 24px;
      font-weight: 760;
      letter-spacing: 0;
      line-height: 1.1;
    }

    .gauge {
      --gauge-color: var(--green);
      --gauge-angle: 90deg;
      --gauge-track: #e8edf5;
      width: min(230px, 100%);
      aspect-ratio: 2 / 1;
      border-radius: 999px 999px 0 0;
      display: grid;
      place-items: end center;
      padding-bottom: 10px;
      background: conic-gradient(
        from 270deg at 50% 100%,
        var(--gauge-color) 0deg var(--gauge-angle),
        var(--gauge-track) var(--gauge-angle) 180deg,
        transparent 180deg 360deg
      );
      position: relative;
      overflow: hidden;
      filter: drop-shadow(0 10px 14px rgba(15, 23, 42, 0.07));
    }

    .gauge::before {
      content: "";
      position: absolute;
      left: clamp(16px, 10%, 26px);
      right: clamp(16px, 10%, 26px);
      top: clamp(16px, 18%, 26px);
      bottom: 0;
      border-radius: 999px 999px 0 0;
      background: #fff;
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.9);
    }

    .gauge-value {
      position: relative;
      z-index: 1;
      color: var(--gauge-color);
      font-size: clamp(22px, 5vw, 34px);
      font-weight: 800;
      letter-spacing: 0;
      line-height: 1;
      text-shadow: 0 1px 0 rgba(255, 255, 255, 0.8);
    }

    .gauge-caption {
      color: var(--muted);
      font-size: 13px;
      line-height: 1.25;
      text-align: center;
      min-height: 30px;
    }

    .simple-controls {
      display: grid;
      gap: 14px;
    }

    .device-controls,
    .mode-controls {
      display: grid;
      gap: 10px;
    }

    .device-controls {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }

    .mode-card {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 88px;
      gap: 10px;
      align-items: stretch;
    }

    .mode-label {
      min-height: 72px;
      padding: 13px 42px 12px 16px;
      border: 1px solid var(--line);
      color: var(--text);
      background: rgba(255, 255, 255, 0.94);
      text-align: left;
      box-shadow: var(--shadow-soft);
      position: relative;
    }

    .mode-label::after {
      content: "";
      position: absolute;
      right: 18px;
      top: 50%;
      width: 9px;
      height: 9px;
      border-right: 2px solid #8a94a6;
      border-bottom: 2px solid #8a94a6;
      transform: translateY(-65%) rotate(45deg);
      transition: transform 0.16s ease;
    }

    .mode-label.open::after {
      transform: translateY(-20%) rotate(225deg);
    }

    .mode-title {
      display: block;
      font-size: 18px;
      font-weight: 800;
      line-height: 1.15;
    }

    .mode-subtext {
      display: block;
      margin-top: 6px;
      color: var(--muted);
      font-size: 13px;
      font-weight: 700;
      line-height: 1.15;
    }

    .simple-toggle {
      min-height: 88px;
      padding: 14px 13px 14px 15px;
      border: 1px solid var(--line);
      color: var(--text);
      background: rgba(255, 255, 255, 0.94);
      display: grid;
      grid-template-columns: minmax(0, 1fr) 44px;
      align-items: center;
      gap: 12px;
      text-align: left;
      box-shadow: var(--shadow-soft);
      position: relative;
      overflow: hidden;
    }

    .simple-toggle::before {
      content: "";
      position: absolute;
      left: 0;
      top: 0;
      bottom: 0;
      width: 4px;
      background: #cbd5e1;
    }

    .simple-toggle.active {
      border-color: #a9c1ff;
      background: var(--blue-soft);
    }

    .simple-toggle.active::before {
      background: var(--blue);
    }

    .simple-toggle.inactive {
      border-color: var(--line);
      background: rgba(255, 255, 255, 0.94);
    }

    .simple-toggle:disabled {
      cursor: default;
      opacity: 0.68;
    }

    .device-copy {
      display: grid;
      gap: 7px;
      min-width: 0;
    }

    .device-name {
      font-size: 18px;
      font-weight: 850;
      line-height: 1.1;
    }

    .device-status {
      width: fit-content;
      padding: 5px 9px;
      border-radius: 999px;
      color: #64748b;
      background: #f1f5f9;
      font-size: 12px;
      font-weight: 800;
      line-height: 1;
    }

    .simple-toggle.active .device-status {
      color: #1d4ed8;
      background: #dbeafe;
    }

    .device-action {
      width: 44px;
      height: 44px;
      border-radius: 50%;
      display: grid;
      place-items: center;
      background: #f8fafc;
      box-shadow: inset 0 0 0 1px var(--line);
      position: relative;
    }

    .device-action::before {
      content: "";
      width: 18px;
      height: 18px;
      border: 3px solid #94a3b8;
      border-top-color: transparent;
      border-radius: 50%;
    }

    .device-action::after {
      content: "";
      position: absolute;
      top: 11px;
      width: 3px;
      height: 12px;
      border-radius: 2px;
      background: #94a3b8;
    }

    .simple-toggle.active .device-action {
      background: var(--blue);
      box-shadow: 0 8px 18px rgba(37, 99, 235, 0.22);
    }

    .simple-toggle.active .device-action::before {
      border-color: #fff;
      border-top-color: transparent;
    }

    .simple-toggle.active .device-action::after {
      background: #fff;
    }

    .simple-switch {
      min-height: 72px;
      padding: 0 92px 0 16px;
      border: 1px solid var(--line);
      color: var(--text);
      background: rgba(255, 255, 255, 0.94);
      font-size: 18px;
      font-weight: 800;
      text-align: left;
      position: relative;
      box-shadow: var(--shadow-soft);
    }

    .simple-switch::before {
      content: "";
      position: absolute;
      right: 16px;
      top: 50%;
      width: 58px;
      height: 32px;
      border-radius: 16px;
      background: #cbd5e1;
      transform: translateY(-50%);
      transition: background 0.16s ease;
    }

    .simple-switch::after {
      content: "";
      position: absolute;
      right: 42px;
      top: 50%;
      width: 24px;
      height: 24px;
      border-radius: 50%;
      background: #fff;
      transform: translateY(-50%);
      transition: right 0.16s ease;
    }

    .simple-switch.active::before {
      background: var(--blue);
    }

    .simple-switch.active::after {
      right: 20px;
    }

    .switch-only {
      min-height: 72px;
      padding: 0;
      font-size: 0;
      color: transparent;
    }

    .switch-only::before {
      right: 15px;
    }

    .switch-only::after {
      right: 41px;
    }

    .switch-only.active::after {
      right: 19px;
    }

    .auto-preset-drawer {
      display: none;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
      padding: 10px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: rgba(255, 255, 255, 0.76);
      box-shadow: 0 1px 2px rgba(15, 23, 42, 0.05);
      backdrop-filter: blur(10px);
    }

    .auto-preset-drawer.visible {
      display: grid;
    }

    .preset-option {
      min-height: 62px;
      padding: 10px 11px;
      border: 1px solid var(--line);
      color: var(--text);
      background: rgba(255, 255, 255, 0.9);
      text-align: left;
      box-shadow: none;
    }

    .preset-option span {
      display: block;
      font-size: 15px;
      font-weight: 800;
      line-height: 1.1;
    }

    .preset-option small {
      display: block;
      margin-top: 6px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 650;
      line-height: 1.15;
    }

    .preset-option.active {
      border-color: #9db8fb;
      background: #eff5ff;
      color: #1d4ed8;
    }

    .simple-sleep-prompt {
      display: none;
      grid-template-columns: 1fr auto auto;
      align-items: center;
      gap: 8px;
      min-height: 48px;
      padding: 10px 12px;
      border: 1px solid #f2c94c;
      border-radius: 8px;
      background: #fff8dd;
      color: #5f4100;
      font-size: 14px;
      font-weight: 700;
      box-shadow: var(--shadow-soft);
    }

    .simple-sleep-prompt.visible {
      display: grid;
    }

    .simple-sleep-prompt button {
      min-height: 34px;
      padding: 0 12px;
      font-size: 13px;
    }

    .simple-auto-temp {
      display: none;
      gap: 14px;
      padding: 15px 14px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: rgba(255, 255, 255, 0.94);
      box-shadow: var(--shadow-soft);
    }

    .simple-auto-temp.visible {
      display: grid;
    }

    .simple-auto-temp .label {
      margin-bottom: 4px;
    }

    .temp-slider-row {
      display: grid;
      gap: 8px;
    }

    .slider-head {
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 10px;
      font-size: 14px;
      color: var(--muted);
      line-height: 1.2;
    }

    .slider-value {
      color: var(--text);
      font-size: 21px;
      font-weight: 800;
      letter-spacing: 0;
      white-space: nowrap;
    }

    .temp-range {
      width: 100%;
      height: 24px;
      appearance: none;
      -webkit-appearance: none;
      background: transparent;
      accent-color: var(--blue);
      cursor: pointer;
    }

    .temp-range::-webkit-slider-runnable-track {
      height: 8px;
      border-radius: 999px;
      background: #e1e7f0;
      box-shadow: inset 0 1px 2px rgba(15, 23, 42, 0.1);
    }

    .temp-range::-webkit-slider-thumb {
      -webkit-appearance: none;
      margin-top: -7px;
      width: 22px;
      height: 22px;
      border: 3px solid #fff;
      border-radius: 50%;
      background: var(--blue);
      box-shadow: 0 4px 10px rgba(37, 99, 235, 0.28);
    }

    .temp-range::-moz-range-track {
      height: 8px;
      border-radius: 999px;
      background: #e1e7f0;
      box-shadow: inset 0 1px 2px rgba(15, 23, 42, 0.1);
    }

    .temp-range::-moz-range-thumb {
      width: 18px;
      height: 18px;
      border: 3px solid #fff;
      border-radius: 50%;
      background: var(--blue);
      box-shadow: 0 4px 10px rgba(37, 99, 235, 0.28);
    }

    .simple-auto-temp input[type="number"] {
      width: 100%;
      height: 42px;
      font-size: 18px;
      font-weight: 760;
      text-align: center;
    }

    .simple-auto-temp button {
      height: 42px;
      padding: 0 16px;
      white-space: nowrap;
    }

    .simple-threshold-summary {
      color: var(--muted);
      font-size: 13px;
      line-height: 1.3;
    }

    .simple-message {
      min-height: 40px;
      padding: 10px 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      color: var(--muted);
      background: rgba(255, 255, 255, 0.72);
      box-shadow: 0 1px 2px rgba(15, 23, 42, 0.05);
      font-size: 14px;
    }

    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 15px;
      box-shadow: var(--shadow);
    }

    .panel:not(.wide) {
      min-height: 116px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
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
      font-size: 34px;
      font-weight: 700;
      line-height: 1.1;
      letter-spacing: 0;
    }

    .status-row {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 10px;
    }

    .pill {
      display: inline-flex;
      align-items: center;
      min-height: 32px;
      padding: 5px 10px;
      border-radius: 8px;
      border: 1px solid var(--line);
      font-size: 14px;
      background: #fff;
      line-height: 1.25;
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
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
    }

    .thresholds {
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) 120px;
      gap: 10px;
      align-items: end;
    }

    .sleep-prompt {
      display: none;
      border-color: #b8cafc;
      background: #f4f7ff;
      box-shadow: none;
    }

    .sleep-prompt.visible {
      display: block;
    }

    .prompt-actions {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
      margin-top: 12px;
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
      padding: 9px 12px;
      line-height: 1.25;
      white-space: normal;
      transition: border-color 0.16s ease, box-shadow 0.16s ease, transform 0.16s ease, background 0.16s ease;
      -webkit-tap-highlight-color: transparent;
    }

    button:active {
      transform: translateY(1px);
    }

    button:focus-visible,
    input:focus-visible {
      outline: 3px solid rgba(37, 99, 235, 0.18);
      outline-offset: 2px;
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
      padding: 10px 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      color: var(--muted);
      font-size: 14px;
      min-height: 20px;
      background: var(--panel-soft);
    }

    .token-panel {
      display: none;
      grid-template-columns: 1fr auto;
      gap: 8px;
      margin-bottom: 12px;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
    }

    .token-panel.visible {
      display: grid;
    }

    pre {
      white-space: pre-wrap;
      word-break: break-word;
      margin: 0;
      color: var(--muted);
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel-soft);
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 13px;
    }

    @media (max-width: 900px) {
      .grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }

      .controls {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }
    }

    @media (max-width: 680px) {
      main {
        padding: 14px;
      }

      header {
        align-items: center;
        flex-direction: row;
        gap: 8px;
      }

      .controls, .thresholds, .prompt-actions, .auto-preset-drawer, .simple-sleep-prompt, .simple-auto-temp {
        grid-template-columns: 1fr;
      }

      .value {
        font-size: 30px;
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
      <div class="top-status presence-chip disabled" id="simplePresence">재실 감지 OFF</div>
      <div class="top-status updated" id="updated">연결 중</div>
    </header>

    <section class="token-panel" id="tokenPanel">
      <input id="tokenInput" type="password" autocomplete="current-password" placeholder="CONTROL_TOKEN">
      <button class="secondary" id="saveToken">저장</button>
    </section>

    <section class="simple">
      <div class="simple-readout">
        <div class="readout-item">
          <div class="label sensor-label">
            <svg class="sensor-svg thermo" aria-hidden="true" viewBox="0 0 24 24">
              <path d="M14 14.8V5a4 4 0 0 0-8 0v9.8a6 6 0 1 0 8 0Z"></path>
              <path d="M10 6v9"></path>
            </svg>
            <span>온도</span>
          </div>
          <div class="gauge" id="temperatureGauge">
            <div class="gauge-value" id="simpleTemperature">--.- C</div>
          </div>
          <div class="gauge-caption" id="temperatureCaption">기준 확인 중</div>
        </div>
        <div class="readout-item">
          <div class="label sensor-label">
            <svg class="sensor-svg drop" aria-hidden="true" viewBox="0 0 24 24">
              <path d="M12 3.5s6 6.3 6 10.6a6 6 0 0 1-12 0C6 9.8 12 3.5 12 3.5Z"></path>
            </svg>
            <span>습도</span>
          </div>
          <div class="gauge" id="humidityGauge">
            <div class="gauge-value" id="simpleHumidity">--.- %</div>
          </div>
          <div class="gauge-caption" id="humidityCaption">권장 구간 확인 중</div>
        </div>
      </div>

      <div class="simple-controls">
        <div class="device-controls">
          <button class="simple-toggle inactive" id="airconToggle" type="button">에어컨 꺼짐</button>
          <button class="simple-toggle inactive" id="humidifierToggle" type="button">가습기 꺼짐</button>
        </div>
        <div class="mode-controls">
          <div class="mode-card">
            <button class="mode-label" id="autoPresetToggle" type="button" aria-expanded="false" aria-controls="autoPresetDrawer">
              <span class="mode-title">자동제어</span>
              <span class="mode-subtext" id="autoPresetLabel">쾌적하게</span>
            </button>
            <button class="simple-switch switch-only inactive" id="autoToggle" type="button" role="switch" aria-checked="false" aria-label="자동제어 OFF">OFF</button>
          </div>
          <button class="simple-switch inactive" id="sleepToggle" type="button" role="switch" aria-checked="false">수면모드 OFF</button>
        </div>
      </div>

      <div class="auto-preset-drawer" id="autoPresetDrawer">
        <button class="preset-option" type="button" data-auto-preset="warm">
          <span>따뜻하게</span>
          <small>켜짐 29.0 C / 꺼짐 27.0 C</small>
        </button>
        <button class="preset-option" type="button" data-auto-preset="comfort">
          <span>쾌적하게</span>
          <small>켜짐 26.0 C / 꺼짐 24.0 C</small>
        </button>
        <button class="preset-option" type="button" data-auto-preset="cool">
          <span>시원하게</span>
          <small>켜짐 24.0 C / 꺼짐 22.0 C</small>
        </button>
        <button class="preset-option" type="button" data-auto-preset="manual">
          <span>수동조절</span>
          <small>직접 슬라이더 조정</small>
        </button>
      </div>

      <div class="simple-sleep-prompt" id="simpleSleepPrompt">
        <div>불이 꺼졌어요. 수면모드로 전환할까요?</div>
        <button data-command="sleep_on">ON</button>
        <button class="secondary" data-command="dismiss_sleep_suggestion">나중에</button>
      </div>

      <div class="simple-message" id="simpleMessage">대기 중</div>

      <div class="simple-auto-temp" id="simpleAutoTemp">
        <div>
          <div class="label">자동제어 온도</div>
          <div class="simple-threshold-summary" id="simpleThresholdSummary">기준 확인 중</div>
        </div>
        <div class="temp-slider-row">
          <div class="slider-head">
            <span>켜짐 온도</span>
            <span class="slider-value" id="simpleTempOnValue">--.- C</span>
          </div>
          <input class="temp-range" id="simpleTempOnRange" type="range" step="0.5" min="1" max="50">
        </div>
        <div class="temp-slider-row">
          <div class="slider-head">
            <span>꺼짐 온도</span>
            <span class="slider-value" id="simpleTempOffValue">--.- C</span>
          </div>
          <input class="temp-range" id="simpleTempOffRange" type="range" step="0.5" min="1" max="50">
        </div>
      </div>
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

      <article class="panel">
        <div class="label">조도</div>
        <div class="value" id="lightLevel">---</div>
      </article>

      <article class="panel wide">
        <div class="label">장치 상태</div>
        <div class="status-row">
          <span class="pill" id="presence">재실 --</span>
          <span class="pill" id="roomLight">방 상태 --</span>
          <span class="pill" id="sleepMode">수면 --</span>
          <span class="pill" id="aircon">에어컨 --</span>
          <span class="pill" id="humidifier">가습기 --</span>
          <span class="pill" id="autoControl">자동 제어 --</span>
          <span class="pill auto" id="threshold">기준 --</span>
        </div>
      </article>

      <article class="panel wide sleep-prompt" id="sleepPrompt">
        <div class="label">수면모드 제안</div>
        <div>방이 어두운 상태입니다. 수면모드로 전환할까요?</div>
        <div class="prompt-actions">
          <button data-command="sleep_on">수면모드 ON</button>
          <button class="secondary" data-command="dismiss_sleep_suggestion">나중에</button>
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
          <button class="secondary" data-command="sleep_on">수면모드 ON</button>
          <button class="warn" data-command="sleep_off">수면모드 OFF</button>
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
    const simpleMessage = document.getElementById("simpleMessage");
    const simpleTemperature = document.getElementById("simpleTemperature");
    const simpleHumidity = document.getElementById("simpleHumidity");
    const temperatureGauge = document.getElementById("temperatureGauge");
    const humidityGauge = document.getElementById("humidityGauge");
    const temperatureCaption = document.getElementById("temperatureCaption");
    const humidityCaption = document.getElementById("humidityCaption");
    const simplePresence = document.getElementById("simplePresence");
    const airconToggle = document.getElementById("airconToggle");
    const humidifierToggle = document.getElementById("humidifierToggle");
    const autoToggle = document.getElementById("autoToggle");
    const autoPresetToggle = document.getElementById("autoPresetToggle");
    const autoPresetDrawer = document.getElementById("autoPresetDrawer");
    const autoPresetLabel = document.getElementById("autoPresetLabel");
    const autoPresetButtons = Array.from(document.querySelectorAll("[data-auto-preset]"));
    const sleepToggle = document.getElementById("sleepToggle");
    const simpleSleepPrompt = document.getElementById("simpleSleepPrompt");
    const simpleAutoTemp = document.getElementById("simpleAutoTemp");
    const simpleTempOnRange = document.getElementById("simpleTempOnRange");
    const simpleTempOffRange = document.getElementById("simpleTempOffRange");
    const simpleTempOnValue = document.getElementById("simpleTempOnValue");
    const simpleTempOffValue = document.getElementById("simpleTempOffValue");
    const simpleThresholdSummary = document.getElementById("simpleThresholdSummary");
    const AUTO_PRESETS = {
      warm: {label: "따뜻하게", tempOn: 29.0, tempOff: 27.0},
      comfort: {label: "쾌적하게", tempOn: 26.0, tempOff: 24.0},
      cool: {label: "시원하게", tempOn: 24.0, tempOff: 22.0},
      manual: {label: "수동조절"},
    };
    let currentStatus = null;
    let simpleThresholdDirty = false;
    let simpleThresholdSaveTimer = null;
    let autoPresetMode = localStorage.getItem("autoPresetMode") || "comfort";
    let autoPresetDrawerOpen = false;

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

    function setSimpleToggle(button, label, isOn, onText = "켜짐", offText = "꺼짐") {
      const stateText = isOn ? onText : offText;
      button.innerHTML = `
        <span class="device-copy">
          <span class="device-name">${label}</span>
          <span class="device-status">${stateText}</span>
        </span>
        <span class="device-action" aria-hidden="true"></span>
      `;
      button.className = `simple-toggle ${isOn ? "active" : "inactive"}`;
      button.setAttribute("aria-label", `${label} ${stateText}`);
    }

    function setSimpleSwitch(button, label, isOn) {
      button.textContent = `${label} ${isOn ? "ON" : "OFF"}`;
      button.className = `simple-switch ${isOn ? "active" : "inactive"}`;
      button.setAttribute("aria-checked", isOn ? "true" : "false");
    }

    function setSwitchOnly(button, label, isOn) {
      button.textContent = isOn ? "ON" : "OFF";
      button.className = `simple-switch switch-only ${isOn ? "active" : "inactive"}`;
      button.setAttribute("aria-checked", isOn ? "true" : "false");
      button.setAttribute("aria-label", `${label} ${isOn ? "ON" : "OFF"}`);
    }

    function nearlySame(a, b) {
      return Math.abs(Number(a) - Number(b)) < 0.05;
    }

    function detectAutoPreset(tempOn, tempOff) {
      for (const key of ["warm", "comfort", "cool"]) {
        const preset = AUTO_PRESETS[key];
        if (nearlySame(tempOn, preset.tempOn) && nearlySame(tempOff, preset.tempOff)) {
          return key;
        }
      }
      return "manual";
    }

    function updateAutoPresetUi(status = currentStatus) {
      if (!AUTO_PRESETS[autoPresetMode]) {
        autoPresetMode = "comfort";
      }

      if (status && autoPresetMode !== "manual") {
        autoPresetMode = detectAutoPreset(status.temp_on, status.temp_off);
      }

      localStorage.setItem("autoPresetMode", autoPresetMode);
      autoPresetLabel.textContent = AUTO_PRESETS[autoPresetMode].label;
      autoPresetDrawer.classList.toggle("visible", autoPresetDrawerOpen);
      autoPresetToggle.classList.toggle("open", autoPresetDrawerOpen);
      autoPresetToggle.setAttribute("aria-expanded", autoPresetDrawerOpen ? "true" : "false");
      simpleAutoTemp.classList.toggle("visible", autoPresetMode === "manual");

      autoPresetButtons.forEach((button) => {
        button.classList.toggle("active", button.dataset.autoPreset === autoPresetMode);
      });
    }

    function clamp(value, min, max) {
      return Math.max(min, Math.min(max, value));
    }

    function setGauge(gauge, fill, color) {
      const angle = clamp(fill, 0, 100) * 1.8;
      gauge.style.setProperty("--gauge-angle", `${angle}deg`);
      gauge.style.setProperty("--gauge-color", color);
    }

    function humidityRangeForTemperature(temperature) {
      if (temperature === null) {
        return {min: 40, max: 60};
      }

      if (temperature < 18) {
        return {min: 45, max: 60};
      }
      if (temperature < 24) {
        return {min: 40, max: 60};
      }
      if (temperature < 27) {
        return {min: 40, max: 55};
      }
      return {min: 35, max: 50};
    }

    function renderGauges(status) {
      if (status.temperature === null) {
        simpleTemperature.textContent = "--.- C";
        temperatureCaption.textContent = "온도 수신 전";
        setGauge(temperatureGauge, 0, "#98a2b3");
      } else {
        const temp = status.temperature;
        const tempOn = status.temp_on;
        const tempOff = status.temp_off;
        let tempColor = "#16803c";
        let tempText = `유지 ${tempOff.toFixed(1)}-${tempOn.toFixed(1)} C`;

        if (temp >= tempOn) {
          tempColor = "#b42318";
          tempText = `${tempOn.toFixed(1)} C 이상`;
        } else if (temp <= tempOff) {
          tempColor = "#1f6feb";
          tempText = `${tempOff.toFixed(1)} C 이하`;
        }

        simpleTemperature.textContent = `${temp.toFixed(1)} C`;
        temperatureCaption.textContent = tempText;
        setGauge(temperatureGauge, (temp / 40) * 100, tempColor);
      }

      if (status.humidity === null) {
        simpleHumidity.textContent = "--.- %";
        humidityCaption.textContent = "습도 수신 전";
        setGauge(humidityGauge, 0, "#98a2b3");
        return;
      }

      const humidity = status.humidity;
      const range = humidityRangeForTemperature(status.temperature);
      let humidityColor = "#16803c";
      let humidityText = `권장 ${range.min}-${range.max}%`;

      if (humidity < range.min) {
        humidityColor = "#b42318";
        humidityText = `낮음 / 권장 ${range.min}-${range.max}%`;
      } else if (humidity > range.max) {
        humidityColor = "#1f6feb";
        humidityText = `높음 / 권장 ${range.min}-${range.max}%`;
      }

      simpleHumidity.textContent = `${humidity.toFixed(1)} %`;
      humidityCaption.textContent = humidityText;
      setGauge(humidityGauge, humidity, humidityColor);
    }

    function renderSimple(status) {
      currentStatus = status;

      renderGauges(status);

      setSimpleToggle(airconToggle, "에어컨", status.aircon_on);
      airconToggle.disabled = status.auto_control;
      airconToggle.title = status.auto_control
        ? "자동제어 중에는 에어컨 수동 조작이 비활성화됩니다."
        : "에어컨 버튼을 누릅니다.";
      setSimpleToggle(humidifierToggle, "가습기", status.humidifier_on);
      humidifierToggle.disabled = status.auto_control;
      humidifierToggle.title = status.auto_control
        ? "자동제어 중에는 가습기 수동 조작이 비활성화됩니다."
        : "가습기 전원을 전환합니다.";
      setSwitchOnly(autoToggle, "자동제어", status.auto_control);
      setSimpleSwitch(sleepToggle, "수면모드", status.sleep_mode);
    }

    function renderPresence(status) {
      const element = document.getElementById("presence");

      if (!status.presence_enabled) {
        element.textContent = "재실 감지 OFF";
        element.className = "pill";
        simplePresence.textContent = "재실 감지 OFF";
        simplePresence.className = "top-status presence-chip disabled";
        return;
      }

      if (status.is_home) {
        element.textContent = "재실 감지 ON";
        element.className = "pill on";
        simplePresence.textContent = "재실중";
        simplePresence.className = "top-status presence-chip home";
        return;
      }

      if (status.presence_state === "away") {
        element.textContent = "외출";
        element.className = "pill off";
        simplePresence.textContent = "외출 중";
        simplePresence.className = "top-status presence-chip away";
        return;
      }

      if (status.presence_missing_seconds !== null) {
        element.textContent =
          `미감지 ${status.presence_missing_seconds}/${status.away_after_seconds}초`;
        simplePresence.textContent =
          `미감지 ${status.presence_missing_seconds}초`;
      } else {
        element.textContent = "재실 확인 중";
        simplePresence.textContent = "재실 확인 중";
      }
      element.className = "pill auto";
      simplePresence.className = "top-status presence-chip checking";
    }

    function renderLight(status) {
      const lightValue = document.getElementById("lightLevel");
      const roomLight = document.getElementById("roomLight");

      if (status.light_raw === null) {
        lightValue.textContent = "---";
        roomLight.textContent = "조도 수신 전";
        roomLight.className = "pill";
        return;
      }

      lightValue.textContent = `${status.light_raw}`;
      roomLight.textContent = status.is_dark ? "어두움" : "밝음";
      roomLight.className = status.is_dark ? "pill off" : "pill on";
    }

    function renderSleep(status) {
      const sleepMode = document.getElementById("sleepMode");
      const sleepPrompt = document.getElementById("sleepPrompt");

      sleepMode.textContent = `수면 ${status.sleep_mode ? "ON" : "OFF"}`;
      sleepMode.className = status.sleep_mode ? "pill on" : "pill";
      sleepPrompt.classList.toggle("visible", status.sleep_suggestion_pending && !status.sleep_mode);
      simpleSleepPrompt.classList.toggle("visible", status.sleep_suggestion_pending && !status.sleep_mode);
    }

    function syncSimpleThresholdControls(tempOn, tempOff) {
      const onText = `${tempOn.toFixed(1)} C`;
      const offText = `${tempOff.toFixed(1)} C`;

      simpleTempOnRange.value = tempOn.toFixed(1);
      simpleTempOffRange.value = tempOff.toFixed(1);
      simpleTempOnValue.textContent = onText;
      simpleTempOffValue.textContent = offText;
      simpleThresholdSummary.textContent = `켜짐 ${onText} / 꺼짐 ${offText}`;
    }

    function render(status) {
      document.getElementById("temperature").textContent =
        status.temperature === null ? "--.- C" : `${status.temperature.toFixed(1)} C`;
      document.getElementById("humidity").textContent =
        status.humidity === null ? "--.- %" : `${status.humidity.toFixed(1)} %`;

      renderSimple(status);
      renderPresence(status);
      renderLight(status);
      renderSleep(status);
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
      if (!simpleThresholdDirty) {
        syncSimpleThresholdControls(status.temp_on, status.temp_off);
      }
      updateAutoPresetUi(status);

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
      simpleMessage.textContent = "명령 전송 중";
      try {
        const data = await requestJson("/api/command", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({command}),
        });
        render(data.status);
        message.textContent = data.message;
        simpleMessage.textContent = data.message;
      } catch (error) {
        message.textContent = error.message;
        simpleMessage.textContent = error.message;
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
        simpleMessage.textContent = data.message;
      } catch (error) {
        message.textContent = error.message;
        simpleMessage.textContent = error.message;
      }
    }

    function normalizeSimpleThresholds(changed) {
      const min = Number(simpleTempOnRange.min);
      const max = Number(simpleTempOnRange.max);
      let tempOn = Number(simpleTempOnRange.value);
      let tempOff = Number(simpleTempOffRange.value);

      if (changed === "on" && tempOn <= tempOff) {
        tempOff = Math.max(min, tempOn - 0.5);
        if (tempOn <= tempOff) {
          tempOn = Math.min(max, tempOff + 0.5);
        }
      }

      if (changed === "off" && tempOff >= tempOn) {
        tempOn = Math.min(max, tempOff + 0.5);
        if (tempOff >= tempOn) {
          tempOff = Math.max(min, tempOn - 0.5);
        }
      }

      syncSimpleThresholdControls(tempOn, tempOff);
      return {tempOn, tempOff};
    }

    function scheduleSimpleThresholdSave(changed) {
      const thresholds = normalizeSimpleThresholds(changed);
      simpleThresholdDirty = true;
      autoPresetMode = "manual";
      localStorage.setItem("autoPresetMode", autoPresetMode);
      updateAutoPresetUi();
      simpleMessage.textContent = "자동제어 온도 조정 중";

      clearTimeout(simpleThresholdSaveTimer);
      simpleThresholdSaveTimer = setTimeout(() => {
        saveSimpleThresholdsAuto(thresholds.tempOn, thresholds.tempOff);
      }, 650);
    }

    async function saveSimpleThresholdsAuto(tempOn, tempOff) {
      if (!Number.isFinite(tempOn) || !Number.isFinite(tempOff)) {
        simpleMessage.textContent = "자동제어 온도를 확인해주세요.";
        return;
      }

      message.textContent = "온도 기준 저장 중";
      simpleMessage.textContent = "온도 기준 저장 중";
      try {
        const data = await requestJson("/api/command", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({
            command: "set_thresholds",
            temp_on: tempOn,
            temp_off: tempOff,
          }),
        });
        simpleThresholdDirty = false;
        render(data.status);
        message.textContent = data.message;
        simpleMessage.textContent = data.message;
      } catch (error) {
        simpleThresholdDirty = false;
        message.textContent = error.message;
        simpleMessage.textContent = error.message;
        refreshStatus();
      }
    }

    function setAutoPresetDrawer(open) {
      autoPresetDrawerOpen = open;
      updateAutoPresetUi();
    }

    async function applyAutoPreset(mode) {
      if (!AUTO_PRESETS[mode]) {
        return;
      }

      autoPresetMode = mode;
      localStorage.setItem("autoPresetMode", autoPresetMode);
      setAutoPresetDrawer(false);

      if (mode === "manual") {
        simpleMessage.textContent = "수동조절 모드입니다.";
        updateAutoPresetUi();
        return;
      }

      const preset = AUTO_PRESETS[mode];
      simpleMessage.textContent = `${preset.label} 모드 적용 중`;
      await saveSimpleThresholdsAuto(preset.tempOn, preset.tempOff);
    }

    document.querySelectorAll("[data-command]").forEach((button) => {
      button.addEventListener("click", () => sendCommand(button.dataset.command));
    });
    airconToggle.addEventListener("click", () => {
      if (currentStatus && currentStatus.auto_control) {
        simpleMessage.textContent = "자동제어 중에는 에어컨 수동 조작이 비활성화됩니다.";
        return;
      }
      sendCommand("aircon_toggle");
    });
    humidifierToggle.addEventListener("click", () => {
      if (currentStatus && currentStatus.auto_control) {
        simpleMessage.textContent = "자동제어 중에는 가습기 수동 조작이 비활성화됩니다.";
        return;
      }
      sendCommand(currentStatus && currentStatus.humidifier_on ? "humidifier_off" : "humidifier_on");
    });
    autoToggle.addEventListener("click", () => {
      sendCommand(currentStatus && currentStatus.auto_control ? "auto_off" : "auto_on");
    });
    autoPresetToggle.addEventListener("click", () => {
      setAutoPresetDrawer(!autoPresetDrawerOpen);
    });
    autoPresetButtons.forEach((button) => {
      button.addEventListener("click", () => applyAutoPreset(button.dataset.autoPreset));
    });
    sleepToggle.addEventListener("click", () => {
      sendCommand(currentStatus && currentStatus.sleep_mode ? "sleep_off" : "sleep_on");
    });
    saveThresholds.addEventListener("click", saveAutoThresholds);
    simpleTempOnRange.addEventListener("input", () => scheduleSimpleThresholdSave("on"));
    simpleTempOffRange.addEventListener("input", () => scheduleSimpleThresholdSave("off"));

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


def set_sleep_mode(target_on):
    with state_lock:
        state["sleep_mode"] = target_on
        if target_on:
            state["auto_control"] = True
            state["sleep_suggestion_pending"] = False
            state["sleep_suggestion_ignored_until"] = None

    if target_on:
        return "수면모드를 켰습니다. 자동제어도 함께 켰습니다."

    return "수면모드를 껐습니다."


def dismiss_sleep_suggestion():
    ignored_until = time.time() + SLEEP_SUGGEST_DISMISS_SECONDS

    with state_lock:
        state["sleep_suggestion_pending"] = False
        state["sleep_suggestion_ignored_until"] = ignored_until

    minutes = max(1, int(SLEEP_SUGGEST_DISMISS_SECONDS / 60))
    return f"수면모드 제안을 {minutes}분 동안 숨깁니다."


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


def parse_clock_minutes(value):
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", value.strip())
    if not match:
        return None

    hour = int(match.group(1))
    minute = int(match.group(2))
    if hour > 23 or minute > 59:
        return None

    return hour * 60 + minute


def is_in_sleep_suggest_time_window():
    start = parse_clock_minutes(SLEEP_SUGGEST_START)
    end = parse_clock_minutes(SLEEP_SUGGEST_END)
    if start is None or end is None:
        return True

    now = time.localtime()
    current = now.tm_hour * 60 + now.tm_min

    if start <= end:
        return start <= current <= end

    return current >= start or current <= end


def calculate_is_dark(light_raw):
    if LIGHT_DARK_WHEN_LOW:
        return light_raw <= LIGHT_DARK_THRESHOLD

    return light_raw >= LIGHT_DARK_THRESHOLD


def calculate_light_percent(light_raw):
    light_raw = max(0, min(1023, light_raw))
    if LIGHT_DARK_WHEN_LOW:
        return round(light_raw / 1023 * 100, 1)

    return round((1023 - light_raw) / 1023 * 100, 1)


def update_sleep_suggestion(now):
    with state_lock:
        light_enabled = state["light_sensor_enabled"]
        is_dark = state["is_dark"]
        dark_since_at = state["dark_since_at"]
        sleep_mode = state["sleep_mode"]
        ignored_until = state["sleep_suggestion_ignored_until"]
        presence_enabled = state["presence_enabled"]
        is_home = state["is_home"]

        can_suggest = (
            light_enabled
            and is_dark
            and dark_since_at is not None
            and not sleep_mode
            and (ignored_until is None or now >= ignored_until)
            and (not presence_enabled or is_home)
        )

        if can_suggest:
            dark_seconds = now - dark_since_at
            can_suggest = dark_seconds >= state["sleep_suggest_after_seconds"]

        if can_suggest and is_in_sleep_suggest_time_window():
            state["sleep_suggestion_pending"] = True
        elif not is_dark:
            state["sleep_suggestion_pending"] = False


def update_light_state(light_raw):
    now = time.time()
    is_dark = calculate_is_dark(light_raw)
    light_percent = calculate_light_percent(light_raw)

    with state_lock:
        previous_dark = state["is_dark"]
        state["light_raw"] = light_raw
        state["light_level_percent"] = light_percent
        state["is_dark"] = is_dark

        if is_dark and not previous_dark:
            state["dark_since_at"] = now
        elif not is_dark:
            state["dark_since_at"] = None
            state["sleep_suggestion_pending"] = False

    update_sleep_suggestion(now)


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
    light_raw = None

    match = re.search(
        r"온도:\s*(-?[0-9]+(?:\.[0-9]+)?)\s*C,\s*습도:\s*([0-9]+(?:\.[0-9]+)?)",
        line,
    )
    if match:
        temperature = float(match.group(1))
        humidity = float(match.group(2))

    light_match = re.search(r"조도:\s*([0-9]+)", line)
    if light_match:
        light_raw = int(light_match.group(1))

    humidifier_match = re.search(r"가습기:\s*(ON|OFF)", line)

    with state_lock:
        state["last_arduino_line"] = line

        if temperature is not None and humidity is not None:
            state["temperature"] = temperature
            state["humidity"] = humidity
            state["last_sensor_at"] = time.time()

        if humidifier_match:
            state["humidifier_on"] = humidifier_match.group(1) == "ON"

    if light_raw is not None:
        update_light_state(light_raw)

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

    if snapshot["dark_since_at"] is None:
        snapshot["dark_seconds"] = None
    else:
        snapshot["dark_seconds"] = int(time.time() - snapshot["dark_since_at"])

    return snapshot


def run_command(payload):
    if isinstance(payload, dict):
        command = payload.get("command", "")
    else:
        command = payload

    if command == "aircon_on":
        with state_lock:
            auto_control = state["auto_control"]
        if auto_control:
            return "자동제어 중에는 에어컨 수동 조작이 비활성화됩니다."
        return set_aircon(True)
    if command == "aircon_off":
        with state_lock:
            auto_control = state["auto_control"]
        if auto_control:
            return "자동제어 중에는 에어컨 수동 조작이 비활성화됩니다."
        return set_aircon(False)
    if command == "aircon_toggle":
        with state_lock:
            auto_control = state["auto_control"]
            if auto_control:
                return "자동제어 중에는 에어컨 수동 조작이 비활성화됩니다."
            target_on = not state["aircon_on"]
        return set_aircon(target_on)
    if command == "humidifier_on":
        with state_lock:
            auto_control = state["auto_control"]
        if auto_control:
            return "자동제어 중에는 가습기 수동 조작이 비활성화됩니다."
        return set_humidifier(True)
    if command == "humidifier_off":
        with state_lock:
            auto_control = state["auto_control"]
        if auto_control:
            return "자동제어 중에는 가습기 수동 조작이 비활성화됩니다."
        return set_humidifier(False)
    if command == "auto_on":
        return set_auto_control(True)
    if command == "auto_off":
        return set_auto_control(False)
    if command == "sleep_on":
        return set_sleep_mode(True)
    if command == "sleep_off":
        return set_sleep_mode(False)
    if command == "dismiss_sleep_suggestion":
        return dismiss_sleep_suggestion()
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
