#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import slime_audio_session as session

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SESSION = REPO_ROOT / "runtime" / "mix-session.json"
DEFAULT_STATE = REPO_ROOT / "runtime" / "mix-session-state.json"
DEFAULT_HISTORY = REPO_ROOT / "runtime" / "play-history.jsonl"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def append_history(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")


def state_lock_ms(state_path: Path | None, lock_before: str | None) -> int | None:
    if state_path is not None and lock_before is not None:
        raise ValueError("--state and --lock-before cannot both be used")
    if lock_before is not None:
        return session.parse_ms(lock_before, "live edit lock")
    if state_path is None:
        return None
    return session.playhead_ms_from_state(state_path)


def event_summary(payload: dict[str, Any], event_id: str) -> dict[str, Any] | None:
    found = session.find_event(payload, event_id)
    if found is None:
        return None
    collection, index = found
    item = payload.get(collection, [])[index]
    return {
        "collection": collection,
        "deck": item.get("deck"),
        "duration_ms": item.get("duration_ms", item.get("duration")),
        "id": event_id,
        "start_ms": item.get("start_ms", item.get("start")),
        "trim_start_ms": item.get("trim_start_ms", item.get("trim_start")),
    }


def common_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--session", type=Path, default=DEFAULT_SESSION, help="Active mix session to edit.")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE, help="Runner state used as the live edit lock.")
    parser.add_argument("--no-state-lock", action="store_true", help="Disable the default active playhead lock.")
    parser.add_argument("--lock-before", help="Explicit live edit lock timestamp.")
    parser.add_argument("--history-log", type=Path, default=DEFAULT_HISTORY)
    parser.add_argument("--actor", default=os.environ.get("USER") or "unknown")
    parser.add_argument("--reason", default="")
    parser.add_argument("--force", action="store_true", help="Allow edits before the live edit lock.")
    return parser


def add_clip_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--id", required=True)
    parser.add_argument("--deck", required=True)
    parser.add_argument("--path", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--trim-start", default="0")
    parser.add_argument("--duration")
    parser.add_argument("--gain-db", type=float, default=0.0)
    parser.add_argument("--fade-in-ms", type=int, default=0)
    parser.add_argument("--fade-out-ms", type=int, default=0)


def add_mic_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--id", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--text", required=True)
    parser.add_argument("--voice")
    parser.add_argument("--rate")
    parser.add_argument("--volume", type=float, default=1.0)
    parser.add_argument("--duck-volume", type=float)
    parser.add_argument("--lowpass-hz", type=float, default=1400.0)
    parser.add_argument("--duck-ms", type=int, default=2500)


def apply_edit(args: argparse.Namespace, edit: Callable[[dict[str, Any], int | None], dict[str, Any]]) -> None:
    state_path = None if args.no_state_lock else args.state
    lock_ms = state_lock_ms(state_path, args.lock_before)
    before = session.load_payload(args.session)
    after = edit(before, lock_ms)
    session.write_payload(args.session, after)
    affected_ids = [value for value in getattr(args, "affected_ids", []) if value]
    append_history(
        args.history_log,
        {
            "actor": args.actor,
            "affected": [event_summary(after, event_id) or {"id": event_id, "removed": True} for event_id in affected_ids],
            "command": args.command,
            "force": bool(args.force),
            "live_edit_lock_ms": lock_ms,
            "reason": args.reason,
            "session": str(args.session),
            "state": None if state_path is None else str(state_path),
            "type": "live_edit_applied",
            "updated_at": utc_now(),
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Safely edit the active SlimeAudio live mix session.")
    sub = parser.add_subparsers(dest="command", required=True)
    common = common_parser()

    add_clip_parser = sub.add_parser("add-clip", parents=[common])
    add_clip_args(add_clip_parser)

    add_mic_parser = sub.add_parser("add-mic", parents=[common])
    add_mic_args(add_mic_parser)

    remove_parser = sub.add_parser("remove", parents=[common])
    remove_parser.add_argument("--id", required=True)

    move_parser = sub.add_parser("move", parents=[common])
    move_parser.add_argument("--id", required=True)
    move_parser.add_argument("--start", required=True)

    automate_parser = sub.add_parser("automate", parents=[common])
    automate_parser.add_argument("--target", required=True)
    automate_parser.add_argument("--param", required=True)
    automate_parser.add_argument("--points-json", required=True)

    fader_routing_parser = sub.add_parser("fader-routing", parents=[common])
    fader_routing_parser.add_argument("--assign", action="append", required=True)

    crossfader_parser = sub.add_parser("crossfader", parents=[common])
    crossfader_parser.add_argument("--points-json", required=True)

    beat_jump_parser = sub.add_parser("beat-jump", parents=[common])
    beat_jump_parser.add_argument("--id", required=True)
    beat_jump_parser.add_argument("--beats", required=True)
    beat_jump_parser.add_argument("--field", choices=["trim-start", "start"], default="trim-start")
    beat_jump_parser.add_argument("--cache", type=Path, default=session.DEFAULT_DJ_CACHE)
    beat_jump_parser.add_argument("--min-confidence", type=float, default=session.DEFAULT_MIN_BEATGRID_CONFIDENCE)

    instant_double_parser = sub.add_parser("instant-double", parents=[common])
    instant_double_parser.add_argument("--source-id", required=True)
    instant_double_parser.add_argument("--id", required=True)
    instant_double_parser.add_argument("--start")
    instant_double_parser.add_argument("--deck")
    instant_double_parser.add_argument("--duration", default="00:08.000")
    instant_double_parser.add_argument("--gain-db", type=float)
    instant_double_parser.add_argument("--fade-in-ms", type=int, default=0)
    instant_double_parser.add_argument("--fade-out-ms", type=int, default=0)
    instant_double_parser.add_argument("--gate-beats")
    instant_double_parser.add_argument("--gate-offset-beats")
    instant_double_parser.add_argument("--cut-source", action="store_true")
    instant_double_parser.add_argument("--cache", type=Path, default=session.DEFAULT_DJ_CACHE)
    instant_double_parser.add_argument("--min-confidence", type=float, default=session.DEFAULT_MIN_BEATGRID_CONFIDENCE)

    routine_parser = sub.add_parser("instant-double-routine", parents=[common])
    routine_parser.add_argument("--source-id", required=True)
    routine_parser.add_argument("--id", required=True)
    routine_parser.add_argument("--recipe", required=True)
    routine_parser.add_argument("--start")
    routine_parser.add_argument("--cue-kind")
    routine_parser.add_argument("--cue-db", type=Path, default=session.DEFAULT_LIBRARY_DB)
    routine_parser.add_argument("--cache", type=Path, default=session.DEFAULT_DJ_CACHE)
    routine_parser.add_argument("--min-confidence", type=float, default=session.DEFAULT_MIN_BEATGRID_CONFIDENCE)

    mashup_parser = sub.add_parser("mashup-bed", parents=[common])
    mashup_parser.add_argument("--bed-id", required=True)
    mashup_parser.add_argument("--start")
    mashup_parser.add_argument("--end")
    mashup_parser.add_argument("--gain-db", type=float, default=-8.0)
    mashup_parser.add_argument("--lowpass-hz", type=float, default=1800.0)
    mashup_parser.add_argument("--highpass-hz", type=float)

    args = parser.parse_args()

    if args.command == "add-clip":
        args.affected_ids = [args.id]
        apply_edit(
            args,
            lambda payload, lock_ms: session.add_clip(
                payload,
                clip_id=args.id,
                deck=args.deck,
                path=args.path,
                start=args.start,
                trim_start=args.trim_start,
                duration=args.duration,
                gain_db=args.gain_db,
                fade_in_ms=args.fade_in_ms,
                fade_out_ms=args.fade_out_ms,
                lock_before_ms=lock_ms,
                force=args.force,
            ),
        )
    elif args.command == "add-mic":
        args.affected_ids = [args.id]
        apply_edit(
            args,
            lambda payload, lock_ms: session.add_mic_lean_in(
                payload,
                lean_id=args.id,
                start=args.start,
                text=args.text,
                voice=args.voice,
                rate=args.rate,
                volume=args.volume,
                duck_volume=args.duck_volume,
                lowpass_hz=args.lowpass_hz,
                duck_ms=args.duck_ms,
                lock_before_ms=lock_ms,
                force=args.force,
            ),
        )
    elif args.command == "remove":
        args.affected_ids = [args.id]
        apply_edit(args, lambda payload, lock_ms: session.remove_event(payload, args.id, lock_before_ms=lock_ms, force=args.force))
    elif args.command == "move":
        args.affected_ids = [args.id]
        apply_edit(args, lambda payload, lock_ms: session.move_event(payload, args.id, args.start, lock_before_ms=lock_ms, force=args.force))
    elif args.command == "automate":
        args.affected_ids = [args.target]
        apply_edit(
            args,
            lambda payload, lock_ms: session.add_automation(
                payload,
                target=args.target,
                param=args.param,
                points_json=args.points_json,
                lock_before_ms=lock_ms,
                force=args.force,
            ),
        )
    elif args.command == "fader-routing":
        assignments: dict[str, str] = {}
        for value in args.assign:
            if "=" not in value:
                raise ValueError("--assign must be formatted as deck=side")
            deck, side = value.split("=", 1)
            assignments[deck.strip()] = side.strip()
        args.affected_ids = [f"deck:{deck}" for deck in assignments]
        apply_edit(args, lambda payload, _lock_ms: session.set_fader_routing(payload, assignments))
    elif args.command == "crossfader":
        args.affected_ids = ["crossfader"]
        apply_edit(
            args,
            lambda payload, lock_ms: session.add_crossfader_automation(
                payload,
                points_json=args.points_json,
                lock_before_ms=lock_ms,
                force=args.force,
            ),
        )
    elif args.command == "beat-jump":
        args.affected_ids = [args.id]
        apply_edit(
            args,
            lambda payload, lock_ms: session.beat_jump_clip(
                payload,
                args.id,
                args.beats,
                field=args.field,
                cache_path=args.cache,
                min_confidence=args.min_confidence,
                lock_before_ms=lock_ms,
                force=args.force,
            ),
        )
    elif args.command == "instant-double":
        args.affected_ids = [args.id]
        apply_edit(
            args,
            lambda payload, lock_ms: session.add_instant_double(
                payload,
                source_id=args.source_id,
                double_id=args.id,
                start=args.start,
                deck=args.deck,
                duration=args.duration,
                gain_db=args.gain_db,
                fade_in_ms=args.fade_in_ms,
                fade_out_ms=args.fade_out_ms,
                gate_beats=args.gate_beats,
                gate_offset_beats=args.gate_offset_beats,
                cut_source=args.cut_source,
                cache_path=args.cache,
                min_confidence=args.min_confidence,
                lock_before_ms=lock_ms,
                force=args.force,
            ),
        )
    elif args.command == "instant-double-routine":
        args.affected_ids = [args.id, f"{args.id}-double"]
        apply_edit(
            args,
            lambda payload, lock_ms: session.add_instant_double_routine(
                payload,
                source_id=args.source_id,
                routine_id=args.id,
                recipe=args.recipe,
                start=args.start,
                cue_kind=args.cue_kind,
                cue_db=args.cue_db,
                cache_path=args.cache,
                min_confidence=args.min_confidence,
                lock_before_ms=lock_ms,
                force=args.force,
            ),
        )
    elif args.command == "mashup-bed":
        args.affected_ids = [args.bed_id]
        apply_edit(
            args,
            lambda payload, lock_ms: session.add_mashup_bed(
                payload,
                bed_id=args.bed_id,
                start=args.start,
                end=args.end,
                gain_db=args.gain_db,
                lowpass_hz=args.lowpass_hz,
                highpass_hz=args.highpass_hz,
                lock_before_ms=lock_ms,
                force=args.force,
            ),
        )
    else:
        raise SystemExit(f"unsupported command: {args.command}")

    print(f"live edit applied: {args.command}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
