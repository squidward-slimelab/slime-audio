#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import json
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path

from slime_music_library import connect as connect_library

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = REPO_ROOT / "runtime" / "slime-music-library.sqlite3"
DEFAULT_LOCK = REPO_ROOT / "runtime" / "slime-music-library.lock"
DEFAULT_STATE = REPO_ROOT / "runtime" / "slime-music-library-service.json"
DEFAULT_ACTIVE_SET = REPO_ROOT / "runtime" / "active-set.json"


def run_json(command: list[str]) -> dict:
    completed = subprocess.run(command, cwd=REPO_ROOT, text=True, capture_output=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or f"command failed rc={completed.returncode}: {' '.join(command)}")
    return json.loads(completed.stdout)


def atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def parse_timestamp(value: object) -> float | None:
    if not isinstance(value, str) or not value.strip():
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


def active_playback_state_path(active_pointer_path: Path) -> Path | None:
    pointer = load_json(active_pointer_path)
    state_path = pointer.get("active_state_path")
    if isinstance(state_path, str) and state_path:
        return Path(state_path)
    fallback = REPO_ROOT / "runtime" / "mix-session-state.json"
    return fallback if fallback.exists() else None


def live_playback_active(state_path: Path | None, grace_seconds: int) -> bool:
    if state_path is None:
        return False
    state = load_json(state_path)
    if not state or state.get("completed_at"):
        return False
    if not (state.get("current") or state.get("window_started_at")):
        return False
    updated_candidates = [
        parse_timestamp(state.get("runner_updated_at")),
        parse_timestamp(state.get("updated_at")),
        parse_timestamp(state.get("window_started_at")),
    ]
    latest = max((value for value in updated_candidates if value is not None), default=None)
    if latest is None:
        return True
    return time.time() <= latest + grace_seconds


def missing_dj_analysis_paths(db_path: Path, limit: int) -> list[Path]:
    if limit <= 0 or not db_path.exists():
        return []
    conn = connect_library(db_path)
    rows = conn.execute(
        """
        SELECT preferred_path
        FROM tracks
        ORDER BY artist_guess, title_guess, preferred_path
        """
    ).fetchall()
    selected: list[Path] = []
    for row in rows:
        path = Path(str(row["preferred_path"]))
        if not path.exists():
            continue
        stat = path.stat()
        identity_path = str(path.resolve())
        analysis = conn.execute(
            """
            SELECT file_size, file_mtime_ns
            FROM track_dj_analysis
            WHERE path = ?
            """,
            (identity_path,),
        ).fetchone()
        if analysis is None or int(analysis["file_size"]) != stat.st_size or int(analysis["file_mtime_ns"]) != stat.st_mtime_ns:
            selected.append(path)
        if len(selected) >= limit:
            break
    conn.close()
    return selected


def run_once(args: argparse.Namespace) -> dict:
    library = str(REPO_ROOT / "scripts" / "slime_music_library.py")
    dj = str(REPO_ROOT / "scripts" / "slime_audio_dj.py")
    result: dict = {"started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "scan": None, "tunebat_backfill": None, "dj_analysis_backfill": None}
    if args.scan:
        result["scan"] = run_json(["python3", library, "--db", str(args.db), "scan"])
    live_state_path = active_playback_state_path(args.active_pointer)
    live_active = args.skip_backfill_when_live and live_playback_active(live_state_path, args.live_grace_seconds)
    result["live_playback"] = {"active": live_active, "state": str(live_state_path) if live_state_path is not None else None}
    if live_active:
        result["tunebat_backfill"] = {"skipped": True, "reason": "live playback active"}
        result["dj_analysis_backfill"] = {"skipped": True, "reason": "live playback active"}
    elif args.tunebat_backfill_limit > 0:
        result["tunebat_backfill"] = run_json(
            [
                "python3",
                library,
                "--db",
                str(args.db),
                "backfill-tunebat-local",
                "--limit",
                str(args.tunebat_backfill_limit),
                "--max-seconds",
                str(args.tunebat_max_seconds),
            ]
        )
    if not live_active and args.dj_analysis_backfill_limit > 0:
        paths = missing_dj_analysis_paths(args.db, args.dj_analysis_backfill_limit)
        if paths:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
                playlist = Path(handle.name)
                handle.write("\n".join(str(path) for path in paths))
                handle.write("\n")
            try:
                analyses = run_json(["python3", dj, "structure", "--playlist", str(playlist), "--db", str(args.db)])
            finally:
                playlist.unlink(missing_ok=True)
            result["dj_analysis_backfill"] = {"requested": len(paths), "analyzed": len(analyses)}
        else:
            result["dj_analysis_backfill"] = {"requested": 0, "analyzed": 0}
    result["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    atomic_write_json(args.state, result)
    return result


def run_with_lock(args: argparse.Namespace) -> dict:
    args.lock.parent.mkdir(parents=True, exist_ok=True)
    with args.lock.open("w", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"skipped": True, "reason": "already running", "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        return run_once(args)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh SlimeAudio music library and slowly backfill local TuneBat-style analysis.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--scan", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--tunebat-backfill-limit", type=int, default=12)
    parser.add_argument("--tunebat-max-seconds", type=int, default=1200)
    parser.add_argument("--dj-analysis-backfill-limit", type=int, default=6)
    parser.add_argument("--active-pointer", type=Path, default=DEFAULT_ACTIVE_SET)
    parser.add_argument("--skip-backfill-when-live", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument(
        "--live-grace-seconds",
        type=int,
        default=21_600,
        help="Treat active runner state as live for this many quiet seconds before allowing expensive backfills.",
    )
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()
    print(json.dumps(run_with_lock(args), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
