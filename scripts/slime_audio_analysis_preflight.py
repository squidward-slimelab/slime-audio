#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from slime_audio_dj import (
    DEFAULT_CACHE,
    DEFAULT_LIBRARY_DB,
    DEFAULT_TUNEBAT_LOCAL_ANALYZER,
    TrackAnalysis,
    analyze_with_cache,
    has_full_track_key_metadata,
    load_analysis_from_db,
)
from slime_audio_session import load_session, parse_ms


ANALYZER_REPAIR_PROBLEMS = {
    "missing_analysis",
    "missing_bpm",
    "missing_beat_offset",
    "missing_phrase_ms",
    "missing_key",
    "low_bpm_confidence",
    "low_key_confidence",
}


def overlaps_window(start_ms: int, end_ms: int | None, *, from_ms: int | None, horizon_end_ms: int | None) -> bool:
    if from_ms is not None and end_ms is not None and end_ms < from_ms:
        return False
    if horizon_end_ms is not None and start_ms > horizon_end_ms:
        return False
    return True


def session_paths(session_path: Path, *, from_ms: int | None = None, horizon_ms: int | None = None) -> list[Path]:
    session = load_session(session_path)
    end_ms = from_ms + horizon_ms if from_ms is not None and horizon_ms is not None else None
    paths: list[Path] = []
    for clip in sorted(session.clips, key=lambda item: (item.start_ms, item.id)):
        if not clip.path:
            continue
        if clip.kind == "effect-track":
            continue
        if not overlaps_window(clip.start_ms, clip.end_ms, from_ms=from_ms, horizon_end_ms=end_ms):
            continue
        paths.append(Path(clip.path))
    for group in sorted(session.stem_groups, key=lambda item: (item.start_ms, item.id)):
        if not group.source_path:
            continue
        if not overlaps_window(group.start_ms, group.end_ms, from_ms=from_ms, horizon_end_ms=end_ms):
            continue
        paths.append(Path(group.source_path))
    return dedupe_paths(paths)


def playlist_paths(path: Path) -> list[Path]:
    return dedupe_paths(
        [
            Path(line.strip())
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
    )


def dedupe_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    result: list[Path] = []
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def analysis_problems(analysis: TrackAnalysis | None, *, min_bpm_confidence: float, min_key_confidence: float) -> list[str]:
    if analysis is None:
        return ["missing_analysis"]
    problems: list[str] = []
    if analysis.bpm is None:
        problems.append("missing_bpm")
    if analysis.beat_offset_ms is None:
        problems.append("missing_beat_offset")
    if analysis.beatgrid is None or analysis.beatgrid.phrase_ms is None:
        problems.append("missing_phrase_ms")
    if not has_full_track_key_metadata(analysis):
        problems.append("missing_key")
    if float((analysis.confidence or {}).get("bpm", 0.0) or 0.0) < min_bpm_confidence:
        problems.append("low_bpm_confidence")
    if float((analysis.confidence or {}).get("key", 0.0) or 0.0) < min_key_confidence:
        problems.append("low_key_confidence")
    if not analysis.structure:
        problems.append("missing_structure")
    if not analysis.cues:
        problems.append("missing_cues")
    return problems


def paths_needing_analysis(
    paths: list[Path],
    *,
    db_path: Path,
    min_bpm_confidence: float = 0.45,
    min_key_confidence: float = 0.25,
) -> list[Path]:
    needing: list[Path] = []
    for path in paths:
        analysis = load_analysis_from_db(db_path, path)
        problems = analysis_problems(
            analysis,
            min_bpm_confidence=min_bpm_confidence,
            min_key_confidence=min_key_confidence,
        )
        if ANALYZER_REPAIR_PROBLEMS.intersection(problems):
            needing.append(path)
    return needing


def build_report(
    paths: list[Path],
    *,
    db_path: Path,
    min_bpm_confidence: float = 0.45,
    min_key_confidence: float = 0.25,
) -> dict[str, Any]:
    tracks: list[dict[str, Any]] = []
    problem_counts: dict[str, int] = {}
    ready_count = 0
    for path in paths:
        analysis = load_analysis_from_db(db_path, path)
        problems = analysis_problems(
            analysis,
            min_bpm_confidence=min_bpm_confidence,
            min_key_confidence=min_key_confidence,
        )
        if not problems:
            ready_count += 1
        for problem in problems:
            problem_counts[problem] = problem_counts.get(problem, 0) + 1
        tracks.append(
            {
                "path": str(path),
                "ready": not problems,
                "problems": problems,
                "analysis": asdict(analysis) if analysis is not None else None,
            }
        )
    total = len(paths)
    return {
        "track_count": total,
        "ready_count": ready_count,
        "coverage": (ready_count / total) if total else 1.0,
        "problem_counts": problem_counts,
        "tracks": tracks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Preflight local DJ analysis coverage for playlists or SlimeAudio sessions.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--session", type=Path)
    source.add_argument("--playlist", type=Path)
    source.add_argument("--track", action="append", default=[])
    parser.add_argument("--from-ms")
    parser.add_argument("--horizon-ms")
    parser.add_argument("--db", type=Path, default=DEFAULT_LIBRARY_DB)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--backend", choices=["auto", "ffmpeg"], default="auto")
    parser.add_argument("--sample-rate", type=int, default=44_100)
    parser.add_argument("--tunebat-analyzer", type=Path, default=DEFAULT_TUNEBAT_LOCAL_ANALYZER)
    parser.add_argument("--analyze-missing", action="store_true")
    parser.add_argument("--min-bpm-confidence", type=float, default=0.45)
    parser.add_argument("--min-key-confidence", type=float, default=0.25)
    parser.add_argument("--min-coverage", type=float, default=1.0)
    parser.add_argument("--fail", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args()

    from_ms = parse_ms(args.from_ms, "analysis preflight start") if args.from_ms else None
    horizon_ms = parse_ms(args.horizon_ms, "analysis preflight horizon") if args.horizon_ms else None
    if args.session is not None:
        paths = session_paths(args.session, from_ms=from_ms, horizon_ms=horizon_ms)
    elif args.playlist is not None:
        paths = playlist_paths(args.playlist)
    else:
        paths = dedupe_paths([Path(value) for value in args.track])

    if args.analyze_missing:
        needing_analysis = paths_needing_analysis(
            paths,
            db_path=args.db,
            min_bpm_confidence=args.min_bpm_confidence,
            min_key_confidence=args.min_key_confidence,
        )
        if needing_analysis:
            analyze_with_cache(needing_analysis, args.cache, args.backend, args.sample_rate, args.db, args.tunebat_analyzer)

    report = build_report(
        paths,
        db_path=args.db,
        min_bpm_confidence=args.min_bpm_confidence,
        min_key_confidence=args.min_key_confidence,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    if args.fail and report["coverage"] < args.min_coverage:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
