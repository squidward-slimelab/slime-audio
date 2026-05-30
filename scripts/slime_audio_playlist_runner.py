#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import random
import subprocess
import time
from pathlib import Path
from typing import Any

from slime_audio_dj import analyze_with_cache, transition_plan

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PLAYLIST = REPO_ROOT / "runtime" / "playlist.txt"
DEFAULT_STATE = REPO_ROOT / "runtime" / "playlist-state.json"
DEFAULT_HISTORY = REPO_ROOT / "runtime" / "play-history.jsonl"
DEFAULT_DJ_CACHE = REPO_ROOT / "runtime" / "dj-analysis-cache.json"


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


def append_history(path: Path | None, event: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")


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
            "--prebuffer-ms",
            str(args.prebuffer_ms),
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
    analyses = None
    if args.dj_plan:
        analyses = analyze_with_cache([Path(track) for track in order], args.dj_cache, args.backend, args.analysis_sample_rate)

    if args.dry_run:
        print(f"playlist={args.playlist}")
        print(f"state={args.state}")
        print(f"index={index}/{len(order)}")
        print(f"current={state.get('current')}")
        for offset, track in enumerate(order[index : index + args.show_next], start=index + 1):
            print(f"next {offset}/{len(order)} {track}")
        if analyses is not None:
            for offset in range(index, min(len(order) - 1, index + args.show_next - 1)):
                plan = transition_plan(analyses[offset], analyses[offset + 1], args.max_pitch_shift)
                print(
                    "transition "
                    f"{offset + 1}->{offset + 2} score={plan.score} "
                    f"key={plan.key_relation} pitch={plan.pitch_shift_semitones:+d} "
                    f"tempo={plan.target_tempo_shift_pct}"
                )
        return 0

    while index < len(order):
        track = order[index]
        active_transition = None
        if analyses is not None and index + 1 < len(analyses):
            active_transition = transition_plan(analyses[index], analyses[index + 1], args.max_pitch_shift)
        state["index"] = index
        state["current"] = track
        state["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        state["playlist"] = str(args.playlist)
        if active_transition is not None:
            state["next_transition"] = {
                "next": order[index + 1],
                "score": active_transition.score,
                "target_tempo_shift_pct": active_transition.target_tempo_shift_pct,
                "pitch_shift_semitones": active_transition.pitch_shift_semitones,
                "key_relation": active_transition.key_relation,
                "phrase_wait_beats": active_transition.phrase_wait_beats,
            }
        else:
            state.pop("next_transition", None)
        write_state(args.state, state)
        if active_transition is not None:
            print(
                "next transition "
                f"score={active_transition.score} key={active_transition.key_relation} "
                f"pitch={active_transition.pitch_shift_semitones:+d} "
                f"tempo={active_transition.target_tempo_shift_pct}",
                flush=True,
            )
            append_history(
                args.history_log,
                {
                    "event": "transition_planned",
                    "index": index,
                    "next_track": order[index + 1],
                    "pitch_shift_semitones": active_transition.pitch_shift_semitones,
                    "playlist": str(args.playlist),
                    "score": active_transition.score,
                    "state": str(args.state),
                    "target_tempo_shift_pct": active_transition.target_tempo_shift_pct,
                    "timestamp": state["started_at"],
                    "track": track,
                    "key_relation": active_transition.key_relation,
                    "notes": active_transition.notes,
                },
            )
        append_history(
            args.history_log,
            {
                "event": "track_started",
                "index": index,
                "playlist": str(args.playlist),
                "state": str(args.state),
                "target": args.target,
                "timestamp": state["started_at"],
                "track": track,
            },
        )

        print(f"[{state['started_at']}] streaming {index + 1}/{len(order)} {track}", flush=True)
        result = subprocess.run(stream_command(args, track), cwd=REPO_ROOT, check=False)
        if result.returncode != 0:
            print(f"stream failed rc={result.returncode} path={track}", flush=True)
            append_history(
                args.history_log,
                {
                    "event": "track_failed",
                    "index": index,
                    "playlist": str(args.playlist),
                    "returncode": result.returncode,
                    "state": str(args.state),
                    "target": args.target,
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "track": track,
                },
            )
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
        append_history(
            args.history_log,
            {
                "event": "track_completed",
                "index": index - 1,
                "playlist": str(args.playlist),
                "state": str(args.state),
                "target": args.target,
                "timestamp": state["completed_at"],
                "track": track,
            },
        )

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
    parser.add_argument("--delay-ms", type=int, default=7000)
    parser.add_argument("--chunk-ms", type=int, default=7)
    parser.add_argument("--prebuffer-ms", type=int, default=15000)
    parser.add_argument("--retry-seconds", type=int, default=5)
    parser.add_argument("--history-log", type=Path, default=DEFAULT_HISTORY)
    parser.add_argument("--dj-plan", action="store_true", help="Analyze tracks and write next-transition metadata to state/history.")
    parser.add_argument("--dj-cache", type=Path, default=DEFAULT_DJ_CACHE)
    parser.add_argument("--analysis-sample-rate", type=int, default=44100)
    parser.add_argument("--max-pitch-shift", type=int, default=2)
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
