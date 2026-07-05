#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
import math
import re
import sqlite3
import subprocess
import wave
from dataclasses import dataclass, field
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from time import time
from typing import Any

MAX_DECKS = 5
DEFAULT_MUSIC_DECKS = ["deck-1", "deck-2", "deck-3", "deck-4"]
VOCAL_DECK = "deck-5"
DEFAULT_SESSION_DECKS = [*DEFAULT_MUSIC_DECKS, VOCAL_DECK]
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
    "loop-roll": {"duration": "00:04.000", "loop_pattern": True, "loop_beats": "1", "cut_source": False},
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
    "mute",
    "solo",
}
STEM_NAMES = {"vocals", "drums", "bass", "other"}
SOURCE_ARTIFACT_PATH_MARKERS = (
    "/separated/",
    "/isolated/",
    "/duplicate/",
    "/duplicated/",
    "/duplicates/",
)
STEM_AUTOMATABLE_PARAMS = {
    "gain_db",
    "mute",
    "solo",
    "eq_low_db",
    "eq_mid_db",
    "eq_high_db",
    "lowpass_hz",
    "highpass_hz",
    "send_echo",
    "send_reverb",
}
FADER_SIDES = {"A", "B", "THRU"}
DEFAULT_FADER_ASSIGNMENTS = {
    "deck-1": "A",
    "deck-2": "B",
    "deck-3": "A",
    "deck-4": "B",
    "deck-5": "THRU",
}


def is_artifact_source_path(path: str) -> bool:
    normalized = str(path).replace("\\", "/").lower()
    return any(marker in normalized for marker in SOURCE_ARTIFACT_PATH_MARKERS)

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
    # Stem selection for clip playback. The renderer honors this by premixing
    # ready stem artifacts for the source; rendering fails loudly when the
    # requested stems are not ready instead of silently playing the full track.
    play_stems: tuple[str, ...] | None = None
    # Deck-clock provenance: set when this clip is a materialized segment of a
    # load_track action (loading is how songs play; raw clips are fallback).
    source_action_id: str | None = None
    deck_clock_segment: bool = False
    automations: list[Automation] = field(default_factory=list)

    @property
    def end_ms(self) -> int | None:
        if self.duration_ms is None:
            return None
        return self.start_ms + self.duration_ms


@dataclass(frozen=True)
class StemState:
    path: str | None = None
    enabled: bool = True
    gain_db: float = 0.0
    mute: bool = False
    solo: bool = False
    eq_low_db: float = 0.0
    eq_mid_db: float = 0.0
    eq_high_db: float = 0.0
    lowpass_hz: float | None = None
    highpass_hz: float | None = None
    send_echo: float = 0.0
    send_reverb: float = 0.0
    automations: list[Automation] = field(default_factory=list)


@dataclass(frozen=True)
class StemGroup:
    id: str
    deck: str
    source_path: str
    start_ms: int
    trim_start_ms: int = 0
    duration_ms: int | None = None
    stem_set_id: str | None = None
    manifest_path: str | None = None
    gain_db: float = 0.0
    tempo_shift_pct: float = 0.0
    pitch_shift_semitones: int = 0
    fade_in_ms: int = 0
    fade_out_ms: int = 0
    reverse: bool = False
    playback_rate: float = 1.0
    stems: dict[str, StemState] = field(default_factory=dict)
    automations: list[Automation] = field(default_factory=list)

    @property
    def end_ms(self) -> int | None:
        if self.duration_ms is None:
            return None
        return self.start_ms + self.duration_ms


@dataclass(frozen=True)
class MicLeanIn:
    id: str
    deck: str
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
    stem_groups: list[StemGroup]
    mic_lean_ins: list[MicLeanIn]
    effects: list[EffectEvent]
    automations: list[Automation]
    deck_automations: list[Automation] = field(default_factory=list)
    slip_events: list[SlipEvent] = field(default_factory=list)
    fader_routing: dict[str, str] = field(default_factory=dict)

    @property
    def event_ids(self) -> set[str]:
        return (
            {clip.id for clip in self.clips}
            | {group.id for group in self.stem_groups}
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
    play_stems_value = payload.get("play_stems", payload.get("enabled_stems"))
    play_stems: tuple[str, ...] | None = None
    if isinstance(play_stems_value, list):
        play_stems = tuple(str(stem) for stem in play_stems_value)
        unsupported = sorted(set(play_stems) - STEM_NAMES)
        if unsupported:
            raise ValueError(f"clip {clip_id} has unsupported play_stems: {', '.join(unsupported)}")
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
        play_stems=play_stems,
        source_action_id=str(payload["source_action_id"]) if payload.get("source_action_id") else None,
        deck_clock_segment=bool(payload.get("deck_clock_segment", False)),
        automations=[
            parse_automation(item, default_target=clip_id)
            for item in payload.get("automations", [])
        ],
    )
    return clip


def parse_stem_state(group_id: str, stem_name: str, payload: dict[str, Any] | bool) -> StemState:
    if stem_name not in STEM_NAMES:
        raise ValueError(f"stem group {group_id} has unsupported stem {stem_name}")
    if isinstance(payload, bool):
        payload = {"enabled": payload}
    if not isinstance(payload, dict):
        raise ValueError(f"stem group {group_id} stem {stem_name} must be an object or boolean")
    return StemState(
        path=str(payload["path"]) if payload.get("path") else None,
        enabled=bool(payload.get("enabled", True)),
        gain_db=float(payload.get("gain_db", 0.0)),
        mute=bool(payload.get("mute", False)),
        solo=bool(payload.get("solo", False)),
        eq_low_db=float(payload.get("eq_low_db", 0.0)),
        eq_mid_db=float(payload.get("eq_mid_db", 0.0)),
        eq_high_db=float(payload.get("eq_high_db", 0.0)),
        lowpass_hz=float(payload["lowpass_hz"]) if payload.get("lowpass_hz") is not None else None,
        highpass_hz=float(payload["highpass_hz"]) if payload.get("highpass_hz") is not None else None,
        send_echo=float(payload.get("send_echo", 0.0)),
        send_reverb=float(payload.get("send_reverb", 0.0)),
        automations=[
            parse_automation(item, default_target=f"stem-group:{group_id}:{stem_name}")
            for item in payload.get("automations", [])
        ],
    )


def parse_stem_group(payload: dict[str, Any]) -> StemGroup:
    group_id = str(payload.get("id") or "").strip()
    deck = str(payload.get("deck") or "").strip()
    source_path = str(payload.get("source_path") or payload.get("path") or "").strip()
    if not group_id:
        raise ValueError("stem group id is required")
    if not deck:
        raise ValueError(f"stem group {group_id} deck is required")
    if not source_path:
        raise ValueError(f"stem group {group_id} source_path is required")
    duration = payload.get("duration_ms", payload.get("duration"))
    stems_payload = payload.get("stems") or {}
    if not isinstance(stems_payload, dict):
        raise ValueError(f"stem group {group_id} stems must be an object")
    return StemGroup(
        id=group_id,
        deck=deck,
        source_path=source_path,
        stem_set_id=str(payload["stem_set_id"]) if payload.get("stem_set_id") else None,
        manifest_path=str(payload["manifest_path"]) if payload.get("manifest_path") else None,
        start_ms=parse_ms(payload.get("start_ms", payload.get("start", 0)), f"stem group {group_id} start"),
        trim_start_ms=parse_ms(payload.get("trim_start_ms", payload.get("trim_start", 0)), f"stem group {group_id} trim_start"),
        duration_ms=parse_ms(duration, f"stem group {group_id} duration") if duration is not None else None,
        gain_db=float(payload.get("gain_db", 0.0)),
        tempo_shift_pct=float(payload.get("tempo_shift_pct", 0.0)),
        pitch_shift_semitones=int(payload.get("pitch_shift_semitones", 0)),
        fade_in_ms=parse_ms(payload.get("fade_in_ms", 0), f"stem group {group_id} fade_in_ms"),
        fade_out_ms=parse_ms(payload.get("fade_out_ms", 0), f"stem group {group_id} fade_out_ms"),
        reverse=bool(payload.get("reverse", False)),
        playback_rate=float(payload.get("playback_rate", 1.0)),
        stems={
            stem_name: parse_stem_state(group_id, stem_name, stem_payload)
            for stem_name, stem_payload in stems_payload.items()
        },
        automations=[
            parse_automation(item, default_target=group_id)
            for item in payload.get("automations", [])
        ],
    )


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
        deck=str(payload.get("deck") or VOCAL_DECK),
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
    return apply_master_key(apply_master_tempo(json.loads(path.read_text(encoding="utf-8"))))


DEFAULT_MAX_WARP_STRETCH_PCT = 16.0
DEFAULT_MAX_KEY_SHIFT_SEMITONES = 2

KEY_NAME_TO_SEMITONE = {
    "c": 0, "b#": 0, "c#": 1, "db": 1, "d": 2, "d#": 3, "eb": 3, "e": 4, "fb": 4,
    "f": 5, "e#": 5, "f#": 6, "gb": 6, "g": 7, "g#": 8, "ab": 8, "a": 9,
    "a#": 10, "bb": 10, "b": 11, "cb": 11,
}


def parse_master_key(value: Any) -> tuple[int, str] | None:
    """Parse a master key like "A minor", "F# major", "Bbm", or {"tonic", "mode"}."""
    if value is None:
        return None
    if isinstance(value, dict):
        tonic = value.get("tonic")
        mode = str(value.get("mode") or "major").lower()
        if tonic is None:
            return None
        return int(tonic) % 12, ("minor" if mode.startswith("min") or mode == "m" else "major")
    text = str(value).strip().lower()
    if not text:
        return None
    mode = "major"
    for suffix in (" minor", "minor", " min", "min"):
        if text.endswith(suffix):
            mode = "minor"
            text = text[: -len(suffix)].strip()
            break
    else:
        for suffix in (" major", "major", " maj", "maj"):
            if text.endswith(suffix):
                text = text[: -len(suffix)].strip()
                break
        else:
            if text.endswith("m") and text[:-1] in KEY_NAME_TO_SEMITONE:
                mode = "minor"
                text = text[:-1]
    if text not in KEY_NAME_TO_SEMITONE:
        raise ValueError(f"unrecognized master key: {value!r}")
    return KEY_NAME_TO_SEMITONE[text], mode


def relative_major_tonic(tonic: int, mode: str) -> int:
    # Minor converts to its relative major before any pitch-step math.
    return (int(tonic) + 3) % 12 if str(mode).lower().startswith("min") else int(tonic) % 12


def master_key_shift_semitones(tonic: int, mode: str, master: tuple[int, str], max_shift: int) -> int | None:
    """Smallest pitch shift aligning a track's relative-major tonic to the master's.

    Returns None when no shift within the limit aligns them — such material
    plays at its native key instead of being audibly wrenched.
    """
    delta = (relative_major_tonic(*master) - relative_major_tonic(tonic, mode)) % 12
    if delta > 6:
        delta -= 12
    if abs(delta) > max_shift:
        return None
    return delta


def master_key_at(payload: dict[str, Any], at_ms: int) -> tuple[int, str] | None:
    """The master key at a timeline position.

    Keys are discrete, so `master_key_automation` points are step changes:
    the base `master_key` holds until the first point, then each point's
    value holds until the next. The DJ modulates the set's key strategically
    when the upcoming crate sits out of reach of the current center — far
    material should trigger a key ride, not a fallback to native clashes.
    """
    base = parse_master_key(payload.get("master_key"))
    points = sorted(
        ((parse_ms(point.get("at", point.get("at_ms")), "master key point"), point["value"]) for point in payload.get("master_key_automation", [])),
        key=lambda item: item[0],
    )
    current = base
    for point_ms, value in points:
        if at_ms >= point_ms:
            current = parse_master_key(value)
        else:
            break
    return current


def apply_master_key(payload: dict[str, Any]) -> dict[str, Any]:
    """Keymatch clips/stem groups to the session's master key, DAW-style.

    Auto keymatching is on by default for every event carrying key metadata;
    `keymatch: false` opts a track out (it then keeps its authored pitch
    shift, exactly as before). Minor keys convert to their relative major
    before the pitch-step math. The master key is rideable
    (`master_key_automation` step points); each event matches the key at its
    own start, so a modulation lands with an incoming record. Material out
    of reach of the *current* key plays native — the cue to modulate, not a
    steady state. Idempotent, applied on every payload load like the tempo.
    """
    if not payload.get("master_key") and not payload.get("master_key_automation"):
        return payload
    max_shift = abs(int(payload.get("max_key_shift_semitones", DEFAULT_MAX_KEY_SHIFT_SEMITONES)))
    events = list(payload.get("clips", [])) + list(payload.get("stem_groups", payload.get("stemGroups", [])))
    # A RECORD pins the center once, at its first compiled segment: stem
    # toggles segment a load, and pinning each segment at its own start made
    # a junction modulation re-pitch the outgoing record's remaining segments
    # mid-play (an audible key jump inside one song).
    record_anchor_ms: dict[str, int] = {}
    for event in events:
        source = str(event.get("source_action_id") or "")
        if source:
            start = int(event.get("start_ms") or 0)
            record_anchor_ms[source] = min(record_anchor_ms.get(source, start), start)
    for event in events:
        if not event.get("keymatch", True):
            continue
        tonic = event.get("tonic")
        mode = event.get("mode")
        if tonic is None or mode not in ("major", "minor"):
            continue
        source = str(event.get("source_action_id") or "")
        anchor_ms = record_anchor_ms.get(source, int(event.get("start_ms") or 0)) if source else int(event.get("start_ms") or 0)
        master = master_key_at(payload, anchor_ms)
        if master is None:
            continue
        shift = master_key_shift_semitones(int(tonic), str(mode), master, max_shift)
        event["pitch_shift_semitones"] = int(shift) if shift is not None else 0
    return payload


