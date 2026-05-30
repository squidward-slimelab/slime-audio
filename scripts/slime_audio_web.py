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

from slime_audio_session import load_payload, parse_ms, parse_session

REPO_ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = REPO_ROOT / "web" / "slime-audio"
DEFAULT_STATE = REPO_ROOT / "runtime" / "playlist-state.json"
DEFAULT_SESSION = REPO_ROOT / "runtime" / "mix-session.json"


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


def playlist_state_to_session(state: dict[str, Any]) -> dict[str, Any]:
    order = list(state.get("order") or [])
    completed = set(state.get("completed") or [])
    current = state.get("current")
    failed = set(state.get("failed") or [])
    current_index = int(state.get("index", 0) or 0)
    clips: list[dict[str, Any]] = []
    for index, track in enumerate(order):
        if track in failed:
            status = "failed"
        elif track == current:
            status = "current"
        elif track in completed or index < current_index:
            status = "done"
        else:
            status = "planned"
        clips.append(
            {
                "id": f"track-{index + 1}",
                "deck": "deck-1",
                "path": str(track),
                "status": status,
                "index": index,
                "resolved_path": state.get("resolved_current") if track == current else None,
            }
        )
    return {
        "version": 1,
        "source": "playlist-runner-state",
        "source_state": state,
        "decks": ["deck-3", "deck-1", "deck-2", "deck-4"],
        "clips": clips,
        "mic_lean_ins": [],
        "automations": [],
    }


def legacy_clip_event(clip: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": clip.get("id"),
        "kind": "song",
        "deck": clip.get("deck"),
        "index": clip.get("index"),
        "start_ms": None,
        "trim_start_ms": None,
        "duration_ms": None,
        "end_ms": None,
        "status": clip.get("status"),
        "resolved_path": clip.get("resolved_path"),
        **format_title(str(clip.get("path") or "")),
    }


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
        **format_title(str(clip.get("path") or "")),
    }


def session_events(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if payload.get("source") != "playlist-runner-state":
        parse_session(payload)
    events: list[dict[str, Any]] = []
    for clip in payload.get("clips", []):
        event = legacy_clip_event(clip) if payload.get("source") == "playlist-runner-state" else session_clip_event(clip)
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
    kind_order = {"song": 0, "vocal": 1, "automation": 2}
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


def now_from_state(state: dict[str, Any], session_payload: dict[str, Any]) -> dict[str, Any]:
    current = state.get("current")
    started_at = parse_timestamp(state.get("started_at"))
    elapsed_ms = max(0, int(round((time.time() - started_at) * 1000))) if started_at is not None and current else None
    current_event = next(
        (
            event
            for event in session_events(session_payload)
            if event.get("status") == "current" or (current and event.get("path") == current)
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


def load_dashboard_state(state_path: Path, session_path: Path | None) -> dict[str, Any]:
    state = load_payload(state_path) if state_path.exists() else {}
    if session_path and session_path.exists():
        session_payload = load_payload(session_path)
        session_source = str(session_path)
    else:
        session_payload = playlist_state_to_session(state)
        session_source = str(state_path)
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "state_path": str(state_path),
        "now": now_from_state(state, session_payload),
        "session": {
            "path": session_source,
            "raw": session_payload,
            "events": session_events(session_payload),
        },
    }


def choose_state_path(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    candidates = sorted(
        (path for path in (REPO_ROOT / "runtime").glob("*state.json") if path.is_file()),
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
        self.serve_static(parsed.path)

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
