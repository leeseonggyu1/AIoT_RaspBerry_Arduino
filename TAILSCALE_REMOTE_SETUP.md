# Tailscale 원격 집안 제어 실행 방법

## 구조

- 라즈베리파이: 집 Wi-Fi에 연결, Tailscale 로그인, `home_tailscale_server.py` 실행
- 아두이노: `home_controller.ino` 업로드, DHT 센서/서보모터/가습기 릴레이 제어
- 휴대폰: Tailscale 앱 로그인 후 브라우저로 라즈베리파이 접속

## 노트북에서 먼저 검증하기

### 1. 아두이노 없이 웹 화면만 검증

PowerShell에서 실행:

```powershell
$env:MOCK_ARDUINO="1"
$env:AUTO_CONTROL="0"
$env:CONTROL_TOKEN="1234"
python .\home_tailscale_server.py
```

노트북 브라우저에서 접속:

```text
http://127.0.0.1:8000/?token=1234
```

온습도 값이 가짜로 표시되고 버튼을 눌렀을 때 명령 메시지가 바뀌면 웹 서버 흐름은 정상입니다.

### 2. 노트북 + 실제 아두이노로 검증

아두이노를 노트북 USB에 연결한 뒤 장치 관리자에서 COM 포트를 확인합니다.

PowerShell에서 실행:

```powershell
python -m pip install pyserial
$env:ARDUINO_PORT="COM5"
$env:AUTO_CONTROL="0"
$env:CONTROL_TOKEN="1234"
python .\home_tailscale_server.py
```

브라우저에서 접속:

```text
http://127.0.0.1:8000/?token=1234
```

### 3. 노트북 + 휴대폰 Tailscale 접속 검증

노트북과 휴대폰 둘 다 Tailscale 앱에 같은 계정으로 로그인합니다.

PowerShell에서 실행:

```powershell
$env:MOCK_ARDUINO="1"
$env:HOST="0.0.0.0"
$env:AUTO_CONTROL="0"
$env:CONTROL_TOKEN="1234"
python .\home_tailscale_server.py
```

다른 PowerShell에서 노트북의 Tailscale IP를 확인합니다.

```powershell
tailscale ip -4
```

휴대폰 브라우저에서 접속:

```text
http://노트북_Tailscale_IP:8000/?token=1234
```

## 라즈베리파이 준비

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
tailscale status
```

블루투스 도구 설치:

```bash
sudo apt update
sudo apt install -y bluez
sudo systemctl enable --now bluetooth
```

파이썬 시리얼 라이브러리 설치:

```bash
python3 -m pip install pyserial
```

아두이노 포트 확인:

```bash
ls /dev/ttyACM*
ls /dev/ttyUSB*
```

## 블루투스 재실 감지 설정

휴대폰 MAC 주소를 확인합니다. 라즈베리파이에서:

```bash
bluetoothctl
power on
scan on
```

휴대폰 블루투스 설정 화면을 열어두면 검색이 더 잘 됩니다.
검색 결과에서 내 휴대폰의 MAC 주소를 확인한 뒤 종료합니다.

```bash
scan off
quit
```

더 안정적으로 쓰려면 휴대폰을 한 번 페어링/신뢰 처리합니다.

```bash
bluetoothctl
power on
agent on
pair 내휴대폰_MAC
trust 내휴대폰_MAC
quit
```

서버 실행 전에 아래 값을 설정합니다.

```bash
export PHONE_BLUETOOTH_MAC=내휴대폰_MAC
export PRESENCE_ENABLED=1
export AWAY_AFTER_SECONDS=300
```

동작 방식:

- 휴대폰이 감지되면 `재실`
- 감지가 끊겨도 바로 외출 처리하지 않고 5분 동안 대기
- 5분 동안 계속 미감지이면 `외출`
- 외출로 전환되는 순간 에어컨/가습기 상태를 OFF로 변경
- 재실 감지가 켜져 있으면 자동 온도 제어는 재실 상태에서만 동작

## 권장 실행: Tailscale Serve

웹 서버는 라즈베리파이 내부 `127.0.0.1:8000`에만 열고, Tailscale Serve가 Tailnet 안에서 HTTPS로 연결해주는 방식입니다.

```bash
export ARDUINO_PORT=/dev/ttyACM0
export PHONE_BLUETOOTH_MAC=내휴대폰_MAC
export PRESENCE_ENABLED=1
export AWAY_AFTER_SECONDS=300
export CONTROL_TOKEN=원하는비밀번호
python3 home_tailscale_server.py
```

다른 터미널에서:

```bash
tailscale serve --bg 8000
tailscale serve status
```

휴대폰에서 Tailscale 앱을 켠 뒤 브라우저로 Tailscale Serve 주소에 접속합니다.
처음 접속할 때 `CONTROL_TOKEN`을 입력하거나 주소 끝에 `?token=원하는비밀번호`를 붙이면 됩니다.

## 간단 실행: Tailscale IP 직접 접속

```bash
export HOST=0.0.0.0
export ARDUINO_PORT=/dev/ttyACM0
export PHONE_BLUETOOTH_MAC=내휴대폰_MAC
export PRESENCE_ENABLED=1
export AWAY_AFTER_SECONDS=300
export CONTROL_TOKEN=원하는비밀번호
python3 home_tailscale_server.py
```

라즈베리파이 Tailscale IP 확인:

```bash
tailscale ip -4
```

휴대폰 브라우저에서 접속:

```text
http://라즈베리파이_Tailscale_IP:8000/?token=원하는비밀번호
```

## 웹에서 가능한 기능

- 온도/습도 확인
- 블루투스 휴대폰 감지 기반 재실/외출 확인
- 에어컨 ON/OFF
- 가습기 ON/OFF
- 온도 자동 제어 ON/OFF
- 자동 제어 ON/OFF 기준 온도 변경

## 주의

- `CONTROL_TOKEN`은 꼭 설정하는 것을 권장합니다.
- 화면에서 변경한 자동 제어 온도는 `home_control_config.json`에 저장됩니다.
- 휴대폰 기종에 따라 일반 블루투스 검색에 항상 나타나지 않을 수 있습니다. 이 경우 페어링 후 `trust` 처리하고, 휴대폰 블루투스가 켜져 있는지 확인하세요.
- 에어컨은 현재 서보모터로 버튼을 누르는 방식이라 실제 상태와 코드의 기억 상태가 어긋날 수 있습니다.
- 가습기는 릴레이 모듈에 연결되어 있어야 실제 ON/OFF가 됩니다.
- 릴레이가 반대로 동작하면 `home_controller.ino`의 `RELAY_ACTIVE_LOW` 값을 `false`로 바꾸세요.
