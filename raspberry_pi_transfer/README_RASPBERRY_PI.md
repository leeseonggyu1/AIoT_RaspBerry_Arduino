# Raspberry Pi Transfer Package

이 폴더는 PC에서 작업한 최신 웹 UI/제어 서버를 라즈베리파이에 옮기기 위한 배포용 폴더입니다.

## 포함 파일

- `home_tailscale_server.py`: Tailscale 웹 UI + Arduino 시리얼 제어 서버
- `home_controller.ino`: Arduino 센서/가습기/에어컨 버튼 제어 스케치
- `home_control_config.json`: 자동제어 온도 기준 설정
- `requirements.txt`: Python 의존성
- `run_on_raspberry_pi.sh`: 라즈베리파이 실행 스크립트
- `TAILSCALE_REMOTE_SETUP.md`: Tailscale 원격 접속 참고 문서

## 라즈베리파이에 옮긴 뒤

```bash
cd ~/raspberry_pi_transfer
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
chmod +x run_on_raspberry_pi.sh
./run_on_raspberry_pi.sh
```

## 접속 주소

라즈베리파이에서 Tailscale IP를 확인합니다.

```bash
tailscale ip -4
```

휴대폰에서 Tailscale을 켠 뒤 아래 주소로 접속합니다.

```text
http://라즈베리파이_TAILSCALE_IP:8000/?token=1234
```

## Arduino 포트가 다를 때

기본 포트는 `/dev/ttyACM0`입니다. 포트가 다르면 이렇게 실행합니다.

```bash
ARDUINO_PORT=/dev/ttyUSB0 ./run_on_raspberry_pi.sh
```

포트 확인:

```bash
ls /dev/ttyACM* /dev/ttyUSB*
```

## 블루투스 재실 감지 설정

휴대폰 Bluetooth MAC 또는 이름 키워드를 `home_control_config.json`에 추가하면 됩니다.

```json
{
  "temp_on": 26.0,
  "temp_off": 24.0,
  "presence_enabled": true,
  "phone_bluetooth_mac": "AA:BB:CC:DD:EE:FF",
  "away_after_seconds": 300
}
```

MAC 주소 대신 이름으로 감지하려면:

```json
{
  "presence_enabled": true,
  "phone_name_keyword": "내폰이름"
}
```

## 주의

- 이 폴더에는 실행 로그(`server.log`)를 넣지 않았습니다.
- PC 테스트용 `MOCK_ARDUINO=1`은 라즈베리파이에서는 기본으로 꺼져 있습니다.
- 외부 공개 포트포워딩 없이 Tailscale 안에서만 쓰는 구성을 권장합니다.
