#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import json
import sys
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from slime_audio_tts import convert_to_wav, send_wav, synthesize


@dataclass(frozen=True)
class Target:
    host: str
    port: int


@dataclass(frozen=True)
class Drop:
    id: str
    text: str
    offset_ms: int
    track_uri: str | None = None
    track_id: str | None = None
    track_name: str | None = None
    voice: str | None = None
    rate: str | None = None
    volume: float | None = None


@dataclass(frozen=True)
class DropPlan:
    targets: list[Target]
    drops: list[Drop]
    voice: str = "en-US-GuyNeural"
    rate: str = "-12%"
    volume: float = 1.0
    lead_ms: int = 8000
    late_tolerance_ms: int = 1200
    poll_ms: int = 5000
    max_poll_ms: int = 30000
    delay_pad_ms: int = 250
    require_known_progress: bool = True


class ProgressClock:
    """Keeps usable progress when Spotify reports pause/track but stale progress_ms."""

    def __init__(self) -> None:
        self._track_key: str | None = None
        self._base_progress_ms = 0
        self._base_time = time.monotonic()
        self._was_playing = False
        self._progress_known = False

    def update(self, status: dict[str, Any]) -> dict[str, Any]:
        now = time.monotonic()
        item = status.get("item") if isinstance(status, dict) else None
        track_key = track_identity(item)
        is_playing = bool(status.get("is_playing"))
        raw_progress_ms = int(status.get("progress_ms") or 0)

        saw_track_change = self._track_key is not None and track_key != self._track_key

        if track_key != self._track_key:
            self._track_key = track_key
            self._base_progress_ms = raw_progress_ms
            self._base_time = now
            self._progress_known = raw_progress_ms > 0 or saw_track_change
        elif not is_playing:
            self._base_progress_ms = self.estimated_progress(now)
            self._base_time = now
        elif not self._was_playing:
            self._base_progress_ms = raw_progress_ms if raw_progress_ms > 0 else self._base_progress_ms
            self._base_time = now
            self._progress_known = self._progress_known or raw_progress_ms > 0
        elif raw_progress_ms > self._base_progress_ms + 750:
            self._base_progress_ms = raw_progress_ms
            self._base_time = now
            self._progress_known = True

        self._was_playing = is_playing
        enriched = dict(status)
        enriched["progress_ms"] = self.estimated_progress(now) if is_playing else self._base_progress_ms
        enriched["progress_source"] = "spotify" if raw_progress_ms > 0 else "estimated"
        enriched["progress_known"] = self._progress_known
        return enriched

    def estimated_progress(self, now: float | None = None) -> int:
        now = time.monotonic() if now is None else now
        if not self._was_playing:
            return self._base_progress_ms
        return self._base_progress_ms + int((now - self._base_time) * 1000)


def track_identity(item: Any) -> str | None:
    if not isinstance(item, dict):
        return None
    return str(item.get("uri") or item.get("id") or item.get("name") or "") or None


def parse_offset_ms(value: Any) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("drop offset must be milliseconds or a time string")

    text = value.strip()
    if text.isdigit():
        return int(text)

    parts = text.split(":")
    if len(parts) not in {2, 3}:
        raise ValueError(f"invalid time offset: {value}")

    seconds = float(parts[-1])
    minutes = int(parts[-2])
    hours = int(parts[0]) if len(parts) == 3 else 0
    return int(round(((hours * 3600) + (minutes * 60) + seconds) * 1000))


def parse_target(value: str, default_port: int = 47777) -> Target:
    if ":" not in value:
        return Target(value, default_port)
    host, port = value.rsplit(":", 1)
    return Target(host, int(port))


def parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on"}
    return bool(value)


def load_plan(path: Path) -> DropPlan:
    payload = json.loads(path.read_text())
    target_values = payload.get("targets") or ([payload["target"]] if payload.get("target") else [])
    targets = [parse_target(str(target)) for target in target_values]
    if not targets:
        raise ValueError("plan must include target or targets")

    drops = []
    for index, item in enumerate(payload.get("drops") or []):
        text = str(item.get("text") or "").strip()
        if not text:
            raise ValueError(f"drop {index} is missing text")
        offset = item.get("offset_ms", item.get("at"))
        if offset is None:
            raise ValueError(f"drop {index} is missing at or offset_ms")
        track_uri = item.get("track_uri") or item.get("uri")
        track_id = item.get("track_id")
        if track_uri and not track_id and str(track_uri).startswith("spotify:track:"):
            track_id = str(track_uri).split(":")[-1]
        drops.append(
            Drop(
                id=str(item.get("id") or f"drop-{index + 1}"),
                text=text,
                offset_ms=parse_offset_ms(offset),
                track_uri=str(track_uri) if track_uri else None,
                track_id=str(track_id) if track_id else None,
                track_name=str(item["track_name"]) if item.get("track_name") else None,
                voice=str(item["voice"]) if item.get("voice") else None,
                rate=str(item["rate"]) if item.get("rate") else None,
                volume=float(item["volume"]) if item.get("volume") is not None else None,
            )
        )

    if not drops:
        raise ValueError("plan must include at least one drop")

    return DropPlan(
        targets=targets,
        drops=sorted(drops, key=lambda drop: drop.offset_ms),
        voice=str(payload.get("voice") or "en-US-GuyNeural"),
        rate=str(payload.get("rate") or "-12%"),
        volume=float(payload.get("volume", 1.0)),
        lead_ms=int(payload.get("lead_ms", 8000)),
        late_tolerance_ms=int(payload.get("late_tolerance_ms", 1200)),
        poll_ms=max(3000, int(payload.get("poll_ms", 5000))),
        max_poll_ms=max(5000, int(payload.get("max_poll_ms", 30000))),
        delay_pad_ms=int(payload.get("delay_pad_ms", 250)),
        require_known_progress=parse_bool(payload.get("require_known_progress"), True),
    )


