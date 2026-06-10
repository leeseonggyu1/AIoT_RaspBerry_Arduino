@echo off
set MOCK_ARDUINO=0
set CONTROL_TOKEN=1234
set HOST=0.0.0.0
set WEB_PORT=8000
set ARDUINO_PORT=COM5
cd /d "%~dp0"
"C:\Users\LeeSeonggyu\anaconda3\python.exe" -u home_tailscale_server.py
