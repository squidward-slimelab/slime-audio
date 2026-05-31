#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE = REPO_ROOT / "tests" / "fixtures" / "slime-audio-web-active-state.json"
DEFAULT_SESSION = REPO_ROOT / "tests" / "fixtures" / "slime-audio-web-active-session.json"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for(url: str, timeout_s: float = 8.0) -> None:
    deadline = time.monotonic() + timeout_s
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as response:
                if response.status == 200:
                    return
        except Exception as ex:  # pragma: no cover - diagnostic path
            last_error = ex
        time.sleep(0.1)
    raise RuntimeError(f"server did not become ready: {last_error}")


def chrome_binary() -> str:
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        path = shutil.which(name)
        if path:
            return path
    raise FileNotFoundError("google-chrome/chromium is required for the web smoke test")


def run_chrome(chrome: str, url: str, out_dir: Path, name: str, size: str) -> str:
    profile = out_dir / f"profile-{name}"
    screenshot = out_dir / f"{name}.png"
    command = [
        chrome,
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--hide-scrollbars",
        "--run-all-compositor-stages-before-draw",
        "--virtual-time-budget=4000",
        f"--window-size={size}",
        f"--user-data-dir={profile}",
        f"--screenshot={screenshot}",
        "--dump-dom",
        url,
    ]
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    if not screenshot.exists() or screenshot.stat().st_size < 10_000:
        raise AssertionError(f"{name} screenshot looks empty: {screenshot}")
    dom = result.stdout
    required = ["transport-strip", "timeline-event", "playhead", "short incoming vocal note"]
    missing = [needle for needle in required if needle not in dom]
    if missing:
        raise AssertionError(f"{name} DOM missing expected dashboard markers: {missing}")
    if "dashboard error" in dom.lower():
        raise AssertionError(f"{name} rendered dashboard error")
    return str(screenshot)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run fixture-backed browser smoke checks for the SlimeAudio dashboard.")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--session", type=Path, default=DEFAULT_SESSION)
    parser.add_argument("--out-dir", type=Path, default=REPO_ROOT / "runtime" / "web-smoke")
    args = parser.parse_args()

    port = free_port()
    env = dict(os.environ)
    env["PYTHONPATH"] = f"{REPO_ROOT / 'scripts'}:{REPO_ROOT / 'src'}"
    server = subprocess.Popen(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "slime_audio_web.py"),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--state",
            str(args.state),
            "--session",
            str(args.session),
        ],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        url = f"http://127.0.0.1:{port}/"
        wait_for(f"http://127.0.0.1:{port}/api/state")
        args.out_dir.mkdir(parents=True, exist_ok=True)
        chrome = chrome_binary()
        desktop = run_chrome(chrome, url, args.out_dir, "desktop", "1440,1000")
        mobile = run_chrome(chrome, url, args.out_dir, "mobile", "390,900")
        print(f"desktop screenshot: {desktop}")
        print(f"mobile screenshot: {mobile}")
        return 0
    finally:
        server.terminate()
        try:
            server.wait(timeout=3)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=3)


if __name__ == "__main__":
    raise SystemExit(main())
