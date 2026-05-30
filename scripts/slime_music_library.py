#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = REPO_ROOT / "runtime" / "slime-music-library.sqlite3"
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

        CREATE INDEX IF NOT EXISTS idx_files_duplicate_key ON files(duplicate_key);
        CREATE INDEX IF NOT EXISTS idx_files_title ON files(normalized_title);
        CREATE INDEX IF NOT EXISTS idx_files_artist ON files(normalized_artist);
        CREATE INDEX IF NOT EXISTS idx_sources_server ON sources(server);

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
                GROUP_CONCAT(sources.server || ':' || files.path, '\n') AS locations
            FROM duplicate_groups groups
            JOIN ranked ON ranked.duplicate_key = groups.duplicate_key AND ranked.row_number = 1
            JOIN preferred_files preferred ON preferred.id = ranked.id
            JOIN files ON files.duplicate_key = groups.duplicate_key
            JOIN sources ON sources.id = files.source_id
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


def command_stats(conn: sqlite3.Connection) -> None:
    summary = {
        "files": conn.execute("SELECT COUNT(*) AS value FROM files").fetchone()["value"],
        "unique_tracks": conn.execute("SELECT COUNT(*) AS value FROM duplicate_groups").fetchone()["value"],
        "duplicate_groups": conn.execute("SELECT COUNT(*) AS value FROM duplicate_groups WHERE copies > 1").fetchone()["value"],
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
    if args.command == "route":
        command_route(conn, args.path)
        return 0
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
