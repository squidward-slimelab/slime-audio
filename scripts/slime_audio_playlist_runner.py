#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import random
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

from slime_audio_dj import analyze_with_cache, transition_plan
from slime_music_library import DEFAULT_DB as DEFAULT_LIBRARY_DB
from slime_music_library import connect as connect_library
from slime_music_library import preferred_path_for_file

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PLAYLIST = REPO_ROOT / "runtime" / "playlist.txt"
DEFAULT_STATE = REPO_ROOT / "runtime" / "playlist-state.json"
DEFAULT_HISTORY = REPO_ROOT / "runtime" / "play-history.jsonl"
DEFAULT_DJ_CACHE = REPO_ROOT / "runtime" / "dj-analysis-cache.json"
_active_stream: subprocess.Popen[bytes] | None = None


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


def read_state_or_die(path: Path) -> dict[str, Any]:
    state = load_json(path)
    if state is None:
        raise SystemExit(f"state file is missing or invalid: {path}")
    return state


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
    if state is not None and isinstance(state.get("order"), list) and int(state.get("index", 0)) <= len(state["order"]):
        merged, appended = merge_playlist_future(state, tracks)
        if appended:
            merged["queue_updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            write_state(path, merged)
        return merged

    state = new_state(tracks, shuffle)
    write_state(path, state)
    return state


def merge_playlist_future(state: dict[str, Any], tracks: list[str]) -> tuple[dict[str, Any], list[str]]:
    order = list(state.get("order") or [])
    index = int(state.get("index", 0))
    protected = set(order[: index + 1])
    existing = set(order)
    appended = [track for track in tracks if track not in existing and track not in protected]
    if not appended:
        return state, []
    next_state = dict(state)
    next_state["order"] = order + appended
    return next_state, appended


def future_start_index(state: dict[str, Any]) -> int:
    index = int(state.get("index", 0))
    return index + 1 if state.get("current") else index


def assert_future_track(state: dict[str, Any], track: str) -> int:
    order = list(state.get("order") or [])
    try:
        position = order.index(track)
    except ValueError as ex:
        raise ValueError(f"track is not in queue: {track}") from ex
    if position < future_start_index(state):
        raise ValueError(f"cannot edit current or completed track: {track}")
    return position


def edit_append(state: dict[str, Any], tracks: list[str]) -> tuple[dict[str, Any], list[str]]:
    next_state = dict(state)
    order = list(next_state.get("order") or [])
    appended = [track for track in tracks if track not in order]
    next_state["order"] = order + appended
    return next_state, appended


def edit_remove(state: dict[str, Any], tracks: list[str]) -> tuple[dict[str, Any], list[str]]:
    next_state = dict(state)
    order = list(next_state.get("order") or [])
    removed = []
    for track in tracks:
        position = assert_future_track(next_state, track)
        removed.append(order[position])
        del order[position]
        next_state["order"] = order
    return next_state, removed


def edit_swap(state: dict[str, Any], old_track: str, new_track: str) -> dict[str, Any]:
    next_state = dict(state)
    order = list(next_state.get("order") or [])
    position = assert_future_track(next_state, old_track)
    if new_track in order and new_track != old_track:
        raise ValueError(f"replacement is already in queue: {new_track}")
    order[position] = new_track
    next_state["order"] = order
    return next_state


def edit_move(state: dict[str, Any], track: str, after: str | None) -> dict[str, Any]:
    next_state = dict(state)
    order = list(next_state.get("order") or [])
    position = assert_future_track(next_state, track)
    item = order.pop(position)
    if after is None:
        insert_at = future_start_index(next_state)
    else:
        after_position = assert_future_track(next_state, after)
        if after_position >= position:
            after_position -= 1
        insert_at = after_position + 1
    order.insert(insert_at, item)
    next_state["order"] = order
    return next_state


def record_queue_edit(history_path: Path | None, state_path: Path, action: str, payload: dict[str, Any]) -> None:
    append_history(
        history_path,
        {
            "event": "queue_edited",
            "action": action,
            "state": str(state_path),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            **payload,
        },
    )


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
            "--packet-redundancy",
            str(args.packet_redundancy),
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
    if args.mode == "snapcast":
        command.extend(["--snapcast-port", str(args.snapcast_port)])
        command.extend(["--snapcast-buffer-ms", str(args.snapcast_buffer_ms)])
        command.extend(["--snapcast-fifo", str(args.snapcast_fifo)])
    return command


def resolve_stream_track(args: argparse.Namespace, track: str) -> str:
    if not args.prefer_library_source or not args.library_db.exists():
        return track
    try:
        conn = connect_library(args.library_db)
        preferred = preferred_path_for_file(conn, Path(track))
    except Exception:
        return track
    if preferred is None:
        return track
    return str(preferred)


def stop_active_stream() -> None:
    global _active_stream
    process = _active_stream
    if process is None or process.poll() is not None:
        _active_stream = None
        return

    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        _active_stream = None
        return

    try:
        process.wait(timeout=5)
    except ProcessLookupError:
        pass
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        try:
            process.wait()
        except ProcessLookupError:
            pass
    finally:
        _active_stream = None


def run_stream(command: list[str]) -> int:
    global _active_stream
    process = subprocess.Popen(command, cwd=REPO_ROOT, start_new_session=True)
    _active_stream = process
    try:
        return process.wait()
    finally:
        if _active_stream is process:
            stop_active_stream()


def install_signal_handlers() -> None:
    def handle_stop(signum: int, _frame: object) -> None:
        stop_active_stream()
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, handle_stop)
    signal.signal(signal.SIGINT, handle_stop)


