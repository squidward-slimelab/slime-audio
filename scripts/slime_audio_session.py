#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
import math
import re
import sqlite3
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
DEFAULT_LIBRARY_DB = REPO_ROOT / "runtime" / "slime-music-library.sqlite3"
DEFAULT_MIN_BEATGRID_CONFIDENCE = 0.45
SUPPORTED_INSTANT_DOUBLE_RECIPES = {
    "stabs": {"duration": "00:08.000", "gate_beats": "1/2", "cut_source": True},
    "one-beat-trades": {"duration": "00:12.000", "gate_beats": "1", "cut_source": True},
    "hook-tease": {"duration": "00:08.000", "gate_beats": "1", "cut_source": False, "cue_kind": "hook"},
    "offbeat-swaps": {"duration": "00:08.000", "gate_beats": "1/2", "gate_offset_beats": "1/2", "cut_source": True},
    "echo-stabs": {"duration": "00:08.000", "gate_beats": "1/2", "cut_source": True, "effect": "echo"},
    "echo-drop": {"duration": "00:08.000", "gate_beats": "1", "cut_source": True, "effect": "reverb"},
    "scratch-cuts": {"duration": "00:08.000", "scratch_pattern": True, "cut_source": False},
    "slip-brake": {"duration": "00:04.000", "cut_source": False, "effect": "vinyl_brake", "effect_beats": "1", "slip": True, "effect_track": True},
    "brake-drop": {"duration": "00:04.000", "cut_source": False, "effect": "vinyl_brake", "effect_beats": "1", "timing_brake": True, "effect_track": True},
}
DEFERRED_ROUTINE_RECIPES = {
}
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
    "position",
}
FADER_SIDES = {"A", "B", "THRU"}
DEFAULT_FADER_ASSIGNMENTS = {
    "deck-1": "A",
    "deck-2": "B",
    "deck-3": "A",
    "deck-4": "B",
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
    trim_db: float = 0.0
    gain_db: float = 0.0
    tempo_shift_pct: float = 0.0
    pitch_shift_semitones: int = 0
    fade_in_ms: int = 0
    fade_out_ms: int = 0
    reverse: bool = False
    playback_rate: float = 1.0
    kind: str = "song"
    attached_deck: str | None = None
    effect_parent_clip_id: str | None = None
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
class EffectEvent:
    id: str
    type: str
    target: str
    start_ms: int
    duration_ms: int
    tail_ms: int = 0
    wet: float = 0.35
    gain_db: float = -6.0
    delay_ms: int = 375
    feedback: float = 0.35
    room_size: float = 0.6
    damping: float = 0.45
    lowpass_hz: float | None = None
    preset: str | None = None
    routine_id: str | None = None
    routine_recipe: str | None = None

    @property
    def end_ms(self) -> int:
        return self.start_ms + self.duration_ms + self.tail_ms


@dataclass(frozen=True)
class SlipEvent:
    id: str
    source_clip_id: str
    target_clip_id: str
    start_ms: int
    duration_ms: int
    source_start_ms: int
    source_resume_ms: int
    routine_id: str | None = None
    routine_recipe: str | None = None

    @property
    def end_ms(self) -> int:
        return self.start_ms + self.duration_ms


AUDACITY_REVERB_PRESETS: dict[str, dict[str, Any]] = {
    # Audacity 3.7 factory presets. Source order is:
    # room size, pre-delay, reverberance, hf damping, tone low, tone high,
    # wet gain, dry gain, stereo width, wet only.
    "defaults": {"room_size": 0.75, "delay_ms": 10, "feedback": 0.5, "damping": 0.5, "wet": 0.89, "gain_db": -1.0, "tail_ms": 6000},
    "acoustic": {"room_size": 0.50, "delay_ms": 10, "feedback": 0.75, "damping": 1.0, "wet": 1.0, "gain_db": -14.0, "tail_ms": 4500},
    "ambience": {"room_size": 1.0, "delay_ms": 55, "feedback": 1.0, "damping": 0.5, "wet": 1.0, "gain_db": 0.0, "tail_ms": 8000},
    "artificial": {"room_size": 0.81, "delay_ms": 99, "feedback": 0.23, "damping": 0.62, "wet": 1.0, "gain_db": -4.0, "tail_ms": 5000},
    "clean": {"room_size": 0.50, "delay_ms": 10, "feedback": 0.75, "damping": 1.0, "wet": 1.0, "gain_db": -18.0, "tail_ms": 4500},
    "modern": {"room_size": 0.50, "delay_ms": 10, "feedback": 0.75, "damping": 1.0, "wet": 1.0, "gain_db": -15.0, "tail_ms": 4500},
    "vocal-i": {"room_size": 0.70, "delay_ms": 20, "feedback": 0.40, "damping": 0.99, "wet": 1.0, "gain_db": -12.0, "tail_ms": 5000},
    "vocal-ii": {"room_size": 0.50, "delay_ms": 0, "feedback": 0.50, "damping": 0.99, "wet": 1.0, "gain_db": -1.0, "tail_ms": 5000},
    "dance-vocal": {"room_size": 0.90, "delay_ms": 2, "feedback": 0.60, "damping": 0.77, "wet": 1.0, "gain_db": -10.0, "tail_ms": 6500},
    "modern-vocal": {"room_size": 0.66, "delay_ms": 27, "feedback": 0.77, "damping": 0.08, "wet": 1.0, "gain_db": -10.0, "tail_ms": 6000},
    "voice-tail": {"room_size": 0.66, "delay_ms": 27, "feedback": 1.0, "damping": 0.08, "wet": 1.0, "gain_db": -6.0, "tail_ms": 7000},
    "bathroom": {"room_size": 0.16, "delay_ms": 8, "feedback": 0.80, "damping": 0.0, "wet": 1.0, "gain_db": -6.0, "tail_ms": 3000},
    "small-room-bright": {"room_size": 0.30, "delay_ms": 10, "feedback": 0.50, "damping": 0.50, "wet": 1.0, "gain_db": -1.0, "tail_ms": 3500},
    "small-room-dark": {"room_size": 0.30, "delay_ms": 10, "feedback": 0.50, "damping": 0.50, "wet": 1.0, "gain_db": -1.0, "tail_ms": 3500},
    "medium-room": {"room_size": 0.75, "delay_ms": 10, "feedback": 0.40, "damping": 0.50, "wet": 1.0, "gain_db": -1.0, "tail_ms": 5000},
    "large-room": {"room_size": 0.85, "delay_ms": 10, "feedback": 0.40, "damping": 0.50, "wet": 1.0, "gain_db": 0.0, "tail_ms": 6000},
    "church-hall": {"room_size": 0.90, "delay_ms": 32, "feedback": 0.60, "damping": 0.50, "wet": 1.0, "gain_db": 0.0, "tail_ms": 7500},
    "cathedral": {"room_size": 0.90, "delay_ms": 16, "feedback": 0.90, "damping": 0.50, "wet": 1.0, "gain_db": 0.0, "tail_ms": 8000},
    "big-cave": {"room_size": 1.0, "delay_ms": 55, "feedback": 1.0, "damping": 0.50, "wet": 1.0, "gain_db": 5.0, "tail_ms": 9000},
}

EFFECT_DEFAULTS: dict[str, dict[str, Any]] = {
    "echo": {
        "tail_ms": 2000,
        "wet": 0.35,
        "gain_db": -6.0,
        "delay_ms": 375,
        "feedback": 0.35,
        "room_size": 0.6,
        "damping": 0.45,
    },
    "reverb": AUDACITY_REVERB_PRESETS["defaults"],
    "vinyl_brake": {
        "tail_ms": 0,
        "wet": 1.0,
        "gain_db": 0.0,
        "delay_ms": 1,
        "feedback": 0.0,
        "room_size": 0.6,
        "damping": 0.45,
    },
}


def effect_default(effect_type: str, field: str) -> Any:
    return EFFECT_DEFAULTS.get(effect_type, EFFECT_DEFAULTS["echo"])[field]


def resolved_effect_args(args: argparse.Namespace) -> dict[str, Any]:
    preset = getattr(args, "preset", None)
    if preset and args.type != "reverb":
        raise ValueError("--preset is only supported for reverb effects")
    defaults = AUDACITY_REVERB_PRESETS[preset] if preset else EFFECT_DEFAULTS.get(args.type, EFFECT_DEFAULTS["echo"])
    return {
        "preset": preset,
        "tail_ms": args.tail_ms if args.tail_ms is not None else defaults["tail_ms"],
        "wet": args.wet if args.wet is not None else defaults["wet"],
        "gain_db": args.gain_db if args.gain_db is not None else defaults["gain_db"],
        "delay_ms": args.delay_ms if args.delay_ms is not None else defaults["delay_ms"],
        "feedback": args.feedback if args.feedback is not None else defaults["feedback"],
        "room_size": args.room_size if args.room_size is not None else defaults["room_size"],
        "damping": args.damping if args.damping is not None else defaults["damping"],
        "lowpass_hz": args.lowpass_hz,
    }


@dataclass(frozen=True)
class MixSession:
    version: int
    decks: list[str]
    clips: list[Clip]
    mic_lean_ins: list[MicLeanIn]
    effects: list[EffectEvent]
    automations: list[Automation]
    slip_events: list[SlipEvent] = field(default_factory=list)
    fader_routing: dict[str, str] = field(default_factory=dict)

    @property
    def event_ids(self) -> set[str]:
        return (
            {clip.id for clip in self.clips}
            | {lean_in.id for lean_in in self.mic_lean_ins}
            | {effect.id for effect in self.effects}
            | {event.id for event in self.slip_events}
        )


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
        trim_db=float(payload.get("trim_db", 0.0)),
        gain_db=float(payload.get("gain_db", 0.0)),
        tempo_shift_pct=float(payload.get("tempo_shift_pct", 0.0)),
        pitch_shift_semitones=int(payload.get("pitch_shift_semitones", 0)),
        fade_in_ms=parse_ms(payload.get("fade_in_ms", 0), f"clip {clip_id} fade_in_ms"),
        fade_out_ms=parse_ms(payload.get("fade_out_ms", 0), f"clip {clip_id} fade_out_ms"),
        reverse=bool(payload.get("reverse", False)),
        playback_rate=float(payload.get("playback_rate", 1.0)),
        kind=str(payload.get("kind") or "song"),
        attached_deck=str(payload["attached_deck"]) if payload.get("attached_deck") else None,
        effect_parent_clip_id=str(payload["effect_parent_clip_id"]) if payload.get("effect_parent_clip_id") else None,
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


def parse_effect_event(payload: dict[str, Any]) -> EffectEvent:
    effect_id = str(payload.get("id") or "").strip()
    effect_type = str(payload.get("type") or payload.get("effect") or "").strip()
    target = str(payload.get("target") or "").strip()
    if not effect_id:
        raise ValueError("effect id is required")
    if effect_type not in {"echo", "reverb", "vinyl_brake"}:
        raise ValueError(f"effect {effect_id} type must be echo, reverb, or vinyl_brake")
    if not target:
        raise ValueError(f"effect {effect_id} target is required")
    return EffectEvent(
        id=effect_id,
        type=effect_type,
        target=target,
        start_ms=parse_ms(payload.get("start_ms", payload.get("start", 0)), f"effect {effect_id} start"),
        duration_ms=parse_ms(payload.get("duration_ms", payload.get("duration", 0)), f"effect {effect_id} duration"),
        tail_ms=parse_ms(payload.get("tail_ms", payload.get("tail", 0)), f"effect {effect_id} tail"),
        wet=max(0.0, min(1.0, float(payload.get("wet", 0.35)))),
        gain_db=float(payload.get("gain_db", -6.0)),
        delay_ms=parse_ms(payload.get("delay_ms", 375), f"effect {effect_id} delay"),
        feedback=max(0.0, min(0.95, float(payload.get("feedback", 0.35)))),
        room_size=max(0.0, min(1.0, float(payload.get("room_size", 0.6)))),
        damping=max(0.0, min(1.0, float(payload.get("damping", 0.45)))),
        lowpass_hz=float(payload["lowpass_hz"]) if payload.get("lowpass_hz") is not None else None,
        preset=str(payload["preset"]) if payload.get("preset") else None,
        routine_id=str(payload["routine_id"]) if payload.get("routine_id") else None,
        routine_recipe=str(payload["routine_recipe"]) if payload.get("routine_recipe") else None,
    )


def parse_slip_event(payload: dict[str, Any]) -> SlipEvent:
    event_id = str(payload.get("id") or "").strip()
    source_clip_id = str(payload.get("source_clip_id") or payload.get("source") or "").strip()
    target_clip_id = str(payload.get("target_clip_id") or payload.get("target") or "").strip()
    if not event_id:
        raise ValueError("slip event id is required")
    if not source_clip_id:
        raise ValueError(f"slip event {event_id} source_clip_id is required")
    if not target_clip_id:
        raise ValueError(f"slip event {event_id} target_clip_id is required")
    start_ms = parse_ms(payload.get("start_ms", payload.get("start", 0)), f"slip event {event_id} start")
    duration_ms = parse_ms(payload.get("duration_ms", payload.get("duration", 0)), f"slip event {event_id} duration")
    return SlipEvent(
        id=event_id,
        source_clip_id=source_clip_id,
        target_clip_id=target_clip_id,
        start_ms=start_ms,
        duration_ms=duration_ms,
        source_start_ms=parse_ms(payload.get("source_start_ms", 0), f"slip event {event_id} source start"),
        source_resume_ms=parse_ms(payload.get("source_resume_ms", 0), f"slip event {event_id} source resume"),
        routine_id=str(payload["routine_id"]) if payload.get("routine_id") else None,
        routine_recipe=str(payload["routine_recipe"]) if payload.get("routine_recipe") else None,
    )


def parse_fader_routing(payload: dict[str, Any], decks: list[str]) -> dict[str, str]:
    routing = payload.get("fader_routing", payload.get("faderRouting"))
    assignments = routing.get("deck_assignments", routing.get("deckAssignments", {})) if isinstance(routing, dict) else {}
    if not isinstance(assignments, dict):
        raise ValueError("fader_routing.deck_assignments must be an object")
    result = {
        deck: str(assignments.get(deck, DEFAULT_FADER_ASSIGNMENTS.get(deck, "THRU"))).strip().upper()
        for deck in decks
    }
    return result


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
        effects=[parse_effect_event(item) for item in payload.get("effects", [])],
        automations=[parse_automation(item) for item in payload.get("automations", [])],
        slip_events=[parse_slip_event(item) for item in payload.get("slip_events", payload.get("slipEvents", []))],
        fader_routing=parse_fader_routing(payload, decks),
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
    for deck, side in session.fader_routing.items():
        if deck not in deck_set:
            errors.append(f"fader routing uses unknown deck {deck}")
        if side not in FADER_SIDES:
            errors.append(f"fader routing for {deck} must be A, B, or THRU")
    for event_id in [clip.id for clip in session.clips] + [lean_in.id for lean_in in session.mic_lean_ins] + [effect.id for effect in session.effects] + [event.id for event in session.slip_events]:
        if event_id in seen_ids:
            errors.append(f"duplicate event id: {event_id}")
        seen_ids.add(event_id)

    for clip in session.clips:
        if clip.deck not in deck_set:
            errors.append(f"clip {clip.id} uses unknown deck {clip.deck}")
        if clip.attached_deck is not None and clip.attached_deck not in deck_set:
            errors.append(f"clip {clip.id} attaches to unknown deck {clip.attached_deck}")
        if clip.effect_parent_clip_id is not None and clip.effect_parent_clip_id not in {item.id for item in session.clips}:
            errors.append(f"clip {clip.id} effect parent does not exist: {clip.effect_parent_clip_id}")
        if clip.start_ms < 0:
            errors.append(f"clip {clip.id} starts before zero")
        if clip.trim_start_ms < 0:
            errors.append(f"clip {clip.id} trim starts before zero")
        if clip.duration_ms is not None and clip.duration_ms <= 0:
            errors.append(f"clip {clip.id} duration must be positive")
        if clip.fade_in_ms < 0 or clip.fade_out_ms < 0:
            errors.append(f"clip {clip.id} fades must be non-negative")
        if clip.playback_rate <= 0:
            errors.append(f"clip {clip.id} playback_rate must be positive")
        for automation in clip.automations:
            validate_automation(automation, session.event_ids, errors, prefix=f"clip {clip.id}")

    for deck in session.decks:
        clips = sorted(
            [clip for clip in session.clips if clip.deck == deck and clip.end_ms is not None and clip.kind != "effect-track"],
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

    for effect in session.effects:
        if effect.target not in session.event_ids and not effect.target.startswith("deck:") and effect.target not in {"master", "all"}:
            errors.append(f"effect {effect.id} target does not exist: {effect.target}")
        if effect.start_ms < 0:
            errors.append(f"effect {effect.id} starts before zero")
        if effect.duration_ms <= 0:
            errors.append(f"effect {effect.id} duration must be positive")
        if effect.tail_ms < 0:
            errors.append(f"effect {effect.id} tail must be non-negative")
        if effect.delay_ms <= 0:
            errors.append(f"effect {effect.id} delay must be positive")

    clip_ids = {clip.id for clip in session.clips}
    for event in session.slip_events:
        if event.source_clip_id not in clip_ids:
            errors.append(f"slip event {event.id} source clip does not exist: {event.source_clip_id}")
        if event.target_clip_id not in clip_ids:
            errors.append(f"slip event {event.id} target clip does not exist: {event.target_clip_id}")
        if event.start_ms < 0:
            errors.append(f"slip event {event.id} starts before zero")
        if event.duration_ms <= 0:
            errors.append(f"slip event {event.id} duration must be positive")
        if event.source_resume_ms < event.source_start_ms:
            errors.append(f"slip event {event.id} resume must be after source start")

    for automation in session.automations:
        validate_automation(automation, session.event_ids, errors, prefix="session")

    if errors:
        raise ValueError("\n".join(errors))


def validate_automation(automation: Automation, event_ids: set[str], errors: list[str], prefix: str) -> None:
    if automation.param not in AUTOMATABLE_PARAMS:
        errors.append(f"{prefix} automation {automation.target}.{automation.param} is not an automatable param")
    if automation.target not in event_ids and automation.target not in {"master", "all", "crossfader"} and not automation.target.startswith("deck:"):
        errors.append(f"{prefix} automation target does not exist: {automation.target}")
    previous = -1
    for point in automation.points:
        if point.at_ms < 0:
            errors.append(f"{prefix} automation {automation.target}.{automation.param} has negative point time")
        if point.at_ms < previous:
            errors.append(f"{prefix} automation {automation.target}.{automation.param} points must be sorted")
        if automation.target == "crossfader" and automation.param == "position":
            try:
                value = float(point.value)
            except (TypeError, ValueError):
                errors.append(f"{prefix} automation crossfader.position must be numeric")
            else:
                if value < -1.0 or value > 1.0:
                    errors.append(f"{prefix} automation crossfader.position must stay between -1 and 1")
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
        "fader_routing": session.fader_routing,
        "clip_count": len(session.clips),
        "mic_lean_in_count": len(session.mic_lean_ins),
        "effect_count": len(session.effects),
        "slip_event_count": len(session.slip_events),
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
        "fader_routing": {
            "deck_assignments": DEFAULT_FADER_ASSIGNMENTS,
        },
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
        "effects": [],
        "slip_events": [],
    }


def base_payload(path: Path, create: bool) -> dict[str, Any]:
    if path.exists():
        return load_payload(path)
    if not create:
        raise FileNotFoundError(path)
    return {"version": 1, "decks": [f"deck-{index + 1}" for index in range(MAX_DECKS)], "clips": [], "mic_lean_ins": [], "effects": [], "slip_events": [], "automations": []}


def find_event(payload: dict[str, Any], event_id: str) -> tuple[str, int] | None:
    for collection in ("clips", "mic_lean_ins", "effects", "slip_events"):
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


def parse_gate_offset_beats(value: str | None) -> Fraction:
    if value is None:
        return Fraction(0, 1)
    try:
        beats = Fraction(value)
    except ValueError as error:
        raise ValueError(f"invalid beat offset: {value}") from error
    if beats < 0:
        raise ValueError("beat offset cannot be negative")
    if beats not in {Fraction(0, 1), Fraction(1, 2), Fraction(1, 1), Fraction(2, 1), Fraction(4, 1), Fraction(8, 1)}:
        raise ValueError("beat offset must be one of 0, 1/2, 1, 2, 4, or 8 beats")
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


def cached_cue(
    db_path: Path,
    clip_path: str,
    kind: str,
    *,
    min_confidence: float = DEFAULT_MIN_BEATGRID_CONFIDENCE,
    force: bool = False,
) -> tuple[int, float, str]:
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as error:
        raise ValueError(f"could not open music library db: {db_path}") from error
    normalized = str(Path(clip_path).resolve())
    try:
        rows = conn.execute(
            """
            SELECT at_ms, confidence, label
            FROM track_dj_cues
            WHERE path = ? AND kind = ?
            ORDER BY confidence DESC, at_ms ASC
            """,
            (normalized, kind),
        ).fetchall()
    except sqlite3.Error as error:
        conn.close()
        raise ValueError(f"could not read persisted cues from music library db: {db_path}") from error
    conn.close()
    if not rows:
        raise ValueError(f"no persisted {kind} cue for {clip_path}; run slime_audio_dj.py cues/structure first")
    row = rows[0]
    confidence = float(row["confidence"] or 0.0)
    if confidence < min_confidence and not force:
        raise ValueError(
            f"persisted cue confidence too low for {clip_path} {kind}: {confidence:.3f} < {min_confidence:.3f}; use --force to override"
        )
    return int(row["at_ms"]), confidence, str(row["label"] or kind)


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
    trim_db: float,
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
        "trim_db": trim_db,
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


def set_fader_routing(payload: dict[str, Any], assignments: dict[str, str]) -> dict[str, Any]:
    next_payload = copy.deepcopy(payload)
    decks = [str(deck) for deck in next_payload.get("decks", [])] or [f"deck-{index + 1}" for index in range(MAX_DECKS)]
    current = parse_fader_routing(next_payload, decks)
    current.update({str(deck): str(side).upper() for deck, side in assignments.items()})
    next_payload["fader_routing"] = {"deck_assignments": current}
    parse_session(next_payload)
    return next_payload


def add_crossfader_automation(
    payload: dict[str, Any],
    *,
    points_json: str,
    lock_before_ms: int | None = None,
    force: bool = False,
) -> dict[str, Any]:
    return add_automation(
        payload,
        target="crossfader",
        param="position",
        points_json=points_json,
        lock_before_ms=lock_before_ms,
        force=force,
    )


def add_effect_event(
    payload: dict[str, Any],
    *,
    effect_id: str,
    effect_type: str,
    target: str,
    start: str,
    duration: str,
    tail_ms: int,
    wet: float,
    gain_db: float,
    delay_ms: int,
    feedback: float,
    room_size: float = 0.6,
    damping: float = 0.45,
    lowpass_hz: float | None = None,
    preset: str | None = None,
    routine_id: str | None = None,
    routine_recipe: str | None = None,
    lock_before_ms: int | None = None,
    force: bool = False,
) -> dict[str, Any]:
    next_payload = copy.deepcopy(payload)
    require_unique_event_id(next_payload, effect_id)
    start_ms = parse_ms(start, f"effect {effect_id} start")
    guard_live_edit(label=f"effect {effect_id}", start_ms=start_ms, lock_before_ms=lock_before_ms, force=force)
    effect: dict[str, Any] = {
        "id": effect_id,
        "type": effect_type,
        "target": target,
        "start_ms": start_ms,
        "duration_ms": parse_ms(duration, f"effect {effect_id} duration"),
        "tail_ms": tail_ms,
        "wet": wet,
        "gain_db": gain_db,
        "delay_ms": delay_ms,
        "feedback": feedback,
        "room_size": room_size,
        "damping": damping,
    }
    if lowpass_hz is not None:
        effect["lowpass_hz"] = lowpass_hz
    if preset is not None:
        effect["preset"] = preset
    if routine_id is not None:
        effect["routine_id"] = routine_id
    if routine_recipe is not None:
        effect["routine_recipe"] = routine_recipe
    next_payload.setdefault("effects", []).append(effect)
    parse_session(next_payload)
    return next_payload


def add_slip_event(
    payload: dict[str, Any],
    *,
    slip_id: str,
    source_id: str,
    target_id: str,
    start: str,
    duration: str,
    routine_id: str | None = None,
    routine_recipe: str | None = None,
    lock_before_ms: int | None = None,
    force: bool = False,
) -> dict[str, Any]:
    next_payload = copy.deepcopy(payload)
    require_unique_event_id(next_payload, slip_id)
    source_found = find_event(next_payload, source_id)
    target_found = find_event(next_payload, target_id)
    if source_found is None:
        raise ValueError(f"source clip does not exist: {source_id}")
    if target_found is None:
        raise ValueError(f"target clip does not exist: {target_id}")
    if source_found[0] != "clips" or target_found[0] != "clips":
        raise ValueError("slip source and target must both be clips")
    source = next_payload[source_found[0]][source_found[1]]
    start_ms = parse_ms(start, f"slip event {slip_id} start")
    duration_ms = parse_ms(duration, f"slip event {slip_id} duration")
    guard_live_edit(label=f"slip event {slip_id}", start_ms=start_ms, lock_before_ms=lock_before_ms, force=force)
    source_start_ms, source_end_ms = clip_start_end(next_payload, source_id)
    if start_ms < source_start_ms or start_ms + duration_ms > source_end_ms:
        raise ValueError(f"slip event {slip_id} must stay inside source clip {source_id}")
    trim_start_ms = parse_ms(source.get("trim_start_ms", source.get("trim_start", 0)), f"clip {source_id} trim_start")
    source_start = trim_start_ms + int(round((start_ms - source_start_ms) * clip_tempo_factor(source)))
    source_resume = source_start + int(round(duration_ms * clip_tempo_factor(source)))
    slip_event: dict[str, Any] = {
        "id": slip_id,
        "source_clip_id": source_id,
        "target_clip_id": target_id,
        "start_ms": start_ms,
        "duration_ms": duration_ms,
        "source_start_ms": source_start,
        "source_resume_ms": source_resume,
    }
    if routine_id is not None:
        slip_event["routine_id"] = routine_id
    if routine_recipe is not None:
        slip_event["routine_recipe"] = routine_recipe
    next_payload.setdefault("slip_events", []).append(slip_event)
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


def clip_tempo_factor(clip: dict[str, Any]) -> float:
    factor = 1 + (float(clip.get("tempo_shift_pct", 0.0)) / 100)
    if factor <= 0:
        raise ValueError(f"clip {clip.get('id')} tempo_shift_pct produces non-positive tempo")
    return factor


def choose_free_deck(payload: dict[str, Any], start_ms: int, end_ms: int, *, avoid: str | None = None) -> str:
    decks = [str(deck) for deck in payload.get("decks", []) if str(deck)]
    if not decks:
        decks = [f"deck-{index + 1}" for index in range(MAX_DECKS)]
    for deck in decks:
        if deck == avoid:
            continue
        if not any(
            str(clip.get("deck")) == deck
            and event_start_ms(clip) < end_ms
            and start_ms < clip_start_end(payload, str(clip.get("id")))[1]
            for clip in payload.get("clips", [])
            if clip.get("duration_ms", clip.get("duration")) is not None
        ):
            return deck
    raise ValueError(f"no compatible free deck/window for instant double {start_ms}ms-{end_ms}ms")


def add_instant_double(
    payload: dict[str, Any],
    *,
    source_id: str,
    double_id: str,
    start: str | None,
    deck: str | None,
    duration: str,
    gain_db: float | None,
    fade_in_ms: int,
    fade_out_ms: int,
    gate_beats: str | None,
    gate_offset_beats: str | None = None,
    cut_source: bool,
    cache_path: Path = DEFAULT_DJ_CACHE,
    min_confidence: float = DEFAULT_MIN_BEATGRID_CONFIDENCE,
    lock_before_ms: int | None = None,
    force: bool = False,
) -> dict[str, Any]:
    next_payload = copy.deepcopy(payload)
    require_unique_event_id(next_payload, double_id)
    found = find_event(next_payload, source_id)
    if found is None:
        raise ValueError(f"event id does not exist: {source_id}")
    collection, index = found
    if collection != "clips":
        raise ValueError(f"instant double source must be a clip: {source_id}")
    source = next_payload[collection][index]
    source_start_ms, source_end_ms = clip_start_end(next_payload, source_id)
    start_ms = parse_ms(start, "instant double start") if start is not None else source_start_ms
    duration_ms = parse_ms(duration, "instant double duration")
    end_ms = start_ms + duration_ms
    if start_ms < source_start_ms or start_ms >= source_end_ms:
        raise ValueError(f"instant double start must land inside source clip {source_id}")
    if end_ms > source_end_ms:
        raise ValueError(f"instant double cannot outlive source clip {source_id}")
    guard_live_edit(label=f"instant double {double_id}", start_ms=start_ms, lock_before_ms=lock_before_ms, force=force)

    trim_start_ms = parse_ms(source.get("trim_start_ms", source.get("trim_start", 0)), f"clip {source_id} trim_start")
    trim_start_ms += int(round((start_ms - source_start_ms) * clip_tempo_factor(source)))
    target_deck = deck or choose_free_deck(next_payload, start_ms, end_ms, avoid=str(source.get("deck") or ""))
    if deck is not None:
        decks = [str(item) for item in next_payload.get("decks", [])]
        if deck not in decks:
            raise ValueError(f"unknown deck: {deck}")
        if any(
            str(clip.get("deck")) == deck
            and event_start_ms(clip) < end_ms
            and start_ms < clip_start_end(next_payload, str(clip.get("id")))[1]
            for clip in next_payload.get("clips", [])
            if clip.get("duration_ms", clip.get("duration")) is not None
        ):
            raise ValueError(f"deck {deck} is not free for instant double {start_ms}ms-{end_ms}ms")

    double_clip: dict[str, Any] = {
        "id": double_id,
        "deck": target_deck,
        "path": source.get("path"),
        "start_ms": start_ms,
        "trim_start_ms": trim_start_ms,
        "duration_ms": duration_ms,
        "trim_db": float(source.get("trim_db", 0.0)),
        "gain_db": float(source.get("gain_db", 0.0)) if gain_db is None else gain_db,
        "tempo_shift_pct": float(source.get("tempo_shift_pct", 0.0)),
        "pitch_shift_semitones": int(source.get("pitch_shift_semitones", 0)),
        "fade_in_ms": fade_in_ms,
        "fade_out_ms": fade_out_ms,
        "kind": "planner-double",
        "planner_role": "instant-double",
        "source_clip_id": source_id,
    }
    next_payload.setdefault("clips", []).append(double_clip)

    if gate_beats:
        bpm, _beat_offset_ms, _confidence = cached_beatgrid(
            cache_path,
            str(source.get("path") or ""),
            min_confidence=min_confidence,
            force=force,
        )
        source_deck = str(source.get("deck") or "")
        routing = parse_fader_routing(next_payload, [str(deck) for deck in next_payload.get("decks", [])] or [f"deck-{index + 1}" for index in range(MAX_DECKS)])
        source_side = routing.get(source_deck, DEFAULT_FADER_ASSIGNMENTS.get(source_deck, "A"))
        if source_side == "THRU":
            source_side = DEFAULT_FADER_ASSIGNMENTS.get(source_deck, "A")
        target_side = "B" if source_side == "A" else "A"
        on_position = -1.0 if target_side == "A" else 1.0
        off_position = -1.0 if source_side == "A" else 1.0
        if cut_source:
            next_payload = set_fader_routing(next_payload, {source_deck: source_side, target_deck: target_side})
        gate_ms = max(1, int(round(float(parse_beats(gate_beats)) * (60_000 / bpm))))
        offset_ms = max(0, int(round(float(parse_gate_offset_beats(gate_offset_beats)) * (60_000 / bpm))))
        automations = next_payload.setdefault("automations", [])
        at_ms = min(end_ms, start_ms + offset_ms)
        if cut_source and at_ms > start_ms:
            automations.append(
                {
                    "target": "crossfader",
                    "param": "position",
                    "planner_role": "instant-double-crossfader-hold",
                    "points": [{"at_ms": start_ms, "value": off_position}, {"at_ms": at_ms, "value": off_position}],
                }
            )
        gate_index = 0
        while at_ms < end_ms:
            on_end = min(end_ms, at_ms + gate_ms)
            off_end = min(end_ms, on_end + gate_ms)
            if cut_source:
                automations.append(
                    {
                        "target": "crossfader",
                        "param": "position",
                        "planner_role": "instant-double-crossfader-cut",
                        "points": [{"at_ms": at_ms, "value": on_position}, {"at_ms": on_end, "value": on_position}],
                    }
                )
            else:
                automations.append(
                    {
                        "target": double_id,
                        "param": "gain_db",
                        "planner_role": "instant-double-gate",
                        "points": [{"at_ms": at_ms, "value": double_clip["gain_db"]}, {"at_ms": on_end, "value": double_clip["gain_db"]}],
                    }
                )
            if off_end > on_end:
                if cut_source:
                    automations.append(
                        {
                            "target": "crossfader",
                            "param": "position",
                            "planner_role": "instant-double-crossfader-return",
                            "points": [{"at_ms": on_end, "value": off_position}, {"at_ms": off_end, "value": off_position}],
                        }
                    )
                else:
                    automations.append(
                        {
                            "target": double_id,
                            "param": "gain_db",
                            "planner_role": "instant-double-gate",
                            "points": [{"at_ms": on_end, "value": -96.0}, {"at_ms": off_end, "value": -96.0}],
                        }
                    )
            gate_index += 1
            at_ms = start_ms + offset_ms + (gate_index * gate_ms * 2)

    parse_session(next_payload)
    return next_payload


def add_instant_double_routine(
    payload: dict[str, Any],
    *,
    source_id: str,
    routine_id: str,
    recipe: str,
    start: str | None,
    cue_kind: str | None = None,
    cue_db: Path = DEFAULT_LIBRARY_DB,
    cache_path: Path = DEFAULT_DJ_CACHE,
    min_confidence: float = DEFAULT_MIN_BEATGRID_CONFIDENCE,
    lock_before_ms: int | None = None,
    force: bool = False,
) -> dict[str, Any]:
    if recipe in DEFERRED_ROUTINE_RECIPES:
        raise ValueError(f"recipe {recipe} is not available yet: {DEFERRED_ROUTINE_RECIPES[recipe]}")
    config = SUPPORTED_INSTANT_DOUBLE_RECIPES.get(recipe)
    if config is None:
        known = ", ".join(sorted([*SUPPORTED_INSTANT_DOUBLE_RECIPES, *DEFERRED_ROUTINE_RECIPES]))
        raise ValueError(f"unknown instant-double recipe {recipe}; known recipes: {known}")
    found = find_event(payload, source_id)
    if found is None:
        raise ValueError(f"event id does not exist: {source_id}")
    collection, index = found
    if collection != "clips":
        raise ValueError(f"instant-double routine source must be a clip: {source_id}")
    source = payload[collection][index]
    resolved_cue_kind = cue_kind or str(config.get("cue_kind") or "")
    if start is not None and cue_kind is not None:
        raise ValueError("--start and --cue-kind cannot both be used")
    cue_label = None
    if start is None and resolved_cue_kind:
        cue_ms, _cue_confidence, cue_label = cached_cue(
            cue_db,
            str(source.get("path") or ""),
            resolved_cue_kind,
            min_confidence=min_confidence,
            force=force,
        )
        source_start_ms = event_start_ms(source)
        source_trim_ms = parse_ms(source.get("trim_start_ms", source.get("trim_start", 0)), f"clip {source_id} trim_start")
        start_ms = source_start_ms + int(round((cue_ms - source_trim_ms) / clip_tempo_factor(source)))
        start = str(start_ms)
    bpm, _beat_offset_ms, _confidence = cached_beatgrid(
        cache_path,
        str(source.get("path") or ""),
        min_confidence=min_confidence,
        force=force,
    )
    if config.get("scratch_pattern"):
        return add_scratch_cut_routine(
            payload,
            source_id=source_id,
            routine_id=routine_id,
            recipe=recipe,
            start=start,
            bpm=bpm,
            lock_before_ms=lock_before_ms,
            force=force,
        )
    double_id = f"{routine_id}-double"
    next_payload = add_instant_double(
        payload,
        source_id=source_id,
        double_id=double_id,
        start=start,
        deck=None,
        duration=str(config["duration"]),
        gain_db=None,
        fade_in_ms=0,
        fade_out_ms=0,
        gate_beats=str(config["gate_beats"]) if config.get("gate_beats") else None,
        gate_offset_beats=str(config.get("gate_offset_beats", "0")),
        cut_source=bool(config["cut_source"]),
        cache_path=cache_path,
        min_confidence=min_confidence,
        lock_before_ms=lock_before_ms,
        force=force,
    )
    for clip in next_payload.get("clips", []):
        if clip.get("id") == double_id:
            clip["routine_id"] = routine_id
            clip["routine_recipe"] = recipe
            clip["source_technique"] = "instant-doubles"
            if resolved_cue_kind:
                clip["cue_kind"] = resolved_cue_kind
            if cue_label:
                clip["cue_label"] = cue_label
            break
    if config.get("slip"):
        double_clip = next(clip for clip in next_payload.get("clips", []) if clip.get("id") == double_id)
        effect_beats = str(config.get("effect_beats", "1"))
        slip_duration_ms = max(1, int(round(float(Fraction(effect_beats)) * (60_000 / bpm))))
        next_payload = add_slip_event(
            next_payload,
            slip_id=f"{routine_id}-slip",
            source_id=source_id,
            target_id=double_id,
            start=str(double_clip["start_ms"]),
            duration=str(slip_duration_ms),
            routine_id=routine_id,
            routine_recipe=recipe,
            lock_before_ms=lock_before_ms,
            force=force,
        )
    if config.get("effect") in {"echo", "reverb", "vinyl_brake"}:
        double_clip = next(clip for clip in next_payload.get("clips", []) if clip.get("id") == double_id)
        effect_type = str(config["effect"])
        if effect_type == "vinyl_brake":
            source_payload = next(clip for clip in next_payload.get("clips", []) if clip.get("id") == source_id)
            double_clip["gain_db"] = -96.0
            if config.get("effect_track"):
                double_clip["kind"] = "effect-track"
                double_clip["attached_deck"] = str(source_payload.get("deck") or "")
                double_clip["effect_parent_clip_id"] = source_id
            brake_start = int(double_clip["start_ms"])
            brake_duration = max(1, int(round(float(Fraction(str(config.get("effect_beats", "1")))) * (60_000 / bpm))))
            if config.get("timing_brake"):
                source_start_ms, source_end_ms = clip_start_end(next_payload, source_id)
                if source_end_ms <= brake_start:
                    raise ValueError(f"timing brake {routine_id} must start before source clip ends")
                resume_id = f"{routine_id}-resume"
                require_unique_event_id(next_payload, resume_id)
                source_trim_ms = parse_ms(source_payload.get("trim_start_ms", source_payload.get("trim_start", 0)), f"clip {source_id} trim_start")
                source_trim_at_brake = source_trim_ms + int(round((brake_start - source_start_ms) * clip_tempo_factor(source_payload)))
                remaining_ms = max(1, source_end_ms - brake_start)
                resume_fade_out_ms = source_payload.get("fade_out_ms", 0)
                source_payload.pop("duration", None)
                source_payload["duration_ms"] = max(1, brake_start - source_start_ms)
                source_payload["fade_out_ms"] = 0
                resume_clip = {
                    "id": resume_id,
                    "deck": source_payload.get("deck"),
                    "path": source_payload.get("path"),
                    "start_ms": brake_start + brake_duration,
                    "trim_start_ms": source_trim_at_brake,
                    "duration_ms": remaining_ms,
                    "trim_db": float(source_payload.get("trim_db", 0.0)),
                    "gain_db": float(source_payload.get("gain_db", 0.0)),
                    "tempo_shift_pct": float(source_payload.get("tempo_shift_pct", 0.0)),
                    "pitch_shift_semitones": int(source_payload.get("pitch_shift_semitones", 0)),
                    "fade_in_ms": 0,
                    "fade_out_ms": resume_fade_out_ms,
                    "kind": "song",
                    "planner_role": "timing-brake-resume",
                    "source_clip_id": source_id,
                    "routine_id": routine_id,
                    "routine_recipe": recipe,
                    "source_technique": "timing-brake",
                }
                next_payload.setdefault("clips", []).append(resume_clip)
            source_deck = str(source_payload.get("deck") or "")
            routing = parse_fader_routing(
                next_payload,
                [str(deck) for deck in next_payload.get("decks", [])] or [f"deck-{index + 1}" for index in range(MAX_DECKS)],
            )
            source_side = routing.get(source_deck, DEFAULT_FADER_ASSIGNMENTS.get(source_deck, "A"))
            if source_side == "THRU":
                source_side = DEFAULT_FADER_ASSIGNMENTS.get(source_deck, "A")
            target_side = "B" if source_side == "A" else "A"
            on_position = -1.0 if target_side == "A" else 1.0
            decks = [str(deck) for deck in next_payload.get("decks", [])] or [f"deck-{index + 1}" for index in range(MAX_DECKS)]
            brake_assignments = {deck: source_side for deck in decks}
            next_payload = set_fader_routing(next_payload, brake_assignments)
            next_payload.setdefault("automations", []).append(
                {
                    "target": "crossfader",
                    "param": "position",
                    "planner_role": "vinyl-brake-crossfader-cut",
                    "points": [
                        {"at_ms": brake_start, "value": on_position},
                        {"at_ms": brake_start + brake_duration, "value": on_position},
                    ],
                    "routine_id": routine_id,
                    "routine_recipe": recipe,
                }
            )
        effect_duration = str(double_clip["duration_ms"])
        if effect_type == "vinyl_brake":
            effect_beats = str(config.get("effect_beats", "1"))
            effect_duration = str(max(1, int(round(float(Fraction(effect_beats)) * (60_000 / bpm)))))
        next_payload = add_effect_event(
            next_payload,
            effect_id=f"{routine_id}-{effect_type}",
            effect_type=effect_type,
            target=double_id,
            start=str(double_clip["start_ms"]),
            duration=effect_duration,
            tail_ms=3500 if effect_type == "reverb" else 0 if effect_type == "vinyl_brake" else 2000,
            wet=1.0 if effect_type == "vinyl_brake" else 0.82 if effect_type == "reverb" else 0.42,
            gain_db=-7.5 if effect_type == "vinyl_brake" else -2.0 if effect_type == "reverb" else -9.0,
            delay_ms=1 if effect_type == "vinyl_brake" else 10 if effect_type == "reverb" else 375,
            feedback=0.0 if effect_type == "vinyl_brake" else 0.5 if effect_type == "reverb" else 0.38,
            room_size=0.75,
            damping=0.5,
            lowpass_hz=None if effect_type in {"reverb", "vinyl_brake"} else 4200.0,
            routine_id=routine_id,
            routine_recipe=recipe,
            lock_before_ms=lock_before_ms,
            force=force,
        )
    for automation in next_payload.get("automations", []):
        if automation.get("target") in {double_id, source_id, "crossfader"} and str(automation.get("planner_role", "")).startswith("instant-double"):
            automation["routine_id"] = routine_id
            automation["routine_recipe"] = recipe
    parse_session(next_payload)
    return next_payload


def add_scratch_cut_routine(
    payload: dict[str, Any],
    *,
    source_id: str,
    routine_id: str,
    recipe: str,
    start: str | None,
    bpm: float,
    lock_before_ms: int | None = None,
    force: bool = False,
) -> dict[str, Any]:
    if start is None:
        raise ValueError(f"{recipe} requires --start")
    source_payload = next(clip for clip in payload.get("clips", []) if clip.get("id") == source_id)
    start_ms = parse_ms(start, f"routine {routine_id} start")
    guard_live_edit(label=f"routine {routine_id}", start_ms=start_ms, lock_before_ms=lock_before_ms, force=force)
    source_start_ms, source_end_ms = clip_start_end(payload, source_id)
    if source_end_ms <= start_ms:
        raise ValueError(f"scratch routine {routine_id} must start before source clip ends")
    beat_ms = 60_000 / bpm
    routine_duration_ms = parse_ms(SUPPORTED_INSTANT_DOUBLE_RECIPES[recipe]["duration"], f"{recipe} duration")
    routine_end_ms = min(source_end_ms, start_ms + routine_duration_ms)
    if routine_end_ms <= start_ms:
        raise ValueError(f"scratch routine {routine_id} has no duration")
    source_deck = str(source_payload.get("deck") or "")
    source_trim_ms = parse_ms(source_payload.get("trim_start_ms", source_payload.get("trim_start", 0)), f"clip {source_id} trim_start")
    source_trim_at_start = source_trim_ms + int(round((start_ms - source_start_ms) * clip_tempo_factor(source_payload)))
    decks = [str(deck) for deck in payload.get("decks", [])] or [f"deck-{index + 1}" for index in range(MAX_DECKS)]
    scratch_deck = next((deck for deck in decks if deck != source_deck), None)
    if scratch_deck is None:
        raise ValueError(f"scratch routine {routine_id} needs a free scratch deck")
    next_payload = copy.deepcopy(payload)
    routing = parse_fader_routing(next_payload, decks)
    source_side = routing.get(source_deck, DEFAULT_FADER_ASSIGNMENTS.get(source_deck, "A"))
    if source_side == "THRU":
        source_side = DEFAULT_FADER_ASSIGNMENTS.get(source_deck, "A")
    scratch_side = "B" if source_side == "A" else "A"
    source_position = -1.0 if source_side == "A" else 1.0
    scratch_position = -1.0 if scratch_side == "A" else 1.0
    assignments = {deck: source_side for deck in decks}
    assignments[scratch_deck] = scratch_side
    next_payload = set_fader_routing(next_payload, assignments)
    cut_points: list[dict[str, int | float]] = [{"at_ms": start_ms, "value": source_position}]
    # Scratch-cuts use the older continuous scratch vocabulary: short but audible
    # record-motion pulls spaced across the phrase, with the source deck audible
    # between cuts. Keep this sparse; dense micro-slices read as glitch stutter.
    pattern = [
        (0, 200, False, 0.72, 1.08),
        (2_000, 160, True, 0.95, 1.00),
        (4_000, 240, False, 1.18, 1.16),
        (6_500, 180, True, 0.82, 0.92),
    ]
    for index, (offset_ms, duration_ms, reverse, playback_rate, gain) in enumerate(pattern):
        clip_start = start_ms + offset_ms
        clip_duration = duration_ms
        if clip_start >= routine_end_ms:
            continue
        clip_duration = min(clip_duration, routine_end_ms - clip_start)
        trim_offset_ms = int(round(offset_ms * clip_tempo_factor(source_payload)))
        scratch_gain_db = 2.0 + (20 * math.log10(gain))
        scratch_clip = {
            "id": f"{routine_id}-scratch-{index + 1:02d}",
            "deck": scratch_deck,
            "path": source_payload.get("path"),
            "start_ms": clip_start,
            "trim_start_ms": source_trim_at_start + trim_offset_ms,
            "duration_ms": clip_duration,
            "trim_db": float(source_payload.get("trim_db", 0.0)),
            "gain_db": float(source_payload.get("gain_db", 0.0)) + scratch_gain_db,
            "tempo_shift_pct": 0.0,
            "pitch_shift_semitones": int(source_payload.get("pitch_shift_semitones", 0)),
            "fade_in_ms": min(18, clip_duration // 4),
            "fade_out_ms": min(18, clip_duration // 4),
            "reverse": reverse,
            "playback_rate": playback_rate,
            "kind": "effect-track",
            "attached_deck": source_deck,
            "effect_parent_clip_id": source_id,
            "planner_role": "scratch-cut",
            "source_clip_id": source_id,
            "routine_id": routine_id,
            "routine_recipe": recipe,
            "source_technique": "slip-transform-scratch",
        }
        next_payload.setdefault("clips", []).append(scratch_clip)
        cut_points.extend(
            [
                {"at_ms": clip_start, "value": source_position},
                {"at_ms": clip_start + 1, "value": scratch_position},
                {"at_ms": clip_start + max(1, clip_duration - 1), "value": scratch_position},
                {"at_ms": clip_start + clip_duration, "value": source_position},
            ]
        )
    cut_points.append({"at_ms": routine_end_ms, "value": source_position})
    deduped_points: list[dict[str, int | float]] = []
    for point in sorted(cut_points, key=lambda item: (int(item["at_ms"]), float(item["value"]))):
        if deduped_points and deduped_points[-1]["at_ms"] == point["at_ms"] and deduped_points[-1]["value"] == point["value"]:
            continue
        deduped_points.append(point)
    next_payload.setdefault("automations", []).append(
        {
            "target": "crossfader",
            "param": "position",
            "planner_role": "scratch-transform-cuts",
            "points": deduped_points,
            "routine_id": routine_id,
            "routine_recipe": recipe,
        }
    )
    next_payload.setdefault("slip_events", []).append(
        {
            "id": f"{routine_id}-slip",
            "source_clip_id": source_id,
            "target_clip_id": f"{routine_id}-scratch-01",
            "start_ms": start_ms,
            "duration_ms": routine_end_ms - start_ms,
            "source_start_ms": source_trim_at_start,
            "source_resume_ms": source_trim_at_start + int(round((routine_end_ms - start_ms) * clip_tempo_factor(source_payload))),
            "routine_id": routine_id,
            "routine_recipe": recipe,
        }
    )
    parse_session(next_payload)
    return next_payload


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
    add_clip_parser.add_argument("--trim-db", type=float, default=0.0)
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

    instant_double_parser = sub.add_parser("instant-double")
    instant_double_parser.add_argument("session", type=Path)
    instant_double_parser.add_argument("--source-id", required=True)
    instant_double_parser.add_argument("--id", required=True)
    instant_double_parser.add_argument("--start")
    instant_double_parser.add_argument("--deck")
    instant_double_parser.add_argument("--duration", default="00:08.000")
    instant_double_parser.add_argument("--gain-db", type=float)
    instant_double_parser.add_argument("--fade-in-ms", type=int, default=0)
    instant_double_parser.add_argument("--fade-out-ms", type=int, default=0)
    instant_double_parser.add_argument("--gate-beats", help="Optional on/off gate size, e.g. 1/2 or 1.")
    instant_double_parser.add_argument("--gate-offset-beats", help="Optional offset before the first gate, e.g. 1/2 for off-beat swaps.")
    instant_double_parser.add_argument("--cut-source", action="store_true", help="When gating, cut the source clip while the double is open.")
    instant_double_parser.add_argument("--cache", type=Path, default=DEFAULT_DJ_CACHE)
    instant_double_parser.add_argument("--min-confidence", type=float, default=DEFAULT_MIN_BEATGRID_CONFIDENCE)
    add_live_edit_args(instant_double_parser)

    instant_double_routine_parser = sub.add_parser("instant-double-routine")
    instant_double_routine_parser.add_argument("session", type=Path)
    instant_double_routine_parser.add_argument("--source-id", required=True)
    instant_double_routine_parser.add_argument("--id", required=True)
    instant_double_routine_parser.add_argument("--recipe", required=True)
    instant_double_routine_parser.add_argument("--start")
    instant_double_routine_parser.add_argument("--cue-kind")
    instant_double_routine_parser.add_argument("--cue-db", type=Path, default=DEFAULT_LIBRARY_DB)
    instant_double_routine_parser.add_argument("--cache", type=Path, default=DEFAULT_DJ_CACHE)
    instant_double_routine_parser.add_argument("--min-confidence", type=float, default=DEFAULT_MIN_BEATGRID_CONFIDENCE)
    add_live_edit_args(instant_double_routine_parser)

    automate_parser = sub.add_parser("automate")
    automate_parser.add_argument("session", type=Path)
    automate_parser.add_argument("--target", required=True)
    automate_parser.add_argument("--param", required=True)
    automate_parser.add_argument("--points-json", required=True)
    add_live_edit_args(automate_parser)

    effect_parser = sub.add_parser("add-effect")
    effect_parser.add_argument("session", type=Path)
    effect_parser.add_argument("--id", required=True)
    effect_parser.add_argument("--type", choices=["echo", "reverb", "vinyl_brake"], default="echo")
    effect_parser.add_argument("--preset", choices=sorted(AUDACITY_REVERB_PRESETS))
    effect_parser.add_argument("--target", required=True)
    effect_parser.add_argument("--start", required=True)
    effect_parser.add_argument("--duration", required=True)
    effect_parser.add_argument("--tail-ms", type=int)
    effect_parser.add_argument("--wet", type=float)
    effect_parser.add_argument("--gain-db", type=float)
    effect_parser.add_argument("--delay-ms", type=int)
    effect_parser.add_argument("--feedback", type=float)
    effect_parser.add_argument("--room-size", type=float)
    effect_parser.add_argument("--damping", type=float)
    effect_parser.add_argument("--lowpass-hz", type=float)
    add_live_edit_args(effect_parser)

    slip_parser = sub.add_parser("slip")
    slip_parser.add_argument("session", type=Path)
    slip_parser.add_argument("--id", required=True)
    slip_parser.add_argument("--source-id", required=True)
    slip_parser.add_argument("--target-id", required=True)
    slip_parser.add_argument("--start", required=True)
    slip_parser.add_argument("--duration", required=True)
    add_live_edit_args(slip_parser)

    fader_routing_parser = sub.add_parser("fader-routing")
    fader_routing_parser.add_argument("session", type=Path)
    fader_routing_parser.add_argument(
        "--assign",
        action="append",
        required=True,
        help="Deck assignment like deck-1=A, deck-2=B, or deck-4=THRU. Repeat for multiple decks.",
    )

    crossfader_parser = sub.add_parser("crossfader")
    crossfader_parser.add_argument("session", type=Path)
    crossfader_parser.add_argument("--points-json", required=True)
    add_live_edit_args(crossfader_parser)

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
            trim_db=args.trim_db,
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

    if args.command == "instant-double":
        lock_before_ms = live_edit_lock(args)
        write_payload(
            args.session,
            add_instant_double(
                load_payload(args.session),
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
                lock_before_ms=lock_before_ms,
                force=args.force,
            ),
        )
        print(f"added instant double {args.id} from {args.source_id}")
        return 0

    if args.command == "instant-double-routine":
        lock_before_ms = live_edit_lock(args)
        write_payload(
            args.session,
            add_instant_double_routine(
                load_payload(args.session),
                source_id=args.source_id,
                routine_id=args.id,
                recipe=args.recipe,
                start=args.start,
                cue_kind=args.cue_kind,
                cue_db=args.cue_db,
                cache_path=args.cache,
                min_confidence=args.min_confidence,
                lock_before_ms=lock_before_ms,
                force=args.force,
            ),
        )
        print(f"added instant double routine {args.id} ({args.recipe}) from {args.source_id}")
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

    if args.command == "add-effect":
        lock_before_ms = live_edit_lock(args)
        effect_args = resolved_effect_args(args)
        write_payload(
            args.session,
            add_effect_event(
                load_payload(args.session),
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
                lock_before_ms=lock_before_ms,
                force=args.force,
            ),
        )
        print(f"added effect {args.id}")
        return 0

    if args.command == "slip":
        lock_before_ms = live_edit_lock(args)
        write_payload(
            args.session,
            add_slip_event(
                load_payload(args.session),
                slip_id=args.id,
                source_id=args.source_id,
                target_id=args.target_id,
                start=args.start,
                duration=args.duration,
                lock_before_ms=lock_before_ms,
                force=args.force,
            ),
        )
        print(f"added slip event {args.id}")
        return 0

    if args.command == "fader-routing":
        assignments: dict[str, str] = {}
        for value in args.assign:
            if "=" not in value:
                raise ValueError("--assign must be formatted as deck=side")
            deck, side = value.split("=", 1)
            assignments[deck.strip()] = side.strip()
        write_payload(args.session, set_fader_routing(load_payload(args.session), assignments))
        print(f"updated fader routing for {len(assignments)} deck(s)")
        return 0

    if args.command == "crossfader":
        lock_before_ms = live_edit_lock(args)
        write_payload(
            args.session,
            add_crossfader_automation(
                load_payload(args.session),
                points_json=args.points_json,
                lock_before_ms=lock_before_ms,
                force=args.force,
            ),
        )
        print("added crossfader automation")
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
