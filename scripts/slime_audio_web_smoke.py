#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
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


def assert_json_error(url: str) -> None:
    request = urllib.request.Request(url)
    try:
        urllib.request.urlopen(request, timeout=2.0)
    except urllib.error.HTTPError as ex:
        body = ex.read().decode("utf-8")
        content_type = ex.headers.get("content-type", "")
        if "application/json" not in content_type:
            raise AssertionError(f"api error was not JSON: {content_type}") from ex
        payload = json.loads(body)
        if not payload.get("error"):
            raise AssertionError(f"api error payload missing error field: {payload}")
        return
    raise AssertionError(f"expected api error from {url}")


def fetch_text(url: str) -> str:
    with urllib.request.urlopen(url, timeout=2.0) as response:
        return response.read().decode("utf-8")


def assert_state_payload(url: str) -> None:
    payload = json.loads(fetch_text(url))
    dashboard = payload.get("dashboard") or {}
    required = ["transport", "events", "lanes", "now", "upcoming"]
    missing = [key for key in required if key not in dashboard]
    if missing:
        raise AssertionError(f"dashboard payload missing keys: {missing}")
    event_text = json.dumps(dashboard.get("events", []))
    if "short incoming vocal note" not in event_text:
        raise AssertionError("fixture payload missing planned vocal marker")
    if not dashboard.get("lanes"):
        raise AssertionError("fixture payload has no lanes")


def chrome_binary() -> str:
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        path = shutil.which(name)
        if path:
            return path
    raise FileNotFoundError("google-chrome/chromium is required for the web smoke test")


def run_chrome(chrome: str, url: str, out_dir: Path, name: str, size: str) -> str:
    profile = out_dir / f"profile-{name}"
    screenshot = out_dir / f"{name}.png"
    shutil.rmtree(profile, ignore_errors=True)
    command = [
        chrome,
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--hide-scrollbars",
        "--run-all-compositor-stages-before-draw",
        f"--window-size={size}",
        f"--user-data-dir={profile}",
        f"--screenshot={screenshot}",
        url,
    ]
    subprocess.run(command, check=True, capture_output=True, text=True, timeout=45)
    if not screenshot.exists() or screenshot.stat().st_size < 10_000:
        raise AssertionError(f"{name} screenshot looks empty: {screenshot}")
    return str(screenshot)


def run_tv_chrome(chrome: str, url: str, out_dir: Path) -> str:
    profile = out_dir / "profile-tv"
    screenshot = out_dir / "tv.png"
    shutil.rmtree(profile, ignore_errors=True)
    command = [
        chrome,
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--hide-scrollbars",
        "--run-all-compositor-stages-before-draw",
        "--window-size=1920,1080",
        f"--user-data-dir={profile}",
        f"--screenshot={screenshot}",
        url,
    ]
    subprocess.run(command, check=True, capture_output=True, text=True, timeout=45)
    if not screenshot.exists() or screenshot.stat().st_size < 10_000:
        raise AssertionError(f"tv screenshot looks empty: {screenshot}")
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
        assert_json_error(f"http://127.0.0.1:{port}/api/not-a-real-endpoint")
        assert_state_payload(f"http://127.0.0.1:{port}/api/state")
        root_html = fetch_text(url)
        tv_html = fetch_text(f"http://127.0.0.1:{port}/tv")
        if "app-shell" not in root_html or "tv-shell" not in tv_html:
            raise AssertionError("dashboard static shells did not render expected HTML")
        args.out_dir.mkdir(parents=True, exist_ok=True)
        chrome = chrome_binary()
        desktop = run_chrome(chrome, url, args.out_dir, "desktop", "1440,1000")
        mobile = run_chrome(chrome, url, args.out_dir, "mobile", "390,900")
        tv = run_tv_chrome(chrome, f"http://127.0.0.1:{port}/tv", args.out_dir)
        print(f"desktop screenshot: {desktop}")
        print(f"mobile screenshot: {mobile}")
        print(f"tv screenshot: {tv}")
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
