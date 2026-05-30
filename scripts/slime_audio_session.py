#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAX_DECKS = 4
AUTOMATABLE_PARAMS = {
    "gain_db",
    "lowpass_hz",
    "highpass_hz",
    "tempo_shift_pct",
    "pitch_shift_semitones",
    "eq_low_db",
    "eq_mid_db",
    "eq_high_db",
    "send_reverb",
    "duck_volume",
}


@dataclass(frozen=True)
class AutomationPoint:
    at_ms: int
    value: float | str | bool
    curve: str = "linear"


@dataclass(frozen=True)
class Automation:
    target: str
    param: str
    points: list[AutomationPoint]


@dataclass(frozen=True)
class Clip:
    id: str
    deck: str
    path: str
    start_ms: int
    trim_start_ms: int = 0
    duration_ms: int | None = None
    gain_db: float = 0.0
    tempo_shift_pct: float = 0.0
    pitch_shift_semitones: int = 0
    fade_in_ms: int = 0
    fade_out_ms: int = 0
    automations: list[Automation] = field(default_factory=list)

    @property
    def end_ms(self) -> int | None:
        if self.duration_ms is None:
            return None
        return self.start_ms + self.duration_ms


@dataclass(frozen=True)
class MicLeanIn:
    id: str
    start_ms: int
    text: str
    voice: str | None = None
    rate: str | None = None
    ducking: Automation | None = None


@dataclass(frozen=True)
class MixSession:
    version: int
    decks: list[str]
    clips: list[Clip]
    mic_lean_ins: list[MicLeanIn]
    automations: list[Automation]

    @property
    def event_ids(self) -> set[str]:
        return {clip.id for clip in self.clips} | {lean_in.id for lean_in in self.mic_lean_ins}


