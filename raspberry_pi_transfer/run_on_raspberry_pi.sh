#!/usr/bin/env bash
set -eu

cd "$(dirname "$0")"

export HOST="${HOST:-0.0.0.0}"
export WEB_PORT="${WEB_PORT:-8000}"
export CONTROL_TOKEN="${CONTROL_TOKEN:-1234}"
export ARDUINO_PORT="${ARDUINO_PORT:-/dev/ttyACM0}"
export ARDUINO_BAUD="${ARDUINO_BAUD:-9600}"
export MOCK_ARDUINO="${MOCK_ARDUINO:-0}"

python3 home_tailscale_server.py
