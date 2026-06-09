#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import random
import socket
import subprocess
import sys
import time
import urllib.request
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from slime_audio_candidates import (
    DEFAULT_CONSTRAINTS,
    DEFAULT_HISTORY,
    VIBE_STOP_WORDS,
    candidate_rows,
    constraints_to_payload,
    load_constraints,
)
from slime_audio_session import playlist_to_session_payload, probe_duration_ms, write_payload
from slime_music_library import DEFAULT_DB, connect, normalize

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNTIME = REPO_ROOT / "runtime"
DEFAULT_TARGETS = ["192.168.0.123:47777", "192.168.0.163:47777"]
DEFAULT_MIN_RUNWAY_MS = 35 * 60 * 1000
DEFAULT_MAX_TRACKS = 10
AUTODJ_LOCK = Path("/tmp/slime-audio-autodj.lock")
DEFAULT_SKIP_TERMS = [
    "christmas",
    "copy",
    "disney",
    "8-bit",
    "intermission master",
    "karaoke",
    "kids",
    "lifter",
    "loopmasters",
    "metal gear",
    "ministry of sound",
    "remix everything metal uk",
    "nintendo",
    "ost",
    "soundtrack",
    "soundtracks",
    "tribute",
    "utility",
    "rekordbox",
    "various",
    "various artist",
    "various artista",
    "video game",
]
DEFAULT_QUERY_LANES = [
    "leftfield",
    "techno",
    "breakbeat",
    "dubstep",
    "hip-hop",
    "punk",
    "industrial",
    "experimental",
    "electronic",
    "drum and bass",
    "post-punk",
    "garage",
    "alternative",
    "hardcore",
    "metal",
    "noise",
    "electro",
    "indie",
    "dance-punk",
    "post-hardcore",
]


@dataclass(frozen=True)
class SelectedTrack:
    path: str
    artist: str
    title: str
    album: str
    score: float
    duration_ms: int | None
    last_played_at: str | None
    plays_seen: int
    reasons: list[str]


def iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def slugify(value: str) -> str:
    normalized = normalize(value).replace(" ", "-")
    return "-".join(part for part in normalized.split("-") if part) or "autodj"


def load_state() -> dict[str, Any]:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8765/api/state", timeout=4) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception:
        return {}


