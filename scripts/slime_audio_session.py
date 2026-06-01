#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from time import time
from typing import Any

MAX_DECKS = 4
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DJ_CACHE = REPO_ROOT / "runtime" / "dj-analysis-cache.json"
DEFAULT_MIN_BEATGRID_CONFIDENCE = 0.45
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
    volume: float = 1.0
    effects: list[Automation] = field(default_factory=list)

    @property
    def ducking(self) -> Automation | None:
        return next((effect for effect in self.effects if effect.param == "duck_volume"), None)


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


def parse_timestamp(value: str | None) -> float | None:
    if not value:
        return None
    text = value.strip()
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        if len(text) >= 5 and (text[-5] in {"+", "-"}) and text[-3] != ":":
            text = f"{text[:-2]}:{text[-2:]}"
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


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
    effects: list[Automation] = []
    ducking_payload = payload.get("ducking")
    if isinstance(ducking_payload, dict):
        effects.append(parse_automation(ducking_payload, default_target="master"))
    lowpass_payload = payload.get("lowpass")
    if isinstance(lowpass_payload, dict):
        effects.append(parse_automation(lowpass_payload, default_target="master"))
    effects.extend(parse_automation(item, default_target="master") for item in payload.get("effects", []))
    return MicLeanIn(
        id=lean_id,
        start_ms=parse_ms(payload.get("start_ms", payload.get("start", 0)), f"mic lean-in {lean_id} start"),
        text=text,
        voice=str(payload["voice"]) if payload.get("voice") else None,
        rate=str(payload["rate"]) if payload.get("rate") else None,
        volume=float(payload.get("volume", 1.0)),
        effects=effects,
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


def playhead_ms_from_state(path: Path, now: float | None = None) -> int:
    payload = json.loads(path.read_text(encoding="utf-8"))
    explicit = payload.get("playhead_ms", payload.get("mix_playhead_ms"))
    if explicit is not None:
        return max(0, parse_ms(explicit, "state playhead"))
    window_started_at = parse_timestamp(payload.get("window_started_at"))
    if window_started_at is not None:
        window_start_ms = parse_ms(payload.get("window_start_ms", 0), "state window start")
        return max(0, window_start_ms + int(round(((now if now is not None else time()) - window_started_at) * 1000)))
    mix_started_at = parse_timestamp(payload.get("mix_started_at"))
    if mix_started_at is not None:
        return max(0, int(round(((now if now is not None else time()) - mix_started_at) * 1000)))
    started_at = parse_timestamp(payload.get("started_at"))
    if started_at is None:
        raise ValueError(f"state has no started_at/playhead_ms: {path}")
    elapsed_ms = max(0, int(round(((now if now is not None else time()) - started_at) * 1000)))
    order = payload.get("order")
    if not isinstance(order, list):
        return elapsed_ms

    durations: dict[str, int] = {}
    inline_durations = payload.get("timeline_durations_ms")
    if isinstance(inline_durations, dict):
        durations.update(
            {
                str(track): int(duration)
                for track, duration in inline_durations.items()
                if isinstance(duration, (int, float)) and duration > 0
            }
        )
    cache_path = path.parent / "timeline-duration-cache.json"
    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cached = {}
        if isinstance(cached, dict):
            durations.update(
                {
                    str(track): int(duration)
                    for track, duration in cached.items()
                    if isinstance(duration, (int, float)) and duration > 0
                }
            )

    index = int(payload.get("index", 0) or 0)
    prior_tracks = [str(track) for track in order[: max(0, index)]]
    if prior_tracks and any(track not in durations for track in prior_tracks):
        return elapsed_ms
    return sum(durations.get(track, 0) for track in prior_tracks) + elapsed_ms


def read_playlist(path: Path) -> list[str]:
    tracks = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not tracks:
        raise ValueError(f"playlist is empty: {path}")
    return tracks


def slug(value: str, fallback: str) -> str:
    stem = Path(value).stem or fallback
    text = re.sub(r"[^a-zA-Z0-9]+", "-", stem.lower()).strip("-")
    return text or fallback


def probe_duration_ms(path: str) -> int:
    result = subprocess.run(
        [
            "ffprobe",
            "-hide_banner",
            "-loglevel",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            path,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    duration_seconds = float(result.stdout.strip())
    if duration_seconds <= 0:
        raise ValueError(f"could not determine positive duration for {path}")
    return int(round(duration_seconds * 1000))


def playlist_to_session_payload(
    tracks: list[str],
    *,
    start_ms: int,
    decks: list[str],
    gap_ms: int,
    overlap_ms: int,
    default_duration_ms: int | None,
    probe: bool,
) -> dict[str, Any]:
    if gap_ms and overlap_ms:
        raise ValueError("gap_ms and overlap_ms cannot both be set")
    if not decks:
        decks = [f"deck-{index + 1}" for index in range(MAX_DECKS)]
    if len(decks) > MAX_DECKS:
        raise ValueError(f"too many decks: {len(decks)} > {MAX_DECKS}")

    cursor_ms = start_ms
    clips: list[dict[str, Any]] = []
    for index, track in enumerate(tracks):
        if probe:
            duration_ms = probe_duration_ms(track)
        elif default_duration_ms is not None:
            duration_ms = default_duration_ms
        else:
            duration_ms = None
        clip: dict[str, Any] = {
            "id": f"clip-{index + 1:03d}-{slug(track, f'track-{index + 1}')}",
            "deck": decks[index % len(decks)],
            "path": track,
            "start_ms": cursor_ms,
            "trim_start_ms": 0,
        }
        if duration_ms is not None:
            clip["duration_ms"] = duration_ms
            cursor_ms += max(0, duration_ms + gap_ms - overlap_ms)
        clips.append(clip)

    payload = {
        "version": 1,
        "decks": decks,
        "clips": clips,
        "mic_lean_ins": [],
        "automations": [],
    }
    parse_session(payload)
    return payload


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
        for effect in lean_in.effects:
            validate_automation(effect, session.event_ids, errors, prefix=f"mic lean-in {lean_in.id}")

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
        "automation_count": (
            len(session.automations)
            + sum(len(clip.automations) for clip in session.clips)
            + sum(len(lean_in.effects) for lean_in in session.mic_lean_ins)
        ),
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
                "volume": 1.4,
                "ducking": {
                    "target": "master",
                    "param": "duck_volume",
                    "points": [
                        {"at": "00:43.750", "value": 0.45},
                        {"at": "00:47.000", "value": 1.0},
                    ],
                },
                "lowpass": {
                    "target": "master",
                    "param": "lowpass_hz",
                    "points": [
                        {"at": "00:43.750", "value": 1400},
                        {"at": "00:47.000", "value": 22050},
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


def event_start_ms(item: dict[str, Any]) -> int:
    return parse_ms(item.get("start_ms", item.get("start", 0)), "event start")


def event_end_ms(item: dict[str, Any]) -> int | None:
    duration = item.get("duration_ms", item.get("duration"))
    if duration is None:
        return None
    return event_start_ms(item) + parse_ms(duration, "event duration")


def parse_beats(value: str) -> Fraction:
    try:
        beats = Fraction(value)
    except ValueError as error:
        raise ValueError(f"invalid beat count: {value}") from error
    if beats == 0:
        raise ValueError("beat jump cannot be zero")
    if abs(beats) not in {Fraction(1, 2), Fraction(1, 1), Fraction(2, 1), Fraction(4, 1), Fraction(8, 1)}:
        raise ValueError("beat jump must be one of +/-1/2, +/-1, +/-2, +/-4, +/-8 beats")
    return beats


def cached_beatgrid(
    cache_path: Path,
    clip_path: str,
    *,
    min_confidence: float = DEFAULT_MIN_BEATGRID_CONFIDENCE,
    force: bool = False,
) -> tuple[float, int, float]:
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ValueError(f"dj analysis cache not found: {cache_path}") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"dj analysis cache is not valid JSON: {cache_path}") from error
    if not isinstance(cache, dict):
        raise ValueError(f"dj analysis cache must be an object: {cache_path}")

    normalized = str(Path(clip_path))
    analysis = None
    for value in cache.values():
        if not isinstance(value, dict):
            continue
        path = str(value.get("path") or "")
        if path == clip_path or str(Path(path)) == normalized:
            analysis = value
            break
    if analysis is None:
        raise ValueError(f"no cached beatgrid analysis for {clip_path}")

    beatgrid = analysis.get("beatgrid") if isinstance(analysis.get("beatgrid"), dict) else {}
    bpm = beatgrid.get("bpm", analysis.get("bpm"))
    beat_offset_ms = beatgrid.get("beat_offset_ms", analysis.get("beat_offset_ms"))
    confidence_payload = analysis.get("confidence") if isinstance(analysis.get("confidence"), dict) else {}
    confidence = float(confidence_payload.get("bpm", 0.0) or 0.0)
    if bpm is None or float(bpm) <= 0:
        raise ValueError(f"cached analysis has no usable bpm for {clip_path}")
    if beat_offset_ms is None:
        raise ValueError(f"cached analysis has no usable beat offset for {clip_path}")
    if confidence < min_confidence and not force:
        raise ValueError(
            f"cached beatgrid confidence too low for {clip_path}: {confidence:.3f} < {min_confidence:.3f}; use --force to override"
        )
    return float(bpm), parse_ms(beat_offset_ms, "beat offset"), confidence


def beat_quantum_ms(bpm: float, beats: Fraction) -> float:
    return (60_000 / bpm) / beats.denominator


def quantize_ms(value_ms: int, *, offset_ms: int, quantum_ms: float) -> int:
    steps = round((value_ms - offset_ms) / quantum_ms)
    return int(round(offset_ms + (steps * quantum_ms)))


def guard_live_edit(
    *,
    label: str,
    start_ms: int,
    lock_before_ms: int | None,
    force: bool,
) -> None:
    if force or lock_before_ms is None:
        return
    if start_ms < lock_before_ms:
        raise ValueError(
            f"{label} is before the live edit lock ({start_ms}ms < {lock_before_ms}ms); "
            "only future events can be edited without --force"
        )


def guard_event_live_edit(
    payload: dict[str, Any],
    event_id: str,
    *,
    lock_before_ms: int | None,
    force: bool,
) -> None:
    found = find_event(payload, event_id)
    if found is None:
        raise ValueError(f"event id does not exist: {event_id}")
    collection, index = found
    item = payload[collection][index]
    start_ms = event_start_ms(item)
    guard_live_edit(label=f"event {event_id}", start_ms=start_ms, lock_before_ms=lock_before_ms, force=force)


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
    lock_before_ms: int | None = None,
    force: bool = False,
) -> dict[str, Any]:
    next_payload = copy.deepcopy(payload)
    require_unique_event_id(next_payload, clip_id)
    guard_live_edit(
        label=f"clip {clip_id}",
        start_ms=parse_ms(start, f"clip {clip_id} start"),
        lock_before_ms=lock_before_ms,
        force=force,
    )
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
    volume: float,
    duck_volume: float | None,
    lowpass_hz: float | None,
    duck_ms: int,
    lock_before_ms: int | None = None,
    force: bool = False,
) -> dict[str, Any]:
    next_payload = copy.deepcopy(payload)
    require_unique_event_id(next_payload, lean_id)
    start_ms = parse_ms(start, f"mic lean-in {lean_id} start")
    guard_live_edit(label=f"mic lean-in {lean_id}", start_ms=start_ms, lock_before_ms=lock_before_ms, force=force)
    lean_in: dict[str, Any] = {"id": lean_id, "start": start, "text": text}
    if voice is not None:
        lean_in["voice"] = voice
    if rate is not None:
        lean_in["rate"] = rate
    lean_in["volume"] = volume
    if duck_volume is not None:
        lean_in["ducking"] = {
            "target": "master",
            "param": "duck_volume",
            "points": [
                {"at": max(0, start_ms - 250), "value": duck_volume},
                {"at": start_ms + duck_ms, "value": 1.0},
            ],
        }
        if lowpass_hz is not None:
            lean_in["lowpass"] = {
                "target": "master",
                "param": "lowpass_hz",
                "points": [
                    {"at": max(0, start_ms - 250), "value": lowpass_hz},
                    {"at": start_ms + duck_ms, "value": 22050},
                ],
            }
    next_payload.setdefault("mic_lean_ins", []).append(lean_in)
    parse_session(next_payload)
    return next_payload


def remove_event(
    payload: dict[str, Any],
    event_id: str,
    *,
    lock_before_ms: int | None = None,
    force: bool = False,
) -> dict[str, Any]:
    next_payload = copy.deepcopy(payload)
    found = find_event(next_payload, event_id)
    if found is None:
        raise ValueError(f"event id does not exist: {event_id}")
    guard_event_live_edit(next_payload, event_id, lock_before_ms=lock_before_ms, force=force)
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


def move_event(
    payload: dict[str, Any],
    event_id: str,
    start: str,
    *,
    lock_before_ms: int | None = None,
    force: bool = False,
) -> dict[str, Any]:
    next_payload = copy.deepcopy(payload)
    found = find_event(next_payload, event_id)
    if found is None:
        raise ValueError(f"event id does not exist: {event_id}")
    guard_event_live_edit(next_payload, event_id, lock_before_ms=lock_before_ms, force=force)
    guard_live_edit(
        label=f"new start for {event_id}",
        start_ms=parse_ms(start, f"event {event_id} start"),
        lock_before_ms=lock_before_ms,
        force=force,
    )
    collection, index = found
    next_payload[collection][index]["start"] = start
    parse_session(next_payload)
    return next_payload


def beat_jump_clip(
    payload: dict[str, Any],
    event_id: str,
    beats_text: str,
    *,
    field: str = "trim-start",
    cache_path: Path = DEFAULT_DJ_CACHE,
    min_confidence: float = DEFAULT_MIN_BEATGRID_CONFIDENCE,
    lock_before_ms: int | None = None,
    force: bool = False,
) -> dict[str, Any]:
    beats = parse_beats(beats_text)
    next_payload = copy.deepcopy(payload)
    found = find_event(next_payload, event_id)
    if found is None:
        raise ValueError(f"event id does not exist: {event_id}")
    collection, index = found
    if collection != "clips":
        raise ValueError(f"beat-jump only supports clips, not {collection}: {event_id}")
    clip = next_payload[collection][index]
    guard_event_live_edit(next_payload, event_id, lock_before_ms=lock_before_ms, force=force)

    bpm, beat_offset_ms, _confidence = cached_beatgrid(
        cache_path,
        str(clip.get("path") or ""),
        min_confidence=min_confidence,
        force=force,
    )
    quantum_ms = beat_quantum_ms(bpm, beats)
    delta_ms = int(round(float(beats) * (60_000 / bpm)))
    if field == "trim-start":
        current_trim = parse_ms(clip.get("trim_start_ms", clip.get("trim_start", 0)), f"clip {event_id} trim_start")
        updated_trim = quantize_ms(current_trim + delta_ms, offset_ms=beat_offset_ms, quantum_ms=quantum_ms)
        if updated_trim < 0:
            raise ValueError(f"beat jump would move clip {event_id} before source zero")
        clip.pop("trim_start", None)
        clip["trim_start_ms"] = updated_trim
    elif field == "start":
        current_start = event_start_ms(clip)
        current_trim = parse_ms(clip.get("trim_start_ms", clip.get("trim_start", 0)), f"clip {event_id} trim_start")
        timeline_offset_ms = current_start + beat_offset_ms - current_trim
        updated_start = quantize_ms(current_start + delta_ms, offset_ms=timeline_offset_ms, quantum_ms=quantum_ms)
        guard_live_edit(label=f"new start for {event_id}", start_ms=updated_start, lock_before_ms=lock_before_ms, force=force)
        clip.pop("start", None)
        clip["start_ms"] = updated_start
    else:
        raise ValueError("--field must be trim-start or start")
    parse_session(next_payload)
    return next_payload


def add_automation(
    payload: dict[str, Any],
    *,
    target: str,
    param: str,
    points_json: str,
    lock_before_ms: int | None = None,
    force: bool = False,
) -> dict[str, Any]:
    next_payload = copy.deepcopy(payload)
    points = json.loads(points_json)
    if not isinstance(points, list):
        raise ValueError("--points-json must be a JSON list")
    for point in points:
        if isinstance(point, dict):
            guard_live_edit(
                label=f"automation {target}.{param}",
                start_ms=parse_ms(point.get("at_ms", point.get("at", 0)), "automation point time"),
                lock_before_ms=lock_before_ms,
                force=force,
            )
    automation = {"target": target, "param": param, "points": points}
    next_payload.setdefault("automations", []).append(automation)
    parse_session(next_payload)
    return next_payload


def clip_start_end(payload: dict[str, Any], clip_id: str) -> tuple[int, int]:
    found = find_event(payload, clip_id)
    if found is None:
        raise ValueError(f"event id does not exist: {clip_id}")
    collection, index = found
    if collection != "clips":
        raise ValueError(f"mashup bed target must be a clip: {clip_id}")
    clip = payload[collection][index]
    start_ms = event_start_ms(clip)
    duration = clip.get("duration_ms", clip.get("duration"))
    if duration is None:
        raise ValueError(f"clip {clip_id} needs a duration before it can be used as a mashup bed")
    return start_ms, start_ms + parse_ms(duration, f"clip {clip_id} duration")


def add_mashup_bed(
    payload: dict[str, Any],
    *,
    bed_id: str,
    start: str | None,
    end: str | None,
    gain_db: float,
    lowpass_hz: float | None,
    highpass_hz: float | None,
    lock_before_ms: int | None = None,
    force: bool = False,
) -> dict[str, Any]:
    next_payload = copy.deepcopy(payload)
    clip_start_ms, clip_end_ms = clip_start_end(next_payload, bed_id)
    start_ms = parse_ms(start, "mashup bed start") if start is not None else clip_start_ms
    end_ms = parse_ms(end, "mashup bed end") if end is not None else clip_end_ms
    if end_ms <= start_ms:
        raise ValueError("--end must be after --start")
    guard_event_live_edit(next_payload, bed_id, lock_before_ms=lock_before_ms, force=force)
    guard_live_edit(label=f"mashup bed automation for {bed_id}", start_ms=start_ms, lock_before_ms=lock_before_ms, force=force)

    def points(value: float) -> list[dict[str, float | int]]:
        return [{"at_ms": start_ms, "value": value}, {"at_ms": end_ms, "value": value}]

    automations = next_payload.setdefault("automations", [])
    automations.append({"target": bed_id, "param": "gain_db", "points": points(gain_db)})
    if lowpass_hz is not None:
        automations.append({"target": bed_id, "param": "lowpass_hz", "points": points(lowpass_hz)})
    if highpass_hz is not None:
        automations.append({"target": bed_id, "param": "highpass_hz", "points": points(highpass_hz)})
    parse_session(next_payload)
    return next_payload


def add_live_edit_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--lock-before",
        help="Reject edits before this mix timestamp. Use the current playhead for live editing.",
    )
    parser.add_argument(
        "--state",
        type=Path,
        help="Read the live edit lock from a runner state file started_at/playhead_ms.",
    )
    parser.add_argument("--force", action="store_true", help="Allow edits before the live edit lock.")


def live_edit_lock(args: argparse.Namespace) -> int | None:
    if getattr(args, "lock_before", None) and getattr(args, "state", None):
        raise ValueError("--lock-before and --state cannot both be used")
    if getattr(args, "lock_before", None):
        return parse_ms(args.lock_before, "live edit lock")
    if getattr(args, "state", None):
        return playhead_ms_from_state(args.state)
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate and inspect mutable SlimeAudio mix sessions.")
    sub = parser.add_subparsers(dest="command", required=True)
    validate_parser = sub.add_parser("validate")
    validate_parser.add_argument("session", type=Path)
    summary_parser = sub.add_parser("summary")
    summary_parser.add_argument("session", type=Path)
    sub.add_parser("template")

    import_playlist_parser = sub.add_parser("import-playlist")
    import_playlist_parser.add_argument("session", type=Path)
    import_playlist_parser.add_argument("--playlist", type=Path, required=True)
    import_playlist_parser.add_argument("--start", default="0")
    import_playlist_parser.add_argument("--decks", default="deck-1,deck-2,deck-3,deck-4")
    import_playlist_parser.add_argument("--gap-ms", type=int, default=0)
    import_playlist_parser.add_argument("--overlap-ms", type=int, default=0)
    import_playlist_parser.add_argument("--default-duration")
    import_playlist_parser.add_argument("--probe", action=argparse.BooleanOptionalAction, default=True)

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
    add_live_edit_args(add_clip_parser)

    add_mic_parser = sub.add_parser("add-mic")
    add_mic_parser.add_argument("session", type=Path)
    add_mic_parser.add_argument("--create", action="store_true")
    add_mic_parser.add_argument("--id", required=True)
    add_mic_parser.add_argument("--start", required=True)
    add_mic_parser.add_argument("--text", required=True)
    add_mic_parser.add_argument("--voice")
    add_mic_parser.add_argument("--rate")
    add_mic_parser.add_argument("--volume", type=float, default=1.0)
    add_mic_parser.add_argument("--duck-volume", type=float)
    add_mic_parser.add_argument("--lowpass-hz", type=float, default=1400.0)
    add_mic_parser.add_argument("--duck-ms", type=int, default=2500)
    add_live_edit_args(add_mic_parser)

    remove_parser = sub.add_parser("remove")
    remove_parser.add_argument("session", type=Path)
    remove_parser.add_argument("--id", required=True)
    add_live_edit_args(remove_parser)

    move_parser = sub.add_parser("move")
    move_parser.add_argument("session", type=Path)
    move_parser.add_argument("--id", required=True)
    move_parser.add_argument("--start", required=True)
    add_live_edit_args(move_parser)

    beat_jump_parser = sub.add_parser("beat-jump")
    beat_jump_parser.add_argument("session", type=Path)
    beat_jump_parser.add_argument("--id", required=True)
    beat_jump_parser.add_argument("--beats", required=True, help="Beat jump amount: +/-1/2, +/-1, +/-2, +/-4, or +/-8.")
    beat_jump_parser.add_argument("--field", choices=["trim-start", "start"], default="trim-start")
    beat_jump_parser.add_argument("--cache", type=Path, default=DEFAULT_DJ_CACHE)
    beat_jump_parser.add_argument("--min-confidence", type=float, default=DEFAULT_MIN_BEATGRID_CONFIDENCE)
    add_live_edit_args(beat_jump_parser)

    automate_parser = sub.add_parser("automate")
    automate_parser.add_argument("session", type=Path)
    automate_parser.add_argument("--target", required=True)
    automate_parser.add_argument("--param", required=True)
    automate_parser.add_argument("--points-json", required=True)
    add_live_edit_args(automate_parser)

    mashup_bed_parser = sub.add_parser("mashup-bed")
    mashup_bed_parser.add_argument("session", type=Path)
    mashup_bed_parser.add_argument("--bed-id", required=True)
    mashup_bed_parser.add_argument("--start")
    mashup_bed_parser.add_argument("--end")
    mashup_bed_parser.add_argument("--gain-db", type=float, default=-8.0)
    mashup_bed_parser.add_argument("--lowpass-hz", type=float, default=1800.0)
    mashup_bed_parser.add_argument("--highpass-hz", type=float)
    add_live_edit_args(mashup_bed_parser)
    args = parser.parse_args()

    if args.command == "template":
        print(json.dumps(template_session(), indent=2, sort_keys=True))
        return 0

    if args.command == "import-playlist":
        tracks = read_playlist(args.playlist)
        decks = [deck.strip() for deck in args.decks.split(",") if deck.strip()]
        payload = playlist_to_session_payload(
            tracks,
            start_ms=parse_ms(args.start, "timeline start"),
            decks=decks,
            gap_ms=args.gap_ms,
            overlap_ms=args.overlap_ms,
            default_duration_ms=parse_ms(args.default_duration, "default duration") if args.default_duration else None,
            probe=args.probe,
        )
        write_payload(args.session, payload)
        print(f"imported {len(tracks)} playlist tracks into timestamped session {args.session}")
        return 0

    if args.command == "add-clip":
        payload = base_payload(args.session, args.create)
        lock_before_ms = live_edit_lock(args)
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
            lock_before_ms=lock_before_ms,
            force=args.force,
        )
        write_payload(args.session, updated)
        print(f"added clip {args.id}")
        return 0

    if args.command == "add-mic":
        payload = base_payload(args.session, args.create)
        lock_before_ms = live_edit_lock(args)
        updated = add_mic_lean_in(
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
            lock_before_ms=lock_before_ms,
            force=args.force,
        )
        write_payload(args.session, updated)
        print(f"added mic lean-in {args.id}")
        return 0

    if args.command == "remove":
        lock_before_ms = live_edit_lock(args)
        write_payload(
            args.session,
            remove_event(load_payload(args.session), args.id, lock_before_ms=lock_before_ms, force=args.force),
        )
        print(f"removed {args.id}")
        return 0

    if args.command == "move":
        lock_before_ms = live_edit_lock(args)
        write_payload(
            args.session,
            move_event(load_payload(args.session), args.id, args.start, lock_before_ms=lock_before_ms, force=args.force),
        )
        print(f"moved {args.id}")
        return 0

    if args.command == "beat-jump":
        lock_before_ms = live_edit_lock(args)
        write_payload(
            args.session,
            beat_jump_clip(
                load_payload(args.session),
                args.id,
                args.beats,
                field=args.field,
                cache_path=args.cache,
                min_confidence=args.min_confidence,
                lock_before_ms=lock_before_ms,
                force=args.force,
            ),
        )
        print(f"beat-jumped {args.id} {args.beats} beats on {args.field}")
        return 0

    if args.command == "automate":
        lock_before_ms = live_edit_lock(args)
        write_payload(
            args.session,
            add_automation(
                load_payload(args.session),
                target=args.target,
                param=args.param,
                points_json=args.points_json,
                lock_before_ms=lock_before_ms,
                force=args.force,
            ),
        )
        print(f"added automation {args.target}.{args.param}")
        return 0

    if args.command == "mashup-bed":
        lock_before_ms = live_edit_lock(args)
        write_payload(
            args.session,
            add_mashup_bed(
                load_payload(args.session),
                bed_id=args.bed_id,
                start=args.start,
                end=args.end,
                gain_db=args.gain_db,
                lowpass_hz=args.lowpass_hz,
                highpass_hz=args.highpass_hz,
                lock_before_ms=lock_before_ms,
                force=args.force,
            ),
        )
        print(f"added mashup bed automation for {args.bed_id}")
        return 0

    session = load_session(args.session)
    if args.command == "validate":
        print(f"ok clips={len(session.clips)} mic_lean_ins={len(session.mic_lean_ins)} automations={len(session.automations)}")
        return 0
    print(json.dumps(session_summary(session), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
