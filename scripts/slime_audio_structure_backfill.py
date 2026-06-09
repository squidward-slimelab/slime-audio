#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any

from slime_audio_candidates import DEFAULT_CONSTRAINTS, DEFAULT_HISTORY, candidate_rows, load_constraints
from slime_audio_dj import (
    DEFAULT_CACHE,
    DEFAULT_LIBRARY_DB,
    DEFAULT_TUNEBAT_LOCAL_ANALYZER,
    TrackAnalysis,
    analyze_with_cache,
    coerce_structure,
    load_analysis_from_db,
)
from slime_music_library import connect

DEFAULT_QUERY_LANES = [
    None,
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


def has_usable_anchor(analysis: TrackAnalysis | None, *, min_anchor_ms: int) -> bool:
    if analysis is None:
        return False
    for window in coerce_structure(analysis.structure):
        if window.kind == "outro":
            continue
        if window.confidence < 0.45:
            continue
        if window.end_ms <= window.start_ms:
            continue
        if window.end_ms - window.start_ms >= min_anchor_ms:
            return True
    return False


def connect_with_retries(args: argparse.Namespace):
    for attempt in range(args.db_retries + 1):
        try:
            return connect(args.db)
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt >= args.db_retries:
                raise
            time.sleep(args.db_retry_sleep_s)
    raise RuntimeError("unreachable database retry state")


def candidate_paths(args: argparse.Namespace) -> list[Path]:
    constraints = load_constraints(args.constraints)
    conn = connect_with_retries(args)
    seen: set[str] = set()
    paths: list[Path] = []
    for query in DEFAULT_QUERY_LANES[: args.query_lanes]:
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
            path = Path(str(row.get("preferred_path") or ""))
            key = str(row.get("duplicate_key") or path)
            if not path.exists() or key in seen:
                continue
            seen.add(key)
            analysis = load_analysis_from_db(args.db, path)
            if not args.include_existing and has_usable_anchor(analysis, min_anchor_ms=args.min_anchor_ms):
                continue
            paths.append(path)
            if len(paths) >= args.limit:
                return paths
    return paths


def summarize(analyses: list[TrackAnalysis]) -> dict[str, Any]:
    kinds: Counter[str] = Counter()
    usable_paths = 0
    for analysis in analyses:
        usable = False
        for window in coerce_structure(analysis.structure):
            kinds[window.kind] += 1
            if window.kind != "outro" and window.confidence >= 0.45 and window.end_ms > window.start_ms:
                usable = True
        if usable:
            usable_paths += 1
    return {
        "analyzed": len(analyses),
        "usable_paths": usable_paths,
        "structure_kinds": dict(sorted(kinds.items())),
        "tracks": [
            {
                "path": analysis.path,
                "bpm": analysis.bpm,
                "key": analysis.key,
                "camelot": analysis.camelot,
                "structure": [asdict(window) for window in coerce_structure(analysis.structure)],
            }
            for analysis in analyses
        ],
    }


def analyze_paths_with_retries(paths: list[Path], args: argparse.Namespace) -> tuple[list[TrackAnalysis], list[dict[str, str]]]:
    analyses: list[TrackAnalysis] = []
    failures: list[dict[str, str]] = []
    for path in paths:
        for attempt in range(args.db_retries + 1):
            try:
                analyses.extend(analyze_with_cache([path], args.cache, args.backend, args.sample_rate, args.db, args.tunebat_analyzer))
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt >= args.db_retries:
                    failures.append({"path": str(path), "error": str(exc)})
                    break
                time.sleep(args.db_retry_sleep_s)
    return analyses, failures


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill local SlimeAudio DJ structure analysis for future autodj/watchdog sets.")
    parser.add_argument("--db", type=Path, default=DEFAULT_LIBRARY_DB)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--constraints", type=Path, default=DEFAULT_CONSTRAINTS)
    parser.add_argument("--history", type=Path, default=DEFAULT_HISTORY)
    parser.add_argument("--backend", choices=["auto", "ffmpeg"], default="ffmpeg")
    parser.add_argument("--sample-rate", type=int, default=44_100)
    parser.add_argument("--tunebat-analyzer", type=Path, default=DEFAULT_TUNEBAT_LOCAL_ANALYZER)
    parser.add_argument("--limit", type=int, default=40)
    parser.add_argument("--pool-per-query", type=int, default=80)
    parser.add_argument("--sql-pool-limit", type=int, default=1200)
    parser.add_argument("--query-lanes", type=int, default=len(DEFAULT_QUERY_LANES))
    parser.add_argument("--recent-limit", type=int, default=120)
    parser.add_argument("--min-anchor-ms", type=int, default=8_000)
    parser.add_argument("--db-retries", type=int, default=5)
    parser.add_argument("--db-retry-sleep-s", type=float, default=3.0)
    parser.add_argument("--include-existing", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    started = time.time()
    paths = candidate_paths(args)
    if args.dry_run:
        print(json.dumps({"status": "dry_run", "selected": [str(path) for path in paths]}, indent=2, sort_keys=True))
        return 0
    analyses, failures = analyze_paths_with_retries(paths, args) if paths else ([], [])
    report = summarize(analyses)
    report.update({"status": "ok", "selected": len(paths), "failures": failures, "elapsed_s": round(time.time() - started, 3)})
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