def master_tempo_shift_pct(source_bpm: float, master_bpm: float, max_stretch_pct: float) -> float | None:
    """Smallest render stretch that puts a source at the session's master tempo.

    Tries straight, double-time, and half-time interpretations (a 180 BPM
    source fits a 90 master unstretched at half-time feel). Returns None when
    no interpretation lands inside the stretch limit — such material plays at
    its authored tempo instead of being audibly warped.
    """
    if source_bpm <= 0 or master_bpm <= 0:
        return None
    best: float | None = None
    for multiple in (1.0, 2.0, 0.5):
        shift = (master_bpm * multiple / source_bpm - 1.0) * 100.0
        if abs(shift) <= max_stretch_pct and (best is None or abs(shift) < abs(best)):
            best = shift
    return best


def master_bpm_at(payload: dict[str, Any], at_ms: int) -> float | None:
    """The master tempo knob's value at a timeline position.

    `master_bpm` is the knob's base position; `master_bpm_automation` points
    (at_ms/value) ride it — linear between points, base before the first,
    held after the last.
    """
    base = payload.get("master_bpm")
    points = sorted(
        ((int(point["at_ms"]), float(point["value"])) for point in payload.get("master_bpm_automation", [])),
        key=lambda item: item[0],
    )
    for point_ms, value in points:
        if point_ms < 0:
            raise ValueError("master_bpm_automation points must not be negative")
        if value <= 0:
            raise ValueError("master_bpm_automation values must be positive BPM")
    if not points:
        return float(base) if base else None
    if base:
        points.insert(0, (0, float(base)))
    if at_ms <= points[0][0]:
        return points[0][1]
    if at_ms >= points[-1][0]:
        return points[-1][1]
    for (left_ms, left_value), (right_ms, right_value) in zip(points, points[1:]):
        if left_ms <= at_ms <= right_ms:
            if right_ms == left_ms:
                return right_value
            pct = (at_ms - left_ms) / (right_ms - left_ms)
            return left_value + (right_value - left_value) * pct
    return points[-1][1]


def apply_master_tempo(payload: dict[str, Any]) -> dict[str, Any]:
    """Warp clips/stem groups to the session's master tempo, DAW-style.

    The session owns tempo: every clip carrying its analyzed `source_bpm`
    renders at the master tempo (or its double/half) unless it opts out with
    `warp: false` — the escape for sample drops and free-time material.
    The master is a knob: `master_bpm_automation` points ride it over the
    timeline, and each clip warps to the knob's value at its own start (a
    tempo drift lands with each incoming record; a clip never wobbles
    internally). Derivation is idempotent and runs on every payload load, so
    tempo edits behind the playhead retempo all future windows at the
    runner's next reload. Mic lean-ins are not clips and never warp.
    """
    if not payload.get("master_bpm") and not payload.get("master_bpm_automation"):
        return payload
    max_stretch = abs(float(payload.get("max_tempo_stretch_pct", DEFAULT_MAX_WARP_STRETCH_PCT)))
    for event in list(payload.get("clips", [])) + list(payload.get("stem_groups", payload.get("stemGroups", []))):
        if not event.get("warp", True):
            continue
        source_bpm = event.get("source_bpm")
        if not source_bpm:
            continue
        master = master_bpm_at(payload, int(event.get("start_ms") or 0))
        if master is None:
            continue
        shift = master_tempo_shift_pct(float(source_bpm), master, max_stretch)
        event["tempo_shift_pct"] = round(shift, 3) if shift is not None else 0.0
    return payload


