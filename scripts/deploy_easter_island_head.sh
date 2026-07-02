#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/home/squidward/.openclaw/workspace/slime-audio}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="$APP_DIR/.venv"

cd "$APP_DIR"

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/python" -m pip install -U pip setuptools wheel
"$VENV_DIR/bin/python" -m pip install -e .
"$VENV_DIR/bin/python" -m compileall scripts src tests

# Native DJ analyzer is the only analysis DSP implementation; analysis
# commands refuse to run without it, so the build must succeed.
make -C native

sudo install -m 0644 deploy/systemd/slime-audio-web.service /etc/systemd/system/slime-audio-web.service
sudo install -m 0644 deploy/systemd/slime-music-library.service /etc/systemd/system/slime-music-library.service
sudo install -m 0644 deploy/systemd/slime-music-library.timer /etc/systemd/system/slime-music-library.timer
sudo systemctl daemon-reload
sudo systemctl enable slime-audio-web.service slime-music-library.timer
sudo systemctl restart slime-audio-web.service
sudo systemctl start slime-music-library.timer

for _ in {1..30}; do
  if curl -fsS --max-time 2 -o /dev/null http://127.0.0.1:8765/api/state; then
    break
  fi
  sleep 1
done
curl -fsS --max-time 5 -o /dev/null http://127.0.0.1:8765/api/state
systemctl is-active --quiet slime-audio-web.service
systemctl is-active --quiet slime-music-library.timer
systemctl is-active --quiet snapserver.service