def run_playlist(args: argparse.Namespace) -> int:
    install_signal_handlers()
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
            resolved = resolve_stream_track(args, track)
            suffix = f" -> {resolved}" if resolved != track else ""
            print(f"next {offset}/{len(order)} {track}{suffix}")
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
        stream_track = resolve_stream_track(args, track)
        active_transition = None
        if analyses is not None and index + 1 < len(analyses):
            active_transition = transition_plan(analyses[index], analyses[index + 1], args.max_pitch_shift)
        state["index"] = index
        state["current"] = track
        state["resolved_current"] = stream_track
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
                "resolved_track": stream_track,
                "playlist": str(args.playlist),
                "state": str(args.state),
                "target": args.target,
                "timestamp": state["started_at"],
                "track": track,
            },
        )

        if stream_track != track:
            print(f"routing via preferred library source: {stream_track}", flush=True)
        print(f"[{state['started_at']}] streaming {index + 1}/{len(order)} {stream_track}", flush=True)
        returncode = run_stream(stream_command(args, stream_track))
        if returncode != 0:
            print(f"stream failed rc={returncode} path={track}", flush=True)
            append_history(
                args.history_log,
                {
                    "event": "track_failed",
                    "index": index,
                    "playlist": str(args.playlist),
                    "returncode": returncode,
                    "resolved_track": stream_track,
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
                "resolved_track": stream_track,
                "state": str(args.state),
                "target": args.target,
                "timestamp": state["completed_at"],
                "track": track,
            },
        )
        if args.reload_playlist:
            latest_tracks = load_playlist(args.playlist)
            state, appended = merge_playlist_future(state, latest_tracks)
            if appended:
                state["queue_updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
                write_state(args.state, state)
                record_queue_edit(args.history_log, args.state, "append_from_playlist", {"tracks": appended})
                print(f"appended {len(appended)} future tracks from playlist", flush=True)
        order = state["order"]
        if args.dj_plan:
            analyses = analyze_with_cache([Path(track) for track in order], args.dj_cache, args.backend, args.analysis_sample_rate)

    print("playlist done", flush=True)
    return 0


def run_queue_edit(args: argparse.Namespace) -> int:
    state = read_state_or_die(args.state)
    if args.queue_command == "queue-append":
        tracks = [line.strip() for line in args.tracks_file.read_text(encoding="utf-8").splitlines() if line.strip()] if args.tracks_file else args.track
        state, appended = edit_append(state, tracks)
        write_state(args.state, state)
        record_queue_edit(args.history_log, args.state, "append", {"tracks": appended})
        print(f"appended {len(appended)} tracks")
        return 0
    if args.queue_command == "queue-remove":
        state, removed = edit_remove(state, args.track)
        write_state(args.state, state)
        record_queue_edit(args.history_log, args.state, "remove", {"tracks": removed})
        print(f"removed {len(removed)} tracks")
        return 0
    if args.queue_command == "queue-swap":
        state = edit_swap(state, args.old, args.new)
        write_state(args.state, state)
        record_queue_edit(args.history_log, args.state, "swap", {"old": args.old, "new": args.new})
        print("swapped future track")
        return 0
    if args.queue_command == "queue-move":
        state = edit_move(state, args.track, args.after)
        write_state(args.state, state)
        record_queue_edit(args.history_log, args.state, "move", {"track": args.track, "after": args.after})
        print("moved future track")
        return 0
    raise AssertionError(args.queue_command)


def add_runner_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--playlist", type=Path, default=DEFAULT_PLAYLIST)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--target", action="append", default=None, help="Receiver name, host:port, or all")
    parser.add_argument("--shuffle", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--mode", choices=["packets", "multicast", "snapcast"], default="packets")
    parser.add_argument("--backend", choices=["auto", "ffmpeg"], default="auto")
    parser.add_argument("--discover-timeout-ms", type=int, default=4000)
    parser.add_argument("--delay-ms", type=int, default=7000)
    parser.add_argument("--chunk-ms", type=int, default=7)
    parser.add_argument("--prebuffer-ms", type=int, default=15000)
    parser.add_argument("--packet-redundancy", type=int, default=2)
    parser.add_argument("--retry-seconds", type=int, default=5)
    parser.add_argument("--history-log", type=Path, default=DEFAULT_HISTORY)
    parser.add_argument("--dj-plan", action="store_true", help="Analyze tracks and write next-transition metadata to state/history.")
    parser.add_argument("--dj-cache", type=Path, default=DEFAULT_DJ_CACHE)
    parser.add_argument("--library-db", type=Path, default=DEFAULT_LIBRARY_DB)
    parser.add_argument("--prefer-library-source", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--analysis-sample-rate", type=int, default=44100)
    parser.add_argument("--max-pitch-shift", type=int, default=2)
    parser.add_argument("--multicast-group", default="239.77.77.77")
    parser.add_argument("--multicast-port", type=int, default=47778)
    parser.add_argument("--snapcast-port", type=int, default=1704)
    parser.add_argument("--snapcast-buffer-ms", type=int, default=1000)
    parser.add_argument("--snapcast-fifo", type=Path, default=Path("/tmp/slime-audio-snapfifo"))
    parser.add_argument("--no-auto-listeners", action="store_true")
    parser.add_argument("--stop-listeners-when-done", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--show-next", type=int, default=10)
    parser.add_argument("--reload-playlist", action=argparse.BooleanOptionalAction, default=True)


def add_queue_edit_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--history-log", type=Path, default=DEFAULT_HISTORY)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a persistent SlimeAudio playlist without restarting from track one.")
    sub = parser.add_subparsers(dest="queue_command")
    run_parser = sub.add_parser("run")
    add_runner_args(run_parser)

    append_parser = sub.add_parser("queue-append")
    add_queue_edit_args(append_parser)
    append_parser.add_argument("track", nargs="*")
    append_parser.add_argument("--tracks-file", type=Path)

    remove_parser = sub.add_parser("queue-remove")
    add_queue_edit_args(remove_parser)
    remove_parser.add_argument("track", nargs="+")

    swap_parser = sub.add_parser("queue-swap")
    add_queue_edit_args(swap_parser)
    swap_parser.add_argument("old")
    swap_parser.add_argument("new")

    move_parser = sub.add_parser("queue-move")
    add_queue_edit_args(move_parser)
    move_parser.add_argument("track")
    move_parser.add_argument("--after")

    add_runner_args(parser)
    args = parser.parse_args()
    if args.target is None:
        args.target = ["all"]
    return args


def main() -> int:
    args = parse_args()
    if args.queue_command and args.queue_command != "run":
        return run_queue_edit(args)
    return run_playlist(args)


if __name__ == "__main__":
    raise SystemExit(main())
