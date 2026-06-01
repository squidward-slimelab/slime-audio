#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import json
import subprocess
import tempfile
import time
from pathlib import Path

from slime_music_library import connect as connect_library

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = REPO_ROOT / "runtime" / "slime-music-library.sqlite3"
DEFAULT_LOCK = REPO_ROOT / "runtime" / "slime-music-library.lock"
DEFAULT_STATE = REPO_ROOT / "runtime" / "slime-music-library-service.json"


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
    if args.tunebat_backfill_limit > 0:
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
    if args.dj_analysis_backfill_limit > 0:
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh SlimeAudio music library and slowly backfill local TuneBat-style analysis.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--scan", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--tunebat-backfill-limit", type=int, default=12)
    parser.add_argument("--tunebat-max-seconds", type=int, default=1200)
    parser.add_argument("--dj-analysis-backfill-limit", type=int, default=6)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    print(json.dumps(run_with_lock(args), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
