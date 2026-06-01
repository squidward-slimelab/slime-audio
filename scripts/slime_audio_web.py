#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import mimetypes
import time
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from slime_audio_session import load_payload, parse_ms, parse_session, playhead_ms_from_state
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
DECK_ORDER = ["deck-3", "deck-1", "deck-2", "deck-4"]
DEFAULT_VOCAL_DURATION_MS = 4500


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
        "kind": "song",
        "deck": clip.get("deck"),
        "index": clip.get("index"),
        "start_ms": start_ms,
        "trim_start_ms": parse_ms(clip.get("trim_start_ms", clip.get("trim_start", 0)), "clip trim_start"),
        "duration_ms": duration_ms,
        "end_ms": start_ms + duration_ms if duration_ms is not None else None,
        "status": clip.get("status"),
        "gain_db": clip.get("gain_db", 0.0),
        "tempo_shift_pct": clip.get("tempo_shift_pct", 0.0),
        "pitch_shift_semitones": clip.get("pitch_shift_semitones", 0),
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
                "deck": "voice",
                "start_ms": start_ms,
                "duration_ms": None,
                "end_ms": None,
                "text": lean_in.get("text"),
                "voice": lean_in.get("voice"),
                "rate": lean_in.get("rate"),
            }
        )
        for key in ("ducking", "lowpass"):
            if isinstance(lean_in.get(key), dict):
                events.append(automation_payload(lean_in[key], owner=str(lean_in.get("id"))))
        for automation in lean_in.get("effects", []):
            events.append(automation_payload(automation, owner=str(lean_in.get("id"))))
    for automation in payload.get("automations", []):
        events.append(automation_payload(automation))
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
    if event.get("kind") == "slip":
        return "slip/flux"
    return str(event.get("title") or event.get("id") or "untitled")


def display_meta_for_event(event: dict[str, Any]) -> str:
    if event.get("kind") == "automation":
        if event.get("target") == "crossfader":
            return "crossfader motion"
        target = event.get("target") or event.get("owner") or "master"
        return f"{target} | {event.get('param') or 'automation'}"
    if event.get("kind") == "vocal":
        return "mic lean-in"
    if event.get("kind") == "effect":
        target = event.get("target") or "session"
        recipe = f" | {event.get('routine_recipe')}" if event.get("routine_recipe") else ""
        tail_ms = int(event.get("tail_ms") or 0)
        tail = f" | tail {tail_ms / 1000:.1f}s" if tail_ms else ""
        return f"{target}{tail}{recipe}"
    if event.get("kind") == "slip":
        recipe = f" | {event.get('routine_recipe')}" if event.get("routine_recipe") else ""
        return f"{event.get('target_clip_id')} over {event.get('source_clip_id')}{recipe}"
    if event.get("routine_recipe"):
        return f"{event.get('routine_recipe')} routine of {event.get('source_clip_id')}"
    if event.get("planner_role") == "instant-double":
        return f"instant double of {event.get('source_clip_id')}"
    artist_album = " - ".join(str(value) for value in (event.get("artist"), event.get("album")) if value)
    return artist_album or str(event.get("path") or "")


def normalize_event(event: dict[str, Any], playhead_ms: int | None) -> dict[str, Any]:
    start = event_start(event)
    end = event_end(event)
    duration = None if start is None or end is None else max(0, end - start)
    lane = "fader" if event.get("kind") == "automation" and event.get("target") == "crossfader" else str(event.get("deck") or event.get("kind") or "timeline")
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
            "is_timed": start is not None and end is not None,
        }
    )
    return normalized


def lane_rows(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered_lanes: list[str] = []
    for lane in DECK_ORDER:
        ordered_lanes.append(lane)
    for lane in ("voice", "effects", "fader", "automation"):
        ordered_lanes.append(lane)
    for event in events:
        lane = str(event.get("lane") or "timeline")
        if lane not in ordered_lanes:
            ordered_lanes.append(lane)
    return [
        {
            "id": lane,
            "label": lane.replace("-", " "),
            "kind": "deck" if lane.startswith("deck-") else lane,
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
            "fader_routing": session_payload.get("fader_routing", session_payload.get("faderRouting", {})),
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


def choose_state_path(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
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
        print(f"{self.address_string()} - {format % args}")


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
