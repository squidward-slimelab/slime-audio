#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import random
import subprocess
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PLAYLIST = REPO_ROOT / "runtime" / "playlist.txt"
DEFAULT_STATE = REPO_ROOT / "runtime" / "playlist-state.json"


def load_playlist(path: Path) -> list[str]:
    tracks = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not tracks:
        raise SystemExit(f"playlist is empty: {path}")
    return tracks


def load_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError:
        return None


def write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def state_matches_playlist(state: dict[str, Any], tracks: list[str]) -> bool:
    order = state.get("order")
    return isinstance(order, list) and set(order) == set(tracks) and len(order) == len(tracks)


def new_state(tracks: list[str], shuffle: bool) -> dict[str, Any]:
    order = tracks[:]
    if shuffle:
        random.shuffle(order)
    return {
        "completed": [],
        "current": None,
        "index": 0,
        "order": order,
        "shuffle": shuffle,
    }


def load_or_create_state(path: Path, tracks: list[str], shuffle: bool) -> dict[str, Any]:
    state = load_json(path)
    if state is not None and state_matches_playlist(state, tracks):
        return state

    state = new_state(tracks, shuffle)
    write_state(path, state)
    return state


def stream_command(args: argparse.Namespace, track: str) -> list[str]:
    command = [
        "python3",
        str(REPO_ROOT / "scripts" / "slime_audio_stream.py"),
        track,
    ]
    for target in args.target:
        command.extend(["--target", target])
    command.extend(
        [
            "--mode",
            args.mode,
            "--discover-timeout-ms",
            str(args.discover_timeout_ms),
            "--delay-ms",
            str(args.delay_ms),
            "--chunk-ms",
            str(args.chunk_ms),
            "--backend",
            args.backend,
        ]
    )
    if args.mode == "multicast":
        command.extend(["--multicast-group", args.multicast_group])
        command.extend(["--multicast-port", str(args.multicast_port)])
        if args.no_auto_listeners:
            command.append("--no-auto-listeners")
        if args.stop_listeners_when_done:
            command.append("--stop-listeners-when-done")
    return command


def run_playlist(args: argparse.Namespace) -> int:
    tracks = load_playlist(args.playlist)
    state = load_or_create_state(args.state, tracks, args.shuffle)
    order = state["order"]
    index = int(state.get("index", 0))

    if args.dry_run:
        print(f"playlist={args.playlist}")
        print(f"state={args.state}")
        print(f"index={index}/{len(order)}")
        print(f"current={state.get('current')}")
        for offset, track in enumerate(order[index : index + args.show_next], start=index + 1):
            print(f"next {offset}/{len(order)} {track}")
        return 0

    while index < len(order):
        track = order[index]
        state["index"] = index
        state["current"] = track
        state["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        state["playlist"] = str(args.playlist)
        write_state(args.state, state)

        print(f"[{state['started_at']}] streaming {index + 1}/{len(order)} {track}", flush=True)
        result = subprocess.run(stream_command(args, track), cwd=REPO_ROOT, check=False)
        if result.returncode != 0:
            print(f"stream failed rc={result.returncode} path={track}", flush=True)
            time.sleep(args.retry_seconds)
            continue

        completed = state.setdefault("completed", [])
        if track not in completed:
            completed.append(track)
        index += 1
        state["index"] = index
        state["current"] = None
        state["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        write_state(args.state, state)

    print("playlist done", flush=True)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a persistent SlimeAudio playlist without restarting from track one.")
    parser.add_argument("--playlist", type=Path, default=DEFAULT_PLAYLIST)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--target", action="append", default=None, help="Receiver name, host:port, or all")
    parser.add_argument("--shuffle", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--mode", choices=["packets", "multicast"], default="packets")
    parser.add_argument("--backend", choices=["auto", "vlc", "gstreamer"], default="auto")
    parser.add_argument("--discover-timeout-ms", type=int, default=4000)
    parser.add_argument("--delay-ms", type=int, default=2200)
    parser.add_argument("--chunk-ms", type=int, default=50)
    parser.add_argument("--retry-seconds", type=int, default=5)
    parser.add_argument("--multicast-group", default="239.77.77.77")
    parser.add_argument("--multicast-port", type=int, default=47778)
    parser.add_argument("--no-auto-listeners", action="store_true")
    parser.add_argument("--stop-listeners-when-done", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--show-next", type=int, default=10)
    args = parser.parse_args()
    if args.target is None:
        args.target = ["all"]
    return args


def main() -> int:
    return run_playlist(parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
