@echo off
set MOCK_ARDUINO=1
set CONTROL_TOKEN=1234
set HOST=127.0.0.1
set WEB_PORT=8000
cd /d "%~dp0"
python home_tailscale_server.py
