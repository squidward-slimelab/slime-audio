#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from slime_audio_session import (
    VOCAL_DECK,
    add_mic_lean_in,
    base_payload,
    edit_lock_ms_from_state,
    load_payload,
    parse_ms,
    parse_session,
    playhead_ms_from_state,
    write_payload,
)

DEFAULT_SESSION = Path("runtime/mix-session.json")
DEFAULT_STATE = Path("runtime/mix-session-state.json")
DEFAULT_LOG = Path("runtime/commentary-plan.jsonl")


@dataclass(frozen=True)
class CommentaryCandidate:
    start_ms: int
    text: str
    kind: str
    clip_id: str | None
    track: str | None
    next_track: str | None
    reason: str


def format_ms(value: int) -> str:
    value = max(0, value)
    hours, remainder = divmod(value, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1000)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"
    return f"{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


def title(path_text: str | None) -> str:
    if not path_text:
        return "the next thing"
    stem = Path(path_text).stem
    return stem or "the next thing"


def existing_lean_times(payload: dict[str, Any]) -> list[int]:
    return sorted(
        parse_ms(item.get("start_ms", item.get("start", 0)), "mic lean-in start")
        for item in payload.get("mic_lean_ins", payload.get("micLeanIns", []))
        if isinstance(item, dict)
    )


def is_spaced(start_ms: int, chosen: list[int], existing: list[int], spacing_ms: int) -> bool:
    return all(abs(start_ms - other) >= spacing_ms for other in [*chosen, *existing])


def transition_text(current: str | None, upcoming: str | None, index: int) -> str:
    templates = [
        "quick note. {current} is making room for {upcoming}. try to look unsurprised.",
        "incoming shift. {upcoming} should fit here, which is annoying because it means the plan is working.",
        "small booth memo. this next blend leans toward {upcoming}; keep the elbows loose.",
        "texture change ahead. {current} into {upcoming}. absurdly reasonable decision.",
    ]
    return templates[index % len(templates)].format(current=title(current), upcoming=title(upcoming))


def track_text(track: str | None, index: int) -> str:
    templates = [
        "tiny interruption. {track} has the wheel for a minute.",
        "control room note. {track} is doing enough work that i will allow it.",
        "briefly: {track}. good little pressure pocket here.",
    ]
    return templates[index % len(templates)].format(track=title(track))


def intro_text(first_track: str | None) -> str:
    return f"alright. live mix is in timestamp mode now. opening stretch starts with {title(first_track)}."


def tension_text(window: dict[str, Any], index: int) -> str:
    points = [str(point) for point in window.get("talking_points", []) if str(point).strip()]
    if points:
        point = next((value for value in points if not value.startswith("analysis estimate:")), points[0])
        return f"quick note. {point}"
    kind = str(window.get("kind") or "tension")
    track = title(window.get("track"))
    if kind == "pre-drop":
        return f"quick note. {track} is about to open up, so i will get out of the way."
    if kind == "transition":
        return transition_text(window.get("track"), window.get("next_track"), index)
    return f"quick note. {track} has a useful {kind} pocket here."


def load_tension_candidates(path: Path, *, earliest_ms: int) -> list[CommentaryCandidate]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    if not isinstance(payload, list):
        return []
    candidates: list[CommentaryCandidate] = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            continue
        start_ms = int(item.get("start_ms", 0))
        if start_ms < earliest_ms:
            continue
        candidates.append(
            CommentaryCandidate(
                start_ms=start_ms,
                text=tension_text(item, index),
                kind=str(item.get("kind") or "tension"),
                clip_id=item.get("clip_id"),
                track=item.get("track"),
                next_track=item.get("next_track"),
                reason=str(item.get("reason") or "analysis tension window"),
            )
        )
    return sorted(candidates, key=lambda item: (item.start_ms, item.kind))


def build_candidates(
    payload: dict[str, Any],
    *,
    earliest_ms: int,
    include_intro: bool,
    intro_cutoff_ms: int,
    transition_lead_ms: int,
    track_offset_ms: int,
) -> list[CommentaryCandidate]:
    session = parse_session(payload)
    clips = sorted(session.clips, key=lambda clip: (clip.start_ms, clip.deck, clip.id))
    candidates: list[CommentaryCandidate] = []
    if include_intro and clips and earliest_ms <= intro_cutoff_ms:
        start_ms = max(earliest_ms, clips[0].start_ms + min(track_offset_ms, 10_000))
        candidates.append(
            CommentaryCandidate(
                start_ms=start_ms,
                text=intro_text(clips[0].path),
                kind="intro",
                clip_id=clips[0].id,
                track=clips[0].path,
                next_track=clips[1].path if len(clips) > 1 else None,
                reason="start-of-set intro",
            )
        )

    for index, clip in enumerate(clips):
        next_clip = clips[index + 1] if index + 1 < len(clips) else None
        if next_clip is not None:
            start_ms = next_clip.start_ms - transition_lead_ms
            if start_ms >= earliest_ms:
                candidates.append(
                    CommentaryCandidate(
                        start_ms=start_ms,
                        text=transition_text(clip.path, next_clip.path, index),
                        kind="transition",
                        clip_id=next_clip.id,
                        track=clip.path,
                        next_track=next_clip.path,
                        reason="before upcoming clip start",
                    )
                )
        track_start = clip.start_ms + track_offset_ms
        if track_start >= earliest_ms:
            candidates.append(
                CommentaryCandidate(
                    start_ms=track_start,
                    text=track_text(clip.path, index),
                    kind="track",
                    clip_id=clip.id,
                    track=clip.path,
                    next_track=next_clip.path if next_clip is not None else None,
                    reason="inside future clip",
                )
            )
    return sorted(candidates, key=lambda item: (item.start_ms, item.kind))


