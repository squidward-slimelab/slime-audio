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
DEFAULT_ACTIVE_POINTER = REPO_ROOT / "runtime" / "active-set.json"


def resolve_live_defaults(args) -> None:
    """Follow the active set pointer when --session/--state are not given.

    The fixed mix-session.json default silently landed live edits on a
    dormant session (found in production twice); editing what is actually
    playing must be the default behavior.
    """
    if args.session == DEFAULT_SESSION or args.state == DEFAULT_STATE:
        try:
            pointer = json.loads(DEFAULT_ACTIVE_POINTER.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return
        if args.session == DEFAULT_SESSION and pointer.get("active_session_path"):
            args.session = Path(str(pointer["active_session_path"]))
        if args.state == DEFAULT_STATE and pointer.get("active_state_path"):
            args.state = Path(str(pointer["active_state_path"]))


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
    return session.edit_lock_ms_from_state(state_path)


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
    parser.add_argument("--db", type=Path, default=session.DEFAULT_LIBRARY_DB)
    parser.add_argument("--actor", default=os.environ.get("USER") or "unknown")
    parser.add_argument("--reason", default="")
    parser.add_argument("--force", action="store_true", help="Allow edits before the live edit lock.")
    return parser


def add_mic_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--id", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--text", required=True)
    parser.add_argument("--deck", default=session.VOCAL_DECK)
    parser.add_argument("--voice")
    parser.add_argument("--rate")
    parser.add_argument("--volume", type=float, default=1.45)
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

    add_action_parser = sub.add_parser("add-action", parents=[common])
    add_action_parser.add_argument("--action-json", required=True)

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

    effect_parser = sub.add_parser("add-effect", parents=[common])
    effect_parser.add_argument("--id", required=True)
    effect_parser.add_argument("--type", choices=["echo", "reverb", "vinyl_brake"], default="echo")
    effect_parser.add_argument("--preset", choices=sorted(session.AUDACITY_REVERB_PRESETS))
    effect_parser.add_argument("--target", required=True)
    effect_parser.add_argument("--start", required=True)
    effect_parser.add_argument("--duration", required=True)
    effect_parser.add_argument("--tail-ms", type=int)
    effect_parser.add_argument("--wet", type=float)
    effect_parser.add_argument("--gain-db", type=float)
    effect_parser.add_argument("--delay-ms", type=int)
    effect_parser.add_argument(
        "--delay-beats",
        type=float,
        help="Tempo-synced delay time in beats of the target's rendered tempo (0.5 eighth, 0.75 dotted eighth, 1 quarter). Uses the cached beatgrid.",
    )
    effect_parser.add_argument("--cache", type=Path, default=session.DEFAULT_DJ_CACHE)
    effect_parser.add_argument("--min-confidence", type=float, default=session.DEFAULT_MIN_BEATGRID_CONFIDENCE)
    effect_parser.add_argument("--feedback", type=float)
    effect_parser.add_argument("--room-size", type=float)
    effect_parser.add_argument("--damping", type=float)
    effect_parser.add_argument("--lowpass-hz", type=float)

    slip_parser = sub.add_parser("slip", parents=[common])
    slip_parser.add_argument("--id", required=True)
    slip_parser.add_argument("--source-id", required=True)
    slip_parser.add_argument("--target-id", required=True)
    slip_parser.add_argument("--start", required=True)
    slip_parser.add_argument("--duration", required=True)

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

    set_tempo_parser = sub.add_parser("set-tempo", parents=[common])
    set_tempo_parser.add_argument("--bpm", type=float, required=True, help="Master tempo knob base position; 0 releases warped clips back to native tempo and clears automation.")
    set_tempo_parser.add_argument("--max-stretch-pct", type=float, help="Warp stretch limit; material out of reach plays neutral.")
    set_tempo_parser.add_argument(
        "--points-json",
        help='Automate the master tempo knob: [{"at": "45:00.000", "value": 86}, ...]. Clips warp to the knob\'s value at their own start, so a ride lands with each incoming record.',
    )

    set_warp_parser = sub.add_parser("set-warp", parents=[common])
    set_warp_parser.add_argument("--id", required=True)
    set_warp_parser.add_argument("--off", action="store_true", help="Opt this event out of master-tempo warping (sample drops, free-time material).")
    set_warp_parser.add_argument("--source-bpm", type=float, help="Stamp the source BPM so the event can warp to the master tempo.")
    set_warp_parser.add_argument("--keymatch", action=argparse.BooleanOptionalAction, default=None, help="Toggle master-key matching for this event; disabling frees it for manual pitch shifts.")

    set_key_parser = sub.add_parser("set-key", parents=[common])
    set_key_parser.add_argument("--key", required=True, help='Master key for the set (e.g. "A minor", "F# major"); empty string releases keymatched events to native pitch.')
    set_key_parser.add_argument("--max-shift", type=int, help="Keymatch pitch limit in semitones per track (default 2).")
    set_key_parser.add_argument(
        "--points-json",
        help='Ride the master key across the set as step changes: [{"at": "60:00.000", "value": "C major"}, ...]. Modulate when upcoming material sits out of the current center\'s reach — clips match the key at their own start, so the change lands with an incoming record.',
    )

    routine_parser = sub.add_parser("instant-double-routine", parents=[common])
    routine_parser.add_argument("--source-id", required=True)
    routine_parser.add_argument("--id", required=True)
    routine_parser.add_argument("--recipe", required=True)
    routine_parser.add_argument("--start")
    routine_parser.add_argument("--cue-kind")
    routine_parser.add_argument("--cue-db", type=Path, default=session.DEFAULT_LIBRARY_DB)
    routine_parser.add_argument("--cache", type=Path, default=session.DEFAULT_DJ_CACHE)
    routine_parser.add_argument("--min-confidence", type=float, default=session.DEFAULT_MIN_BEATGRID_CONFIDENCE)

    args = parser.parse_args()
    resolve_live_defaults(args)

    if args.command == "add-action":
        action_payload = json.loads(args.action_json)
        if not isinstance(action_payload, dict):
            raise ValueError("--action-json must be a JSON object")
        args.affected_ids = [str(action_payload.get("id") or action_payload.get("action_id") or "")]
        apply_edit(
            args,
            lambda payload, lock_ms: session.add_action(
                payload,
                action=action_payload,
                db_path=args.db,
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
                deck=args.deck,
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
    elif args.command == "set-tempo":
        args.affected_ids = ["master-tempo"]
        apply_edit(
            args,
            lambda payload, _lock_ms: session.set_master_tempo(
                payload,
                args.bpm,
                max_tempo_stretch_pct=args.max_stretch_pct,
                points_json=args.points_json,
            ),
        )
    elif args.command == "set-warp":
        args.affected_ids = [args.id]
        apply_edit(
            args,
            lambda payload, lock_ms: session.set_event_warp(
                payload,
                args.id,
                warp=not args.off,
                source_bpm=args.source_bpm,
                keymatch=args.keymatch,
                lock_before_ms=lock_ms,
                force=args.force,
            ),
        )
    elif args.command == "set-key":
        args.affected_ids = ["master-key"]
        apply_edit(
            args,
            lambda payload, _lock_ms: session.set_master_key(
                payload,
                args.key,
                max_key_shift_semitones=args.max_shift,
                points_json=args.points_json,
            ),
        )
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
    elif args.command == "add-effect":
        if args.delay_ms is not None and args.delay_beats is not None:
            raise SystemExit("--delay-ms and --delay-beats are mutually exclusive")
        args.affected_ids = [args.id, args.target]
        effect_args = session.resolved_effect_args(args)
        apply_edit(
            args,
            lambda payload, lock_ms: session.add_effect_event(
                payload,
                effect_id=args.id,
                effect_type=args.type,
                target=args.target,
                start=args.start,
                duration=args.duration,
                tail_ms=effect_args["tail_ms"],
                wet=effect_args["wet"],
                gain_db=effect_args["gain_db"],
                delay_ms=effect_args["delay_ms"],
                feedback=effect_args["feedback"],
                room_size=effect_args["room_size"],
                damping=effect_args["damping"],
                lowpass_hz=effect_args["lowpass_hz"],
                preset=effect_args["preset"],
                delay_beats=args.delay_beats,
                cache_path=args.cache,
                beat_min_confidence=args.min_confidence,
                lock_before_ms=lock_ms,
                force=args.force,
            ),
        )
    elif args.command == "slip":
        args.affected_ids = [args.id, args.source_id, args.target_id]
        apply_edit(
            args,
            lambda payload, lock_ms: session.add_slip_event(
                payload,
                slip_id=args.id,
                source_id=args.source_id,
                target_id=args.target_id,
                start=args.start,
                duration=args.duration,
                lock_before_ms=lock_ms,
                force=args.force,
            ),
        )
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
    else:
        raise SystemExit(f"unsupported command: {args.command}")

    print(f"live edit applied: {args.command}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
