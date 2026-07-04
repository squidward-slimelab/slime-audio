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
sudo install -m 0644 deploy/systemd/slime-audio-stems.service /etc/systemd/system/slime-audio-stems.service
sudo install -m 0644 deploy/systemd/slime-audio-stems.timer /etc/systemd/system/slime-audio-stems.timer
sudo install -m 0644 deploy/systemd/slime-audio-extend.service /etc/systemd/system/slime-audio-extend.service
sudo install -m 0644 deploy/systemd/slime-audio-extend.timer /etc/systemd/system/slime-audio-extend.timer
sudo systemctl daemon-reload
sudo systemctl enable slime-audio-web.service slime-music-library.timer slime-audio-stems.timer slime-audio-extend.timer
sudo systemctl restart slime-audio-web.service
sudo systemctl start slime-music-library.timer slime-audio-stems.timer slime-audio-extend.timer

# A live session runner keeps its pre-deploy code in memory; stamping the
# deploy lets it re-exec onto the new code at its next window boundary
# instead of dying on session-format changes (or silently rendering old bugs).
mkdir -p "$APP_DIR/runtime"
date -u +%FT%TZ > "$APP_DIR/runtime/deploy-stamp"

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
