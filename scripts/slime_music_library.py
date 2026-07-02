#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = REPO_ROOT / "runtime" / "slime-music-library.sqlite3"
DEFAULT_TUNEBAT_LOCAL_ANALYZER = REPO_ROOT / "scripts" / "slime_tunebat_analyzer.js"
AUDIO_EXTENSIONS = {
    ".aac",
    ".aif",
    ".aiff",
    ".alac",
    ".flac",
    ".m4a",
    ".mp3",
    ".ogg",
    ".opus",
    ".wav",
    ".wma",
}
DEFAULT_SOURCES = (
    ("patrick", "rockhouse", Path("/mnt/rockhouse/Music"), 100),
    ("robokrabs", "chum-bucket", Path("/mnt/chum-bucket/Music"), 90),
    ("spongebot", "pineapple", Path("/mnt/pineapple/Music"), 60),
    ("spatula", "krusty-krab", Path("/mnt/krusty-krab/Music"), 50),
)


@dataclass(frozen=True)
class Source:
    server: str
    share: str
    root: Path
    priority: int


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DROP VIEW IF EXISTS tracks;
        DROP VIEW IF EXISTS preferred_files;
        DROP VIEW IF EXISTS duplicate_groups;

        CREATE TABLE IF NOT EXISTS sources (
            id INTEGER PRIMARY KEY,
            server TEXT NOT NULL,
            share TEXT NOT NULL,
            root TEXT NOT NULL UNIQUE,
            priority INTEGER NOT NULL DEFAULT 0,
            online INTEGER NOT NULL DEFAULT 1,
            last_seen INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY,
            source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
            path TEXT NOT NULL UNIQUE,
            rel_path TEXT NOT NULL,
            directory TEXT NOT NULL,
            filename TEXT NOT NULL,
            ext TEXT NOT NULL,
            size INTEGER NOT NULL,
            mtime INTEGER NOT NULL,
            title_guess TEXT NOT NULL,
            artist_guess TEXT NOT NULL,
            album_guess TEXT NOT NULL,
            normalized_title TEXT NOT NULL,
            normalized_artist TEXT NOT NULL,
            duplicate_key TEXT NOT NULL,
            quality_score INTEGER NOT NULL,
            scanned_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS track_lyrics (
            duplicate_key TEXT PRIMARY KEY,
            lyrics TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT '',
            source_url TEXT NOT NULL DEFAULT '',
            updated_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS track_tunebat (
            duplicate_key TEXT PRIMARY KEY,
            tunebat_url TEXT NOT NULL DEFAULT '',
            tunebat_title TEXT NOT NULL DEFAULT '',
            tunebat_artist TEXT NOT NULL DEFAULT '',
            key TEXT NOT NULL DEFAULT '',
            mode TEXT NOT NULL DEFAULT '',
            camelot TEXT NOT NULL DEFAULT '',
            bpm REAL,
            popularity INTEGER,
            energy REAL,
            danceability REAL,
            happiness REAL,
            raw_json TEXT NOT NULL DEFAULT '',
            updated_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS track_dj_analysis (
            path TEXT PRIMARY KEY,
            file_size INTEGER NOT NULL,
            file_mtime_ns INTEGER NOT NULL,
            duration_s REAL NOT NULL,
            sample_rate INTEGER NOT NULL,
            channels INTEGER NOT NULL,
            bpm REAL,
            beat_offset_ms INTEGER,
            key TEXT,
            tonic INTEGER,
            mode TEXT,
            camelot TEXT,
            energy REAL NOT NULL,
            loudness_db REAL NOT NULL,
            bpm_confidence REAL NOT NULL DEFAULT 0,
            key_confidence REAL NOT NULL DEFAULT 0,
            phrase_beats INTEGER NOT NULL DEFAULT 32,
            phrase_ms INTEGER,
            analyzer TEXT NOT NULL DEFAULT 'slime_audio_dj',
            raw_json TEXT NOT NULL DEFAULT '',
            updated_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS track_dj_structure (
            path TEXT NOT NULL REFERENCES track_dj_analysis(path) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            start_ms INTEGER NOT NULL,
            end_ms INTEGER NOT NULL,
            confidence REAL NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(path, kind, start_ms, end_ms)
        );

        CREATE TABLE IF NOT EXISTS track_dj_drop_candidates (
            path TEXT NOT NULL REFERENCES track_dj_analysis(path) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            start_ms INTEGER NOT NULL,
            end_ms INTEGER NOT NULL,
            confidence REAL NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            source_kind TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(path, kind, start_ms, end_ms)
        );

        CREATE TABLE IF NOT EXISTS track_dj_cues (
            path TEXT NOT NULL REFERENCES track_dj_analysis(path) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            label TEXT NOT NULL DEFAULT '',
            at_ms INTEGER NOT NULL,
            end_ms INTEGER,
            confidence REAL NOT NULL,
            source TEXT NOT NULL DEFAULT 'detected_structure',
            quantized INTEGER NOT NULL DEFAULT 0,
            reason TEXT NOT NULL DEFAULT '',
            updated_at INTEGER NOT NULL,
            PRIMARY KEY(path, kind, at_ms, label)
        );

        CREATE TABLE IF NOT EXISTS track_stem_sets (
            id TEXT PRIMARY KEY,
            duplicate_key TEXT,
            source_path TEXT NOT NULL,
            source_size INTEGER NOT NULL,
            source_mtime REAL NOT NULL,
            model TEXT NOT NULL,
            profile TEXT NOT NULL,
            artifact_root TEXT NOT NULL,
            sample_rate INTEGER,
            channels INTEGER,
            duration_ms INTEGER,
            status TEXT NOT NULL,
            error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(source_path, source_size, source_mtime, model, profile)
        );

        CREATE TABLE IF NOT EXISTS track_stems (
            stem_set_id TEXT NOT NULL REFERENCES track_stem_sets(id) ON DELETE CASCADE,
            stem_name TEXT NOT NULL,
            path TEXT NOT NULL,
            loudness_db REAL,
            peak_db REAL,
            vocal_presence_score REAL,
            artifact_score REAL,
            PRIMARY KEY(stem_set_id, stem_name)
        );

        CREATE TABLE IF NOT EXISTS track_stem_windows (
            stem_set_id TEXT NOT NULL,
            stem_name TEXT NOT NULL,
            kind TEXT NOT NULL,
            start_ms INTEGER NOT NULL,
            end_ms INTEGER NOT NULL,
            confidence REAL NOT NULL,
            reason TEXT,
            PRIMARY KEY(stem_set_id, stem_name, kind, start_ms, end_ms),
            FOREIGN KEY(stem_set_id, stem_name) REFERENCES track_stems(stem_set_id, stem_name) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_files_duplicate_key ON files(duplicate_key);
        CREATE INDEX IF NOT EXISTS idx_files_title ON files(normalized_title);
        CREATE INDEX IF NOT EXISTS idx_files_artist ON files(normalized_artist);
        CREATE INDEX IF NOT EXISTS idx_sources_server ON sources(server);
        CREATE INDEX IF NOT EXISTS idx_track_dj_structure_kind ON track_dj_structure(kind);
        CREATE INDEX IF NOT EXISTS idx_track_dj_drop_candidates_kind ON track_dj_drop_candidates(kind);
        CREATE INDEX IF NOT EXISTS idx_track_dj_cues_kind ON track_dj_cues(kind);
        CREATE INDEX IF NOT EXISTS idx_track_stem_sets_source ON track_stem_sets(source_path);
        CREATE INDEX IF NOT EXISTS idx_track_stem_sets_duplicate_key ON track_stem_sets(duplicate_key);
        CREATE INDEX IF NOT EXISTS idx_track_stem_windows_kind ON track_stem_windows(kind);

        CREATE VIEW duplicate_groups AS
            SELECT
                duplicate_key,
                COUNT(*) AS copies,
                COUNT(DISTINCT sources.server) AS server_count,
                GROUP_CONCAT(DISTINCT sources.server) AS servers,
                MAX(sources.priority) AS best_server_priority,
                MAX(files.quality_score) AS best_quality_score,
                MIN(files.title_guess) AS title_guess,
                MIN(files.artist_guess) AS artist_guess
            FROM files
            JOIN sources ON sources.id = files.source_id
            GROUP BY duplicate_key;

        CREATE VIEW preferred_files AS
            SELECT files.*, sources.server, sources.share, sources.root, sources.priority AS server_priority
            FROM files
            JOIN sources ON sources.id = files.source_id
            WHERE files.id IN (
                SELECT ranked.id
                FROM (
                    SELECT
                        files.id,
                        ROW_NUMBER() OVER (
                            PARTITION BY files.duplicate_key
                            ORDER BY sources.priority DESC, files.quality_score DESC, files.size DESC, files.path ASC
                        ) AS row_number
                    FROM files
                    JOIN sources ON sources.id = files.source_id
                ) ranked
                WHERE ranked.row_number = 1
            );

        CREATE VIEW tracks AS
            WITH ranked AS (
                SELECT
                    files.id,
                    files.duplicate_key,
                    ROW_NUMBER() OVER (
                        PARTITION BY files.duplicate_key
                        ORDER BY sources.priority DESC, files.quality_score DESC, files.size DESC, files.path ASC
                    ) AS row_number
                FROM files
                JOIN sources ON sources.id = files.source_id
            )
            SELECT
                groups.duplicate_key,
                groups.copies,
                groups.server_count,
                groups.servers,
                preferred.title_guess,
                preferred.artist_guess,
                preferred.album_guess,
                preferred.normalized_title,
                preferred.normalized_artist,
                preferred.path AS preferred_path,
                preferred.server AS preferred_server,
                preferred.share AS preferred_share,
                preferred.quality_score AS preferred_quality_score,
                lyrics.lyrics IS NOT NULL AS has_lyrics,
                lyrics.source AS lyrics_source,
                lyrics.source_url AS lyrics_url,
                tunebat.tunebat_url,
                tunebat.tunebat_title,
                tunebat.tunebat_artist,
                tunebat.key AS tunebat_key,
                tunebat.mode AS tunebat_mode,
                tunebat.camelot AS tunebat_camelot,
                tunebat.bpm AS tunebat_bpm,
                tunebat.popularity AS tunebat_popularity,
                tunebat.energy AS tunebat_energy,
                tunebat.danceability AS tunebat_danceability,
                tunebat.happiness AS tunebat_happiness,
                GROUP_CONCAT(sources.server || ':' || files.path, '\n') AS locations
            FROM duplicate_groups groups
            JOIN ranked ON ranked.duplicate_key = groups.duplicate_key AND ranked.row_number = 1
            JOIN preferred_files preferred ON preferred.id = ranked.id
            JOIN files ON files.duplicate_key = groups.duplicate_key
            JOIN sources ON sources.id = files.source_id
            LEFT JOIN track_lyrics lyrics ON lyrics.duplicate_key = groups.duplicate_key
            LEFT JOIN track_tunebat tunebat ON tunebat.duplicate_key = groups.duplicate_key
            GROUP BY groups.duplicate_key
            ORDER BY preferred.artist_guess, preferred.album_guess, preferred.title_guess;
        """
    )


def parse_source(value: str) -> Source:
    parts = value.split(":", 3)
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("sources must be server:share:priority:/absolute/path")
    server, share, priority, root = parts
    return Source(server=server, share=share, priority=int(priority), root=Path(root))


def mounted_default_sources() -> list[Source]:
    return [Source(*source) for source in DEFAULT_SOURCES if source[2].exists()]


def source_id(conn: sqlite3.Connection, source: Source, now: int) -> int:
    conn.execute(
        """
        INSERT INTO sources(server, share, root, priority, online, last_seen)
        VALUES (?, ?, ?, ?, 1, ?)
        ON CONFLICT(root) DO UPDATE SET
            server=excluded.server,
            share=excluded.share,
            priority=excluded.priority,
            online=1,
            last_seen=excluded.last_seen
        """,
        (source.server, source.share, str(source.root), source.priority, now),
    )
    row = conn.execute("SELECT id FROM sources WHERE root = ?", (str(source.root),)).fetchone()
    if row is None:
        raise RuntimeError(f"failed to upsert source: {source.root}")
    return int(row["id"])


def audio_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for directory, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if not name.startswith(".")]
        for filename in filenames:
            if Path(filename).suffix.casefold() in AUDIO_EXTENSIONS:
                files.append(Path(directory) / filename)
    return files


def normalize(value: str) -> str:
    value = value.casefold()
    value = re.sub(r"\([^)]*\)|\[[^]]*\]", " ", value)
    value = re.sub(r"\b(remaster(?:ed)?|explicit|clean|mono|stereo|disc \d+|cd \d+)\b", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def strip_track_number(stem: str) -> str:
    return re.sub(r"^\s*(?:\d{1,3}[-_. ]+|\d{1,2}\s*-\s*)", "", stem).strip()


def guess_tags(path: Path, root: Path) -> tuple[str, str, str]:
    rel = path.relative_to(root)
    parts = rel.parts
    title = strip_track_number(path.stem)
    artist = parts[0] if len(parts) >= 3 else ""
    album = parts[1] if len(parts) >= 3 else (parts[-2] if len(parts) >= 2 else "")
    return title, artist, album


def quality_score(path: Path, size: int) -> int:
    ext = path.suffix.casefold()
    ext_score = {
        ".flac": 500,
        ".alac": 480,
        ".aiff": 460,
        ".aif": 460,
        ".wav": 430,
        ".m4a": 330,
        ".aac": 310,
        ".ogg": 300,
        ".opus": 300,
        ".mp3": 250,
        ".wma": 180,
    }.get(ext, 100)
    return ext_score + min(200, size // 10_000_000)


def duplicate_key(path: Path, root: Path, size: int) -> tuple[str, str, str, str]:
    title, artist, album = guess_tags(path, root)
    normalized_title = normalize(title)
    normalized_artist = normalize(artist)
    normalized_album = normalize(album)
    if normalized_title and normalized_artist:
        key_material = f"tag:{normalized_artist}:{normalized_album}:{normalized_title}"
    else:
        key_material = f"path:{normalize(path.stem)}:{size}"
    digest = hashlib.sha1(key_material.encode("utf-8")).hexdigest()
    return digest, title, artist, album


def upsert_file(conn: sqlite3.Connection, source_id_value: int, source: Source, path: Path, now: int) -> None:
    stat = path.stat()
    rel_path = str(path.relative_to(source.root))
    title, artist, album = guess_tags(path, source.root)
    key, _title, _artist, _album = duplicate_key(path, source.root, stat.st_size)
    conn.execute(
        """
        INSERT INTO files(
            source_id, path, rel_path, directory, filename, ext, size, mtime,
            title_guess, artist_guess, album_guess, normalized_title, normalized_artist,
            duplicate_key, quality_score, scanned_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET
            source_id=excluded.source_id,
            rel_path=excluded.rel_path,
            directory=excluded.directory,
            filename=excluded.filename,
            ext=excluded.ext,
            size=excluded.size,
            mtime=excluded.mtime,
            title_guess=excluded.title_guess,
            artist_guess=excluded.artist_guess,
            album_guess=excluded.album_guess,
            normalized_title=excluded.normalized_title,
            normalized_artist=excluded.normalized_artist,
            duplicate_key=excluded.duplicate_key,
            quality_score=excluded.quality_score,
            scanned_at=excluded.scanned_at
        """,
        (
            source_id_value,
            str(path),
            rel_path,
            str(path.parent),
            path.name,
            path.suffix.casefold(),
            stat.st_size,
            int(stat.st_mtime),
            title,
            artist,
            album,
            normalize(title),
            normalize(artist),
            key,
            quality_score(path, stat.st_size),
            now,
        ),
    )


def scan(conn: sqlite3.Connection, sources: list[Source], prune: bool) -> dict[str, int]:
    now = time.time_ns()
    totals = {"sources": 0, "files": 0, "pruned": 0}
    for source in sources:
        if not source.root.exists():
            continue
        sid = source_id(conn, source, now)
        totals["sources"] += 1
        paths = audio_files(source.root)
        with conn:
            for path in paths:
                try:
                    upsert_file(conn, sid, source, path, now)
                    totals["files"] += 1
                except OSError:
                    continue
            if prune:
                result = conn.execute("DELETE FROM files WHERE source_id = ? AND scanned_at != ?", (sid, now))
                totals["pruned"] += result.rowcount if result.rowcount is not None else 0
    return totals


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict]:
    return [{key: row[key] for key in row.keys()} for row in rows]


def print_json(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def read_text_value(path: Path | None, value: str | None) -> str:
    if path is not None:
        return path.read_text(encoding="utf-8")
    return value or ""


# Loose text lanes for acquisition planning. These are search heuristics over
# titles/artists/paths, not authoritative genre tags.
GENRE_LANE_TERMS: dict[str, tuple[str, ...]] = {
    "techno": ("techno",),
    "house": ("house",),
    "dnb": ("drum and bass", "drum n bass", "dnb", "jungle", "neurofunk"),
    "dubstep": ("dubstep", "riddim", "brostep"),
    "bass": ("bass music", "breakbeat", "breaks", "garage", "uk bass"),
    "electronic": ("electronic", "electro", "edm", "rave", "trance", "hardstyle", "hardcore techno"),
}


def genre_lane_counts(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute("SELECT normalized_title, normalized_artist, lower(locations) AS locations FROM tracks").fetchall()
    counts = {lane: 0 for lane in GENRE_LANE_TERMS}
    edm_tracks = 0
    total = 0
    for row in rows:
        total += 1
        haystack = " ".join(str(row[key] or "") for key in ("normalized_title", "normalized_artist", "locations"))
        matched = [lane for lane, terms in GENRE_LANE_TERMS.items() if any(term in haystack for term in terms)]
        for lane in matched:
            counts[lane] += 1
        if matched:
            edm_tracks += 1
    return {
        "lanes": counts,
        "edm_tracks": edm_tracks,
        "total_tracks": total,
        "edm_share": round(edm_tracks / total, 4) if total else 0.0,
    }


def command_stats(conn: sqlite3.Connection) -> None:
    summary = {
        "files": conn.execute("SELECT COUNT(*) AS value FROM files").fetchone()["value"],
        "unique_tracks": conn.execute("SELECT COUNT(*) AS value FROM duplicate_groups").fetchone()["value"],
        "duplicate_groups": conn.execute("SELECT COUNT(*) AS value FROM duplicate_groups WHERE copies > 1").fetchone()["value"],
        "genre_lanes": genre_lane_counts(conn),
        "sources": rows_to_dicts(
            conn.execute(
                """
                SELECT sources.server, sources.share, sources.root, sources.priority, COUNT(files.id) AS files
                FROM sources
                LEFT JOIN files ON files.source_id = sources.id
                GROUP BY sources.id
                ORDER BY sources.priority DESC, sources.server
                """
            ).fetchall()
        ),
    }
    print_json(summary)


def command_search(conn: sqlite3.Connection, query: str, limit: int) -> None:
    normalized = f"%{normalize(query)}%"
    rows = conn.execute(
        """
        SELECT
            title_guess,
            artist_guess,
            album_guess,
            preferred_server,
            preferred_share,
            preferred_path,
            has_lyrics,
            lyrics_source,
            tunebat_key,
            tunebat_mode,
            tunebat_camelot,
            tunebat_bpm,
            tunebat_url,
            copies,
            server_count,
            servers,
            locations,
            duplicate_key,
            preferred_quality_score
        FROM tracks
        WHERE normalized_title LIKE ? OR normalized_artist LIKE ? OR lower(locations) LIKE lower(?)
        ORDER BY copies DESC, preferred_quality_score DESC, preferred_path ASC
        LIMIT ?
        """,
        (normalized, normalized, f"%{query}%", limit),
    ).fetchall()
    print_json(rows_to_dicts(rows))


def preferred_path_for_file(conn: sqlite3.Connection, path: Path) -> Path | None:
    row = conn.execute("SELECT duplicate_key FROM files WHERE path = ?", (str(path),)).fetchone()
    if row is None:
        return None
    preferred = conn.execute(
        """
        SELECT path
        FROM preferred_files
        WHERE duplicate_key = ?
        LIMIT 1
        """,
        (row["duplicate_key"],),
    ).fetchone()
    if preferred is None:
        return None
    preferred_path = Path(preferred["path"])
    return preferred_path if preferred_path.exists() else None


def command_route(conn: sqlite3.Connection, path: Path) -> None:
    preferred = preferred_path_for_file(conn, path)
    print_json({"input": str(path), "preferred": str(preferred) if preferred is not None else None})


def command_copies(conn: sqlite3.Connection, query: str, limit: int) -> None:
    normalized = f"%{normalize(query)}%"
    groups = conn.execute(
        """
        SELECT duplicate_key
        FROM duplicate_groups
        WHERE duplicate_key IN (
            SELECT duplicate_key FROM files
            WHERE normalized_title LIKE ? OR normalized_artist LIKE ? OR lower(path) LIKE lower(?)
        )
        ORDER BY copies DESC, best_server_priority DESC
        LIMIT ?
        """,
        (normalized, normalized, f"%{query}%", limit),
    ).fetchall()
    result = []
    for group in groups:
        rows = conn.execute(
            """
            SELECT sources.server, sources.share, sources.priority, files.quality_score, files.path, files.size
            FROM files
            JOIN sources ON sources.id = files.source_id
            WHERE files.duplicate_key = ?
            ORDER BY sources.priority DESC, files.quality_score DESC, files.size DESC, files.path ASC
            """,
            (group["duplicate_key"],),
        ).fetchall()
        result.append({"duplicate_key": group["duplicate_key"], "copies": rows_to_dicts(rows)})
    print_json(result)


def command_tracks(conn: sqlite3.Connection, query: str | None, limit: int, duplicates_only: bool) -> None:
    filters = []
    params: list[object] = []
    if query:
        normalized = f"%{normalize(query)}%"
        filters.append("(normalized_title LIKE ? OR normalized_artist LIKE ? OR lower(locations) LIKE lower(?))")
        params.extend([normalized, normalized, f"%{query}%"])
    if duplicates_only:
        filters.append("copies > 1")
    where = f"WHERE {' AND '.join(filters)}" if filters else ""
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT
            title_guess,
            artist_guess,
            album_guess,
            copies,
            server_count,
            servers,
            preferred_server,
            preferred_path,
            has_lyrics,
            lyrics_source,
            tunebat_key,
            tunebat_mode,
            tunebat_camelot,
            tunebat_bpm,
            tunebat_url,
            locations,
            duplicate_key
        FROM tracks
        {where}
        ORDER BY copies DESC, artist_guess, album_guess, title_guess
        LIMIT ?
        """,
        params,
    ).fetchall()
    print_json(rows_to_dicts(rows))


def command_show(conn: sqlite3.Connection, duplicate_key_value: str, include_lyrics: bool) -> None:
    row = conn.execute("SELECT * FROM tracks WHERE duplicate_key = ?", (duplicate_key_value,)).fetchone()
    if row is None:
        raise SystemExit(f"unknown duplicate_key: {duplicate_key_value}")
    result = {key: row[key] for key in row.keys()}
    if include_lyrics:
        lyrics = conn.execute("SELECT lyrics FROM track_lyrics WHERE duplicate_key = ?", (duplicate_key_value,)).fetchone()
        result["lyrics"] = lyrics["lyrics"] if lyrics is not None else None
    print_json(result)


def command_set_lyrics(
    conn: sqlite3.Connection,
    duplicate_key_value: str,
    lyrics: str,
    source: str,
    source_url: str,
    emit: bool = True,
) -> None:
    if not lyrics.strip():
        raise SystemExit("lyrics cannot be empty")
    now = time.time_ns()
    with conn:
        conn.execute(
            """
            INSERT INTO track_lyrics(duplicate_key, lyrics, source, source_url, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(duplicate_key) DO UPDATE SET
                lyrics=excluded.lyrics,
                source=excluded.source,
                source_url=excluded.source_url,
                updated_at=excluded.updated_at
            """,
            (duplicate_key_value, lyrics, source, source_url, now),
        )
    if emit:
        print_json({"duplicate_key": duplicate_key_value, "lyrics": True, "source": source, "source_url": source_url})


def command_set_tunebat(
    conn: sqlite3.Connection,
    duplicate_key_value: str,
    tunebat_url: str,
    tunebat_title: str,
    tunebat_artist: str,
    key: str,
    mode: str,
    camelot: str,
    bpm: float | None,
    popularity: int | None,
    energy: float | None,
    danceability: float | None,
    happiness: float | None,
    raw_json: object | None,
    emit: bool = True,
) -> None:
    now = time.time_ns()
    raw_text = json.dumps(raw_json, sort_keys=True) if raw_json is not None else ""
    with conn:
        conn.execute(
            """
            INSERT INTO track_tunebat(
                duplicate_key, tunebat_url, tunebat_title, tunebat_artist, key, mode, camelot,
                bpm, popularity, energy, danceability, happiness, raw_json, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(duplicate_key) DO UPDATE SET
                tunebat_url=excluded.tunebat_url,
                tunebat_title=excluded.tunebat_title,
                tunebat_artist=excluded.tunebat_artist,
                key=excluded.key,
                mode=excluded.mode,
                camelot=excluded.camelot,
                bpm=excluded.bpm,
                popularity=excluded.popularity,
                energy=excluded.energy,
                danceability=excluded.danceability,
                happiness=excluded.happiness,
                raw_json=excluded.raw_json,
                updated_at=excluded.updated_at
            """,
            (
                duplicate_key_value,
                tunebat_url,
                tunebat_title,
                tunebat_artist,
                key,
                mode,
                camelot,
                bpm,
                popularity,
                energy,
                danceability,
                happiness,
                raw_text,
                now,
            ),
        )
    if emit:
        print_json({"duplicate_key": duplicate_key_value, "tunebat": True, "key": key, "mode": mode, "camelot": camelot, "bpm": bpm})


def command_import_metadata(conn: sqlite3.Connection, path: Path) -> None:
    payload = read_json(path)
    if isinstance(payload, dict):
        rows = payload.get("tracks", [payload])
    else:
        rows = payload
    if not isinstance(rows, list):
        raise SystemExit("metadata import must be a JSON object, a JSON object with tracks, or a JSON list")

    imported = {"lyrics": 0, "tunebat": 0}
    for item in rows:
        if not isinstance(item, dict):
            continue
        duplicate_key_value = str(item.get("duplicate_key") or "")
        if not duplicate_key_value:
            continue
        lyrics = item.get("lyrics")
        if isinstance(lyrics, dict):
            text = str(lyrics.get("text") or "")
            if text.strip():
                command_set_lyrics(
                    conn,
                    duplicate_key_value,
                    text,
                    str(lyrics.get("source") or ""),
                    str(lyrics.get("source_url") or lyrics.get("url") or ""),
                    emit=False,
                )
                imported["lyrics"] += 1
        elif isinstance(lyrics, str) and lyrics.strip():
            command_set_lyrics(conn, duplicate_key_value, lyrics, "", "", emit=False)
            imported["lyrics"] += 1

        tunebat = item.get("tunebat")
        if isinstance(tunebat, dict):
            command_set_tunebat(
                conn,
                duplicate_key_value,
                str(tunebat.get("url") or tunebat.get("tunebat_url") or ""),
                str(tunebat.get("title") or tunebat.get("tunebat_title") or ""),
                str(tunebat.get("artist") or tunebat.get("tunebat_artist") or ""),
                str(tunebat.get("key") or ""),
                str(tunebat.get("mode") or ""),
                str(tunebat.get("camelot") or ""),
                float(tunebat["bpm"]) if tunebat.get("bpm") is not None else None,
                int(tunebat["popularity"]) if tunebat.get("popularity") is not None else None,
                float(tunebat["energy"]) if tunebat.get("energy") is not None else None,
                float(tunebat["danceability"]) if tunebat.get("danceability") is not None else None,
                float(tunebat["happiness"]) if tunebat.get("happiness") is not None else None,
                tunebat,
                emit=False,
            )
            imported["tunebat"] += 1
    print_json(imported)


def tunebat_local_payload(analyzer: Path, target_path: Path) -> dict:
    command = ["node", str(analyzer), str(target_path)]
    completed = subprocess.run(command, cwd=REPO_ROOT, text=True, capture_output=True, check=False)
    if completed.returncode != 0:
        raise SystemExit(completed.stderr.strip() or f"local TuneBat analyzer failed rc={completed.returncode}")
    payload = json.loads(completed.stdout)
    if not isinstance(payload, dict):
        raise SystemExit("local TuneBat analyzer returned non-object JSON")
    return payload


def store_tunebat_local_payload(conn: sqlite3.Connection, duplicate_key_value: str, target_path: Path, payload: dict) -> None:
    command_set_tunebat(
        conn,
        duplicate_key_value,
        str(payload.get("analyzer_url") or "https://tunebat.com/Analyzer"),
        str(payload.get("filename") or target_path.name),
        "",
        str(payload.get("key") or ""),
        str(payload.get("mode") or ""),
        str(payload.get("camelot") or ""),
        float(payload["bpm"]) if payload.get("bpm") is not None else None,
        None,
        float(payload["energy"]) if payload.get("energy") is not None else None,
        None,
        None,
        payload,
        emit=False,
    )


def command_analyze_tunebat_local(conn: sqlite3.Connection, duplicate_key_value: str, analyzer: Path, path: Path | None) -> None:
    target_path = path
    if target_path is None:
        row = conn.execute("SELECT preferred_path FROM tracks WHERE duplicate_key = ?", (duplicate_key_value,)).fetchone()
        if row is None:
            raise SystemExit(f"unknown duplicate_key: {duplicate_key_value}")
        target_path = Path(row["preferred_path"])
    payload = tunebat_local_payload(analyzer, target_path)
    store_tunebat_local_payload(conn, duplicate_key_value, target_path, payload)
    print_json({"duplicate_key": duplicate_key_value, "path": str(target_path), "tunebat_local": payload})


def command_backfill_tunebat_local(
    conn: sqlite3.Connection,
    analyzer: Path,
    limit: int,
    max_seconds: int,
    force: bool,
    include_derived: bool,
    emit: bool = True,
) -> None:
    if limit < 1:
        raise SystemExit("limit must be at least 1")
    started = time.monotonic()
    filters = []
    if not force:
        filters.append("tunebat_bpm IS NULL")
    if not include_derived:
        filters.extend(
            [
                "normalized_title NOT IN ('vocals', 'drums', 'bass', 'other', 'piano', 'guitar', 'accompaniment', 'no vocals', 'instrumental')",
                "lower(preferred_path) NOT LIKE '%/separated/%'",
                "lower(preferred_path) NOT LIKE '%/htdemucs/%'",
                "lower(preferred_path) NOT LIKE '%/stems/%'",
            ]
        )
    where = f"WHERE {' AND '.join(filters)}" if filters else ""
    rows = conn.execute(
        f"""
        SELECT duplicate_key, preferred_path, title_guess, artist_guess
        FROM tracks
        {where}
        ORDER BY preferred_server = 'patrick' DESC, copies DESC, artist_guess, album_guess, title_guess
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    result = {"requested": limit, "found": len(rows), "analyzed": 0, "failed": 0, "skipped": 0, "errors": []}
    for row in rows:
        if max_seconds > 0 and time.monotonic() - started >= max_seconds:
            break
        target_path = Path(row["preferred_path"])
        if not target_path.exists():
            result["skipped"] += 1
            result["errors"].append({"duplicate_key": row["duplicate_key"], "path": str(target_path), "error": "preferred path missing"})
            continue
        try:
            payload = tunebat_local_payload(analyzer, target_path)
            store_tunebat_local_payload(conn, row["duplicate_key"], target_path, payload)
            result["analyzed"] += 1
        except Exception as error:
            result["failed"] += 1
            result["errors"].append(
                {
                    "duplicate_key": row["duplicate_key"],
                    "path": str(target_path),
                    "error": str(error),
                }
            )
    if emit:
        print_json(result)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Index Slime Lab samba music into sqlite and choose best duplicate routes.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    sub = parser.add_subparsers(dest="command", required=True)

    scan_parser = sub.add_parser("scan")
    scan_parser.add_argument("--source", action="append", type=parse_source, default=None)
    scan_parser.add_argument("--prune", action=argparse.BooleanOptionalAction, default=True)

    sub.add_parser("stats")

    search_parser = sub.add_parser("search")
    search_parser.add_argument("query")
    search_parser.add_argument("--limit", type=int, default=20)

    copies_parser = sub.add_parser("copies")
    copies_parser.add_argument("query")
    copies_parser.add_argument("--limit", type=int, default=10)

    tracks_parser = sub.add_parser("tracks")
    tracks_parser.add_argument("query", nargs="?")
    tracks_parser.add_argument("--limit", type=int, default=50)
    tracks_parser.add_argument("--duplicates-only", action=argparse.BooleanOptionalAction, default=False)

    show_parser = sub.add_parser("show")
    show_parser.add_argument("duplicate_key")
    show_parser.add_argument("--include-lyrics", action=argparse.BooleanOptionalAction, default=False)

    lyrics_parser = sub.add_parser("set-lyrics")
    lyrics_parser.add_argument("duplicate_key")
    lyrics_parser.add_argument("--lyrics")
    lyrics_parser.add_argument("--lyrics-file", type=Path)
    lyrics_parser.add_argument("--source", default="")
    lyrics_parser.add_argument("--source-url", default="")

    tunebat_parser = sub.add_parser("set-tunebat")
    tunebat_parser.add_argument("duplicate_key")
    tunebat_parser.add_argument("--url", default="")
    tunebat_parser.add_argument("--title", default="")
    tunebat_parser.add_argument("--artist", default="")
    tunebat_parser.add_argument("--key", default="")
    tunebat_parser.add_argument("--mode", default="")
    tunebat_parser.add_argument("--camelot", default="")
    tunebat_parser.add_argument("--bpm", type=float)
    tunebat_parser.add_argument("--popularity", type=int)
    tunebat_parser.add_argument("--energy", type=float)
    tunebat_parser.add_argument("--danceability", type=float)
    tunebat_parser.add_argument("--happiness", type=float)
    tunebat_parser.add_argument("--raw-json", type=Path)

    import_parser = sub.add_parser("import-metadata")
    import_parser.add_argument("path", type=Path)

    analyze_parser = sub.add_parser("analyze-tunebat-local")
    analyze_parser.add_argument("duplicate_key")
    analyze_parser.add_argument("--analyzer", type=Path, default=DEFAULT_TUNEBAT_LOCAL_ANALYZER)
    analyze_parser.add_argument("--path", type=Path)

    backfill_parser = sub.add_parser("backfill-tunebat-local")
    backfill_parser.add_argument("--analyzer", type=Path, default=DEFAULT_TUNEBAT_LOCAL_ANALYZER)
    backfill_parser.add_argument("--limit", type=int, default=10)
    backfill_parser.add_argument("--max-seconds", type=int, default=900)
    backfill_parser.add_argument("--force", action=argparse.BooleanOptionalAction, default=False)
    backfill_parser.add_argument("--include-derived", action=argparse.BooleanOptionalAction, default=False)

    route_parser = sub.add_parser("route")
    route_parser.add_argument("path", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    conn = connect(args.db)
    if args.command == "scan":
        sources = args.source if args.source else mounted_default_sources()
        if not sources:
            raise SystemExit("no default samba music sources are mounted; pass --source server:share:priority:/path")
        print_json(scan(conn, sources, args.prune))
        return 0
    if args.command == "stats":
        command_stats(conn)
        return 0
    if args.command == "search":
        command_search(conn, args.query, args.limit)
        return 0
    if args.command == "copies":
        command_copies(conn, args.query, args.limit)
        return 0
    if args.command == "tracks":
        command_tracks(conn, args.query, args.limit, args.duplicates_only)
        return 0
    if args.command == "show":
        command_show(conn, args.duplicate_key, args.include_lyrics)
        return 0
    if args.command == "set-lyrics":
        command_set_lyrics(conn, args.duplicate_key, read_text_value(args.lyrics_file, args.lyrics), args.source, args.source_url)
        return 0
    if args.command == "set-tunebat":
        command_set_tunebat(
            conn,
            args.duplicate_key,
            args.url,
            args.title,
            args.artist,
            args.key,
            args.mode,
            args.camelot,
            args.bpm,
            args.popularity,
            args.energy,
            args.danceability,
            args.happiness,
            read_json(args.raw_json) if args.raw_json else None,
        )
        return 0
    if args.command == "import-metadata":
        command_import_metadata(conn, args.path)
        return 0
    if args.command == "analyze-tunebat-local":
        command_analyze_tunebat_local(conn, args.duplicate_key, args.analyzer, args.path)
        return 0
    if args.command == "backfill-tunebat-local":
        command_backfill_tunebat_local(conn, args.analyzer, args.limit, args.max_seconds, args.force, args.include_derived)
        return 0
    if args.command == "route":
        command_route(conn, args.path)
        return 0
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
