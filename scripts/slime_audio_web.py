#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import subprocess
import time
from array import array
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from slime_audio_session import VOCAL_DECK, load_payload, parse_ms, parse_session, playhead_ms_from_state
from slime_audio_sets import (
    DEFAULT_ACTIVE_SET,
    DEFAULT_SETS_DIR,
    activate_set,
    get_set,
    list_sets,
    load_json,
    new_set,
    render_set,
    replay_set,
    save_loaded_set,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = REPO_ROOT / "web" / "slime-audio"
DEFAULT_STATE = REPO_ROOT / "runtime" / "mix-session-state.json"
DEFAULT_SESSION = REPO_ROOT / "runtime" / "mix-session.json"
DEFAULT_WAVEFORM_CACHE = REPO_ROOT / "runtime" / "waveform-cache.json"
DECK_ORDER = ["deck-3", "deck-1", VOCAL_DECK, "deck-2", "deck-4"]
LANE_LABELS = {VOCAL_DECK: "MIC"}
DEFAULT_VOCAL_DURATION_MS = 4500
WAVEFORM_BINS = 240
WAVEFORM_CACHE_VERSION = 2


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


def format_title(path_text: str) -> dict[str, str]:
    path = Path(path_text)
    title = path.stem
    artist = path.parent.parent.name if len(path.parts) >= 3 else ""
    album = path.parent.name if len(path.parts) >= 2 else ""
    return {"title": title, "artist": artist, "album": album, "path": path_text}


def session_clip_event(clip: dict[str, Any]) -> dict[str, Any]:
    start_ms = parse_ms(clip.get("start_ms", clip.get("start", 0)), "clip start")
    duration = clip.get("duration_ms", clip.get("duration"))
    duration_ms = parse_ms(duration, "clip duration") if duration is not None else None
    return {
        "id": clip.get("id"),
        "kind": clip.get("kind") or "song",
        "deck": clip.get("deck"),
        "attached_deck": clip.get("attached_deck"),
        "effect_parent_clip_id": clip.get("effect_parent_clip_id"),
        "index": clip.get("index"),
        "start_ms": start_ms,
        "trim_start_ms": parse_ms(clip.get("trim_start_ms", clip.get("trim_start", 0)), "clip trim_start"),
        "duration_ms": duration_ms,
        "end_ms": start_ms + duration_ms if duration_ms is not None else None,
        "status": clip.get("status"),
        "gain_db": clip.get("gain_db", 0.0),
        "trim_db": clip.get("trim_db", 0.0),
        "tempo_shift_pct": clip.get("tempo_shift_pct", 0.0),
        "pitch_shift_semitones": clip.get("pitch_shift_semitones", 0),
        "reverse": bool(clip.get("reverse", False)),
        "playback_rate": clip.get("playback_rate", 1.0),
        "planner_role": clip.get("planner_role"),
        "source_clip_id": clip.get("source_clip_id"),
        "routine_id": clip.get("routine_id"),
        "routine_recipe": clip.get("routine_recipe"),
        "source_technique": clip.get("source_technique"),
        **format_title(str(clip.get("path") or "")),
    }


def session_events(payload: dict[str, Any]) -> list[dict[str, Any]]:
    parse_session(payload)
    events: list[dict[str, Any]] = []
    for clip in payload.get("clips", []):
        event = session_clip_event(clip)
        events.append(event)
        for automation in clip.get("automations", []):
            events.append(automation_payload(automation, owner=str(clip.get("id"))))
    for lean_in in payload.get("mic_lean_ins", payload.get("micLeanIns", [])):
        start_ms = parse_ms(lean_in.get("start_ms", lean_in.get("start", 0)), "mic lean-in start")
        events.append(
            {
                "id": lean_in.get("id"),
                "kind": "vocal",
                "deck": lean_in.get("deck") or VOCAL_DECK,
                "start_ms": start_ms,
                "duration_ms": None,
                "end_ms": None,
                "text": lean_in.get("text"),
                "voice": lean_in.get("voice"),
                "rate": lean_in.get("rate"),
                "volume": lean_in.get("volume", 1.0),
            }
        )
        for key in ("ducking", "lowpass"):
            if isinstance(lean_in.get(key), dict):
                events.append(automation_payload(lean_in[key], owner=str(lean_in.get("id"))))
        for automation in lean_in.get("effects", []):
            events.append(automation_payload(automation, owner=str(lean_in.get("id"))))
    for automation in payload.get("automations", []):
        events.append(automation_payload(automation))
    for automation in payload.get("deck_automations", payload.get("deckAutomations", [])):
        events.append({**automation_payload(automation), "deck": automation.get("target")})
    for effect in payload.get("effects", []):
        start_ms = parse_ms(effect.get("start_ms", effect.get("start", 0)), "effect start")
        duration_ms = parse_ms(effect.get("duration_ms", effect.get("duration", 0)), "effect duration")
        tail_ms = parse_ms(effect.get("tail_ms", effect.get("tail", 0)), "effect tail")
        events.append(
            {
                "id": effect.get("id"),
                "kind": "effect",
                "deck": "effects",
                "target": effect.get("target"),
                "effect_type": effect.get("type"),
                "tail_ms": tail_ms,
                "wet": effect.get("wet"),
                "gain_db": effect.get("gain_db"),
                "delay_ms": effect.get("delay_ms"),
                "feedback": effect.get("feedback"),
                "room_size": effect.get("room_size"),
                "damping": effect.get("damping"),
                "lowpass_hz": effect.get("lowpass_hz"),
                "preset": effect.get("preset"),
                "start_ms": start_ms,
                "duration_ms": duration_ms + tail_ms,
                "end_ms": start_ms + duration_ms + tail_ms,
                "routine_id": effect.get("routine_id"),
                "routine_recipe": effect.get("routine_recipe"),
            }
        )
    for slip in payload.get("slip_events", payload.get("slipEvents", [])):
        start_ms = parse_ms(slip.get("start_ms", slip.get("start", 0)), "slip start")
        duration_ms = parse_ms(slip.get("duration_ms", slip.get("duration", 0)), "slip duration")
        events.append(
            {
                "id": slip.get("id"),
                "kind": "slip",
                "deck": "effects",
                "source_clip_id": slip.get("source_clip_id"),
                "target_clip_id": slip.get("target_clip_id"),
                "source_start_ms": slip.get("source_start_ms"),
                "source_resume_ms": slip.get("source_resume_ms"),
                "start_ms": start_ms,
                "duration_ms": duration_ms,
                "end_ms": start_ms + duration_ms,
                "routine_id": slip.get("routine_id"),
                "routine_recipe": slip.get("routine_recipe"),
            }
        )
    kind_order = {"song": 0, "vocal": 1, "effect": 2, "slip": 3, "automation": 4}
    return sorted(
        events,
        key=lambda item: (
            item.get("start_ms") is None,
            item.get("start_ms") or 0,
            item.get("index") is None,
            item.get("index") or 0,
            kind_order.get(str(item.get("kind")), 9),
            str(item.get("id") or item.get("param") or ""),
        ),
    )


def event_start(event: dict[str, Any]) -> int | None:
    value = event.get("start_ms")
    return int(value) if isinstance(value, (int, float)) else None


def event_end(event: dict[str, Any]) -> int | None:
    value = event.get("end_ms")
    if isinstance(value, (int, float)):
        return int(value)
    start = event_start(event)
    if start is None:
        return None
    duration = event.get("duration_ms")
    if isinstance(duration, (int, float)):
        return start + int(duration)
    if event.get("kind") == "vocal":
        return start + DEFAULT_VOCAL_DURATION_MS
    if event.get("kind") == "automation":
        return start + 1000
    return None


def session_duration_ms(events: list[dict[str, Any]], state: dict[str, Any]) -> int:
    ends = [end for end in (event_end(event) for event in events) if end is not None]
    state_duration = state.get("duration_ms")
    if isinstance(state_duration, (int, float)):
        ends.append(int(state_duration))
    return max(ends) if ends else 0


def classify_status(event: dict[str, Any], playhead_ms: int | None) -> str:
    existing = str(event.get("status") or "")
    start = event_start(event)
    end = event_end(event)
    if playhead_ms is None or start is None or end is None:
        return existing or "unknown"
    if end <= playhead_ms:
        return "done"
    if start <= playhead_ms < end:
        return "current"
    return "planned"


def display_title_for_event(event: dict[str, Any]) -> str:
    if event.get("kind") == "automation":
        return f"{event.get('param') or 'automation'}"
    if event.get("kind") == "vocal":
        return str(event.get("text") or event.get("id") or "vocal")
    if event.get("kind") == "effect":
        return str(event.get("effect_type") or event.get("id") or "effect")
    if event.get("kind") == "effect-track":
        return str(event.get("routine_recipe") or event.get("planner_role") or event.get("id") or "effect track")
    if event.get("kind") == "slip":
        return "slip/flux"
    return str(event.get("title") or event.get("id") or "untitled")


def display_meta_for_event(event: dict[str, Any]) -> str:
    if event.get("kind") == "automation":
        if event.get("target") == "crossfader":
            return "crossfader motion"
        target = event.get("target") or event.get("owner") or "master"
        points = event.get("points") or []
        value_text = ""
        if isinstance(points, list) and points:
            values = [point.get("value") for point in points if isinstance(point, dict)]
            if values:
                value_text = f" | {values[0]} -> {values[-1]}" if len(values) > 1 else f" | {values[0]}"
        return f"{target} | {event.get('param') or 'automation'}{value_text}"
    if event.get("kind") == "vocal":
        return "mic lean-in | vocal channel"
    if event.get("kind") == "effect":
        target = event.get("target") or "session"
        recipe = f" | {event.get('routine_recipe')}" if event.get("routine_recipe") else ""
        tail_ms = int(event.get("tail_ms") or 0)
        tail = f" | tail {tail_ms / 1000:.1f}s" if tail_ms else ""
        params = []
        for key, label in (("wet", "wet"), ("gain_db", "gain"), ("feedback", "fb"), ("delay_ms", "delay"), ("preset", "preset")):
            value = event.get(key)
            if value is not None:
                suffix = "ms" if key == "delay_ms" else " dB" if key == "gain_db" else ""
                params.append(f"{label} {value}{suffix}")
        param_text = f" | {', '.join(params)}" if params else ""
        return f"{target}{tail}{param_text}{recipe}"
    if event.get("kind") == "effect-track":
        parent = event.get("effect_parent_clip_id") or event.get("source_clip_id") or event.get("attached_deck") or "deck"
        recipe = f" | {event.get('routine_recipe')}" if event.get("routine_recipe") else ""
        rate = event.get("playback_rate")
        reverse = "reverse | " if event.get("reverse") else ""
        rate_text = f" | {reverse}rate {rate}" if rate not in {None, 1, 1.0} or reverse else ""
        return f"attached to {parent}{rate_text}{recipe}"
    if event.get("kind") == "slip":
        recipe = f" | {event.get('routine_recipe')}" if event.get("routine_recipe") else ""
        return f"{event.get('target_clip_id')} over {event.get('source_clip_id')}{recipe}"
    if event.get("routine_recipe"):
        return f"{event.get('routine_recipe')} routine of {event.get('source_clip_id')}"
    if event.get("planner_role") == "instant-double":
        return f"instant double of {event.get('source_clip_id')}"
    mix_bits: list[str] = []
    for key, label, suffix in (
        ("trim_db", "trim", " dB"),
        ("gain_db", "gain", " dB"),
        ("tempo_shift_pct", "tempo", "%"),
        ("pitch_shift_semitones", "pitch", " st"),
    ):
        value = event.get(key)
        if isinstance(value, (int, float)) and value:
            mix_bits.append(f"{label} {value:g}{suffix}")
    if event.get("reverse"):
        mix_bits.append("reverse")
    rate = event.get("playback_rate")
    if isinstance(rate, (int, float)) and rate != 1:
        mix_bits.append(f"rate {rate:g}")
    artist_album = " - ".join(str(value) for value in (event.get("artist"), event.get("album")) if value)
    source = artist_album or str(event.get("path") or "")
    return " | ".join([part for part in (source, ", ".join(mix_bits)) if part])


def normalize_event(event: dict[str, Any], playhead_ms: int | None) -> dict[str, Any]:
    start = event_start(event)
    end = event_end(event)
    duration = None if start is None or end is None else max(0, end - start)
    if event.get("kind") == "automation" and event.get("target") == "crossfader":
        lane = "fader"
    elif event.get("kind") == "effect-track":
        lane = f"{event.get('attached_deck') or event.get('deck')}-fx"
    else:
        lane = str(event.get("deck") or event.get("kind") or "timeline")
    status = classify_status(event, playhead_ms)
    normalized = dict(event)
    normalized.update(
        {
            "lane": lane,
            "start_ms": start,
            "end_ms": end,
            "duration_ms": duration,
            "status": status,
            "display_title": display_title_for_event(event),
            "display_meta": display_meta_for_event(event),
            "style_flags": event_style_flags(event),
            "is_timed": start is not None and end is not None,
        }
    )
    return normalized


def event_style_flags(event: dict[str, Any]) -> list[str]:
    flags: list[str] = []
    kind = str(event.get("kind") or "")
    if kind:
        flags.append(kind)
    effect_type = event.get("effect_type")
    if effect_type:
        flags.append(f"effect-{effect_type}")
    recipe = event.get("routine_recipe")
    if recipe:
        flags.append(f"routine-{recipe}")
    param = event.get("param")
    if param:
        flags.append(f"param-{param}")
    if event.get("attached_deck"):
        flags.append("attached")
    return [flag.replace("_", "-") for flag in flags]


def lane_rows(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    active_lanes = {str(event.get("lane")) for event in events if event.get("lane") is not None}
    ordered_lanes: list[str] = []
    for lane in DECK_ORDER:
        ordered_lanes.append(lane)
        fx_lane = f"{lane}-fx"
        if fx_lane in active_lanes:
            ordered_lanes.append(fx_lane)
    for lane in ("effects", "fader", "automation"):
        ordered_lanes.append(lane)
    for event in events:
        lane = str(event.get("lane") or "timeline")
        if lane not in ordered_lanes:
            ordered_lanes.append(lane)
    return [
        {
            "id": lane,
            "label": LANE_LABELS.get(lane, lane.replace("-", " ")),
            "kind": "effect-lane" if lane.endswith("-fx") else "deck" if lane.startswith("deck-") else lane,
            "attached_deck": lane[:-3] if lane.endswith("-fx") else None,
            "events": [event for event in events if event.get("lane") == lane],
        }
        for lane in ordered_lanes
    ]


def transport_status(state: dict[str, Any], playhead_ms: int | None, duration_ms: int) -> dict[str, Any]:
    updated_at = state.get("updated_at") or state.get("window_started_at") or state.get("started_at")
    completed_at = state.get("completed_at")
    current = state.get("current")
    status = "idle"
    if completed_at:
        status = "completed"
    elif current:
        status = "playing"
    elif state.get("window_started_at"):
        status = "window-active"
    stale = False
    updated_ts = parse_timestamp(updated_at if isinstance(updated_at, str) else None)
    stale_after_ts = updated_ts + 30 if updated_ts is not None else None
    window_started_ts = parse_timestamp(state.get("window_started_at") if isinstance(state.get("window_started_at"), str) else None)
    window_start_ms = state.get("window_start_ms")
    window_end_ms = state.get("window_end_ms")
    if (
        window_started_ts is not None
        and isinstance(window_start_ms, (int, float))
        and isinstance(window_end_ms, (int, float))
        and window_end_ms > window_start_ms
    ):
        stale_after_ts = max(stale_after_ts or 0, window_started_ts + ((window_end_ms - window_start_ms) / 1000) + 30)
    if stale_after_ts is not None and not completed_at and time.time() > stale_after_ts:
        stale = True
        status = "stale"
    return {
        "status": status,
        "stale": stale,
        "updated_at": updated_at,
        "completed_at": completed_at,
        "playhead_ms": min(playhead_ms, duration_ms) if playhead_ms is not None and duration_ms else playhead_ms,
        "duration_ms": duration_ms,
        "window": {
            "start_ms": state.get("window_start_ms"),
            "end_ms": state.get("window_end_ms"),
            "started_at": state.get("window_started_at"),
        },
    }


def build_dashboard_view(state: dict[str, Any], state_path: Path, session_path: Path, session_payload: dict[str, Any]) -> dict[str, Any]:
    raw_events = session_events(session_payload)
    try:
        playhead_ms = playhead_ms_from_state(state_path) if state_path.exists() else None
    except Exception:
        playhead_ms = None
    duration_ms = session_duration_ms(raw_events, state)
    events = [normalize_event(event, playhead_ms) for event in raw_events]
    current = next((event for event in events if event.get("kind") == "song" and event.get("status") == "current"), None)
    if current is None and state.get("current"):
        current = next((event for event in events if event.get("path") == state.get("current")), None)
    upcoming = [
        event
        for event in events
        if event.get("kind") == "song" and event.get("status") == "planned"
    ][:8]
    commentary = [
        event
        for event in events
        if event.get("kind") == "vocal" and event.get("status") != "done"
    ][:8]
    automation = [
        event
        for event in events
        if event.get("kind") == "automation" and event.get("status") != "done"
    ][:10]
    counts: dict[str, int] = {}
    for event in events:
        counts[str(event.get("kind") or "event")] = counts.get(str(event.get("kind") or "event"), 0) + 1
    fader_routing = session_payload.get("fader_routing", session_payload.get("faderRouting", {}))
    assignments = fader_routing.get("deck_assignments") if isinstance(fader_routing, dict) else {}
    return {
        "schema_version": 1,
        "state_path": str(state_path),
        "session_path": str(session_path),
        "transport": transport_status(state, playhead_ms, duration_ms),
        "session": {
            "timeline_mode": session_payload.get("timeline_mode", "native"),
            "duration_ms": duration_ms,
            "counts": counts,
            "decks": session_payload.get("decks", []),
            "fader_routing": fader_routing,
            "fader_assignments": assignments if isinstance(assignments, dict) else {},
        },
        "now": current,
        "lanes": lane_rows(events),
        "events": events,
        "upcoming": upcoming,
        "commentary": commentary,
        "automation": automation,
        "health": {
            "runner_state": "stale" if transport_status(state, playhead_ms, duration_ms)["stale"] else "ok",
            "receivers": state.get("receivers", []),
            "current_clips": state.get("current_clips", []),
        },
    }


def now_from_state(state: dict[str, Any], session_payload: dict[str, Any]) -> dict[str, Any]:
    current = state.get("current")
    started_at = parse_timestamp(state.get("started_at"))
    elapsed_ms = max(0, int(round((time.time() - started_at) * 1000))) if started_at is not None and current else None
    try:
        playhead_ms = playhead_ms_from_state(Path(str(state.get("_state_path", "")))) if state.get("_state_path") else None
    except Exception:
        playhead_ms = None
    current_event = next(
        (
            event
            for event in session_events(session_payload)
            if (
                event.get("status") == "current"
                or (current and event.get("path") == current)
                or (
                    playhead_ms is not None
                    and event.get("kind") == "song"
                    and isinstance(event.get("start_ms"), int)
                    and isinstance(event.get("end_ms"), int)
                    and event["start_ms"] <= playhead_ms < event["end_ms"]
                )
            )
        ),
        None,
    )
    duration_ms = current_event.get("duration_ms") if current_event else None
    if elapsed_ms is not None and duration_ms is not None:
        elapsed_ms = min(elapsed_ms, duration_ms)
    return {
        "track": format_title(str(current)) if current else None,
        "resolved_track": str(state.get("resolved_current") or current or ""),
        "started_at": state.get("started_at"),
        "elapsed_ms": elapsed_ms,
        "duration_ms": duration_ms,
        "transition": state.get("next_transition"),
    }


def dashboard_payload_from_state(state: dict[str, Any], state_path: Path, session_path: Path, session_payload: dict[str, Any]) -> dict[str, Any]:
    state["_state_path"] = str(state_path)
    session_source = str(session_path)
    active_set = load_json(DEFAULT_ACTIVE_SET, {})
    payload = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "state_path": str(state_path),
        "active_set": active_set,
        "now": now_from_state(state, session_payload),
        "session": {
            "path": session_source,
            "source": session_payload.get("source", "mix-session"),
            "timeline_mode": session_payload.get("timeline_mode", "native"),
            "raw": session_payload,
            "events": session_events(session_payload),
        },
    }
    payload["dashboard"] = build_dashboard_view(state, state_path, session_path, session_payload)
    payload["dashboard"]["active_set"] = active_set
    return payload


def load_dashboard_state(state_path: Path, session_path: Path | None) -> dict[str, Any]:
    state = load_payload(state_path) if state_path.exists() else {}
    if not session_path or not session_path.exists():
        raise FileNotFoundError(f"native mix session is required: {session_path or DEFAULT_SESSION}")
    return dashboard_payload_from_state(state, state_path, session_path, load_payload(session_path))


def load_archived_dashboard_state(slug: str) -> dict[str, Any]:
    metadata = get_set(DEFAULT_SETS_DIR, slug)
    session_path = Path(str(metadata["session_path"]))
    payload = dashboard_payload_from_state({}, Path(""), session_path, load_payload(session_path))
    payload["viewed_set"] = metadata
    payload["dashboard"]["viewed_set"] = metadata
    return payload


def resolve_pointer_path(value: object) -> Path | None:
    if not value:
        return None
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path


def active_pointer_path(key: str) -> Path | None:
    pointer = load_json(DEFAULT_ACTIVE_SET, {})
    path = resolve_pointer_path(pointer.get(key))
    return path if path is not None and path.exists() else None


def choose_state_path(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    active_state = active_pointer_path("active_state_path")
    if active_state is not None:
        return active_state
    if DEFAULT_STATE.exists():
        return DEFAULT_STATE
    candidates = sorted(
        (path for path in (REPO_ROOT / "runtime").glob("*session-state.json") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else DEFAULT_STATE


def choose_session_path(explicit: Path | None) -> Path | None:
    if explicit is not None:
        return explicit if explicit.exists() else None
    active_session = active_pointer_path("active_session_path")
    if active_session is not None:
        return active_session
    if DEFAULT_SESSION.exists():
        return DEFAULT_SESSION
    candidates = sorted(
        (path for path in (REPO_ROOT / "runtime").glob("*session*.json") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def automation_payload(automation: dict[str, Any], owner: str | None = None) -> dict[str, Any]:
    points = automation.get("points") or []
    parsed_points = [
        {
            "at_ms": parse_ms(point.get("at_ms", point.get("at", 0)), "automation point"),
            "value": point.get("value"),
            "curve": point.get("curve", "linear"),
        }
        for point in points
        if isinstance(point, dict)
    ]
    return {
        "kind": "automation",
        "owner": owner,
        "target": automation.get("target") or owner,
        "param": automation.get("param"),
        "points": parsed_points,
        "start_ms": parsed_points[0]["at_ms"] if parsed_points else None,
        "end_ms": parsed_points[-1]["at_ms"] if parsed_points else None,
    }


def waveform_cache_key(path: Path, trim_start_ms: int, duration_ms: int | None, bins: int) -> str:
    stat = path.stat()
    identity = "|".join(
        [
            str(WAVEFORM_CACHE_VERSION),
            str(path.resolve()),
            str(stat.st_size),
            str(stat.st_mtime_ns),
            str(trim_start_ms),
            str(duration_ms or 0),
            str(bins),
        ]
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def load_waveform_cache() -> dict[str, Any]:
    try:
        payload = json.loads(DEFAULT_WAVEFORM_CACHE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def save_waveform_cache(cache: dict[str, Any]) -> None:
    DEFAULT_WAVEFORM_CACHE.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_WAVEFORM_CACHE.write_text(json.dumps(cache, sort_keys=True), encoding="utf-8")


def normalize_peaks(raw_peaks: list[float], peak_max: float | None = None) -> list[float]:
    safe_max = peak_max if peak_max and peak_max > 0 else max(raw_peaks, default=0.0) or 1.0
    return [round(max(0.0, min(1.0, value / safe_max)), 4) for value in raw_peaks]


def band_envelopes(samples: array, rate: int, bins: int) -> dict[str, list[float]]:
    low = 0.0
    mid_low = 0.0
    mid_high = 0.0
    high_low = 0.0
    low_values: list[float] = []
    mid_values: list[float] = []
    high_values: list[float] = []

    def alpha(cutoff: float) -> float:
        return min(1.0, max(0.0, (2.0 * 3.141592653589793 * cutoff) / (rate + (2.0 * 3.141592653589793 * cutoff))))

    low_alpha = alpha(250.0)
    mid_low_alpha = alpha(250.0)
    mid_high_alpha = alpha(2800.0)
    high_alpha = alpha(4200.0)
    for sample in samples:
        value = float(sample)
        low += low_alpha * (value - low)
        mid_low += mid_low_alpha * (value - mid_low)
        mid_high += mid_high_alpha * (value - mid_high)
        high_low += high_alpha * (value - high_low)
        low_values.append(abs(low))
        mid_values.append(abs(mid_high - mid_low))
        high_values.append(abs(value - high_low))

    bin_size = max(1, len(samples) // bins)
    raw = {"low": [], "mid": [], "high": []}
    for index in range(bins):
        start = index * bin_size
        end = len(samples) if index == bins - 1 else min(len(samples), start + bin_size)
        if start >= len(samples):
            for values in raw.values():
                values.append(0.0)
            continue
        raw["low"].append(max(low_values[start:end], default=0.0))
        raw["mid"].append(max(mid_values[start:end], default=0.0))
        raw["high"].append(max(high_values[start:end], default=0.0))
    peak_max = max((value for values in raw.values() for value in values), default=0.0) or 1.0
    return {band: normalize_peaks(values, peak_max) for band, values in raw.items()}


def waveform_payload(path: Path, trim_start_ms: int = 0, duration_ms: int | None = None, bins: int = WAVEFORM_BINS) -> dict[str, Any]:
    resolved = path.expanduser()
    if not resolved.exists() or not resolved.is_file():
        return {"available": False, "path": str(path), "peaks": [], "error": "audio file not found"}
    safe_bins = max(32, min(800, int(bins)))
    safe_trim = max(0, int(trim_start_ms))
    safe_duration = max(1, int(duration_ms)) if duration_ms is not None and int(duration_ms) > 0 else None
    cache = load_waveform_cache()
    key = waveform_cache_key(resolved, safe_trim, safe_duration, safe_bins)
    cached = cache.get(key)
    if isinstance(cached, dict) and isinstance(cached.get("peaks"), list):
        return {**cached, "cache": "hit"}

    command = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    if safe_trim:
        command.extend(["-ss", f"{safe_trim / 1000:.3f}"])
    command.extend(["-i", str(resolved)])
    if safe_duration:
        command.extend(["-t", f"{safe_duration / 1000:.3f}"])
    sample_rate = 12_000
    command.extend(["-vn", "-ac", "1", "-ar", str(sample_rate), "-f", "s16le", "pipe:1"])
    try:
        result = subprocess.run(command, check=True, capture_output=True, timeout=20)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as ex:
        return {"available": False, "path": str(path), "peaks": [], "error": str(ex)}

    samples = array("h")
    samples.frombytes(result.stdout)
    if not samples:
        return {"available": False, "path": str(path), "peaks": [], "error": "no decoded samples"}
    bands = band_envelopes(samples, sample_rate, safe_bins)
    peaks = [max(values) for values in zip(bands["low"], bands["mid"], bands["high"])]
    payload = {
        "available": True,
        "path": str(resolved),
        "trim_start_ms": safe_trim,
        "duration_ms": safe_duration,
        "bins": safe_bins,
        "peaks": peaks,
        "bands": bands,
    }
    cache[key] = payload
    if len(cache) > 500:
        cache = dict(list(cache.items())[-500:])
    save_waveform_cache(cache)
    return {**payload, "cache": "miss"}


class SlimeAudioHandler(BaseHTTPRequestHandler):
    server: "SlimeAudioServer"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/state":
            params = parse_qs(parsed.query)
            set_slug = params.get("set", [""])[0]
            if set_slug:
                try:
                    self.send_json(load_archived_dashboard_state(set_slug))
                except Exception as ex:
                    self.send_json({"error": str(ex)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            state_path = Path(params.get("state", [str(self.server.state_path)])[0]).expanduser()
            session_arg = params.get("session", [str(self.server.session_path) if self.server.session_path else ""])[0]
            session_path = Path(session_arg).expanduser() if session_arg else None
            try:
                payload = load_dashboard_state(state_path, session_path)
            except Exception as ex:
                self.send_json({"error": str(ex)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            self.send_json(payload)
            return
        if parsed.path == "/api/sets":
            self.send_json({"sets": list_sets(DEFAULT_SETS_DIR), "active": load_json(DEFAULT_ACTIVE_SET, {})})
            return
        if parsed.path == "/api/waveform":
            params = parse_qs(parsed.query)
            path_text = params.get("path", [""])[0]
            if not path_text:
                self.send_json({"available": False, "peaks": [], "error": "missing path"}, status=HTTPStatus.BAD_REQUEST)
                return
            try:
                trim_start_ms = parse_ms(params.get("trim_start_ms", ["0"])[0], "trim_start_ms")
                duration_values = params.get("duration_ms", [])
                duration_ms = parse_ms(duration_values[0], "duration_ms") if duration_values else None
                bins = int(params.get("bins", [str(WAVEFORM_BINS)])[0])
                self.send_json(waveform_payload(Path(path_text), trim_start_ms, duration_ms, bins))
            except Exception as ex:
                self.send_json({"available": False, "peaks": [], "error": str(ex)}, status=HTTPStatus.BAD_REQUEST)
            return
        if parsed.path.startswith("/api/"):
            self.send_json({"error": f"unknown endpoint: {parsed.path}"}, status=HTTPStatus.NOT_FOUND)
            return
        self.serve_static(parsed.path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            body = self.read_json_body()
            if parsed.path == "/api/sets/new":
                self.send_json(new_set(sets_dir=DEFAULT_SETS_DIR, title=str(body["title"]), slug=body.get("slug")))
                return
            if parsed.path == "/api/sets/activate":
                self.send_json(activate_set(sets_dir=DEFAULT_SETS_DIR, slug=str(body["slug"]), reset_state=bool(body.get("reset_state", True))))
                return
            if parsed.path == "/api/sets/save-loaded":
                self.send_json(save_loaded_set(sets_dir=DEFAULT_SETS_DIR))
                return
            if parsed.path == "/api/sets/replay":
                target = body.get("target") or ["all"]
                if isinstance(target, str):
                    target = [target]
                self.send_json(
                    replay_set(
                        sets_dir=DEFAULT_SETS_DIR,
                        slug=str(body["slug"]),
                        target=[str(item) for item in target],
                        dry_run=bool(body.get("dry_run", False)),
                        reset_state=bool(body.get("reset_state", True)),
                    )
                )
                return
            if parsed.path == "/api/sets/render":
                self.send_json(
                    render_set(
                        sets_dir=DEFAULT_SETS_DIR,
                        slug=str(body["slug"]) if body.get("slug") else None,
                        session=None,
                        output=None,
                        render_dir=Path(str(body.get("render_dir") or REPO_ROOT / "runtime" / "set-renders")),
                        output_format=str(body.get("format") or "mp3"),
                        mp3_bitrate=str(body.get("mp3_bitrate") or "128k"),
                        from_time=str(body.get("from") or "0"),
                        duration=str(body["duration"]) if body.get("duration") else None,
                        skip_tts=bool(body.get("skip_tts", False)),
                        dry_run=bool(body.get("dry_run", False)),
                        keep=int(body.get("keep", 3)),
                        max_age_hours=float(body.get("max_age_hours", 12)),
                        max_total_mb=float(body.get("max_total_mb", 256)),
                    )
                )
                return
        except Exception as ex:
            self.send_json({"error": str(ex)}, status=HTTPStatus.BAD_REQUEST)
            return
        self.send_json({"error": f"unknown endpoint: {parsed.path}"}, status=HTTPStatus.NOT_FOUND)

    def read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("content-length", "0") or "0")
        if length <= 0:
            return {}
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        return payload

    def send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("cache-control", "no-store")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def serve_static(self, request_path: str) -> None:
        rel = "index.html" if request_path in {"", "/"} else request_path.lstrip("/")
        path = (WEB_ROOT / rel).resolve()
        if WEB_ROOT.resolve() not in path.parents and path != WEB_ROOT.resolve():
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if not path.exists() or not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = path.read_bytes()
        content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("content-type", content_type)
        self.send_header("cache-control", "no-store")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        message = format % args
        if " /api/state " in message or " /api/sets" in message or " /api/waveform" in message:
            return
        print(f"{self.address_string()} - {message}")


class SlimeAudioServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], state_path: Path, session_path: Path | None):
        super().__init__(address, SlimeAudioHandler)
        self.state_path = state_path
        self.session_path = session_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve the SlimeAudio now-playing and DJ session web dashboard.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--session", type=Path)
    args = parser.parse_args()

    session_path = choose_session_path(args.session)
    state_path = choose_state_path(args.state)
    server = SlimeAudioServer((args.host, args.port), state_path, session_path)
    print(f"slime-audio web listening on http://{args.host}:{args.port} state={state_path}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