def write_payload(path: Path, payload: dict[str, Any]) -> None:
    parse_session(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def action_type(action: dict[str, Any]) -> str:
    return str(action.get("type") or action.get("action") or "").strip()


def action_id(action: dict[str, Any]) -> str:
    return str(action.get("id") or action.get("action_id") or "").strip()


def action_at_ms(action: dict[str, Any]) -> int:
    return parse_ms(action.get("at_ms", action.get("at", action.get("start_ms", action.get("start", 0)))), "action time")


def action_position_ms(action: dict[str, Any], label: str = "action position") -> int:
    value = action.get(
        "position_ms",
        action.get("position", action.get("source_ms", action.get("source_position_ms", action.get("cue_ms", action.get("cue", action.get("trim_start_ms", action.get("trim_start", 0))))))),
    )
    return parse_ms(value, label)


def action_has_all_stem_paths(action: dict[str, Any]) -> bool:
    stems = action.get("stems")
    if not isinstance(stems, dict):
        return False
    for stem_name in STEM_NAMES:
        stem_payload = stems.get(stem_name)
        if isinstance(stem_payload, str) and stem_payload.strip():
            continue
        if isinstance(stem_payload, dict) and str(stem_payload.get("path") or "").strip():
            continue
        return False
    return True


def load_track_group_from_action(
    action: dict[str, Any],
    *,
    group_id: str | None = None,
    start_ms: int | None = None,
    trim_start_ms: int | None = None,
    duration_ms: int | None = None,
) -> tuple[dict[str, Any], str]:
    group_id = group_id or action_id(action)
    deck = str(action.get("deck") or "").strip()
    source_path = str(action.get("source_path") or action.get("path") or "").strip()
    stems_payload = action.get("stems") or {}
    if not group_id:
        raise ValueError("load_track action id is required")
    if not deck:
        raise ValueError(f"load_track {group_id} deck is required")
    if not source_path:
        raise ValueError(f"load_track {group_id} source_path is required")
    if not isinstance(stems_payload, dict):
        raise ValueError(f"load_track {group_id} stems must be an object")
    missing_stems = sorted(STEM_NAMES - set(stems_payload))
    if missing_stems:
        raise ValueError(f"load_track {group_id} must include all stems: {', '.join(missing_stems)}")
    enabled_stems = action.get("play_stems", action.get("enabled_stems"))
    enabled_set = {str(stem) for stem in enabled_stems} if isinstance(enabled_stems, list) else None
    stems: dict[str, Any] = {}
    for stem_name in sorted(STEM_NAMES):
        stem_payload = stems_payload[stem_name]
        if isinstance(stem_payload, str):
            stem_payload = {"path": stem_payload}
        elif isinstance(stem_payload, bool):
            stem_payload = {"enabled": stem_payload}
        elif not isinstance(stem_payload, dict):
            raise ValueError(f"load_track {group_id} stem {stem_name} must be an object, path, or boolean")
        stem_payload = dict(stem_payload)
        if enabled_set is not None:
            stem_payload["enabled"] = stem_name in enabled_set
        stems[stem_name] = stem_payload
    raw_duration = action.get("duration_ms", action.get("duration"))
    if duration_ms is None:
        resolved_duration_ms = parse_ms(raw_duration, f"load_track {group_id} duration") if raw_duration is not None else None
    else:
        resolved_duration_ms = int(duration_ms)
    group = {
        "id": group_id,
        "deck": deck,
        "source_path": source_path,
        "start_ms": action_at_ms(action) if start_ms is None else int(start_ms),
        "trim_start_ms": parse_ms(action.get("trim_start_ms", action.get("trim_start", action.get("cue_ms", action.get("cue", 0)))), f"load_track {group_id} trim") if trim_start_ms is None else int(trim_start_ms),
        "duration_ms": resolved_duration_ms,
        "gain_db": float(action.get("gain_db", 0.0)),
        "tempo_shift_pct": float(action.get("tempo_shift_pct", 0.0)),
        "pitch_shift_semitones": int(action.get("pitch_shift_semitones", 0)),
        "fade_in_ms": parse_ms(action.get("fade_in_ms", 0), f"load_track {group_id} fade_in_ms"),
        "fade_out_ms": parse_ms(action.get("fade_out_ms", 0), f"load_track {group_id} fade_out_ms"),
        "stems": stems,
        "source_action_id": action_id(action) or group_id,
    }
    if action.get("stem_set_id"):
        group["stem_set_id"] = str(action["stem_set_id"])
    if action.get("manifest_path"):
        group["manifest_path"] = str(action["manifest_path"])
    if action.get("source_bpm"):
        group["source_bpm"] = float(action["source_bpm"])
    if "warp" in action:
        group["warp"] = bool(action["warp"])
    return group, deck


def action_load_target(action: dict[str, Any]) -> str:
    return str(action.get("target") or action.get("group_id") or action.get("load_id") or action.get("deck") or "").strip()


def action_cue_name(action: dict[str, Any]) -> str:
    return str(action.get("cue_id") or action.get("cue_name") or action.get("name") or action.get("id") or "").strip()


def stem_group_source_position(group: dict[str, Any], mix_ms: int) -> int:
    start_ms = parse_ms(group.get("start_ms", group.get("start", 0)), f"stem group {group.get('id')} start")
    trim_start_ms = parse_ms(group.get("trim_start_ms", group.get("trim_start", 0)), f"stem group {group.get('id')} trim_start")
    factor = clip_tempo_factor(group)
    return trim_start_ms + int(round((mix_ms - start_ms) * factor))


def action_requests_stems(action: dict[str, Any]) -> bool:
    return bool(action.get("play_stems") or action.get("enabled_stems") or action.get("stems"))


def action_stems_untouched(action: dict[str, Any]) -> bool:
    """True when the load's stem interface is at rest: all four stems on with
    nothing customized. Stems are conceptually always present on every load;
    when all four play untouched, the original file is simply the
    higher-quality render of the same thing."""
    enabled = action.get("play_stems", action.get("enabled_stems"))
    if isinstance(enabled, list) and {str(stem) for stem in enabled} != STEM_NAMES:
        return False
    stems = action.get("stems")
    if isinstance(stems, dict):
        for stem_payload in stems.values():
            if isinstance(stem_payload, str):
                continue
            if isinstance(stem_payload, bool):
                if not stem_payload:
                    return False
                continue
            if not isinstance(stem_payload, dict):
                return False
            for key, value in stem_payload.items():
                if key == "path":
                    continue
                if key == "enabled" and bool(value):
                    continue
                if key in {"mute", "solo"} and not value:
                    continue
                if key in {"gain_db", "eq_low_db", "eq_mid_db", "eq_high_db", "send_echo", "send_reverb"} and not value:
                    continue
                if key == "automations" and not value:
                    continue
                return False
    return True


def stem_customized_load_ids(actions: list[dict[str, Any]]) -> set[str]:
    """Loads whose stems get played with anywhere in the action list.

    Toggling stems on and off through a track is a first-class move, so any
    stem_toggle / stem-targeted knob ride / stem mute or solo pins every
    segment of that load to the stems render path."""
    customized: set[str] = set()
    for action in actions:
        kind = action_type(action)
        if kind in {"stem_mute", "stem_solo"}:
            target = str(action.get("target") or action.get("group_id") or action.get("load_id") or "").strip()
            if target:
                customized.add(target)
        elif kind == "knob_lerp":
            target = str(action.get("target") or "")
            if target.startswith("stem-group:"):
                parts = target.split(":")
                if len(parts) >= 2 and parts[1]:
                    customized.add(parts[1])
    return customized


def hydrate_action_stems(action: dict[str, Any], *, db_path: Path | None = None) -> dict[str, Any]:
    """Attach ready stem artifact paths to a load that was authored plain.

    Renders fail loudly rather than silently playing the full track when a
    stem-customized load has no split artifacts."""
    if isinstance(action.get("stems"), dict) and action["stems"]:
        return action
    source_path = str(action.get("source_path") or action.get("path") or "")
    artifacts = ready_stem_artifacts(db_path or DEFAULT_LIBRARY_DB, source_path)
    if artifacts is None:
        raise ValueError(
            f"load_track {action_id(action)} has stem moves but no ready stem artifacts for {source_path}; "
            "run slime_audio_stems.py backfill (splits are queued automatically at generation)"
        )
    hydrated = copy.deepcopy(action)
    hydrated["stems"] = dict(artifacts["stems"])
    hydrated["stem_set_id"] = artifacts["stem_set_id"]
    hydrated["manifest_path"] = artifacts["manifest_path"]
    return hydrated


PLAIN_LOAD_CLIP_FIELDS = (
    "gain_db",
    "tempo_shift_pct",
    "pitch_shift_semitones",
    "fade_in_ms",
    "fade_out_ms",
    "source_bpm",
    "warp",
    "kind",
    "planner_role",
    "source_window_reason",
    "source_structure_kind",
    "source_duration_ms",
    "stems_ready",
    "key",
    "tonic",
    "mode",
    "camelot",
    "transition_decision",
)


def plain_clip_from_load_action(
    load_action: dict[str, Any],
    *,
    clip_id: str,
    start_ms: int,
    trim_start_ms: int,
    duration_ms: int | None,
) -> dict[str, Any]:
    """A stem-less load_track plays the record itself, as a plain clip segment.

    Loading is how songs get onto decks; raw session clips are the fallback
    representation, not the product. Stem selection stays the opt-in that
    requires split artifacts.
    """
    clip: dict[str, Any] = {
        "id": clip_id,
        "deck": str(load_action.get("deck") or ""),
        "path": str(load_action.get("source_path") or load_action.get("path") or ""),
        "start_ms": int(start_ms),
        "trim_start_ms": int(trim_start_ms),
        "source_action_id": action_id(load_action),
        "deck_clock_segment": True,
    }
    if duration_ms is not None:
        clip["duration_ms"] = int(duration_ms)
    for field in PLAIN_LOAD_CLIP_FIELDS:
        if load_action.get(field) is not None:
            clip[field] = load_action[field]
    return clip


def append_deck_clock_segment(
    stem_groups: list[dict[str, Any]],
    group_payloads: dict[str, dict[str, Any]],
    load_action: dict[str, Any],
    *,
    segment_id: str,
    start_ms: int,
    trim_start_ms: int,
    duration_ms: int,
    pending_stem_automations: dict[str, dict[str, list[dict[str, Any]]]] | None = None,
    clips: list[dict[str, Any]] | None = None,
    stem_customized: set[str] | None = None,
    stems_enabled: set[str] | None = None,
    keep_fade_in: bool = True,
    keep_fade_out: bool = True,
) -> dict[str, Any] | None:
    if duration_ms <= 0:
        return None
    # Fades belong to a load's REAL ends. A mid-load segment boundary (a
    # toggle) inheriting the authored fade_in played every stems handoff as
    # a cut-to-zero-and-fade-back (heard live on 2026-07-04).
    if not keep_fade_in or not keep_fade_out:
        load_action = dict(load_action)
        if not keep_fade_in:
            load_action["fade_in_ms"] = 0
        if not keep_fade_out:
            load_action["fade_out_ms"] = 0
    load_id = action_id(load_action)
    segment_all_on = stems_enabled is None or {str(stem) for stem in stems_enabled} == STEM_NAMES
    if stems_enabled is not None and segment_all_on and not action_stems_untouched(load_action):
        candidate = dict(load_action)
        candidate["play_stems"] = sorted(STEM_NAMES)
        if action_stems_untouched(candidate):
            load_action = candidate
    untouched = segment_all_on and action_stems_untouched(load_action) and (not stem_customized or load_id not in stem_customized)
    if stems_enabled is not None and not segment_all_on:
        load_action = dict(load_action)
        load_action["play_stems"] = sorted(stems_enabled)
    if clips is not None and untouched:
        # Stems are always conceptually present; with all four at rest the
        # original file is the higher-quality render of the same thing.
        clip = plain_clip_from_load_action(
            load_action,
            clip_id=segment_id,
            start_ms=start_ms,
            trim_start_ms=trim_start_ms,
            duration_ms=duration_ms,
        )
        clips.append(clip)
        return clip
    load_action = hydrate_action_stems(load_action)
    group, _deck = load_track_group_from_action(
        load_action,
        group_id=segment_id,
        start_ms=start_ms,
        trim_start_ms=trim_start_ms,
        duration_ms=duration_ms,
    )
    group["source_action_id"] = action_id(load_action)
    group["deck_clock_segment"] = True
    # Stem segments must know their key too, or keymatching (and the harmonic
    # guard) goes blind exactly where the mixing happens.
    for field in ("key", "tonic", "mode", "camelot"):
        if load_action.get(field) is not None and group.get(field) is None:
            group[field] = load_action[field]
    load_id = action_id(load_action)
    if pending_stem_automations and load_id in pending_stem_automations:
        for stem_name, automations in pending_stem_automations[load_id].items():
            stem_payload = group.setdefault("stems", {}).setdefault(stem_name, {})
            if isinstance(stem_payload, str):
                stem_payload = {"path": stem_payload}
                group["stems"][stem_name] = stem_payload
            stem_payload.setdefault("automations", []).extend(copy.deepcopy(automations))
    stem_groups.append(group)
    group_payloads[str(group["id"])] = group
    return group


def ready_stem_artifacts(db_path: Path, source_path: str) -> dict[str, Any] | None:
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        return None
    candidates = [source_path]
    expanded = Path(source_path).expanduser()
    if expanded.exists():
        candidates.append(str(expanded.resolve()))
    placeholders = ",".join("?" for _ in candidates)
    try:
        stem_set = conn.execute(
            f"""
            SELECT id, artifact_root
            FROM track_stem_sets
            WHERE status = 'ready'
              AND source_path IN ({placeholders})
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            candidates,
        ).fetchone()
        if stem_set is None:
            return None
        rows = conn.execute(
            """
            SELECT stem_name, path
            FROM track_stems
            WHERE stem_set_id = ?
            """,
            (stem_set["id"],),
        ).fetchall()
    except sqlite3.Error:
        return None
    stems = {str(row["stem_name"]): str(row["path"]) for row in rows}
    if set(stems) != STEM_NAMES:
        return None
    artifact_root = Path(str(stem_set["artifact_root"]))
    manifest_path = artifact_root / "manifest.json"
    if not manifest_path.exists():
        return None
    return {"stem_set_id": str(stem_set["id"]), "manifest_path": str(manifest_path), "stems": stems}


def prepare_load_track_action_stems(
    action: dict[str, Any],
    *,
    db_path: Path = DEFAULT_LIBRARY_DB,
    prepare_stems: bool = True,
) -> dict[str, Any]:
    if action_type(action) != "load_track":
        return copy.deepcopy(action)
    prepared = copy.deepcopy(action)
    source_path = str(prepared.get("source_path") or prepared.get("path") or "").strip()
    load_id = action_id(prepared) or "<unnamed>"
    if not source_path:
        raise ValueError(f"load_track {load_id} source_path is required")
    if not action_requests_stems(prepared):
        # Plain load: the record plays whole; no split artifacts involved.
        return prepared
    if action_has_all_stem_paths(prepared):
        return prepared

    artifacts = ready_stem_artifacts(db_path, source_path)
    if artifacts is None and prepare_stems:
        command = [
            "python3",
            str(REPO_ROOT / "scripts" / "slime_audio_stems.py"),
            "--db",
            str(db_path),
            "split",
            source_path,
        ]
        result = subprocess.run(command, cwd=REPO_ROOT, text=True, capture_output=True, check=False)
        if result.returncode != 0:
            raise RuntimeError(
                "stem pre-generation failed for "
                f"{source_path}: {(result.stderr or result.stdout).strip()[-2000:]}"
            )
        artifacts = ready_stem_artifacts(db_path, source_path)
    if artifacts is None:
        raise ValueError(f"load_track {load_id} has no ready stems for {source_path}")

    existing_stems = prepared.get("stems") if isinstance(prepared.get("stems"), dict) else {}
    stems: dict[str, Any] = {}
    for stem_name in sorted(STEM_NAMES):
        existing = existing_stems.get(stem_name)
        if isinstance(existing, dict):
            stem_payload = dict(existing)
            stem_payload.setdefault("path", artifacts["stems"][stem_name])
        else:
            stem_payload = {"path": artifacts["stems"][stem_name]}
            if isinstance(existing, bool):
                stem_payload["enabled"] = existing
        stems[stem_name] = stem_payload
    prepared["stems"] = stems
    prepared.setdefault("stem_set_id", artifacts["stem_set_id"])
    prepared.setdefault("manifest_path", artifacts["manifest_path"])
    return prepared


def compile_actions_payload_legacy(payload: dict[str, Any]) -> dict[str, Any]:
    actions = payload.get("actions", payload.get("performance_actions", []))
    if not actions:
        return payload
    if not isinstance(actions, list):
        raise ValueError("actions must be a list")

    compiled = copy.deepcopy(payload)
    stem_groups = compiled.setdefault("stem_groups", [])
    deck_automations = compiled.setdefault("deck_automations", [])
    automations = compiled.setdefault("automations", [])
    decks = [str(deck) for deck in compiled.get("decks", [])] or list(DEFAULT_SESSION_DECKS)
    group_payloads: dict[str, dict[str, Any]] = {str(group.get("id")): group for group in stem_groups if group.get("id")}

    for index, action in enumerate(actions):
        if not isinstance(action, dict):
            raise ValueError(f"action {index} must be an object")
        kind = action_type(action)
        if not kind:
            raise ValueError(f"action {index} type is required")
        if kind == "load_track":
            group_id = action_id(action)
            deck = str(action.get("deck") or "").strip()
            source_path = str(action.get("source_path") or action.get("path") or "").strip()
            stems_payload = action.get("stems") or {}
            if not group_id:
                raise ValueError("load_track action id is required")
            if not deck:
                raise ValueError(f"load_track {group_id} deck is required")
            if not source_path:
                raise ValueError(f"load_track {group_id} source_path is required")
            if not isinstance(stems_payload, dict):
                raise ValueError(f"load_track {group_id} stems must be an object")
            missing_stems = sorted(STEM_NAMES - set(stems_payload))
            if missing_stems:
                raise ValueError(f"load_track {group_id} must include all stems: {', '.join(missing_stems)}")
            enabled_stems = action.get("play_stems", action.get("enabled_stems"))
            enabled_set = {str(stem) for stem in enabled_stems} if isinstance(enabled_stems, list) else None
            stems: dict[str, Any] = {}
            for stem_name in sorted(STEM_NAMES):
                stem_payload = stems_payload[stem_name]
                if isinstance(stem_payload, str):
                    stem_payload = {"path": stem_payload}
                elif isinstance(stem_payload, bool):
                    stem_payload = {"enabled": stem_payload}
                elif not isinstance(stem_payload, dict):
                    raise ValueError(f"load_track {group_id} stem {stem_name} must be an object, path, or boolean")
                stem_payload = dict(stem_payload)
                if enabled_set is not None:
                    stem_payload["enabled"] = stem_name in enabled_set
                stems[stem_name] = stem_payload
            group = {
                "id": group_id,
                "deck": deck,
                "source_path": source_path,
                "start_ms": action_at_ms(action),
                "trim_start_ms": parse_ms(action.get("trim_start_ms", action.get("trim_start", action.get("cue_ms", action.get("cue", 0)))), f"load_track {group_id} trim"),
                "duration_ms": parse_ms(action.get("duration_ms", action.get("duration")), f"load_track {group_id} duration") if action.get("duration_ms", action.get("duration")) is not None else None,
                "gain_db": float(action.get("gain_db", 0.0)),
                "tempo_shift_pct": float(action.get("tempo_shift_pct", 0.0)),
                "pitch_shift_semitones": int(action.get("pitch_shift_semitones", 0)),
                "fade_in_ms": parse_ms(action.get("fade_in_ms", 0), f"load_track {group_id} fade_in_ms"),
                "fade_out_ms": parse_ms(action.get("fade_out_ms", 0), f"load_track {group_id} fade_out_ms"),
                "stems": stems,
                "source_action_id": group_id,
            }
            if action.get("stem_set_id"):
                group["stem_set_id"] = str(action["stem_set_id"])
            if action.get("manifest_path"):
                group["manifest_path"] = str(action["manifest_path"])
            if action.get("source_bpm"):
                group["source_bpm"] = float(action["source_bpm"])
            if "warp" in action:
                group["warp"] = bool(action["warp"])
            stem_groups.append(group)
            group_payloads[group_id] = group
            if deck not in decks:
                decks.append(deck)
                compiled["decks"] = decks
        elif kind == "stem_toggle":
            group_id = str(action.get("target") or action.get("group_id") or action.get("load_id") or "").strip()
            stem_name = str(action.get("stem") or "").strip()
            if group_id not in group_payloads:
                raise ValueError(f"stem_toggle target does not exist: {group_id}")
            if stem_name not in STEM_NAMES:
                raise ValueError(f"stem_toggle {group_id} stem must be one of {sorted(STEM_NAMES)}")
            enabled = bool(action.get("enabled", True))
            stem_payload = group_payloads[group_id].setdefault("stems", {}).setdefault(stem_name, {})
            if isinstance(stem_payload, str):
                stem_payload = {"path": stem_payload}
                group_payloads[group_id]["stems"][stem_name] = stem_payload
            stem_payload.setdefault("automations", []).append(
                {
                    "target": f"stem-group:{group_id}:{stem_name}",
                    "param": "mute",
                    "points": [{"at_ms": action_at_ms(action), "value": not enabled}],
                }
            )
        elif kind == "knob_lerp":
            target = str(action.get("target") or "").strip()
            param = str(action.get("param") or "").strip()
            if not target:
                raise ValueError("knob_lerp target is required")
            if not param:
                raise ValueError("knob_lerp param is required")
            start_ms = action_at_ms(action)
            end_source = action.get("end_ms", action.get("end"))
            if end_source is None:
                duration_source = action.get("duration_ms", action.get("duration"))
                if duration_source is None:
                    raise ValueError(f"knob_lerp {target}.{param} requires end or duration")
                end_ms = start_ms + parse_ms(duration_source, f"knob_lerp {target}.{param} duration")
            else:
                end_ms = parse_ms(end_source, f"knob_lerp {target}.{param} end")
            if end_ms <= start_ms:
                raise ValueError(f"knob_lerp {target}.{param} end must be after start")
            automation = {
                "target": target,
                "param": param,
                "points": [
                    {"at_ms": start_ms, "value": action["from"], "curve": str(action.get("curve") or "linear")},
                    {"at_ms": end_ms, "value": action["to"], "curve": str(action.get("curve") or "linear")},
                ],
            }
            if target in decks:
                deck_automations.append(automation)
            elif target.startswith("stem-group:"):
                parts = target.split(":")
                if len(parts) != 3 or parts[1] not in group_payloads or parts[2] not in STEM_NAMES:
                    raise ValueError(f"knob_lerp target does not exist: {target}")
                stem_payload = group_payloads[parts[1]].setdefault("stems", {}).setdefault(parts[2], {})
                if isinstance(stem_payload, str):
                    stem_payload = {"path": stem_payload}
                    group_payloads[parts[1]]["stems"][parts[2]] = stem_payload
                stem_payload.setdefault("automations", []).append(automation)
            else:
                automations.append(automation)
        else:
            raise ValueError(f"unsupported action type: {kind}")
    return compiled


_KEY_METADATA_CACHE: dict[str, tuple[int, str, str] | None] = {}


def hydrate_load_key_metadata(actions: Any, db_path: Path | None = None) -> None:
    """Fill missing tonic/mode/key on load actions from the library analysis."""
    if not isinstance(actions, list):
        return
    db = db_path or DEFAULT_LIBRARY_DB
    for action in actions:
        if not isinstance(action, dict) or action_type(action) != "load_track":
            continue
        if action.get("tonic") is not None and action.get("mode") in ("major", "minor"):
            continue
        source = str(action.get("source_path") or action.get("path") or "").strip()
        if not source:
            continue
        if source not in _KEY_METADATA_CACHE:
            row = None
            try:
                conn = sqlite3.connect(db)
                row = conn.execute(
                    "SELECT tonic, mode, key FROM track_dj_analysis WHERE path = ? AND tonic IS NOT NULL",
                    (source,),
                ).fetchone()
                conn.close()
            except sqlite3.Error:
                row = None
            _KEY_METADATA_CACHE[source] = (
                (int(row[0]), str(row[1]), str(row[2] or "")) if row and row[1] in ("major", "minor") else None
            )
        cached = _KEY_METADATA_CACHE[source]
        if cached is not None:
            action.setdefault("tonic", cached[0])
            action.setdefault("mode", cached[1])
            if cached[2]:
                action.setdefault("key", cached[2])


def compile_actions_payload(payload: dict[str, Any]) -> dict[str, Any]:
    actions = payload.get("actions", payload.get("performance_actions", []))
    if not actions:
        return payload
    if not isinstance(actions, list):
        raise ValueError("actions must be a list")

    compiled = copy.deepcopy(payload)
    # Keymatching only works on events that KNOW their key: a load authored
    # without tonic/mode silently skipped apply_master_key and aired native
    # against the session center. The library usually knows — hydrate here so
    # every compiled segment carries the key.
    hydrate_load_key_metadata(compiled.get("actions", compiled.get("performance_actions", [])))
    compiled["stem_groups"] = list(compiled.get("stem_groups", compiled.get("stemGroups", [])))
    stem_groups = compiled["stem_groups"]
    clips = compiled.setdefault("clips", [])
    stem_customized = stem_customized_load_ids([action for action in actions if isinstance(action, dict)])
    deck_automations = compiled.setdefault("deck_automations", [])
    automations = compiled.setdefault("automations", [])
    decks = [str(deck) for deck in compiled.get("decks", [])] or list(DEFAULT_SESSION_DECKS)
    group_payloads: dict[str, dict[str, Any]] = {str(group.get("id")): group for group in stem_groups if group.get("id")}
    loaded_actions: dict[str, dict[str, Any]] = {}
    future_load_ids = {
        action_id(action)
        for action in actions
        if isinstance(action, dict) and action_type(action) == "load_track" and action_id(action)
    }
    # Toggles that drift ahead of their own load (replans move loads later)
    # absorb into the load's initial stem state instead of crashing compile.
    pre_toggles: dict[str, dict[str, bool]] = {}
    deck_active: dict[str, dict[str, Any]] = {}
    deck_paused: dict[str, dict[str, Any]] = {}
    cues: dict[str, dict[str, int]] = {}
    segment_counts: dict[str, int] = {}
    pending_stem_automations: dict[str, dict[str, list[dict[str, Any]]]] = {}

    def add_deck(deck: str) -> None:
        if deck and deck not in decks:
            decks.append(deck)
            compiled["decks"] = decks

    def next_segment_id(load_id: str) -> str:
        count = segment_counts.get(load_id, 0)
        segment_counts[load_id] = count + 1
        return load_id if count == 0 else f"{load_id}-segment-{count + 1:02d}"

    def effective_render_factor(action: dict[str, Any]) -> float:
        """The rate the renderer actually consumes source at.

        Continuity math must match the render: under a master tempo the warp
        overrides the authored shift, and using the authored factor made every
        toggle boundary jump by elapsed x warp%% (measured +315ms live)."""
        factor = clip_tempo_factor(action)
        if action.get("warp", True) and action.get("source_bpm"):
            master = master_bpm_at(compiled, int(action_at_ms(action)))
            if master is not None:
                max_stretch = abs(float(compiled.get("max_tempo_stretch_pct", DEFAULT_MAX_WARP_STRETCH_PCT)))
                shift = master_tempo_shift_pct(float(action["source_bpm"]), master, max_stretch)
                return 1.0 + (shift or 0.0) / 100.0
        return factor

    def active_natural_end_ms(active: dict[str, Any]) -> int | None:
        raw_duration = active["action"].get("duration_ms", active["action"].get("duration"))
        if raw_duration is None:
            return None
        total_duration_ms = parse_ms(raw_duration, f"load_track {active['load_id']} duration")
        original_trim_ms = parse_ms(
            active["action"].get("trim_start_ms", active["action"].get("trim_start", active["action"].get("cue_ms", active["action"].get("cue", 0)))),
            f"load_track {active['load_id']} trim",
        )
        factor = effective_render_factor(active["action"])
        source_elapsed_ms = int(active["trim_start_ms"]) - original_trim_ms
        # duration_ms is the load's TIMELINE span; consumed source maps back
        # to timeline through the render factor.
        timeline_consumed_ms = source_elapsed_ms / factor if factor > 0 else source_elapsed_ms
        remaining_ms = max(0, int(round(total_duration_ms - timeline_consumed_ms)))
        return int(active["start_ms"]) + remaining_ms

    def close_active(deck: str, end_ms: int) -> None:
        active = deck_active.get(deck)
        if not active:
            return
        start_ms = int(active["start_ms"])
        natural_end = active_natural_end_ms(active)
        if natural_end is not None:
            end_ms = min(end_ms, natural_end)
        if end_ms - start_ms <= 0:
            # Nothing aired (e.g. a toggle at the load's own start): drop the
            # empty span without consuming a segment id.
            deck_active.pop(deck, None)
            return
        first_segment = segment_counts.get(str(active["load_id"]), 0) == 0
        reaches_natural_end = natural_end is not None and end_ms >= natural_end
        append_deck_clock_segment(
            stem_groups,
            group_payloads,
            active["action"],
            segment_id=next_segment_id(active["load_id"]),
            start_ms=start_ms,
            trim_start_ms=int(active["trim_start_ms"]),
            duration_ms=end_ms - start_ms,
            pending_stem_automations=pending_stem_automations,
            clips=clips,
            stem_customized=stem_customized,
            stems_enabled=active.get("stems_enabled"),
            keep_fade_in=first_segment,
            keep_fade_out=reaches_natural_end,
        )
        deck_active.pop(deck, None)

    def active_source_position(active: dict[str, Any], at_ms: int) -> int:
        elapsed_ms = max(0, at_ms - int(active["start_ms"]))
        return int(active["trim_start_ms"]) + int(round(elapsed_ms * effective_render_factor(active["action"])))

    def resolve_loaded_target(action: dict[str, Any], label: str) -> tuple[str, str]:
        target = action_load_target(action)
        if not target:
            raise ValueError(f"{label} target is required")
        if target in loaded_actions:
            return target, str(loaded_actions[target].get("deck") or "")
        if target in decks:
            active = deck_active.get(target) or deck_paused.get(target)
            if active is None:
                raise ValueError(f"{label} deck has no loaded track: {target}")
            return str(active["load_id"]), target
        raise ValueError(f"{label} target does not exist: {target}")

    def cue_position(action: dict[str, Any], load_id: str, label: str) -> int:
        cue_name = str(action.get("cue_id") or action.get("cue_name") or action.get("name") or "").strip()
        has_position = any(
            key in action
            for key in ("position_ms", "position", "source_ms", "source_position_ms", "cue_ms", "cue", "trim_start_ms", "trim_start")
        )
        if has_position:
            return action_position_ms(action, f"{label} {load_id} position")
        if cue_name:
            if cue_name not in cues.get(load_id, {}):
                raise ValueError(f"{label} {load_id} missing cue: {cue_name}")
            return cues[load_id][cue_name]
        raise ValueError(f"{label} {load_id} requires position or cue")

    for index, action in sorted(
        enumerate(actions),
        key=lambda item: (
            action_at_ms(item[1]) if isinstance(item[1], dict) else 0,
            # Loads compile before same-instant actions that reference them
            # (a stem_toggle at the load's own start must find its target).
            0 if isinstance(item[1], dict) and action_type(item[1]) == "load_track" else 1,
            item[0],
        ),
    ):
        if not isinstance(action, dict):
            raise ValueError(f"action {index} must be an object")
        kind = action_type(action)
        if not kind:
            raise ValueError(f"action {index} type is required")
        at_ms = action_at_ms(action)

        if kind == "load_track":
            wants_stem_render = not action_stems_untouched(action) or action_id(action) in stem_customized
            if wants_stem_render:
                action = hydrate_action_stems(action)
                group, deck = load_track_group_from_action(action)
                load_id = str(group["id"])
                trim_ms = int(group["trim_start_ms"])
                group_payloads[load_id] = group
            else:
                # Plain load: the record itself goes on the deck clock; its
                # segments compile to clips (see plain_clip_from_load_action).
                load_id = action_id(action)
                deck = str(action.get("deck") or "").strip()
                if not load_id:
                    raise ValueError("load_track action id is required")
                if not deck:
                    raise ValueError(f"load_track {load_id} deck is required")
                if not str(action.get("source_path") or action.get("path") or "").strip():
                    raise ValueError(f"load_track {load_id} source_path is required")
                trim_ms = parse_ms(
                    action.get("trim_start_ms", action.get("trim_start", action.get("cue_ms", action.get("cue", 0)))),
                    f"load_track {load_id} trim",
                )
            close_active(deck, at_ms)
            loaded_actions[load_id] = action
            add_deck(deck)
            initial_stems: set[str] | None = None
            if load_id in pre_toggles:
                requested = action.get("play_stems", action.get("enabled_stems"))
                initial_stems = {str(s) for s in requested} if isinstance(requested, list) else set(STEM_NAMES)
                for stem_name, stem_on in pre_toggles.pop(load_id).items():
                    (initial_stems.add if stem_on else initial_stems.discard)(stem_name)
            deck_active[deck] = {
                "action": action,
                "load_id": load_id,
                "start_ms": at_ms,
                "trim_start_ms": trim_ms,
                "stems_enabled": initial_stems,
            }
            deck_paused.pop(deck, None)
        elif kind == "set_cue":
            target = action_load_target(action)
            cue_name = action_cue_name(action)
            if not target:
                raise ValueError("set_cue target is required")
            if not cue_name:
                raise ValueError("set_cue name is required")
            position_ms = action_position_ms(action, f"set_cue {cue_name} position")
            cues.setdefault(target, {})[cue_name] = position_ms
        elif kind == "jump_to_cue":
            target = action_load_target(action)
            cue_name = action_cue_name(action)
            if target not in loaded_actions:
                raise ValueError(f"jump_to_cue target does not exist: {target}")
            if not cue_name:
                raise ValueError("jump_to_cue cue name is required")
            if cue_name not in cues.get(target, {}):
                raise ValueError(f"jump_to_cue {target} missing cue: {cue_name}")
            deck = str(loaded_actions[target].get("deck") or "")
            carried_stems = (deck_active.get(deck) or {}).get("stems_enabled")
            close_active(deck, at_ms)
            deck_active[deck] = {
                "action": loaded_actions[target],
                "load_id": target,
                "start_ms": at_ms,
                "trim_start_ms": cues[target][cue_name],
                "stems_enabled": carried_stems,
            }
            deck_paused.pop(deck, None)
        elif kind == "loop_start":
            target = action_load_target(action)
            if target not in loaded_actions:
                raise ValueError(f"loop_start target does not exist: {target}")
            deck = str(loaded_actions[target].get("deck") or "")
            loop_stems_enabled = (deck_active.get(deck) or {}).get("stems_enabled")
            close_active(deck, at_ms)
            source_start_ms = action_position_ms(action, f"loop_start {target} position")
            length_source = action.get("length_ms", action.get("length", action.get("duration_ms", action.get("duration"))))
            if length_source is None:
                raise ValueError(f"loop_start {target} length is required")
            source_length_ms = parse_ms(length_source, f"loop_start {target} length")
            if source_length_ms <= 0:
                raise ValueError(f"loop_start {target} length must be positive")
            exit_source = action.get("exit_ms", action.get("exit_at", action.get("until_ms", action.get("until"))))
            if exit_source is None:
                raise ValueError(f"loop_start {target} exit is required")
            exit_ms = parse_ms(exit_source, f"loop_start {target} exit")
            if exit_ms <= at_ms:
                raise ValueError(f"loop_start {target} exit must be after start")
            timeline_loop_ms = max(1, int(round(source_length_ms / clip_tempo_factor(loaded_actions[target]))))
            cursor = at_ms
            loop_index = 1
            while cursor < exit_ms:
                duration_ms = min(timeline_loop_ms, exit_ms - cursor)
                append_deck_clock_segment(
                    stem_groups,
                    group_payloads,
                    loaded_actions[target],
                    segment_id=f"{target}-loop-{loop_index:02d}",
                    start_ms=cursor,
                    trim_start_ms=source_start_ms,
                    duration_ms=duration_ms,
                    pending_stem_automations=pending_stem_automations,
                    clips=clips,
                    stem_customized=stem_customized,
                    stems_enabled=loop_stems_enabled,
                    keep_fade_in=False,
                    keep_fade_out=False,
                )
                cursor += duration_ms
                loop_index += 1
            deck_active[deck] = {
                "action": loaded_actions[target],
                "load_id": target,
                "start_ms": exit_ms,
                "trim_start_ms": source_start_ms + source_length_ms,
                "stems_enabled": loop_stems_enabled,
            }
            deck_paused.pop(deck, None)
        elif kind == "loop_exit":
            target = action_load_target(action)
            if target not in loaded_actions:
                raise ValueError(f"loop_exit target does not exist: {target}")
            deck = str(loaded_actions[target].get("deck") or "")
            carried_stems = (deck_active.get(deck) or {}).get("stems_enabled")
            close_active(deck, at_ms)
            resume_ms = action_position_ms(action, f"loop_exit {target} position")
            deck_active[deck] = {
                "action": loaded_actions[target],
                "load_id": target,
                "start_ms": at_ms,
                "trim_start_ms": resume_ms,
                "stems_enabled": carried_stems,
            }
            deck_paused.pop(deck, None)
        elif kind in {"pause", "deck_pause"}:
            load_id, deck = resolve_loaded_target(action, kind)
            active = deck_active.get(deck)
            if active is None:
                if deck in deck_paused:
                    continue
                raise ValueError(f"{kind} deck is not playing: {deck}")
            resume_ms = active_source_position(active, at_ms)
            paused_stems = active.get("stems_enabled")
            close_active(deck, at_ms)
            deck_paused[deck] = {
                "action": loaded_actions[load_id],
                "load_id": load_id,
                "trim_start_ms": resume_ms,
                "stems_enabled": paused_stems,
            }
        elif kind in {"play", "deck_play"}:
            load_id, deck = resolve_loaded_target(action, kind)
            default_trim_ms = parse_ms(
                loaded_actions[load_id].get("trim_start_ms", loaded_actions[load_id].get("trim_start", loaded_actions[load_id].get("cue_ms", loaded_actions[load_id].get("cue", 0)))),
                f"load_track {load_id} trim",
            )
            position_ms = cue_position(action, load_id, kind) if (
                action.get("cue_id") or action.get("cue_name") or action.get("name")
                or any(key in action for key in ("position_ms", "position", "source_ms", "source_position_ms", "cue_ms", "cue", "trim_start_ms", "trim_start"))
            ) else int((deck_paused.get(deck) or {}).get("trim_start_ms", default_trim_ms))
            carried_stems = (deck_active.get(deck) or deck_paused.get(deck) or {}).get("stems_enabled")
            close_active(deck, at_ms)
            deck_active[deck] = {
                "action": loaded_actions[load_id],
                "load_id": load_id,
                "start_ms": at_ms,
                "trim_start_ms": position_ms,
                "stems_enabled": carried_stems,
            }
            deck_paused.pop(deck, None)
        elif kind in {"cue", "cue_seek"}:
            load_id, deck = resolve_loaded_target(action, kind)
            position_ms = cue_position(action, load_id, kind)
            carried_stems = (deck_active.get(deck) or deck_paused.get(deck) or {}).get("stems_enabled")
            close_active(deck, at_ms)
            deck_paused[deck] = {
                "action": loaded_actions[load_id],
                "load_id": load_id,
                "trim_start_ms": position_ms,
                "stems_enabled": carried_stems,
            }
        elif kind in {"seek", "deck_seek"}:
            load_id, deck = resolve_loaded_target(action, kind)
            position_ms = cue_position(action, load_id, kind)
            carried_stems = (deck_active.get(deck) or deck_paused.get(deck) or {}).get("stems_enabled")
            close_active(deck, at_ms)
            should_play = bool(action.get("play", True))
            if should_play:
                deck_active[deck] = {
                    "action": loaded_actions[load_id],
                    "load_id": load_id,
                    "start_ms": at_ms,
                    "trim_start_ms": position_ms,
                    "stems_enabled": carried_stems,
                }
                deck_paused.pop(deck, None)
            else:
                deck_paused[deck] = {
                    "action": loaded_actions[load_id],
                    "load_id": load_id,
                    "trim_start_ms": position_ms,
                }
        elif kind == "stem_toggle":
            group_id = str(action.get("target") or action.get("group_id") or action.get("load_id") or "").strip()
            stem_name = str(action.get("stem") or "").strip()
            if group_id not in loaded_actions and group_id not in group_payloads:
                if group_id in future_load_ids:
                    if str(action.get("stem") or "") in STEM_NAMES:
                        pre_toggles.setdefault(group_id, {})[str(action["stem"])] = bool(action.get("enabled", True))
                    continue
                raise ValueError(f"stem_toggle target does not exist: {group_id}")
            if stem_name not in STEM_NAMES:
                raise ValueError(f"stem_toggle {group_id} stem must be one of {sorted(STEM_NAMES)}")
            enabled = bool(action.get("enabled", True))
            toggle_deck = str(loaded_actions[group_id].get("deck") or "") if group_id in loaded_actions else ""
            active = deck_active.get(toggle_deck)
            if active is not None and str(active["load_id"]) == group_id:
                # A toggle segments the load at this instant: the span behind
                # keeps its state (all-four-on spans render the original file),
                # the span ahead continues with the stem flipped. This is the
                # always-on-stems model made literal.
                enabled_before = active.get("stems_enabled")
                if enabled_before is None:
                    requested = active["action"].get("play_stems", active["action"].get("enabled_stems"))
                    if isinstance(requested, list):
                        enabled_before = {str(stem) for stem in requested}
                    else:
                        stems_payload = active["action"].get("stems")
                        if isinstance(stems_payload, dict) and stems_payload:
                            enabled_before = {
                                str(name)
                                for name, stem in stems_payload.items()
                                if (stem.get("enabled", True) if isinstance(stem, dict) else bool(stem) if isinstance(stem, bool) else True)
                            }
                        else:
                            enabled_before = set(STEM_NAMES)
                source_pos = active_source_position(active, at_ms)
                close_active(toggle_deck, at_ms)
                next_state = set(enabled_before)
                (next_state.add if enabled else next_state.discard)(stem_name)
                deck_active[toggle_deck] = {
                    "action": active["action"],
                    "load_id": group_id,
                    "start_ms": at_ms,
                    "trim_start_ms": source_pos,
                    "stems_enabled": next_state,
                }
            else:
                # Not the live load on any deck (e.g. a directly-authored bed
                # group): fall back to a timed mute on the materialized group.
                automation = {
                    "target": f"stem-group:{group_id}:{stem_name}",
                    "param": "mute",
                    "points": [{"at_ms": at_ms, "value": not enabled}],
                }
                pending_stem_automations.setdefault(group_id, {}).setdefault(stem_name, []).append(automation)
                target_groups = [group for group in stem_groups if group.get("source_action_id") == group_id or group.get("id") == group_id]
                for group in target_groups:
                    stem_payload = group.setdefault("stems", {}).setdefault(stem_name, {})
                    if isinstance(stem_payload, str):
                        stem_payload = {"path": stem_payload}
                        group["stems"][stem_name] = stem_payload
                    stem_payload.setdefault("automations", []).append(copy.deepcopy(automation))
        elif kind == "knob_lerp":
            target = str(action.get("target") or "").strip()
            param = str(action.get("param") or "").strip()
            if not target:
                raise ValueError("knob_lerp target is required")
            if not param:
                raise ValueError("knob_lerp param is required")
            end_source = action.get("end_ms", action.get("end"))
            if end_source is None:
                duration_source = action.get("duration_ms", action.get("duration"))
                if duration_source is None:
                    raise ValueError(f"knob_lerp {target}.{param} requires end or duration")
                end_ms = at_ms + parse_ms(duration_source, f"knob_lerp {target}.{param} duration")
            else:
                end_ms = parse_ms(end_source, f"knob_lerp {target}.{param} end")
            if end_ms <= at_ms:
                raise ValueError(f"knob_lerp {target}.{param} end must be after start")
            automation = {
                "target": target,
                "param": param,
                "points": [
                    {"at_ms": at_ms, "value": action["from"], "curve": str(action.get("curve") or "linear")},
                    {"at_ms": end_ms, "value": action["to"], "curve": str(action.get("curve") or "linear")},
                ],
            }
            if target in decks:
                deck_automations.append(automation)
            elif target.startswith("stem-group:"):
                parts = target.split(":")
                if len(parts) != 3 or parts[1] not in group_payloads or parts[2] not in STEM_NAMES:
                    raise ValueError(f"knob_lerp target does not exist: {target}")
                stem_payload = group_payloads[parts[1]].setdefault("stems", {}).setdefault(parts[2], {})
                if isinstance(stem_payload, str):
                    stem_payload = {"path": stem_payload}
                    group_payloads[parts[1]]["stems"][parts[2]] = stem_payload
                stem_payload.setdefault("automations", []).append(automation)
            else:
                automations.append(automation)
        else:
            raise ValueError(f"unsupported action type: {kind}")

    for deck, active in list(deck_active.items()):
        raw_duration = active["action"].get("duration_ms", active["action"].get("duration"))
        if raw_duration is None:
            continue
        total_duration_ms = parse_ms(raw_duration, f"load_track {active['load_id']} duration")
        flush_factor = effective_render_factor(active["action"])
        source_elapsed_ms = int(active["trim_start_ms"]) - parse_ms(
            active["action"].get("trim_start_ms", active["action"].get("trim_start", active["action"].get("cue_ms", active["action"].get("cue", 0)))),
            f"load_track {active['load_id']} trim",
        )
        remaining_ms = int(round(total_duration_ms - (source_elapsed_ms / flush_factor if flush_factor > 0 else source_elapsed_ms)))
        append_deck_clock_segment(
            stem_groups,
            group_payloads,
            active["action"],
            segment_id=next_segment_id(active["load_id"]),
            start_ms=int(active["start_ms"]),
            trim_start_ms=int(active["trim_start_ms"]),
            duration_ms=remaining_ms,
            pending_stem_automations=pending_stem_automations,
            clips=clips,
            stem_customized=stem_customized,
            stems_enabled=active.get("stems_enabled"),
            keep_fade_in=segment_counts.get(str(active["load_id"]), 0) == 0,
            keep_fade_out=True,
        )
    compiled["decks"] = decks
    return compiled


def playhead_ms_from_state(path: Path, now: float | None = None) -> int:
    payload = json.loads(path.read_text(encoding="utf-8"))
    explicit = payload.get("playhead_ms", payload.get("mix_playhead_ms"))
    if explicit is not None:
        return max(0, parse_ms(explicit, "state playhead"))
    window_started_at = parse_timestamp(payload.get("window_started_at"))
    if window_started_at is not None:
        window_start_ms = parse_ms(payload.get("window_start_ms", 0), "state window start")
        playhead = window_start_ms + int(round(((now if now is not None else time()) - window_started_at) * 1000))
        window_end = payload.get("window_end_ms")
        if window_end is not None:
            # A window anchor vouches only for its own span. If the runner
            # dies mid-window the wall clock keeps running; extrapolating past
            # the window end reports audio that never played (a crashed runner
            # once "completed" a set this way while the room sat silent).
            playhead = min(playhead, parse_ms(window_end, "state window end"))
        return max(0, playhead)
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


DEFAULT_RUNNER_WINDOW_MS = 180_000


def edit_lock_ms_from_state(path: Path, now: float | None = None) -> int:
    """Earliest timeline instant that is safe to edit.

    The playhead alone is not the lock: the runner prerenders the entire next
    window once the current one enters its prerender lead, so audio up to
    ``edit_lock_ms`` (published by the runner) may already be baked. Edits
    before that horizon silently miss the render and desync the timeline.
    """
    playhead = playhead_ms_from_state(path, now)
    payload = json.loads(path.read_text(encoding="utf-8"))
    horizon = payload.get("edit_lock_ms")
    if horizon is not None:
        return max(playhead, parse_ms(horizon, "state edit lock"))
    window_end = payload.get("window_end_ms")
    if window_end is not None:
        return max(playhead, parse_ms(window_end, "state window end") + DEFAULT_RUNNER_WINDOW_MS)
    return playhead


def probe_duration_ms(path: str) -> int:
    try:
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
    except FileNotFoundError:
        with wave.open(path, "rb") as audio:
            frame_rate = audio.getframerate()
            duration_seconds = audio.getnframes() / frame_rate if frame_rate else 0
    if duration_seconds <= 0:
        raise ValueError(f"could not determine positive duration for {path}")
    return int(round(duration_seconds * 1000))


def audit_session_durations(session: MixSession, *, threshold_ms: int = 5_000, from_ms: int | None = None) -> dict[str, Any]:
    mismatches: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    checked = 0
    for clip in sorted(session.clips, key=lambda item: (item.start_ms, item.id)):
        if from_ms is not None and clip.end_ms is not None and clip.end_ms < from_ms:
            continue
        if clip.duration_ms is None:
            failures.append({"id": clip.id, "path": clip.path, "error": "missing scheduled duration"})
            continue
        if not clip.path:
            failures.append({"id": clip.id, "path": clip.path, "error": "missing path"})
            continue
        try:
            actual_ms = probe_duration_ms(clip.path)
        except (OSError, subprocess.CalledProcessError, ValueError) as exc:
            failures.append({"id": clip.id, "path": clip.path, "error": str(exc)})
            continue
        checked += 1
        remaining_ms = max(1, actual_ms - clip.trim_start_ms)
        diff_ms = clip.duration_ms - remaining_ms
        if abs(diff_ms) > threshold_ms:
            mismatches.append(
                {
                    "id": clip.id,
                    "path": clip.path,
                    "start_ms": clip.start_ms,
                    "scheduled_duration_ms": clip.duration_ms,
                    "actual_remaining_ms": remaining_ms,
                    "difference_ms": diff_ms,
                    "kind": "scheduled_too_long" if diff_ms > 0 else "scheduled_too_short",
                }
            )
    return {
        "checked": checked,
        "from_ms": from_ms,
        "threshold_ms": threshold_ms,
        "mismatch_count": len(mismatches),
        "failure_count": len(failures),
        "mismatches": mismatches,
        "failures": failures,
    }


def _automation_times_values(automation: Automation) -> tuple[list[int], list[float]]:
    times = [point.at_ms for point in automation.points]
    values: list[float] = []
    for point in automation.points:
        try:
            values.append(float(point.value))
        except (TypeError, ValueError):
            continue
    return times, values


def audit_hidden_volume_sag(
    session: MixSession,
    *,
    from_ms: int | None = None,
    max_fade_out_ms: int = 2_000,
    min_gain_db: float = -6.0,
    min_duck_volume: float = 0.98,
) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    for clip in sorted(session.clips, key=lambda item: (item.start_ms, item.id)):
        if from_ms is not None and clip.end_ms is not None and clip.end_ms < from_ms:
            continue
        if clip.fade_out_ms > max_fade_out_ms:
            findings.append(
                {
                    "kind": "long_clip_fade_out",
                    "id": clip.id,
                    "path": clip.path,
                    "start_ms": clip.start_ms,
                    "fade_out_ms": clip.fade_out_ms,
                    "threshold_ms": max_fade_out_ms,
                }
            )
        for automation in clip.automations:
            times, values = _automation_times_values(automation)
            if from_ms is not None and times and max(times) < from_ms:
                continue
            if automation.param == "gain_db" and values and min(values) < min_gain_db:
                findings.append(
                    {
                        "kind": "clip_gain_dip",
                        "id": clip.id,
                        "param": automation.param,
                        "min_value": min(values),
                        "threshold_db": min_gain_db,
                    }
                )
    for automation in [*session.automations, *session.deck_automations]:
        times, values = _automation_times_values(automation)
        if from_ms is not None and times and max(times) < from_ms:
            continue
        if automation.param == "duck_volume" and values and min(values) < min_duck_volume:
            findings.append(
                {
                    "kind": "master_or_session_duck",
                    "target": automation.target,
                    "param": automation.param,
                    "min_value": min(values),
                    "threshold": min_duck_volume,
                }
            )
        if automation.param == "gain_db" and values and min(values) < min_gain_db:
            findings.append(
                {
                    "kind": "gain_dip",
                    "target": automation.target,
                    "param": automation.param,
                    "min_value": min(values),
                    "threshold_db": min_gain_db,
                }
            )
    return {
        "from_ms": from_ms,
        "finding_count": len(findings),
        "findings": findings,
        "thresholds": {
            "max_fade_out_ms": max_fade_out_ms,
            "min_gain_db": min_gain_db,
            "min_duck_volume": min_duck_volume,
        },
    }


def parse_session(payload: dict[str, Any]) -> MixSession:
    payload = apply_master_key(apply_master_tempo(compile_actions_payload(payload)))
    decks = [str(deck) for deck in payload.get("decks", [])]
    if not decks:
        decks = list(DEFAULT_SESSION_DECKS)
    for lean_in_payload in payload.get("mic_lean_ins", payload.get("micLeanIns", [])):
        lean_deck = str(lean_in_payload.get("deck") or VOCAL_DECK)
        if lean_deck not in decks:
            decks.append(lean_deck)
    session = MixSession(
        version=int(payload.get("version", 1)),
        decks=decks,
        clips=[parse_clip(item) for item in payload.get("clips", [])],
        stem_groups=[parse_stem_group(item) for item in payload.get("stem_groups", payload.get("stemGroups", []))],
        mic_lean_ins=[parse_mic_lean_in(item) for item in payload.get("mic_lean_ins", payload.get("micLeanIns", []))],
        effects=[parse_effect_event(item) for item in payload.get("effects", [])],
        automations=[parse_automation(item) for item in payload.get("automations", [])],
        deck_automations=[parse_automation(item) for item in payload.get("deck_automations", payload.get("deckAutomations", []))],
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
    for event_id in [clip.id for clip in session.clips] + [group.id for group in session.stem_groups] + [lean_in.id for lean_in in session.mic_lean_ins] + [effect.id for effect in session.effects] + [event.id for event in session.slip_events]:
        if event_id in seen_ids:
            errors.append(f"duplicate event id: {event_id}")
        seen_ids.add(event_id)

    for clip in session.clips:
        if clip.deck not in deck_set:
            errors.append(f"clip {clip.id} uses unknown deck {clip.deck}")
        if is_artifact_source_path(clip.path):
            errors.append(f"clip {clip.id} uses artifact/duplicate source path: {clip.path}")
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
            validate_automation(automation, session.event_ids, deck_set, errors, prefix=f"clip {clip.id}", allow_deck_targets=False)

    for group in session.stem_groups:
        if group.deck not in deck_set:
            errors.append(f"stem group {group.id} uses unknown deck {group.deck}")
        if is_artifact_source_path(group.source_path):
            errors.append(f"stem group {group.id} uses artifact/duplicate source path: {group.source_path}")
        if group.start_ms < 0:
            errors.append(f"stem group {group.id} starts before zero")
        if group.trim_start_ms < 0:
            errors.append(f"stem group {group.id} trim starts before zero")
        if group.duration_ms is not None and group.duration_ms <= 0:
            errors.append(f"stem group {group.id} duration must be positive")
        if group.playback_rate <= 0:
            errors.append(f"stem group {group.id} playback_rate must be positive")
        if not group.stems:
            errors.append(f"stem group {group.id} must include at least one stem")
        for stem_name, stem in group.stems.items():
            if stem_name not in STEM_NAMES:
                errors.append(f"stem group {group.id} has unsupported stem {stem_name}")
            if not stem.enabled:
                continue
            if not stem.path and not group.manifest_path and not group.stem_set_id:
                errors.append(f"stem group {group.id} stem {stem_name} has no path, manifest_path, or stem_set_id")
            for automation in stem.automations:
                validate_automation(automation, session.event_ids, deck_set, errors, prefix=f"stem group {group.id}", allow_deck_targets=False)
        for automation in group.automations:
            validate_automation(automation, session.event_ids, deck_set, errors, prefix=f"stem group {group.id}", allow_deck_targets=False)

    for deck in session.decks:
        deck_events = sorted(
            [clip for clip in session.clips if clip.deck == deck and clip.end_ms is not None and clip.kind != "effect-track"]
            + [group for group in session.stem_groups if group.deck == deck and group.end_ms is not None],
            key=lambda event: event.start_ms,
        )
        for left, right in zip(deck_events, deck_events[1:]):
            if left.end_ms is not None and left.end_ms > right.start_ms:
                errors.append(f"clips {left.id} and {right.id} overlap on {deck}")

    for lean_in in session.mic_lean_ins:
        if lean_in.deck not in deck_set:
            errors.append(f"mic lean-in {lean_in.id} uses unknown deck {lean_in.deck}")
        if lean_in.start_ms < 0:
            errors.append(f"mic lean-in {lean_in.id} starts before zero")
        for effect in lean_in.effects:
            validate_automation(effect, session.event_ids, deck_set, errors, prefix=f"mic lean-in {lean_in.id}", allow_deck_targets=False)

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
        validate_automation(automation, session.event_ids, deck_set, errors, prefix="session", allow_deck_targets=False)

    for automation in session.deck_automations:
        validate_automation(automation, session.event_ids, deck_set, errors, prefix="deck", allow_deck_targets=True)
        if automation.target not in deck_set:
            errors.append(f"deck automation target must be a deck: {automation.target}")
        if automation.param == "position":
            errors.append(f"deck automation {automation.target}.position belongs on crossfader automation")

    if errors:
        raise ValueError("\n".join(errors))


def validate_automation(
    automation: Automation,
    event_ids: set[str],
    deck_ids: set[str],
    errors: list[str],
    prefix: str,
    *,
    allow_deck_targets: bool,
) -> None:
    if automation.target.startswith("stem-group:"):
        parts = automation.target.split(":")
        if len(parts) != 3 or parts[1] not in event_ids or parts[2] not in STEM_NAMES:
            errors.append(f"{prefix} automation target does not exist: {automation.target}")
        if automation.param not in STEM_AUTOMATABLE_PARAMS:
            errors.append(f"{prefix} automation {automation.target}.{automation.param} is not an automatable stem param")
        return
    if automation.param not in AUTOMATABLE_PARAMS:
        errors.append(f"{prefix} automation {automation.target}.{automation.param} is not an automatable param")
    target_ok = (
        automation.target in event_ids
        or automation.target in {"master", "all", "crossfader"}
        or automation.target.startswith("deck:")
        or (allow_deck_targets and automation.target in deck_ids)
    )
    if not target_ok:
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
    stem_groups_by_deck = {
        deck: [
            {
                "id": group.id,
                "source_path": group.source_path,
                "stem_set_id": group.stem_set_id,
                "start_ms": group.start_ms,
                "trim_start_ms": group.trim_start_ms,
                "duration_ms": group.duration_ms,
                "end_ms": group.end_ms,
                "stems": {
                    name: {"enabled": stem.enabled, "mute": stem.mute, "solo": stem.solo, "path": stem.path}
                    for name, stem in group.stems.items()
                },
            }
            for group in sorted((item for item in session.stem_groups if item.deck == deck), key=lambda item: item.start_ms)
        ]
        for deck in session.decks
    }
    return {
        "version": session.version,
        "decks": session.decks,
        "fader_routing": session.fader_routing,
        "clip_count": len(session.clips),
        "stem_group_count": len(session.stem_groups),
        "mic_lean_in_count": len(session.mic_lean_ins),
        "effect_count": len(session.effects),
        "slip_event_count": len(session.slip_events),
        "automation_count": (
            len(session.automations)
            + len(session.deck_automations)
            + sum(len(clip.automations) for clip in session.clips)
            + sum(len(group.automations) + sum(len(stem.automations) for stem in group.stems.values()) for group in session.stem_groups)
            + sum(len(lean_in.effects) for lean_in in session.mic_lean_ins)
        ),
        "clips_by_deck": clips_by_deck,
        "stem_groups_by_deck": stem_groups_by_deck,
    }


def template_session() -> dict[str, Any]:
    return {
        "version": 1,
        "decks": DEFAULT_SESSION_DECKS,
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
                "deck": VOCAL_DECK,
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
    return {
        "version": 1,
        "decks": DEFAULT_SESSION_DECKS,
        "clips": [],
        "mic_lean_ins": [],
        "effects": [],
        "slip_events": [],
        "automations": [],
        "deck_automations": [],
    }


def find_event(payload: dict[str, Any], event_id: str) -> tuple[str, int] | None:
    for collection in ("actions", "clips", "stem_groups", "mic_lean_ins", "effects", "slip_events"):
        for index, item in enumerate(payload.get(collection, [])):
            if item.get("id") == event_id:
                return collection, index
    return None


def event_start_ms(item: dict[str, Any]) -> int:
    # Actions author their time as at/at_ms; clips as start/start_ms. Missing
    # either made the live-edit lock read actions as t=0 and refuse valid
    # future edits (or worse, permit past ones).
    return parse_ms(item.get("start_ms", item.get("start", item.get("at_ms", item.get("at", 0)))), "event start")


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
            "audio up to the lock is playing or already prerendered, so an edit there "
            "would not air (and forcing it desyncs the timeline). Move the edit past "
            "the lock instead of using --force"
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


def add_action(
    payload: dict[str, Any],
    *,
    action: dict[str, Any],
    db_path: Path = DEFAULT_LIBRARY_DB,
    prepare_stems: bool = True,
    lock_before_ms: int | None = None,
    force: bool = False,
) -> dict[str, Any]:
    next_payload = copy.deepcopy(payload)
    action = prepare_load_track_action_stems(action, db_path=db_path, prepare_stems=prepare_stems)
    event_id = action_id(action)
    if not event_id:
        raise ValueError("action id is required")
    require_unique_event_id(next_payload, event_id)
    guard_live_edit(
        label=f"action {event_id}",
        start_ms=action_at_ms(action),
        lock_before_ms=lock_before_ms,
        force=force,
    )
    next_payload.setdefault("actions", []).append(action)
    parse_session(next_payload)
    return next_payload


def add_mic_lean_in(
    payload: dict[str, Any],
    *,
    lean_id: str,
    start: str,
    text: str,
    deck: str,
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
    decks = [str(item) for item in next_payload.get("decks", [])]
    if not decks:
        decks = list(DEFAULT_SESSION_DECKS)
    if deck not in decks:
        decks.append(deck)
    next_payload["decks"] = decks
    lean_in: dict[str, Any] = {"id": lean_id, "deck": deck, "start": start, "text": text}
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
        known = [str(item.get("id")) for coll in ("clips", "actions", "mic_lean_ins", "effects", "slip_events") for item in next_payload.get(coll, []) if isinstance(item, dict) and item.get("id")]
        raise ValueError(f"event id does not exist: {event_id}; known ids: {', '.join(sorted(known)[:40])}")
    guard_event_live_edit(next_payload, event_id, lock_before_ms=lock_before_ms, force=force)
    collection, index = found
    del next_payload[collection][index]
    # Removal is cascade-atomic: everything targeting the event (stem toggles,
    # knob rides) goes with it in the same write. Removing children separately
    # races the advancing live-edit lock — a load stripped of its toggles but
    # itself un-removable aired a vocal clash once.
    next_payload["actions"] = [
        action for action in next_payload.get("actions", []) if str(action.get("target") or "") != event_id
    ]
    next_payload["automations"] = [
        automation for automation in next_payload.get("automations", []) if automation.get("target") != event_id
    ]
    # Planner-authored filter/EQ carves live in a separate top-level
    # `deck_automations` collection keyed by `related_clip_id` (not
    # `target`), so they survived a load's removal — orphaned rides past the
    # removed clip's end silently inflated session_duration_ms and broke
    # extend's overrun math (found and fixed live by cold test 53's agent).
    next_payload["deck_automations"] = [
        automation for automation in next_payload.get("deck_automations", []) if automation.get("related_clip_id") != event_id
    ]
    for clip in next_payload.get("clips", []):
        clip["automations"] = [
            automation for automation in clip.get("automations", []) if automation.get("target", clip.get("id")) != event_id
        ]
    parse_session(next_payload)
    return next_payload


def set_master_tempo(
    payload: dict[str, Any],
    bpm: float,
    *,
    max_tempo_stretch_pct: float | None = None,
    points_json: str | None = None,
) -> dict[str, Any]:
    """Set (or with bpm 0 release) the session's master tempo knob.

    `points_json` automates the knob over the timeline (`[{"at": "45:00.000",
    "value": 86}, ...]`); the scalar bpm is its base position. Warped clips
    re-derive at the runner's next reload, so this retempos every future
    window; already-rendered windows are untouched. Releasing returns warped
    clips to their native tempo instead of freezing the last warp, and clears
    any automation.
    """
    next_payload = copy.deepcopy(payload)
    if bpm and float(bpm) > 0:
        next_payload["master_bpm"] = float(bpm)
        if max_tempo_stretch_pct is not None:
            next_payload["max_tempo_stretch_pct"] = abs(float(max_tempo_stretch_pct))
        if points_json is not None:
            points = json.loads(points_json)
            if not isinstance(points, list):
                raise ValueError("master tempo points must be a JSON list")
            next_payload["master_bpm_automation"] = [
                {
                    "at_ms": parse_ms(point.get("at", point.get("at_ms")), "master tempo point"),
                    "value": float(point["value"]),
                }
                for point in points
            ]
    else:
        next_payload.pop("master_bpm", None)
        next_payload.pop("master_bpm_automation", None)
        for event in list(next_payload.get("clips", [])) + list(next_payload.get("stem_groups", [])):
            if event.get("warp", True) and event.get("source_bpm"):
                event["tempo_shift_pct"] = 0.0
    parse_session(next_payload)
    return next_payload


def set_master_key(
    payload: dict[str, Any],
    key: str | None,
    *,
    max_key_shift_semitones: int | None = None,
    points_json: str | None = None,
) -> dict[str, Any]:
    """Set (or with an empty key release) the session's master key.

    `points_json` rides the key across the timeline as step changes
    (`[{"at": "60:00.000", "value": "C major"}, ...]`) — the strategic
    modulation for when upcoming material sits out of the current center's
    reach. Keymatched events re-derive at the next load; releasing returns
    them to their native pitch and clears any ride.
    """
    next_payload = copy.deepcopy(payload)
    if key:
        parse_master_key(key)  # validate before storing
        next_payload["master_key"] = str(key)
        if max_key_shift_semitones is not None:
            next_payload["max_key_shift_semitones"] = abs(int(max_key_shift_semitones))
        if points_json is not None:
            points = json.loads(points_json)
            if not isinstance(points, list):
                raise ValueError("master key points must be a JSON list")
            for point in points:
                parse_master_key(point["value"])  # validate each stop
                parse_ms(point.get("at", point.get("at_ms")), "master key point")
            next_payload["master_key_automation"] = points
    else:
        next_payload.pop("master_key", None)
        next_payload.pop("master_key_automation", None)
        for event in list(next_payload.get("clips", [])) + list(next_payload.get("stem_groups", [])):
            if event.get("keymatch", True) and event.get("tonic") is not None:
                event["pitch_shift_semitones"] = 0
    parse_session(next_payload)
    return next_payload


def set_event_warp(
    payload: dict[str, Any],
    event_id: str,
    *,
    warp: bool,
    source_bpm: float | None = None,
    keymatch: bool | None = None,
    lock_before_ms: int | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Opt a clip in or out of master-tempo warping and/or keymatching.

    Disabling keymatch freezes the event's current pitch as authored — it can
    then be pitch-shifted manually, exactly as before master keys existed."""
    next_payload = copy.deepcopy(payload)
    found = find_event(next_payload, event_id)
    if found is None:
        known = [str(item.get("id")) for coll in ("clips", "actions", "mic_lean_ins", "effects", "slip_events") for item in next_payload.get(coll, []) if isinstance(item, dict) and item.get("id")]
        raise ValueError(f"event id does not exist: {event_id}; known ids: {', '.join(sorted(known)[:40])}")
    guard_event_live_edit(next_payload, event_id, lock_before_ms=lock_before_ms, force=force)
    collection, index = found
    event = next_payload[collection][index]
    event["warp"] = bool(warp)
    if source_bpm is not None:
        event["source_bpm"] = float(source_bpm)
    if keymatch is not None:
        event["keymatch"] = bool(keymatch)
    if not warp:
        event["tempo_shift_pct"] = 0.0
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
        known = [str(item.get("id")) for coll in ("clips", "actions", "mic_lean_ins", "effects", "slip_events") for item in next_payload.get(coll, []) if isinstance(item, dict) and item.get("id")]
        raise ValueError(f"event id does not exist: {event_id}; known ids: {', '.join(sorted(known)[:40])}")
    guard_event_live_edit(next_payload, event_id, lock_before_ms=lock_before_ms, force=force)
    guard_live_edit(
        label=f"new start for {event_id}",
        start_ms=parse_ms(start, f"event {event_id} start"),
        lock_before_ms=lock_before_ms,
        force=force,
    )
    collection, index = found
    # Readers prefer the pre-baked *_ms integers over the human-readable
    # at/start strings, so writing only the string made move a silent no-op
    # on any already-planned event (found live by the DJ agent 2026-07-04).
    # Keep every time field the event carries in sync.
    event = next_payload[collection][index]
    new_ms = parse_ms(start, f"event {event_id} start")
    if collection == "actions":
        event["at"] = start
        if "at_ms" in event:
            event["at_ms"] = new_ms
        if "start_ms" in event:
            event["start_ms"] = new_ms
    else:
        event["start"] = start
        if "start_ms" in event:
            event["start_ms"] = new_ms
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
        known = [str(item.get("id")) for coll in ("clips", "actions", "mic_lean_ins", "effects", "slip_events") for item in next_payload.get(coll, []) if isinstance(item, dict) and item.get("id")]
        raise ValueError(f"event id does not exist: {event_id}; known ids: {', '.join(sorted(known)[:40])}")
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
    decks = [str(deck) for deck in next_payload.get("decks", [])] or list(DEFAULT_SESSION_DECKS)
    if target in decks and target not in {str(event.get("id")) for event in next_payload.get("clips", [])}:
        next_payload.setdefault("deck_automations", []).append(automation)
    else:
        next_payload.setdefault("automations", []).append(automation)
    parse_session(next_payload)
    return next_payload


def set_fader_routing(payload: dict[str, Any], assignments: dict[str, str]) -> dict[str, Any]:
    next_payload = copy.deepcopy(payload)
    decks = [str(deck) for deck in next_payload.get("decks", [])] or list(DEFAULT_SESSION_DECKS)
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


def beat_synced_delay_ms(
    payload: dict[str, Any],
    target: str,
    delay_beats: float,
    *,
    cache_path: Path = DEFAULT_DJ_CACHE,
    min_confidence: float = DEFAULT_MIN_BEATGRID_CONFIDENCE,
    force: bool = False,
) -> int:
    """Resolve a hardware-style tempo-synced delay time for the target event.

    One beat is a quarter note at the target's *rendered* tempo: the cached
    analyzed BPM scaled by the event's tempo_shift_pct. Common divisions:
    0.5 = eighth, 0.75 = dotted eighth, 1.0 = quarter, 1.5 = dotted quarter.
    """
    if delay_beats <= 0:
        raise ValueError("delay beats must be positive")
    found = find_event(payload, target)
    if found is None:
        raise ValueError(
            f"beat-synced delay needs a clip/load target with analyzable audio; {target!r} is not an event id. "
            "Use an explicit delay in ms for deck/master targets."
        )
    event = payload[found[0]][found[1]]
    source_path = str(event.get("source_path") or event.get("path") or "").strip()
    if not source_path:
        raise ValueError(f"beat-synced delay target {target} has no source path to analyze")
    bpm, _beat_offset_ms, _confidence = cached_beatgrid(
        cache_path, source_path, min_confidence=min_confidence, force=force
    )
    rendered_bpm = bpm * (1 + float(event.get("tempo_shift_pct", 0.0) or 0.0) / 100)
    if rendered_bpm <= 0:
        raise ValueError(f"beat-synced delay target {target} has a non-positive rendered tempo")
    return max(1, int(round((60_000.0 / rendered_bpm) * delay_beats)))


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
    delay_beats: float | None = None,
    cache_path: Path = DEFAULT_DJ_CACHE,
    beat_min_confidence: float = DEFAULT_MIN_BEATGRID_CONFIDENCE,
    lock_before_ms: int | None = None,
    force: bool = False,
) -> dict[str, Any]:
    next_payload = copy.deepcopy(payload)
    require_unique_event_id(next_payload, effect_id)
    start_ms = parse_ms(start, f"effect {effect_id} start")
    guard_live_edit(label=f"effect {effect_id}", start_ms=start_ms, lock_before_ms=lock_before_ms, force=force)
    if delay_beats is not None:
        delay_ms = beat_synced_delay_ms(
            next_payload,
            target,
            delay_beats,
            cache_path=cache_path,
            min_confidence=beat_min_confidence,
            force=force,
        )
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
    if delay_beats is not None:
        effect["delay_beats"] = delay_beats
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
        raise ValueError(f"clip window target must be a clip: {clip_id}")
    clip = payload[collection][index]
    start_ms = event_start_ms(clip)
    duration = clip.get("duration_ms", clip.get("duration"))
    if duration is None:
        raise ValueError(f"clip {clip_id} needs a duration before its window can be used")
    return start_ms, start_ms + parse_ms(duration, f"clip {clip_id} duration")


def clip_tempo_factor(clip: dict[str, Any]) -> float:
    factor = 1 + (float(clip.get("tempo_shift_pct", 0.0)) / 100)
    if factor <= 0:
        raise ValueError(f"clip {clip.get('id')} tempo_shift_pct produces non-positive tempo")
    return factor


def choose_free_deck(payload: dict[str, Any], start_ms: int, end_ms: int, *, avoid: str | None = None) -> str:
    decks = [str(deck) for deck in payload.get("decks", []) if str(deck)]
    if not decks:
        decks = list(DEFAULT_MUSIC_DECKS)
    for deck in decks:
        if deck == avoid or deck == VOCAL_DECK:
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
        routing = parse_fader_routing(next_payload, [str(deck) for deck in next_payload.get("decks", [])] or list(DEFAULT_SESSION_DECKS))
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
    source: dict[str, Any] | None = None
    if found is not None:
        collection, index = found
        if collection == "clips":
            source = payload[collection][index]
        elif not (collection == "actions" and action_type(payload[collection][index]) == "load_track"):
            raise ValueError(f"instant-double routine source must be a clip or a load_track action: {source_id}")
    if source is None:
        # Loads are how songs play; the routine must reach them. Resolve a
        # load (or compiled segment id) through the rendered deck clock —
        # two cold DJs burned their edit windows discovering routines only
        # took legacy raw clips.
        compiled = apply_master_tempo(compile_actions_payload(copy.deepcopy(payload)))
        candidates = [
            clip
            for clip in compiled.get("clips", [])
            if str(clip.get("source_action_id") or "") == source_id or str(clip.get("id") or "") == source_id
        ]
        if not candidates:
            raise ValueError(
                f"instant-double source {source_id} is neither a raw clip nor a load with compiled segments"
            )
        source = min(candidates, key=lambda clip: int(clip.get("start_ms") or 0))
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
    if config.get("loop_pattern"):
        return add_loop_roll_routine(
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
                [str(deck) for deck in next_payload.get("decks", [])] or list(DEFAULT_SESSION_DECKS),
            )
            source_side = routing.get(source_deck, DEFAULT_FADER_ASSIGNMENTS.get(source_deck, "A"))
            if source_side == "THRU":
                source_side = DEFAULT_FADER_ASSIGNMENTS.get(source_deck, "A")
            target_side = "B" if source_side == "A" else "A"
            on_position = -1.0 if target_side == "A" else 1.0
            decks = [str(deck) for deck in next_payload.get("decks", [])] or list(DEFAULT_SESSION_DECKS)
            brake_assignments = {deck: (source_side if deck != VOCAL_DECK else "THRU") for deck in decks}
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


def add_loop_roll_routine(
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
        raise ValueError(f"loop routine {routine_id} must start before source clip ends")
    beat_ms = 60_000 / bpm
    config = SUPPORTED_INSTANT_DOUBLE_RECIPES[recipe]
    loop_ms = max(80, int(round(float(Fraction(str(config.get("loop_beats", "1")))) * beat_ms)))
    routine_duration_ms = parse_ms(config["duration"], f"{recipe} duration")
    routine_end_ms = min(source_end_ms, start_ms + routine_duration_ms)
    if routine_end_ms <= start_ms:
        raise ValueError(f"loop routine {routine_id} has no duration")
    source_deck = str(source_payload.get("deck") or "")
    source_trim_ms = parse_ms(source_payload.get("trim_start_ms", source_payload.get("trim_start", 0)), f"clip {source_id} trim_start")
    source_trim_at_start = source_trim_ms + int(round((start_ms - source_start_ms) * clip_tempo_factor(source_payload)))
    next_payload = copy.deepcopy(payload)
    index = 1
    clip_start = start_ms
    while clip_start < routine_end_ms:
        duration_ms = min(loop_ms, routine_end_ms - clip_start)
        if duration_ms <= 0:
            break
        next_payload.setdefault("clips", []).append(
            {
                "id": f"{routine_id}-loop-{index:02d}",
                "deck": source_deck,
                "path": source_payload.get("path"),
                "start_ms": clip_start,
                "trim_start_ms": source_trim_at_start,
                "duration_ms": duration_ms,
                "trim_db": float(source_payload.get("trim_db", 0.0)),
                "gain_db": float(source_payload.get("gain_db", 0.0)) + 1.0,
                "tempo_shift_pct": float(source_payload.get("tempo_shift_pct", 0.0)),
                "pitch_shift_semitones": int(source_payload.get("pitch_shift_semitones", 0)),
                "fade_in_ms": min(12, duration_ms // 4),
                "fade_out_ms": min(12, duration_ms // 4),
                "kind": "effect-track",
                "attached_deck": source_deck,
                "effect_parent_clip_id": source_id,
                "planner_role": "loop-roll",
                "source_clip_id": source_id,
                "routine_id": routine_id,
                "routine_recipe": recipe,
                "source_technique": "slip-loop-roll",
            }
        )
        index += 1
        clip_start += loop_ms
    next_payload.setdefault("automations", []).append(
        {
            "target": source_id,
            "param": "gain_db",
            "planner_role": "loop-roll-source-duck",
            "points": [
                {"at_ms": start_ms, "value": -96.0},
                {"at_ms": routine_end_ms, "value": -96.0},
            ],
            "routine_id": routine_id,
            "routine_recipe": recipe,
        }
    )
    next_payload.setdefault("slip_events", []).append(
        {
            "id": f"{routine_id}-slip",
            "source_clip_id": source_id,
            "target_clip_id": f"{routine_id}-loop-01",
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
    next_payload = copy.deepcopy(payload)
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
            "deck": source_deck,
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
        next_payload.setdefault("automations", []).append(
            {
                "target": source_id,
                "param": "gain_db",
                "planner_role": "scratch-source-duck",
                "points": [
                    {"at_ms": clip_start, "value": -96.0},
                    {"at_ms": clip_start + clip_duration, "value": -96.0},
                ],
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
        return edit_lock_ms_from_state(args.state)
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate and inspect mutable SlimeAudio mix sessions.")
    sub = parser.add_subparsers(dest="command", required=True)
    validate_parser = sub.add_parser("validate")
    validate_parser.add_argument("session", type=Path)
    summary_parser = sub.add_parser("summary")
    summary_parser.add_argument("session", type=Path)
    audit_durations_parser = sub.add_parser("audit-durations")
    audit_durations_parser.add_argument("session", type=Path)
    audit_durations_parser.add_argument("--threshold-ms", type=int, default=5_000)
    audit_durations_parser.add_argument("--from-ms")
    audit_durations_parser.add_argument("--fail", action=argparse.BooleanOptionalAction, default=True)
    audit_volume_parser = sub.add_parser("audit-volume")
    audit_volume_parser.add_argument("session", type=Path)
    audit_volume_parser.add_argument("--from-ms")
    audit_volume_parser.add_argument("--max-fade-out-ms", type=int, default=2_000)
    audit_volume_parser.add_argument("--min-gain-db", type=float, default=-6.0)
    audit_volume_parser.add_argument("--min-duck-volume", type=float, default=0.98)
    audit_volume_parser.add_argument("--fail", action=argparse.BooleanOptionalAction, default=True)
    sub.add_parser("template")

    add_action_parser = sub.add_parser("add-action")
    add_action_parser.add_argument("session", type=Path)
    add_action_parser.add_argument("--create", action="store_true")
    add_action_parser.add_argument("--action-json", required=True)
    add_action_parser.add_argument("--db", type=Path, default=DEFAULT_LIBRARY_DB)
    add_live_edit_args(add_action_parser)

    add_mic_parser = sub.add_parser("add-mic")
    add_mic_parser.add_argument("session", type=Path)
    add_mic_parser.add_argument("--create", action="store_true")
    add_mic_parser.add_argument("--id", required=True)
    add_mic_parser.add_argument("--start", required=True)
    add_mic_parser.add_argument("--text", required=True)
    add_mic_parser.add_argument("--deck", default=VOCAL_DECK)
    add_mic_parser.add_argument("--voice")
    add_mic_parser.add_argument("--rate")
    add_mic_parser.add_argument("--volume", type=float, default=1.45)
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
    effect_parser.add_argument(
        "--delay-beats",
        type=float,
        help="Tempo-synced delay time in beats of the target's rendered tempo (0.5 eighth, 0.75 dotted eighth, 1 quarter). Uses the cached beatgrid.",
    )
    effect_parser.add_argument("--cache", type=Path, default=DEFAULT_DJ_CACHE)
    effect_parser.add_argument("--min-confidence", type=float, default=DEFAULT_MIN_BEATGRID_CONFIDENCE)
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

    args = parser.parse_args()

    if args.command == "template":
        print(json.dumps(template_session(), indent=2, sort_keys=True))
        return 0

    if args.command == "audit-durations":
        report = audit_session_durations(
            load_session(args.session),
            threshold_ms=args.threshold_ms,
            from_ms=parse_ms(args.from_ms, "audit start") if args.from_ms else None,
        )
        print(json.dumps(report, indent=2, sort_keys=True))
        if args.fail and (report["mismatch_count"] or report["failure_count"]):
            return 1
        return 0

    if args.command == "audit-volume":
        report = audit_hidden_volume_sag(
            load_session(args.session),
            from_ms=parse_ms(args.from_ms, "audit start") if args.from_ms else None,
            max_fade_out_ms=args.max_fade_out_ms,
            min_gain_db=args.min_gain_db,
            min_duck_volume=args.min_duck_volume,
        )
        print(json.dumps(report, indent=2, sort_keys=True))
        if args.fail and report["finding_count"]:
            return 1
        return 0

    if args.command == "add-action":
        action_payload = json.loads(args.action_json)
        if not isinstance(action_payload, dict):
            raise ValueError("--action-json must be a JSON object")
        payload = base_payload(args.session, args.create)
        lock_before_ms = live_edit_lock(args)
        updated = add_action(
            payload,
            action=action_payload,
            db_path=args.db,
            lock_before_ms=lock_before_ms,
            force=args.force,
        )
        write_payload(args.session, updated)
        print(f"added action {action_id(action_payload)}")
        return 0

    if args.command == "add-mic":
        payload = base_payload(args.session, args.create)
        lock_before_ms = live_edit_lock(args)
        updated = add_mic_lean_in(
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
        if args.delay_ms is not None and args.delay_beats is not None:
            raise SystemExit("--delay-ms and --delay-beats are mutually exclusive")
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
                delay_beats=args.delay_beats,
                cache_path=args.cache,
                beat_min_confidence=args.min_confidence,
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

    session = load_session(args.session)
    if args.command == "validate":
        print(f"ok clips={len(session.clips)} mic_lean_ins={len(session.mic_lean_ins)} automations={len(session.automations)}")
        return 0
    print(json.dumps(session_summary(session), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