def status_matches_drop(status: dict[str, Any], drop: Drop) -> bool:
    item = status.get("item") if isinstance(status, dict) else None
    if not isinstance(item, dict):
        return False

    if drop.track_uri and item.get("uri") == drop.track_uri:
        return True
    if drop.track_id and item.get("id") == drop.track_id:
        return True
    if drop.track_name and str(item.get("name") or "").casefold() == drop.track_name.casefold():
        return True
    return not any([drop.track_uri, drop.track_id, drop.track_name])


def due_delay_ms(status: dict[str, Any], drop: Drop, *, lead_ms: int, late_tolerance_ms: int, delay_pad_ms: int) -> int | None:
    if not status.get("is_playing") or not status_matches_drop(status, drop):
        return None

    progress_ms = int(status.get("progress_ms") or 0)
    remaining_ms = drop.offset_ms - progress_ms
    if remaining_ms > lead_ms:
        return None
    if remaining_ms < -late_tolerance_ms:
        return None
    return max(0, remaining_ms + delay_pad_ms)


def log_event(message: str) -> None:
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {message}", file=sys.stderr, flush=True)


def read_spotify_status(spogo: str) -> dict[str, Any] | None:
    try:
        completed = subprocess.run(
            [spogo, "--json", "--no-color", "status"],
            check=False,
            capture_output=True,
            text=True,
            timeout=12,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log_event(f"spotify status command failed: {exc}")
        return None
    if completed.returncode != 0 or not completed.stdout.strip():
        error = (completed.stderr or completed.stdout).strip()
        log_event(f"spotify status failed rc={completed.returncode} {error[:200]}")
        return None
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        log_event(f"spotify status returned invalid json: {exc}")
        return None


async def prepare_drop(drop: Drop, plan: DropPlan, directory: Path) -> Path:
    mp3_path = directory / f"{drop.id}.mp3"
    wav_path = directory / f"{drop.id}.wav"
    await synthesize(drop.text, mp3_path, drop.voice or plan.voice, drop.rate or plan.rate)
    convert_to_wav(mp3_path, wav_path, drop.volume if drop.volume is not None else plan.volume)
    return wav_path


def send_drop(wav_path: Path, targets: list[Target], delay_ms: int) -> bool:
    ok = True
    for target in targets:
        try:
            send_wav(wav_path, target.host, target.port, delay_ms)
        except OSError as exc:
            ok = False
            log_event(f"send failed target={target.host}:{target.port} error={exc}")
    return ok


async def run_plan(plan: DropPlan, spogo: str, max_minutes: float | None) -> int:
    fired: set[str] = set()
    deadline = None if max_minutes is None else time.monotonic() + (max_minutes * 60)
    clock = ProgressClock()
    next_poll_ms = plan.poll_ms

    with tempfile.TemporaryDirectory(prefix="slime-audio-drops-") as tmp:
        directory = Path(tmp)
        log_event(f"preparing {len(plan.drops)} drops for {len(plan.targets)} targets")
        prepared = {drop.id: await prepare_drop(drop, plan, directory) for drop in plan.drops}
        log_event(f"running drops poll_ms={plan.poll_ms} max_poll_ms={plan.max_poll_ms}")

        while len(fired) < len(plan.drops):
            if deadline is not None and time.monotonic() > deadline:
                break

            raw_status = read_spotify_status(spogo)
            if raw_status is None:
                log_event(f"backing off spotify status polling to {min(plan.max_poll_ms, next_poll_ms * 2)}ms")
                await asyncio.sleep(next_poll_ms / 1000)
                next_poll_ms = min(plan.max_poll_ms, next_poll_ms * 2)
                continue
            next_poll_ms = plan.poll_ms
            status = clock.update(raw_status)
            item = status.get("item") or {}
            if status.get("is_playing") and plan.require_known_progress and not status.get("progress_known"):
                log_event(
                    "waiting for reliable song clock "
                    f"track={item.get('name')} uri={item.get('uri')} source={status.get('progress_source')}"
                )
                await asyncio.sleep(next_poll_ms / 1000)
                continue

            for drop in plan.drops:
                if drop.id in fired:
                    continue
                delay_ms = due_delay_ms(
                    status,
                    drop,
                    lead_ms=plan.lead_ms,
                    late_tolerance_ms=plan.late_tolerance_ms,
                    delay_pad_ms=plan.delay_pad_ms,
                )
                if delay_ms is None:
                    continue
                log_event(f"firing drop={drop.id} delay_ms={delay_ms} progress_ms={status.get('progress_ms')} track={item.get('name')}")
                if send_drop(prepared[drop.id], plan.targets, delay_ms):
                    fired.add(drop.id)

            await asyncio.sleep(next_poll_ms / 1000)

    print(json.dumps({"ok": len(fired) == len(plan.drops), "fired": sorted(fired), "remaining": len(plan.drops) - len(fired)}))
    return 0 if len(fired) == len(plan.drops) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Run timed SlimeAudio sample drops against Spotify playback.")
    parser.add_argument("--plan", required=True, help="JSON plan with targets and drops")
    parser.add_argument("--spogo", default="spogo")
    parser.add_argument("--max-minutes", type=float, default=None)
    args = parser.parse_args()

    return asyncio.run(run_plan(load_plan(Path(args.plan)), args.spogo, args.max_minutes))


if __name__ == "__main__":
    raise SystemExit(main())
