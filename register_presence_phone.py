#!/usr/bin/env python3
import json
import re
import subprocess
import sys
import time
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "home_control_config.json"
DEVICE_RE = re.compile(r"^\s*Device\s+([0-9a-fA-F:]{17})\s+(.+?)\s*$")


def run_bluetoothctl(args, timeout=15):
    try:
        result = subprocess.run(
            ["bluetoothctl", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        raise SystemExit("bluetoothctl을 찾을 수 없습니다. 먼저 `sudo apt install -y bluetooth bluez`를 실행하세요.")

    return f"{result.stdout}\n{result.stderr}"


def parse_devices(output):
    devices = []
    seen = set()

    for line in output.splitlines():
        match = DEVICE_RE.match(line)
        if not match:
            continue

        mac = match.group(1).lower()
        name = match.group(2).strip()
        if mac in seen:
            continue

        seen.add(mac)
        devices.append((mac, name))

    return devices


def list_paired_devices():
    devices = parse_devices(run_bluetoothctl(["paired-devices"]))
    if devices:
        return devices

    return parse_devices(run_bluetoothctl(["devices", "Paired"]))


def scan_devices(seconds=12):
    print(f"\n휴대폰의 블루투스 설정 화면을 열어둔 뒤 {seconds}초 동안 찾습니다.")

    process = subprocess.Popen(
        ["bluetoothctl"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        process.stdin.write("power on\nagent on\ndefault-agent\nscan on\n")
        process.stdin.flush()
        time.sleep(seconds)
        process.stdin.write("devices\nscan off\nquit\n")
        process.stdin.flush()
        stdout, stderr = process.communicate(timeout=10)
    except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
        process.kill()
        stdout, stderr = process.communicate()

    return parse_devices(f"{stdout}\n{stderr}")


def choose_device(devices, title):
    if not devices:
        return None

    print(f"\n{title}")
    for index, (mac, name) in enumerate(devices, start=1):
        print(f"{index}. {name} ({mac})")

    while True:
        value = input("등록할 휴대폰 번호를 입력하세요: ").strip()
        if not value:
            return None

        try:
            selected = int(value)
        except ValueError:
            print("숫자로 입력하세요.")
            continue

        if 1 <= selected <= len(devices):
            return devices[selected - 1]

        print("목록에 있는 번호를 입력하세요.")


def trust_device(mac):
    run_bluetoothctl(["trust", mac], timeout=10)


def pair_and_trust_device(mac):
    print("\n페어링을 시도합니다. 휴대폰에 확인 창이 뜨면 허용하세요.")
    run_bluetoothctl(["pair", mac], timeout=30)
    trust_device(mac)


def load_config():
    if not CONFIG_PATH.exists():
        return {}

    with CONFIG_PATH.open("r", encoding="utf-8") as file:
        return json.load(file)


def save_config(config):
    with CONFIG_PATH.open("w", encoding="utf-8") as file:
        json.dump(config, file, ensure_ascii=False, indent=2)
        file.write("\n")


def main():
    print("블루투스 재실감지 휴대폰 등록")
    print("가능하면 먼저 라즈베리파이와 휴대폰을 블루투스 페어링해두는 방식이 가장 안정적입니다.")

    paired = list_paired_devices()
    selected = choose_device(paired, "페어링된 기기 목록")

    if selected is None:
        scanned = scan_devices()
        selected = choose_device(scanned, "검색된 기기 목록")
        if selected is None:
            raise SystemExit("선택된 기기가 없습니다.")

        pair_and_trust_device(selected[0])
    else:
        trust_device(selected[0])

    mac, name = selected
    config = load_config()
    config["presence_enabled"] = True
    config["phone_name_keyword"] = name
    config.pop("phone_bluetooth_mac", None)
    config.setdefault("away_after_seconds", 300)

    save_config(config)

    print("\n등록 완료")
    print(f"- 선택한 기기: {name}")
    print("- 설정 방식: 페어링된 기기 목록에서 이름으로 자동 선택")
    print(f"- 설정 파일: {CONFIG_PATH}")
    print("\n서버를 재시작하면 재실감지가 켜집니다.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n취소했습니다.")
        sys.exit(1)
