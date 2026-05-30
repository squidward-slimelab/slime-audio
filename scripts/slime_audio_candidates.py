#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from slime_music_library import DEFAULT_DB, connect, normalize, rows_to_dicts

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HISTORY = REPO_ROOT / "runtime" / "play-history.jsonl"
DEFAULT_CONSTRAINTS = REPO_ROOT / "runtime" / "live-set-constraints.json"


@dataclass(frozen=True)
class SetConstraints:
    vibe: str = ""
    direction: str = ""
    energy_target: float | None = None
    exclude_artists: list[str] = field(default_factory=list)
    exclude_terms: list[str] = field(default_factory=list)
    notes: str = ""
    changes: list[dict[str, Any]] = field(default_factory=list)


def default_constraints() -> dict[str, Any]:
    return {
        "vibe": "",
        "direction": "",
        "energy_target": None,
        "exclude_artists": [],
        "exclude_terms": [],
        "notes": "",
        "changes": [],
    }


def load_constraints(path: Path) -> SetConstraints:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        payload = default_constraints()
    return SetConstraints(
        vibe=str(payload.get("vibe") or ""),
        direction=str(payload.get("direction") or ""),
        energy_target=float(payload["energy_target"]) if payload.get("energy_target") is not None else None,
        exclude_artists=[str(item) for item in payload.get("exclude_artists", [])],
        exclude_terms=[str(item) for item in payload.get("exclude_terms", [])],
        notes=str(payload.get("notes") or ""),
        changes=[item for item in payload.get("changes", []) if isinstance(item, dict)],
    )


def constraints_to_payload(constraints: SetConstraints) -> dict[str, Any]:
    return {
        "vibe": constraints.vibe,
        "direction": constraints.direction,
        "energy_target": constraints.energy_target,
        "exclude_artists": constraints.exclude_artists,
        "exclude_terms": constraints.exclude_terms,
        "notes": constraints.notes,
        "changes": constraints.changes,
    }