def parse_ms(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be milliseconds or a time string")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(round(value))
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be milliseconds or a time string")

    text = value.strip()
    if text.isdigit():
        return int(text)
    parts = text.split(":")
    if len(parts) not in {2, 3}:
        raise ValueError(f"invalid {field_name}: {value}")
    seconds = float(parts[-1])
    minutes = int(parts[-2])
    hours = int(parts[0]) if len(parts) == 3 else 0
    return int(round(((hours * 3600) + (minutes * 60) + seconds) * 1000))


def parse_automation(payload: dict[str, Any], default_target: str | None = None) -> Automation:
    target = str(payload.get("target") or default_target or "").strip()
    param = str(payload.get("param") or "").strip()
    points_payload = payload.get("points") or []
    if not target:
        raise ValueError("automation target is required")
    if not param:
        raise ValueError("automation param is required")
    if not isinstance(points_payload, list) or not points_payload:
        raise ValueError(f"automation {target}.{param} must include points")
    points = [
        AutomationPoint(
            at_ms=parse_ms(point.get("at_ms", point.get("at")), "automation point time"),
            value=point["value"],
            curve=str(point.get("curve") or "linear"),
        )
        for point in points_payload
    ]
    return Automation(target=target, param=param, points=points)


def parse_clip(payload: dict[str, Any]) -> Clip:
    clip_id = str(payload.get("id") or "").strip()
    deck = str(payload.get("deck") or "").strip()
    path = str(payload.get("path") or "").strip()
    if not clip_id:
        raise ValueError("clip id is required")
    if not deck:
        raise ValueError(f"clip {clip_id} deck is required")
    if not path:
        raise ValueError(f"clip {clip_id} path is required")
    duration = payload.get("duration_ms", payload.get("duration"))
    clip = Clip(
        id=clip_id,
        deck=deck,
        path=path,
        start_ms=parse_ms(payload.get("start_ms", payload.get("start", 0)), f"clip {clip_id} start"),
        trim_start_ms=parse_ms(payload.get("trim_start_ms", payload.get("trim_start", 0)), f"clip {clip_id} trim_start"),
        duration_ms=parse_ms(duration, f"clip {clip_id} duration") if duration is not None else None,
        gain_db=float(payload.get("gain_db", 0.0)),
        tempo_shift_pct=float(payload.get("tempo_shift_pct", 0.0)),
        pitch_shift_semitones=int(payload.get("pitch_shift_semitones", 0)),
        fade_in_ms=parse_ms(payload.get("fade_in_ms", 0), f"clip {clip_id} fade_in_ms"),
        fade_out_ms=parse_ms(payload.get("fade_out_ms", 0), f"clip {clip_id} fade_out_ms"),
        automations=[
            parse_automation(item, default_target=clip_id)
            for item in payload.get("automations", [])
        ],
    )
    return clip


def parse_mic_lean_in(payload: dict[str, Any]) -> MicLeanIn:
    lean_id = str(payload.get("id") or "").strip()
    text = str(payload.get("text") or "").strip()
    if not lean_id:
        raise ValueError("mic lean-in id is required")
    if not text:
        raise ValueError(f"mic lean-in {lean_id} text is required")
    ducking_payload = payload.get("ducking")
    return MicLeanIn(
        id=lean_id,
        start_ms=parse_ms(payload.get("start_ms", payload.get("start", 0)), f"mic lean-in {lean_id} start"),
        text=text,
        voice=str(payload["voice"]) if payload.get("voice") else None,
        rate=str(payload["rate"]) if payload.get("rate") else None,
        ducking=parse_automation(ducking_payload, default_target="master") if isinstance(ducking_payload, dict) else None,
    )


def load_session(path: Path) -> MixSession:
    payload = load_payload(path)
    return parse_session(payload)


def load_payload(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_payload(path: Path, payload: dict[str, Any]) -> None:
    parse_session(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def parse_session(payload: dict[str, Any]) -> MixSession:
    decks = [str(deck) for deck in payload.get("decks", [])]
    if not decks:
        decks = [f"deck-{index + 1}" for index in range(MAX_DECKS)]
    session = MixSession(
        version=int(payload.get("version", 1)),
        decks=decks,
        clips=[parse_clip(item) for item in payload.get("clips", [])],
        mic_lean_ins=[parse_mic_lean_in(item) for item in payload.get("mic_lean_ins", payload.get("micLeanIns", []))],
        automations=[parse_automation(item) for item in payload.get("automations", [])],
    )
    validate_session(session)
    return session


def validate_session(session: MixSession) -> None:
    errors: list[str] = []
    if session.version != 1:
        errors.append(f"unsupported session version: {session.version}")
    if len(session.decks) > MAX_DECKS:
        errors.append(f"too many decks: {len(session.decks)} > {MAX_DECKS}")
    if len(set(session.decks)) != len(session.decks):
        errors.append("deck names must be unique")

    seen_ids: set[str] = set()
    deck_set = set(session.decks)
    for event_id in [clip.id for clip in session.clips] + [lean_in.id for lean_in in session.mic_lean_ins]:
        if event_id in seen_ids:
            errors.append(f"duplicate event id: {event_id}")
        seen_ids.add(event_id)

    for clip in session.clips:
        if clip.deck not in deck_set:
            errors.append(f"clip {clip.id} uses unknown deck {clip.deck}")
        if clip.start_ms < 0:
            errors.append(f"clip {clip.id} starts before zero")
        if clip.trim_start_ms < 0:
            errors.append(f"clip {clip.id} trim starts before zero")
        if clip.duration_ms is not None and clip.duration_ms <= 0:
            errors.append(f"clip {clip.id} duration must be positive")
        if clip.fade_in_ms < 0 or clip.fade_out_ms < 0:
            errors.append(f"clip {clip.id} fades must be non-negative")
        for automation in clip.automations:
            validate_automation(automation, session.event_ids, errors, prefix=f"clip {clip.id}")

    for deck in session.decks:
        clips = sorted(
            [clip for clip in session.clips if clip.deck == deck and clip.end_ms is not None],
            key=lambda clip: clip.start_ms,
        )
        for left, right in zip(clips, clips[1:]):
            if left.end_ms is not None and left.end_ms > right.start_ms:
                errors.append(f"clips {left.id} and {right.id} overlap on {deck}")

    for lean_in in session.mic_lean_ins:
        if lean_in.start_ms < 0:
            errors.append(f"mic lean-in {lean_in.id} starts before zero")
        if lean_in.ducking is not None:
            validate_automation(lean_in.ducking, session.event_ids, errors, prefix=f"mic lean-in {lean_in.id}")

    for automation in session.automations:
        validate_automation(automation, session.event_ids, errors, prefix="session")

    if errors:
        raise ValueError("\n".join(errors))


def validate_automation(automation: Automation, event_ids: set[str], errors: list[str], prefix: str) -> None:
    if automation.param not in AUTOMATABLE_PARAMS:
        errors.append(f"{prefix} automation {automation.target}.{automation.param} is not an automatable param")
    if automation.target not in event_ids and automation.target not in {"master", "all"} and not automation.target.startswith("deck:"):
        errors.append(f"{prefix} automation target does not exist: {automation.target}")
    previous = -1
    for point in automation.points:
        if point.at_ms < 0:
            errors.append(f"{prefix} automation {automation.target}.{automation.param} has negative point time")
        if point.at_ms < previous:
            errors.append(f"{prefix} automation {automation.target}.{automation.param} points must be sorted")
        previous = point.at_ms


def session_summary(session: MixSession) -> dict[str, Any]:
    clips_by_deck = {
        deck: [
            {
                "id": clip.id,
                "path": clip.path,
                "start_ms": clip.start_ms,
                "trim_start_ms": clip.trim_start_ms,
                "duration_ms": clip.duration_ms,
                "end_ms": clip.end_ms,
            }
            for clip in sorted((item for item in session.clips if item.deck == deck), key=lambda item: item.start_ms)
        ]
        for deck in session.decks
    }
    return {
        "version": session.version,
        "decks": session.decks,
        "clip_count": len(session.clips),
        "mic_lean_in_count": len(session.mic_lean_ins),
        "automation_count": len(session.automations) + sum(len(clip.automations) for clip in session.clips),
        "clips_by_deck": clips_by_deck,
    }


def template_session() -> dict[str, Any]:
    return {
        "version": 1,
        "decks": ["deck-1", "deck-2", "deck-3", "deck-4"],
        "clips": [
            {
                "id": "intro-loop",
                "deck": "deck-1",
                "path": "/mnt/rockhouse/Music/example-a.flac",
                "start": "00:00.000",
                "trim_start": "00:32.000",
                "duration": "01:04.000",
                "gain_db": -1.5,
                "fade_in_ms": 250,
                "fade_out_ms": 2000,
            },
            {
                "id": "vocal-hook",
                "deck": "deck-2",
                "path": "/mnt/rockhouse/Music/example-b.flac",
                "start": "00:48.000",
                "trim_start": "01:16.000",
                "duration": "00:24.000",
                "gain_db": -4.0,
            },
        ],
        "mic_lean_ins": [
            {
                "id": "squid-drop-1",
                "start": "00:44.000",
                "text": "incoming, try to act normal",
                "ducking": {
                    "target": "master",
                    "param": "duck_volume",
                    "points": [
                        {"at": "00:43.750", "value": 0.45},
                        {"at": "00:47.000", "value": 1.0},
                    ],
                },
            }
        ],
        "automations": [
            {
                "target": "vocal-hook",
                "param": "gain_db",
                "points": [
                    {"at": "00:48.000", "value": -18.0},
                    {"at": "00:52.000", "value": -4.0},
                ],
            }
        ],
    }


def base_payload(path: Path, create: bool) -> dict[str, Any]:
    if path.exists():
        return load_payload(path)
    if not create:
        raise FileNotFoundError(path)
    return {"version": 1, "decks": [f"deck-{index + 1}" for index in range(MAX_DECKS)], "clips": [], "mic_lean_ins": [], "automations": []}


def find_event(payload: dict[str, Any], event_id: str) -> tuple[str, int] | None:
    for collection in ("clips", "mic_lean_ins"):
        for index, item in enumerate(payload.get(collection, [])):
            if item.get("id") == event_id:
                return collection, index
    return None


def require_unique_event_id(payload: dict[str, Any], event_id: str) -> None:
    if find_event(payload, event_id) is not None:
        raise ValueError(f"event id already exists: {event_id}")


def add_clip(
    payload: dict[str, Any],
    *,
    clip_id: str,
    deck: str,
    path: str,
    start: str,
    trim_start: str,
    duration: str | None,
    gain_db: float,
    fade_in_ms: int,
    fade_out_ms: int,
) -> dict[str, Any]:
    next_payload = copy.deepcopy(payload)
    require_unique_event_id(next_payload, clip_id)
    clip: dict[str, Any] = {
        "id": clip_id,
        "deck": deck,
        "path": path,
        "start": start,
        "trim_start": trim_start,
        "gain_db": gain_db,
        "fade_in_ms": fade_in_ms,
        "fade_out_ms": fade_out_ms,
    }
    if duration is not None:
        clip["duration"] = duration
    next_payload.setdefault("clips", []).append(clip)
    parse_session(next_payload)
    return next_payload


def add_mic_lean_in(
    payload: dict[str, Any],
    *,
    lean_id: str,
    start: str,
    text: str,
    voice: str | None,
    rate: str | None,
    duck_volume: float | None,
    duck_ms: int,
) -> dict[str, Any]:
    next_payload = copy.deepcopy(payload)
    require_unique_event_id(next_payload, lean_id)
    lean_in: dict[str, Any] = {"id": lean_id, "start": start, "text": text}
    if voice is not None:
        lean_in["voice"] = voice
    if rate is not None:
        lean_in["rate"] = rate
    if duck_volume is not None:
        start_ms = parse_ms(start, f"mic lean-in {lean_id} start")
        lean_in["ducking"] = {
            "target": "master",
            "param": "duck_volume",
            "points": [
                {"at": max(0, start_ms - 250), "value": duck_volume},
                {"at": start_ms + duck_ms, "value": 1.0},
            ],
        }
    next_payload.setdefault("mic_lean_ins", []).append(lean_in)
    parse_session(next_payload)
    return next_payload


def remove_event(payload: dict[str, Any], event_id: str) -> dict[str, Any]:
    next_payload = copy.deepcopy(payload)
    found = find_event(next_payload, event_id)
    if found is None:
        raise ValueError(f"event id does not exist: {event_id}")
    collection, index = found
    del next_payload[collection][index]
    next_payload["automations"] = [
        automation for automation in next_payload.get("automations", []) if automation.get("target") != event_id
    ]
    for clip in next_payload.get("clips", []):
        clip["automations"] = [
            automation for automation in clip.get("automations", []) if automation.get("target", clip.get("id")) != event_id
        ]
    parse_session(next_payload)
    return next_payload


def move_event(payload: dict[str, Any], event_id: str, start: str) -> dict[str, Any]:
    next_payload = copy.deepcopy(payload)
    found = find_event(next_payload, event_id)
    if found is None:
        raise ValueError(f"event id does not exist: {event_id}")
    collection, index = found
    next_payload[collection][index]["start"] = start
    parse_session(next_payload)
    return next_payload


def add_automation(
    payload: dict[str, Any],
    *,
    target: str,
    param: str,
    points_json: str,
) -> dict[str, Any]:
    next_payload = copy.deepcopy(payload)
    points = json.loads(points_json)
    if not isinstance(points, list):
        raise ValueError("--points-json must be a JSON list")
    automation = {"target": target, "param": param, "points": points}
    next_payload.setdefault("automations", []).append(automation)
    parse_session(next_payload)
    return next_payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate and inspect mutable SlimeAudio mix sessions.")
    sub = parser.add_subparsers(dest="command", required=True)
    validate_parser = sub.add_parser("validate")
    validate_parser.add_argument("session", type=Path)
    summary_parser = sub.add_parser("summary")
    summary_parser.add_argument("session", type=Path)
    sub.add_parser("template")
    add_clip_parser = sub.add_parser("add-clip")
    add_clip_parser.add_argument("session", type=Path)
    add_clip_parser.add_argument("--create", action="store_true")
    add_clip_parser.add_argument("--id", required=True)
    add_clip_parser.add_argument("--deck", required=True)
    add_clip_parser.add_argument("--path", required=True)
    add_clip_parser.add_argument("--start", required=True)
    add_clip_parser.add_argument("--trim-start", default="0")
    add_clip_parser.add_argument("--duration")
    add_clip_parser.add_argument("--gain-db", type=float, default=0.0)
    add_clip_parser.add_argument("--fade-in-ms", type=int, default=0)
    add_clip_parser.add_argument("--fade-out-ms", type=int, default=0)

    add_mic_parser = sub.add_parser("add-mic")
    add_mic_parser.add_argument("session", type=Path)
    add_mic_parser.add_argument("--create", action="store_true")
    add_mic_parser.add_argument("--id", required=True)
    add_mic_parser.add_argument("--start", required=True)
    add_mic_parser.add_argument("--text", required=True)
    add_mic_parser.add_argument("--voice")
    add_mic_parser.add_argument("--rate")
    add_mic_parser.add_argument("--duck-volume", type=float)
    add_mic_parser.add_argument("--duck-ms", type=int, default=2500)

    remove_parser = sub.add_parser("remove")
    remove_parser.add_argument("session", type=Path)
    remove_parser.add_argument("--id", required=True)

    move_parser = sub.add_parser("move")
    move_parser.add_argument("session", type=Path)
    move_parser.add_argument("--id", required=True)
    move_parser.add_argument("--start", required=True)

    automate_parser = sub.add_parser("automate")
    automate_parser.add_argument("session", type=Path)
    automate_parser.add_argument("--target", required=True)
    automate_parser.add_argument("--param", required=True)
    automate_parser.add_argument("--points-json", required=True)
    args = parser.parse_args()

    if args.command == "template":
        print(json.dumps(template_session(), indent=2, sort_keys=True))
        return 0

    if args.command == "add-clip":
        payload = base_payload(args.session, args.create)
        updated = add_clip(
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
        )
        write_payload(args.session, updated)
        print(f"added clip {args.id}")
        return 0

    if args.command == "add-mic":
        payload = base_payload(args.session, args.create)
        updated = add_mic_lean_in(
            payload,
            lean_id=args.id,
            start=args.start,
            text=args.text,
            voice=args.voice,
            rate=args.rate,
            duck_volume=args.duck_volume,
            duck_ms=args.duck_ms,
        )
        write_payload(args.session, updated)
        print(f"added mic lean-in {args.id}")
        return 0

    if args.command == "remove":
        write_payload(args.session, remove_event(load_payload(args.session), args.id))
        print(f"removed {args.id}")
        return 0

    if args.command == "move":
        write_payload(args.session, move_event(load_payload(args.session), args.id, args.start))
        print(f"moved {args.id}")
        return 0

    if args.command == "automate":
        write_payload(
            args.session,
            add_automation(load_payload(args.session), target=args.target, param=args.param, points_json=args.points_json),
        )
        print(f"added automation {args.target}.{args.param}")
        return 0

    session = load_session(args.session)
    if args.command == "validate":
        print(f"ok clips={len(session.clips)} mic_lean_ins={len(session.mic_lean_ins)} automations={len(session.automations)}")
        return 0
    print(json.dumps(session_summary(session), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