def choose_candidates(
    candidates: list[CommentaryCandidate],
    *,
    existing: list[int],
    count: int,
    spacing_ms: int,
    horizon_ms: int | None,
    earliest_ms: int,
) -> list[CommentaryCandidate]:
    chosen: list[CommentaryCandidate] = []
    chosen_times: list[int] = []
    latest_ms = earliest_ms + horizon_ms if horizon_ms is not None else None
    for candidate in candidates:
        if latest_ms is not None and candidate.start_ms > latest_ms:
            continue
        if not is_spaced(candidate.start_ms, chosen_times, existing, spacing_ms):
            continue
        chosen.append(candidate)
        chosen_times.append(candidate.start_ms)
        if len(chosen) >= count:
            break
    return chosen


def add_commentary(
    payload: dict[str, Any],
    candidate: CommentaryCandidate,
    *,
    lean_id: str,
    voice: str | None,
    rate: str | None,
    deck: str,
    volume: float,
    duck_volume: float,
    lowpass_hz: float,
    duck_ms: int,
    lock_before_ms: int | None,
    force: bool,
) -> dict[str, Any]:
    return add_mic_lean_in(
        payload,
        lean_id=lean_id,
        start=format_ms(candidate.start_ms),
        text=candidate.text,
        deck=deck,
        voice=voice,
        rate=rate,
        volume=volume,
        duck_volume=duck_volume,
        lowpass_hz=lowpass_hz,
        duck_ms=duck_ms,
        lock_before_ms=lock_before_ms,
        force=force,
    )


def log_event(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")


def parse_args_from(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plan tasteful live DJ commentary as future mix-session lean-ins.")
    parser.add_argument("--session", type=Path, default=DEFAULT_SESSION)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--create", action="store_true")
    parser.add_argument("--id-prefix", default="commentary")
    parser.add_argument("--count", type=int, default=3)
    parser.add_argument("--lead-ms", type=int, default=90_000)
    parser.add_argument("--min-spacing-ms", type=int, default=240_000)
    parser.add_argument("--horizon-ms", type=int, default=45 * 60_000)
    parser.add_argument("--intro", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--intro-cutoff-ms", type=int, default=180_000)
    parser.add_argument("--transition-lead-ms", type=int, default=8_000)
    parser.add_argument("--track-offset-ms", type=int, default=20_000)
    parser.add_argument("--tension-plan", type=Path)
    parser.add_argument("--voice")
    parser.add_argument("--rate")
    parser.add_argument("--deck", default=VOCAL_DECK)
    parser.add_argument("--volume", type=float, default=1.7)
    parser.add_argument("--duck-volume", type=float, default=0.45)
    parser.add_argument("--lowpass-hz", type=float, default=1400.0)
    parser.add_argument("--duck-ms", type=int, default=3500)
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args_from()
    if args.count <= 0:
        raise SystemExit("--count must be positive")
    payload = base_payload(args.session, args.create)
    playhead_ms = playhead_ms_from_state(args.state) if args.state.exists() else 0
    lock_ms = edit_lock_ms_from_state(args.state) if args.state.exists() else 0
    earliest_ms = max(playhead_ms + args.lead_ms, lock_ms)
    existing = existing_lean_times(payload)
    candidates = build_candidates(
        payload,
        earliest_ms=earliest_ms,
        include_intro=args.intro,
        intro_cutoff_ms=args.intro_cutoff_ms,
        transition_lead_ms=args.transition_lead_ms,
        track_offset_ms=args.track_offset_ms,
    )
    if args.tension_plan is not None:
        candidates = [
            *load_tension_candidates(args.tension_plan, earliest_ms=earliest_ms),
            *candidates,
        ]
    chosen = choose_candidates(
        candidates,
        existing=existing,
        count=args.count,
        spacing_ms=args.min_spacing_ms,
        horizon_ms=args.horizon_ms,
        earliest_ms=earliest_ms,
    )
    if not chosen:
        print("no commentary windows available")
        return 0

    updated = payload
    timestamp = int(time.time())
    for index, candidate in enumerate(chosen, start=1):
        lean_id = f"{args.id_prefix}-{timestamp}-{index}"
        if not args.dry_run:
            updated = add_commentary(
                updated,
                candidate,
                lean_id=lean_id,
                voice=args.voice,
                rate=args.rate,
                deck=args.deck,
                volume=args.volume,
                duck_volume=args.duck_volume,
                lowpass_hz=args.lowpass_hz,
                duck_ms=args.duck_ms,
                lock_before_ms=playhead_ms,
                force=args.force,
            )
        event = {
            "event": "commentary_planned",
            "id": lean_id,
            "session": str(args.session),
            "state": str(args.state),
            "start_ms": candidate.start_ms,
            "kind": candidate.kind,
            "clip_id": candidate.clip_id,
            "track": candidate.track,
            "next_track": candidate.next_track,
            "reason": candidate.reason,
            "text": candidate.text,
            "volume": args.volume,
            "duck_volume": args.duck_volume,
            "lowpass_hz": args.lowpass_hz,
            "dry_run": args.dry_run,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        log_event(args.log, event)
        print(f"planned {lean_id} at {format_ms(candidate.start_ms)} {candidate.kind}: {candidate.text}")
    if not args.dry_run:
        write_payload(args.session, updated)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