def write_constraints(path: Path, constraints: SetConstraints, reason: str) -> None:
    changes = list(constraints.changes)
    changes.append({"timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "reason": reason})
    payload = constraints_to_payload(
        SetConstraints(
            vibe=constraints.vibe,
            direction=constraints.direction,
            energy_target=constraints.energy_target,
            exclude_artists=constraints.exclude_artists,
            exclude_terms=constraints.exclude_terms,
            notes=constraints.notes,
            changes=changes,
        )
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def set_constraints(
    path: Path,
    *,
    vibe: str | None,
    direction: str | None,
    energy_target: float | None,
    exclude_artist: list[str],
    exclude_term: list[str],
    clear_excludes: bool,
    notes: str | None,
    reason: str,
) -> SetConstraints:
    current = load_constraints(path)
    artists = [] if clear_excludes else list(current.exclude_artists)
    terms = [] if clear_excludes else list(current.exclude_terms)
    for artist in exclude_artist:
        if artist not in artists:
            artists.append(artist)
    for term in exclude_term:
        if term not in terms:
            terms.append(term)
    updated = SetConstraints(
        vibe=current.vibe if vibe is None else vibe,
        direction=current.direction if direction is None else direction,
        energy_target=current.energy_target if energy_target is None else energy_target,
        exclude_artists=artists,
        exclude_terms=terms,
        notes=current.notes if notes is None else notes,
        changes=current.changes,
    )
    write_constraints(path, updated, reason)
    return load_constraints(path)


def recent_duplicate_keys(conn: sqlite3.Connection, history_path: Path, limit: int) -> set[str]:
    if limit <= 0:
        return set()
    try:
        lines = history_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return set()
    paths: list[str] = []
    for line in reversed(lines):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("event") not in {"track_started", "track_completed"}:
            continue
        path = event.get("resolved_track") or event.get("track")
        if isinstance(path, str) and path not in paths:
            paths.append(path)
        if len(paths) >= limit:
            break
    if not paths:
        return set()
    placeholders = ",".join("?" for _ in paths)
    rows = conn.execute(f"SELECT duplicate_key FROM files WHERE path IN ({placeholders})", paths).fetchall()
    return {str(row["duplicate_key"]) for row in rows}


def candidate_rows(
    conn: sqlite3.Connection,
    constraints: SetConstraints,
    *,
    history_path: Path,
    recent_limit: int,
    limit: int,
    query: str | None = None,
    include_untagged: bool = False,
) -> list[dict[str, Any]]:
    recent_keys = recent_duplicate_keys(conn, history_path, recent_limit)
    filters = ["preferred_path IS NOT NULL"]
    if not include_untagged:
        filters.extend(["normalized_artist != ''", "normalized_title != ''"])
    params: list[Any] = []
    if recent_keys:
        placeholders = ",".join("?" for _ in recent_keys)
        filters.append(f"duplicate_key NOT IN ({placeholders})")
        params.extend(sorted(recent_keys))
    for artist in constraints.exclude_artists:
        filters.append("normalized_artist NOT LIKE ?")
        params.append(f"%{normalize(artist)}%")
    for term in constraints.exclude_terms:
        filters.append("lower(locations || ' ' || title_guess || ' ' || artist_guess || ' ' || album_guess) NOT LIKE lower(?)")
        params.append(f"%{term}%")
    if query:
        filters.append("(normalized_title LIKE ? OR normalized_artist LIKE ? OR lower(locations) LIKE lower(?))")
        normalized = f"%{normalize(query)}%"
        params.extend([normalized, normalized, f"%{query}%"])

    where = " AND ".join(filters)
    rows = conn.execute(
        f"""
        SELECT
            duplicate_key,
            title_guess,
            artist_guess,
            album_guess,
            preferred_path,
            preferred_server,
            copies,
            server_count,
            preferred_quality_score,
            tunebat_bpm,
            tunebat_key,
            tunebat_mode,
            tunebat_camelot,
            tunebat_energy,
            tunebat_danceability,
            tunebat_happiness
        FROM tracks
        WHERE {where}
        LIMIT ?
        """,
        params + [max(limit * 4, limit)],
    ).fetchall()

    candidates = []
    for row in rows_to_dicts(rows):
        score, reasons = score_candidate(row, constraints)
        row["score"] = score
        row["reasons"] = reasons
        candidates.append(row)
    candidates.sort(key=lambda item: (item["score"], item["copies"], item["preferred_quality_score"]), reverse=True)
    return candidates[:limit]


def score_candidate(row: dict[str, Any], constraints: SetConstraints) -> tuple[float, list[str]]:
    score = 0.0
    reasons: list[str] = []
    copies = int(row.get("copies") or 0)
    server_count = int(row.get("server_count") or 0)
    quality = int(row.get("preferred_quality_score") or 0)
    score += min(0.25, copies * 0.03)
    score += min(0.20, server_count * 0.05)
    score += min(0.20, quality / 3000)
    if copies > 1:
        reasons.append(f"{copies} copies")
    if server_count > 1:
        reasons.append(f"{server_count} servers")
    if row.get("tunebat_bpm"):
        score += 0.10
        reasons.append(f"bpm {row['tunebat_bpm']}")
    energy = row.get("tunebat_energy")
    if energy is not None and constraints.energy_target is not None:
        distance = abs(float(energy) - constraints.energy_target)
        energy_score = max(0.0, 0.30 - distance)
        score += energy_score
        reasons.append(f"energy {float(energy):.2f} vs target {constraints.energy_target:.2f}")
    elif energy is not None:
        score += 0.05
        reasons.append(f"energy {float(energy):.2f}")
    if constraints.vibe:
        haystack = " ".join(str(row.get(key) or "") for key in ("title_guess", "artist_guess", "album_guess")).casefold()
        for word in normalize(constraints.vibe).split():
            if len(word) >= 4 and word in haystack:
                score += 0.05
                reasons.append(f"vibe word {word}")
                break
    if not reasons:
        reasons.append("library candidate")
    return round(score, 4), reasons


def print_json(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Pick SlimeAudio DJ candidates from the music database and live set constraints.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--constraints", type=Path, default=DEFAULT_CONSTRAINTS)
    parser.add_argument("--history", type=Path, default=DEFAULT_HISTORY)
    sub = parser.add_subparsers(dest="command", required=True)

    show_parser = sub.add_parser("constraints")
    show_parser.add_argument("--init", action="store_true")

    set_parser = sub.add_parser("set-constraints")
    set_parser.add_argument("--vibe")
    set_parser.add_argument("--direction")
    set_parser.add_argument("--energy-target", type=float)
    set_parser.add_argument("--exclude-artist", action="append", default=[])
    set_parser.add_argument("--exclude-term", action="append", default=[])
    set_parser.add_argument("--clear-excludes", action="store_true")
    set_parser.add_argument("--notes")
    set_parser.add_argument("--reason", default="operator update")

    candidate_parser = sub.add_parser("candidates")
    candidate_parser.add_argument("query", nargs="?")
    candidate_parser.add_argument("--limit", type=int, default=20)
    candidate_parser.add_argument("--recent-limit", type=int, default=30)
    candidate_parser.add_argument("--include-untagged", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "constraints":
        constraints = load_constraints(args.constraints)
        if args.init and not args.constraints.exists():
            write_constraints(args.constraints, constraints, "init")
            constraints = load_constraints(args.constraints)
        print_json(constraints_to_payload(constraints))
        return 0
    if args.command == "set-constraints":
        constraints = set_constraints(
            args.constraints,
            vibe=args.vibe,
            direction=args.direction,
            energy_target=args.energy_target,
            exclude_artist=args.exclude_artist,
            exclude_term=args.exclude_term,
            clear_excludes=args.clear_excludes,
            notes=args.notes,
            reason=args.reason,
        )
        print_json(constraints_to_payload(constraints))
        return 0
    if args.command == "candidates":
        if not args.db.exists():
            raise SystemExit(f"music database is missing: {args.db}")
        conn = connect(args.db)
        print_json(
            {
                "constraints": constraints_to_payload(load_constraints(args.constraints)),
                "candidates": candidate_rows(
                    conn,
                    load_constraints(args.constraints),
                    history_path=args.history,
                    recent_limit=args.recent_limit,
                    limit=args.limit,
                    query=args.query,
                    include_untagged=args.include_untagged,
                ),
            }
        )
        return 0
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
