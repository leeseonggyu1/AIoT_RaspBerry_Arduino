# Raspberry Pi Transfer Package

이 폴더는 PC에서 작업한 최신 웹 UI/제어 서버를 라즈베리파이에 옮기기 위한 배포용 폴더입니다.

## 포함 파일

- `home_tailscale_server.py`: Tailscale 웹 UI + Arduino 시리얼 제어 서버
- `home_controller.ino`: Arduino 센서/가습기/에어컨 버튼 제어 스케치
- `home_control_config.json`: 자동제어 온도 기준 설정
- `requirements.txt`: Python 의존성
- `run_on_raspberry_pi.sh`: 라즈베리파이 실행 스크립트
- `register_presence_phone.py`: 블루투스 재실감지 휴대폰 등록 도구
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

가장 쉬운 방법은 라즈베리파이에서 등록 도구를 실행하는 것입니다.

```bash
chmod +x register_presence_phone.sh
./register_presence_phone.sh
```

등록 도구는 페어링된 블루투스 기기 목록을 보여주고, 선택한 휴대폰 이름을 `home_control_config.json`에 저장합니다. 서버는 이후 MAC 주소를 직접 고정하지 않고 페어링된 기기 목록에서 그 이름을 찾아 재실감지에 사용합니다.

수동으로 설정하려면 `home_control_config.json`에 아래처럼 넣으면 됩니다.

```json
{
  "presence_enabled": true,
  "phone_name_keyword": "Galaxy S25",
  "away_after_seconds": 300
}
```

## 현재 하드웨어 기준

- 에어컨 서보: `D9`
- 가습기 서보: `D10`
- 가습기 다이얼 각도: `OFF=0도`, `ON=150도`
- IR 송신: `D3`
- IR 수신: `D4`
- DHT 온습도 센서: `D2`
- CdS 조도센서: `A0`

## 수면모드 프리셋

수면모드를 켜면 자동제어도 함께 켜지고, 일반 자동제어 기준 대신 수면 프리셋 기준으로 동작합니다. 수면모드 중에는 자동제어만 따로 끌 수 없고, 수면모드를 끄면 일반 자동제어 기준으로 돌아간 뒤 자동제어도 함께 꺼집니다.

- 시원하게: `ON=26도`, `OFF=24도`
- 쾌적하게: `ON=27도`, `OFF=25도`
- 따뜻하게: `ON=28도`, `OFF=26도`

## 주의

- 이 폴더에는 실행 로그(`server.log`)를 넣지 않았습니다.
- PC 테스트용 `MOCK_ARDUINO=1`은 라즈베리파이에서는 기본으로 꺼져 있습니다.
- 블루투스 재실 판단은 페어링/캐시 목록이 아니라 현재 스캔에서 연결 가능하게 보이는 기기만 기준으로 합니다.
- 외부 공개 포트포워딩 없이 Tailscale 안에서만 쓰는 구성을 권장합니다.