def audio_process_alive() -> bool:
    result = subprocess.run(
        ["pgrep", "-f", r"slime_audio_stream.py |slime_audio_session_runner.py|/usr/bin/ffmpeg .* -f s16le .*pipe:1"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def playback_healthy() -> bool:
    state = load_state()
    transport = state.get("transport") or state.get("dashboard", {}).get("transport") or {}
    health = state.get("dashboard", {}).get("health") or {}
    status = transport.get("status") or health.get("runner_status") or ""
    stale = bool(transport.get("stale"))
    return audio_process_alive() and not stale and status not in {"completed", "stopped"}


def acquire_lock() -> int:
    fd = os.open(AUTODJ_LOCK, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("autodj already running")
    return fd


def candidate_pool(args: argparse.Namespace) -> list[dict[str, Any]]:
    constraints = load_constraints(args.constraints)
    conn = connect(args.db)
    seen: set[str] = set()
    pool: list[dict[str, Any]] = []
    queries = [None] if args.include_broad_pool else []
    source_words: list[str] = []
    for word in normalize(f"{constraints.direction} {constraints.notes}").split():
        if len(word) >= 4 and word not in VIBE_STOP_WORDS:
            source_words.append(word)
    queries.extend(source_words[: args.query_count])
    queries.extend(DEFAULT_QUERY_LANES)
    for query in queries:
        rows = candidate_rows(
            conn,
            constraints,
            history_path=args.history,
            recent_limit=args.recent_limit,
            limit=args.pool_per_query,
            query=query,
            pool_limit=args.sql_pool_limit,
            randomize_pool=query is None,
        )
        for row in rows:
            key = str(row.get("duplicate_key") or row.get("preferred_path"))
            if key in seen:
                continue
            seen.add(key)
            pool.append(row)
    pool.sort(key=lambda item: float(item.get("score") or 0.0), reverse=True)
    return pool


def select_tracks(args: argparse.Namespace) -> list[SelectedTrack]:
    pool = candidate_pool(args)
    if not pool:
        raise SystemExit("no candidates available")
    rng = random.SystemRandom()
    artist_counts: Counter[str] = Counter()
    title_counts: Counter[str] = Counter()
    selected: list[SelectedTrack] = []
    runway_ms = 0
    top_window = min(len(pool), max(args.max_tracks * 12, 120))
    ranked = pool[:top_window]
    rng.shuffle(ranked)
    ranked.sort(key=lambda item: float(item.get("score") or 0.0) + rng.uniform(0.0, args.selection_jitter), reverse=True)

    for row in ranked:
        path = str(row.get("preferred_path") or "")
        if not path or not Path(path).exists():
            continue
        haystack = normalize(
            " ".join(str(row.get(key) or "") for key in ("title_guess", "artist_guess", "album_guess", "preferred_path"))
        )
        if any(normalize(term) in haystack for term in args.skip_term):
            continue
        if args.require_analysis and not (row.get("tunebat_bpm") or row.get("tunebat_energy") is not None):
            continue
        if args.min_score is not None and float(row.get("score") or 0.0) < args.min_score:
            continue
        artist = str(row.get("artist_guess") or "").strip()
        artist_key = normalize(artist) or artist.casefold()
        title_key = normalize(str(row.get("title_guess") or ""))
        if artist_key and artist_counts[artist_key] >= args.max_per_artist:
            continue
        if title_key and title_counts[title_key] >= 1:
            continue
        duration_ms = probe_duration_ms(path)
        if duration_ms is not None and duration_ms < args.min_track_ms:
            continue
        selected.append(
            SelectedTrack(
                path=path,
                artist=artist,
                title=str(row.get("title_guess") or Path(path).stem),
                album=str(row.get("album_guess") or ""),
                score=float(row.get("score") or 0.0),
                duration_ms=duration_ms,
                last_played_at=row.get("last_played_at"),
                plays_seen=int(row.get("plays_seen") or 0),
                reasons=[str(reason) for reason in row.get("reasons") or []],
            )
        )
        if artist_key:
            artist_counts[artist_key] += 1
        if title_key:
            title_counts[title_key] += 1
        runway_ms += duration_ms or args.default_track_ms
        if len(selected) >= args.max_tracks or runway_ms >= args.min_runway_ms:
            break

    if len(selected) < args.min_tracks and runway_ms < args.min_runway_ms:
        raise SystemExit(
            f"only selected {len(selected)} tracks / {round(runway_ms / 60000, 1)} min; refusing weak autodj set"
        )
    return selected


def session_payload(selected: list[SelectedTrack], args: argparse.Namespace) -> dict[str, Any]:
    payload = playlist_to_session_payload(
        [track.path for track in selected],
        start_ms=0,
        decks=["deck-1", "deck-2", "deck-3", "deck-4"],
        gap_ms=0,
        overlap_ms=args.base_overlap_ms,
        default_duration_ms=args.default_track_ms,
        probe=True,
    )
    for index, clip in enumerate(payload.get("clips", [])):
        clip["fade_in_ms"] = 0 if index == 0 else args.fade_in_ms
        clip["fade_out_ms"] = args.fade_out_ms
    payload["title"] = args.title
    payload["notes"] = {
        "created_at": iso_now(),
        "intent": args.intent,
        "selection_process": "database candidates plus play-history freshness penalties; no hardcoded tracks",
        "tracks": [asdict(track) for track in selected],
    }
    return payload


def run_planner(session_path: Path, args: argparse.Namespace) -> dict[str, Any]:
    command = [
        sys.executable,
        "scripts/slime_audio_mix_planner.py",
        "--session",
        str(session_path),
        "--cached-analysis-only",
        "--routine-every",
        str(args.routine_every),
        "--no-routines",
        "--apply",
    ]
    result = subprocess.run(command, cwd=REPO_ROOT, text=True, capture_output=True, check=False)
    return {"command": command, "returncode": result.returncode, "stdout": result.stdout[-4000:], "stderr": result.stderr[-4000:]}


def load_session_payload(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def clip_start_ms(clip: dict[str, Any]) -> int:
    return int(clip.get("start_ms", clip.get("start", 0)) or 0)


def clip_duration_ms(clip: dict[str, Any]) -> int:
    return int(clip.get("duration_ms", clip.get("duration", 0)) or 0)


def clip_end_ms(clip: dict[str, Any]) -> int:
    return clip_start_ms(clip) + clip_duration_ms(clip)


def run_session_edit(command_args: list[str]) -> dict[str, Any]:
    command = [sys.executable, "scripts/slime_audio_session.py", *command_args]
    result = subprocess.run(command, cwd=REPO_ROOT, text=True, capture_output=True, check=False)
    return {
        "command": command,
        "returncode": result.returncode,
        "stdout": result.stdout[-2000:],
        "stderr": result.stderr[-2000:],
    }


def creative_source_score(clip: dict[str, Any]) -> int:
    haystack = normalize(" ".join(str(clip.get(key) or "") for key in ("id", "path")))
    score = 0
    for word in ("techno", "breakbeat", "dubstep", "industrial", "function", "jungle", "garage"):
        if word in haystack:
            score += 2
    if clip_duration_ms(clip) >= 120_000:
        score += 1
    return score


def vocal_target_score(clip: dict[str, Any]) -> int:
    haystack = normalize(" ".join(str(clip.get(key) or "") for key in ("id", "path")))
    score = 0
    for word in ("vocal", "rap", "hip-hop", "punk", "song", "feat", "with"):
        if word in haystack:
            score += 1
    for word in ("instrumental", "withoutvocals", "bed", "techno", "breakbeat"):
        if word in haystack:
            score -= 2
    if clip_duration_ms(clip) >= 90_000:
        score += 1
    return score


def apply_creative_pass(session_path: Path, args: argparse.Namespace) -> dict[str, Any]:
    if args.no_creative_pass:
        return {"required": False, "moves": [], "skipped": "disabled"}

    payload = load_session_payload(session_path)
    clips = sorted(
        [clip for clip in payload.get("clips", []) if clip.get("id") and clip.get("path") and clip_duration_ms(clip) > 0],
        key=clip_start_ms,
    )
    moves: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    if len(clips) < 3:
        raise SystemExit("creative pass needs at least 3 clips")

    # Crossfader movement across future handoffs: a basic DJ gesture that every
    # unattended set should have, even when deeper beatgrid metadata is sparse.
    points: list[dict[str, float | int]] = []
    for incoming in clips[1:4]:
        start = clip_start_ms(incoming)
        points.extend(
            [
                {"at_ms": max(0, start - 8_000), "value": -0.25},
                {"at_ms": start + 2_000, "value": 0.25},
                {"at_ms": start + 12_000, "value": 0.0},
            ]
        )
    result = run_session_edit(["crossfader", str(session_path), "--points-json", json.dumps(points)])
    if result["returncode"] == 0:
        moves.append({"kind": "crossfader-motion", "result": result})
    else:
        failures.append({"kind": "crossfader-motion", "result": result})

    echo_target = next((clip for clip in clips[1:] if clip_duration_ms(clip) >= 70_000), clips[1])
    echo_start = max(clip_start_ms(echo_target), clip_end_ms(echo_target) - 12_000)
    result = run_session_edit(
        [
            "add-effect",
            str(session_path),
            "--id",
            f"autodj-echo-{echo_target['id']}",
            "--type",
            "echo",
            "--target",
            str(echo_target["id"]),
            "--start",
            str(echo_start),
            "--duration",
            "2500",
            "--tail-ms",
            "4500",
            "--wet",
            "0.34",
            "--gain-db",
            "-6",
            "--delay-ms",
            "330",
            "--feedback",
            "0.42",
        ]
    )
    if result["returncode"] == 0:
        moves.append({"kind": "echo-exit", "target": echo_target["id"], "result": result})
    else:
        failures.append({"kind": "echo-exit", "target": echo_target["id"], "result": result})

    bed_candidates = [clip for clip in clips if creative_source_score(clip) > 0]
    bed_source = max(bed_candidates or clips, key=creative_source_score)
    targets = [clip for clip in clips[1:] if str(clip.get("id")) != str(bed_source.get("id"))]
    target = max(targets, key=vocal_target_score) if targets else clips[-1]
    bed_start = clip_start_ms(target) + min(30_000, max(0, clip_duration_ms(target) // 4))
    bed_duration = min(96_000, max(32_000, clip_end_ms(target) - bed_start - 2_000))
    if bed_duration >= 32_000:
        bed_id = f"autodj-bed-{slugify(str(bed_source['id']))[:32]}"
        result = run_session_edit(
            [
                "add-clip",
                str(session_path),
                "--id",
                bed_id,
                "--deck",
                "deck-4",
                "--path",
                str(bed_source["path"]),
                "--start",
                str(bed_start),
                "--trim-start",
                str(min(60_000, max(0, clip_duration_ms(bed_source) - bed_duration - 1_000))),
                "--duration",
                str(bed_duration),
                "--gain-db",
                "-6",
                "--fade-in-ms",
                "3000",
                "--fade-out-ms",
                "1500",
            ]
        )
        if result["returncode"] == 0:
            moves.append({"kind": "rhythm-bed", "source": bed_source["id"], "target": target["id"], "result": result})
            bed_result = run_session_edit(
                [
                    "mashup-bed",
                    str(session_path),
                    "--bed-id",
                    bed_id,
                    "--start",
                    str(bed_start),
                    "--end",
                    str(bed_start + bed_duration),
                    "--gain-db",
                    "-6",
                    "--lowpass-hz",
                    "1800",
                    "--highpass-hz",
                    "90",
                ]
            )
            if bed_result["returncode"] == 0:
                moves.append({"kind": "bed-filter-carve", "source": bed_id, "result": bed_result})
            else:
                failures.append({"kind": "bed-filter-carve", "source": bed_id, "result": bed_result})
        else:
            failures.append({"kind": "rhythm-bed", "source": bed_source["id"], "target": target["id"], "result": result})

    validate = subprocess.run(
        [sys.executable, "scripts/slime_audio_session.py", "validate", str(session_path)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if validate.returncode != 0:
        raise SystemExit(validate.stderr or validate.stdout or "creative session validation failed")
    if len(moves) < args.min_creative_moves:
        raise SystemExit(f"creative pass only added {len(moves)} move(s); refusing playlist-only autodj set")
    return {
        "required": True,
        "moves": moves,
        "failures": failures,
        "validate": {"returncode": validate.returncode, "stdout": validate.stdout[-2000:], "stderr": validate.stderr[-2000:]},
    }


def launch_runner(session_path: Path, state_path: Path, args: argparse.Namespace) -> int:
    log_path = args.runtime / f"{args.slug}-runner.log"
    pid_path = args.runtime / f"{args.slug}-runner.pid"
    command = [
        sys.executable,
        "scripts/slime_audio_session_runner.py",
        "--session",
        str(session_path),
        "--state",
        str(state_path),
        "--dashboard-title",
        args.title,
        "--dashboard-slug",
        args.slug,
        "--mode",
        "snapcast",
        "--backend",
        "ffmpeg",
        "--window-ms",
        str(args.window_ms),
        "--prerender-lead-ms",
        str(args.prerender_lead_ms),
        "--discover-timeout-ms",
        str(args.discover_timeout_ms),
        "--reset-state",
    ]
    for target in args.target:
        command.extend(["--target", target])
    log = log_path.open("ab")
    process = subprocess.Popen(command, cwd=REPO_ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    pid_path.write_text(f"{process.pid}\n", encoding="utf-8")
    return process.pid


def continue_set(args: argparse.Namespace) -> int:
    args.runtime.mkdir(parents=True, exist_ok=True)
    lock_fd = acquire_lock()
    try:
        if not args.force and playback_healthy():
            print(json.dumps({"status": "ok", "reason": "playback healthy; not stomping"}))
            return 0

        selected = select_tracks(args)
        session_path = args.runtime / f"{args.slug}.json"
        state_path = args.runtime / f"{args.slug}-state.json"
        plan_path = args.runtime / f"{args.slug}-plan.json"
        payload = session_payload(selected, args)
        write_payload(session_path, payload)
        planner = run_planner(session_path, args)
        creative = apply_creative_pass(session_path, args)
        validate = subprocess.run(
            [sys.executable, "scripts/slime_audio_session.py", "validate", str(session_path)],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        if validate.returncode != 0:
            raise SystemExit(validate.stderr or validate.stdout or "session validation failed")
        pid = None if args.dry_run else launch_runner(session_path, state_path, args)
        plan = {
            "created_at": iso_now(),
            "status": "dry_run" if args.dry_run else "started",
            "pid": pid,
            "session": str(session_path),
            "state": str(state_path),
            "title": args.title,
            "slug": args.slug,
            "intent": args.intent,
            "planner": planner,
            "creative": creative,
            "tracks": [asdict(track) for track in selected],
        }
        plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(plan, indent=2, sort_keys=True))
        return 0
    finally:
        os.close(lock_fd)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Start a normal database-backed SlimeAudio DJ continuation set.")
    sub = parser.add_subparsers(dest="command", required=True)
    cont = sub.add_parser("continue")
    cont.add_argument("--db", type=Path, default=DEFAULT_DB)
    cont.add_argument("--constraints", type=Path, default=DEFAULT_CONSTRAINTS)
    cont.add_argument("--history", type=Path, default=DEFAULT_HISTORY)
    cont.add_argument("--runtime", type=Path, default=DEFAULT_RUNTIME)
    cont.add_argument("--title", default=f"Autodj Continuation {time.strftime('%Y-%m-%d %H%M')}")
    cont.add_argument("--slug", default=f"autodj-continuation-{time.strftime('%Y%m%d-%H%M%S')}")
    cont.add_argument("--intent", default="continue the room from fresh database-backed candidates without hardcoded tracks")
    cont.add_argument("--target", action="append", default=list(DEFAULT_TARGETS))
    cont.add_argument("--min-runway-ms", type=int, default=DEFAULT_MIN_RUNWAY_MS)
    cont.add_argument("--min-tracks", type=int, default=5)
    cont.add_argument("--max-tracks", type=int, default=DEFAULT_MAX_TRACKS)
    cont.add_argument("--max-per-artist", type=int, default=1)
    cont.add_argument("--recent-limit", type=int, default=120)
    cont.add_argument("--pool-per-query", type=int, default=60)
    cont.add_argument("--sql-pool-limit", type=int, default=600)
    cont.add_argument("--query-count", type=int, default=0)
    cont.add_argument("--include-broad-pool", action="store_true")
    cont.add_argument("--selection-jitter", type=float, default=0.12)
    cont.add_argument("--skip-term", action="append", default=list(DEFAULT_SKIP_TERMS))
    cont.add_argument("--require-analysis", action=argparse.BooleanOptionalAction, default=False)
    cont.add_argument("--min-score", type=float, default=0.20)
    cont.add_argument("--default-track-ms", type=int, default=240_000)
    cont.add_argument("--min-track-ms", type=int, default=90_000)
    cont.add_argument("--base-overlap-ms", type=int, default=8_000)
    cont.add_argument("--fade-in-ms", type=int, default=2_500)
    cont.add_argument("--fade-out-ms", type=int, default=5_000)
    cont.add_argument("--routine-every", type=int, default=3)
    cont.add_argument("--min-creative-moves", type=int, default=2)
    cont.add_argument("--no-creative-pass", action="store_true")
    cont.add_argument("--window-ms", type=int, default=180_000)
    cont.add_argument("--prerender-lead-ms", type=int, default=60_000)
    cont.add_argument("--discover-timeout-ms", type=int, default=4000)
    cont.add_argument("--force", action="store_true")
    cont.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "continue":
        return continue_set(args)
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
