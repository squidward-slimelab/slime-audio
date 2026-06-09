#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime
import json
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from slime_music_library import DEFAULT_DB, connect, normalize, rows_to_dicts

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HISTORY = REPO_ROOT / "runtime" / "play-history.jsonl"
DEFAULT_CONSTRAINTS = REPO_ROOT / "runtime" / "live-set-constraints.json"
VIBE_STOP_WORDS = {
    "current",
    "every",
    "flow",
    "allow",
    "avoid",
    "beds",
    "callbacks",
    "comfort",
    "doubles",
    "fallback",
    "genuinely",
    "good",
    "intentional",
    "loops",
    "metadata",
    "musical",
    "overdoing",
    "played",
    "music",
    "pick",
    "reasons",
    "records",
    "repeats",
    "right",
    "room",
    "strong",
    "stuff",
    "track",
    "tracks",
}


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


def parse_history_timestamp(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        try:
            return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z").timestamp()
        except ValueError:
            return None


def recent_play_index(conn: sqlite3.Connection, history_path: Path, limit: int) -> dict[str, dict[str, Any]]:
    if limit <= 0:
        return {}
    try:
        lines = history_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return {}
    path_stats: dict[str, dict[str, Any]] = {}
    session_cache: dict[str, dict[str, str]] = {}

    def add_path(path: Any, played_at: float | None) -> None:
        if not isinstance(path, str) or not path:
            return
        stats = path_stats.setdefault(path, {"plays_seen": 0, "last_played_ts": None})
        stats["plays_seen"] += 1
        if played_at is not None and (stats["last_played_ts"] is None or played_at > stats["last_played_ts"]):
            stats["last_played_ts"] = played_at

    def session_clip_paths(session_ref: Any) -> dict[str, str]:
        if not isinstance(session_ref, str) or not session_ref:
            return {}
        session_path = Path(session_ref)
        if not session_path.is_absolute():
            session_path = REPO_ROOT / session_path
        cache_key = str(session_path)
        if cache_key in session_cache:
            return session_cache[cache_key]
        try:
            payload = json.loads(session_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            session_cache[cache_key] = {}
            return {}
        mapping = {
            str(clip.get("id")): str(clip.get("path"))
            for clip in payload.get("clips", [])
            if isinstance(clip, dict) and clip.get("id") and clip.get("path")
        }
        session_cache[cache_key] = mapping
        return mapping

    for line in reversed(lines):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        event_type = event.get("event")
        if event_type not in {
            "track_started",
            "track_completed",
            "session_window_started",
            "session_window_completed",
            "autodj_material_selected",
        }:
            continue
        played_at = parse_history_timestamp(event.get("timestamp"))
        if event_type in {"track_started", "track_completed"}:
            add_path(event.get("resolved_track") or event.get("track"), played_at)
        elif event_type == "autodj_material_selected":
            for path in event.get("paths") or []:
                add_path(path, played_at)
        elif event_type in {"session_window_started", "session_window_completed"}:
            clip_paths = session_clip_paths(event.get("session"))
            for clip_id in event.get("clips") or []:
                add_path(clip_paths.get(str(clip_id)), played_at)
        if len(path_stats) >= limit:
            break
    if not path_stats:
        return {}
    paths = list(path_stats)
    placeholders = ",".join("?" for _ in paths)
    rows = conn.execute(f"SELECT path, duplicate_key FROM files WHERE path IN ({placeholders})", paths).fetchall()
    by_key: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row["duplicate_key"])
        stats = path_stats.get(str(row["path"]), {})
        entry = by_key.setdefault(key, {"plays_seen": 0, "last_played_ts": None})
        entry["plays_seen"] += int(stats.get("plays_seen") or 0)
        played_at = stats.get("last_played_ts")
        if played_at is not None and (entry["last_played_ts"] is None or played_at > entry["last_played_ts"]):
            entry["last_played_ts"] = played_at
    for entry in by_key.values():
        played_at = entry.get("last_played_ts")
        entry["last_played_at"] = datetime.fromtimestamp(played_at).isoformat() if played_at is not None else None
    return by_key


def recent_duplicate_keys(conn: sqlite3.Connection, history_path: Path, limit: int) -> set[str]:
    return set(recent_play_index(conn, history_path, limit))


def candidate_rows(
    conn: sqlite3.Connection,
    constraints: SetConstraints,
    *,
    history_path: Path,
    recent_limit: int,
    limit: int,
    query: str | None = None,
    include_untagged: bool = False,
    pool_limit: int | None = None,
    randomize_pool: bool = False,
    require_structure: bool = False,
    min_structure_ms: int = 8_000,
) -> list[dict[str, Any]]:
    recent_plays = recent_play_index(conn, history_path, recent_limit)
    filters = [
        "preferred_path IS NOT NULL",
        "lower(preferred_path) NOT LIKE '%/separated/%'",
        "lower(preferred_path) NOT LIKE '%/duplicate/%'",
        "lower(preferred_path) NOT LIKE '%/duplicated/%'",
        "lower(preferred_path) NOT LIKE '%/duplicates/%'",
        "lower(title_guess) NOT IN ('bass', 'drums', 'other', 'vocals')",
        "lower(title_guess) NOT GLOB 'bass [0-9]*'",
        "lower(title_guess) NOT GLOB 'drums [0-9]*'",
        "lower(title_guess) NOT GLOB 'other [0-9]*'",
        "lower(title_guess) NOT GLOB 'vocals [0-9]*'",
        "normalized_title NOT GLOB 'bass [0-9]*'",
        "normalized_title NOT GLOB 'drums [0-9]*'",
        "normalized_title NOT GLOB 'other [0-9]*'",
        "normalized_title NOT GLOB 'vocals [0-9]*'",
        "lower(title_guess || ' ' || locations) NOT LIKE '%withoutdrums%'",
        "lower(title_guess || ' ' || locations) NOT LIKE '%withoutbass%'",
        "lower(title_guess || ' ' || locations) NOT LIKE '%withoutvocals%'",
    ]
    if not include_untagged:
        filters.extend(["normalized_artist != ''", "normalized_title != ''"])
    params: list[Any] = []
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
    if require_structure:
        filters.append(
            """
            EXISTS (
                SELECT 1
                FROM track_dj_structure s
                WHERE s.path = tracks.preferred_path
                  AND s.kind != 'outro'
                  AND s.confidence >= 0.45
                  AND s.end_ms > s.start_ms
                  AND s.end_ms - s.start_ms >= ?
            )
            """
        )
        params.append(min_structure_ms)

    where = " AND ".join(filters)
    order_by = "ORDER BY random()" if randomize_pool else "ORDER BY preferred_quality_score DESC, copies DESC, server_count DESC"
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
        {order_by}
        LIMIT ?
        """,
        params + [pool_limit or max(limit * 20, 200)],
    ).fetchall()

    candidates = []
    for row in rows_to_dicts(rows):
        play_meta = recent_plays.get(str(row.get("duplicate_key"))) or {}
        row["last_played_at"] = play_meta.get("last_played_at")
        row["plays_seen"] = play_meta.get("plays_seen", 0)
        score, reasons = score_candidate(row, constraints, play_meta=play_meta)
        row["score"] = score
        row["reasons"] = reasons
        candidates.append(row)
    candidates.sort(key=lambda item: (item["score"], item["copies"], item["preferred_quality_score"]), reverse=True)
    return diversify_candidates(candidates, limit=limit)


def diversify_candidates(candidates: list[dict[str, Any]], *, limit: int, per_artist: int = 2) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    artist_counts: Counter[str] = Counter()
    for candidate in candidates:
        raw_artist = str(candidate.get("artist_guess") or "").strip().lower()
        artist_key = normalize(raw_artist) or raw_artist
        if not artist_key or artist_counts[artist_key] < per_artist:
            selected.append(candidate)
            artist_counts[artist_key] += 1
        else:
            deferred.append(candidate)
        if len(selected) >= limit:
            return selected
    for candidate in deferred:
        selected.append(candidate)
        if len(selected) >= limit:
            break
    return selected


def score_candidate(row: dict[str, Any], constraints: SetConstraints, *, play_meta: dict[str, Any] | None = None) -> tuple[float, list[str]]:
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
        if distance > 0.35:
            score -= min(0.20, (distance - 0.35) * 0.5)
    elif energy is not None:
        score += 0.05
        reasons.append(f"energy {float(energy):.2f}")
    if constraints.vibe:
        haystack = set(normalize(" ".join(str(row.get(key) or "") for key in ("title_guess", "artist_guess", "album_guess"))).split())
        for word in normalize(constraints.vibe).split():
            if len(word) >= 4 and word not in VIBE_STOP_WORDS and word in haystack:
                score += 0.05
                reasons.append(f"vibe word {word}")
                break
    if play_meta:
        last_played_ts = play_meta.get("last_played_ts")
        plays_seen = int(play_meta.get("plays_seen") or 0)
        penalty = min(0.12, plays_seen * 0.03)
        if isinstance(last_played_ts, (int, float)):
            age_hours = max(0.0, (time.time() - float(last_played_ts)) / 3600)
            if age_hours < 6:
                penalty += 0.35
            elif age_hours < 24:
                penalty += 0.22
            elif age_hours < 72:
                penalty += 0.12
            elif age_hours < 168:
                penalty += 0.06
            reasons.append(f"last played {age_hours:.1f}h ago")
        if plays_seen:
            reasons.append(f"recent plays {plays_seen}")
        if penalty:
            score -= penalty
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
    candidate_parser.add_argument("--pool-limit", type=int)
    candidate_parser.add_argument("--randomize-pool", action="store_true")
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
                    pool_limit=args.pool_limit,
                    randomize_pool=args.randomize_pool,
                ),
            }
        )
        return 0
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
