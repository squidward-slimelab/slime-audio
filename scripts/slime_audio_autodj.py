#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sqlite3
import json
import os
import random
import signal
import socket
import subprocess
import sys
import time
import traceback
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
    recent_play_index,
    rows_to_dicts,
    score_candidate,
)
from slime_audio_dj import (
    DEFAULT_CACHE as DEFAULT_ANALYSIS_CACHE,
    DEFAULT_TUNEBAT_LOCAL_ANALYZER,
    TrackAnalysis,
    analyze_with_cache,
    coerce_structure,
    cue_points_for_analysis,
    has_full_track_key_metadata,
    load_analysis_from_db,
    major_equivalent_tonic,
    transition_plan,
)
from slime_audio_session import (
    VOCAL_DECK,
    add_action,
    compile_actions_payload,
    parse_ms,
    parse_session,
    playhead_ms_from_state,
    prepare_load_track_action_stems,
    probe_duration_ms,
    write_payload,
)
from slime_audio_vocal_cues import audit_vocal_alignment_payload, audit_vocal_overlap_payload
from slime_music_library import DEFAULT_DB, connect, normalize

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNTIME = REPO_ROOT / "runtime"
DEFAULT_TASTE_PROFILE = DEFAULT_RUNTIME / "spotify-taste-profile.json"
DEFAULT_AUTODJ_PAUSE_FILE = DEFAULT_RUNTIME / "dj-watchdog.paused"
DEFAULT_TARGETS = ["192.168.0.123:47777", "192.168.0.163:47777"]
# First playable buffer target. Live sets should start fast and be extended
# behind the playhead (autodj extend / cron), not planned in full up front.
DEFAULT_MIN_RUNWAY_MS = 5 * 60 * 1000
DEFAULT_EXTEND_BLOCK_MS = 5 * 60 * 1000
DEFAULT_EXTEND_AHEAD_MS = 5 * 60 * 1000
DEFAULT_TARGET_LENGTH_MS = 30 * 60 * 1000
DEFAULT_ACTIVE_SET = DEFAULT_RUNTIME / "active-set.json"
DEFAULT_MAX_TRACKS = 24
DEFAULT_MIN_HARMONIC_OVERLAP_MS = 500
AUTODJ_LOCK = Path("/tmp/slime-audio-autodj.lock")
# Operator-maintained list of proof/scratch material paths to keep out of
# normal selection. Set data stays in runtime; no set names belong in code.
DEFAULT_SCRATCH_SOURCE_FILES = [DEFAULT_RUNTIME / "scratch-source-paths.txt"]
# Mic lines are authored live by the DJ agent driving the skill, never by this
# script. Autodj only publishes commentary_slots (timing plus track context) in
# its plan output so the agent can write and place its own lean-ins.
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
REMIX_QUERY_LANES = [
    "hard techno",
    "drum and bass",
    "dnb",
    "dubstep",
    "riddim",
    "bass music",
    "neurofunk",
    "jungle",
    "breakbeat",
    "techno vocal",
    "vocal",
    "acapella",
]
REMIX_RHYTHM_LANES = {
    "hard techno",
    "drum and bass",
    "dnb",
    "dubstep",
    "riddim",
    "bass music",
    "neurofunk",
    "jungle",
    "breakbeat",
}


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


class WeakSelectionError(SystemExit):
    def __init__(self, selected: list[SelectedTrack], runway_ms: int):
        self.selected = selected
        self.runway_ms = runway_ms
        super().__init__(f"only selected {len(selected)} tracks / {round(runway_ms / 60000, 1)} min; refusing weak autodj set")


@dataclass(frozen=True)
class SourceWindow:
    trim_start_ms: int
    duration_ms: int
    reason: str
    structure_kind: str | None


@dataclass(frozen=True)
class TasteProfile:
    source: str
    top_artists: set[str]
    top_tracks: set[tuple[str, str]]

    @property
    def available(self) -> bool:
        return bool(self.top_artists or self.top_tracks)


def master_tempo_bands(target_bpm: float, max_tempo_stretch_pct: float) -> list[tuple[float, float]]:
    """Analyzed-BPM ranges whose tracks can render at the master tempo.

    Straight plus double/half-time interpretations, mirroring the session
    layer's warp (slime_audio_session.master_tempo_shift_pct).
    """
    stretch = max(0.0, float(max_tempo_stretch_pct)) / 100.0
    return [
        (target_bpm * multiple / (1.0 + stretch), target_bpm * multiple * (1.0 + stretch))
        for multiple in (1.0, 2.0, 0.5)
    ]


def iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def slugify(value: str) -> str:
    normalized = normalize(value).replace(" ", "-")
    return "-".join(part for part in normalized.split("-") if part) or "autodj"


def format_ms(value: int) -> str:
    value = max(0, value)
    minutes, milliseconds = divmod(value, 60_000)
    seconds, milliseconds = divmod(milliseconds, 1000)
    return f"{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


def _profile_name(item: Any) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        return str(item.get("name") or item.get("artist") or item.get("title") or "")
    return ""


def _profile_track(item: Any) -> tuple[str, str] | None:
    if isinstance(item, dict):
        artist = normalize(str(item.get("artist") or item.get("artists") or ""))
        title = normalize(str(item.get("title") or item.get("name") or ""))
        if artist and title:
            return artist, title
    if isinstance(item, str) and " - " in item:
        artist, title = item.split(" - ", 1)
        artist_key = normalize(artist)
        title_key = normalize(title)
        if artist_key and title_key:
            return artist_key, title_key
    return None


def load_taste_profile(path: Path) -> TasteProfile:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return TasteProfile(source=str(path), top_artists=set(), top_tracks=set())
    top_artists: set[str] = set()
    for key in ("top_artists", "artists", "spotify_top_artists"):
        for item in payload.get(key, []) if isinstance(payload, dict) else []:
            name = normalize(_profile_name(item))
            if name:
                top_artists.add(name)
    top_tracks: set[tuple[str, str]] = set()
    for key in ("top_tracks", "tracks", "spotify_top_tracks"):
        for item in payload.get(key, []) if isinstance(payload, dict) else []:
            track = _profile_track(item)
            if track:
                top_tracks.add(track)
                top_artists.add(track[0])
    return TasteProfile(source=str(path), top_artists=top_artists, top_tracks=top_tracks)


def row_artist_title(row: dict[str, Any]) -> tuple[str, str]:
    return normalize(str(row.get("artist_guess") or "")), normalize(str(row.get("title_guess") or ""))


def taste_affinity(row: dict[str, Any], profile: TasteProfile) -> float:
    if not profile.available:
        return 0.0
    artist, title = row_artist_title(row)
    score = 0.0
    if artist and artist in profile.top_artists:
        score += 0.35
    if artist and title and (artist, title) in profile.top_tracks:
        score += 0.45
    return score


def is_taste_anchor(row: dict[str, Any], profile: TasteProfile) -> bool:
    return taste_affinity(row, profile) > 0


def is_downloaded_candidate(row: dict[str, Any]) -> bool:
    haystack = " ".join(
        str(row.get(field) or "")
        for field in ("preferred_path", "album_guess", "title_guess")
    ).casefold()
    return (
        "/_slime incoming/" in haystack
        or "/downloads/" in haystack
        or "/sldl/" in haystack
        or "-sldl/" in haystack
        or "fresh dig" in haystack
    )


def is_edm_bed_candidate(row: dict[str, Any]) -> bool:
    haystack = normalize(
        " ".join(
            [
                str(row.get("title_guess") or ""),
                str(row.get("artist_guess") or ""),
                str(row.get("album_guess") or ""),
                str(row.get("preferred_path") or ""),
                " ".join(str(reason) for reason in row.get("reasons") or []),
            ]
        )
    )
    for word in (
        "techno",
        "hard techno",
        "drum and bass",
        "dnb",
        "dubstep",
        "riddim",
        "bass music",
        "neurofunk",
        "jungle",
        "breakbeat",
        "sound beds",
        "rhythm lane query",
    ):
        if word in haystack:
            return True
    bpm = row.get("tunebat_bpm")
    try:
        return bpm is not None and 128 <= float(bpm) <= 180 and is_downloaded_candidate(row)
    except (TypeError, ValueError):
        return False


def load_state() -> dict[str, Any]:
    base_url = os.environ.get("SLIME_AUDIO_DASHBOARD_URL", "http://127.0.0.1:8765").rstrip("/")
    try:
        with urllib.request.urlopen(f"{base_url}/api/state", timeout=4) as response:
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


def live_session_runner_pids() -> list[int]:
    result = subprocess.run(["pgrep", "-f", "slime_audio_session_runner.py"], capture_output=True, text=True, check=False)
    return [int(pid) for pid in result.stdout.split() if pid.strip().isdigit()]


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
    by_key: dict[str, dict[str, Any]] = {}
    pool: list[dict[str, Any]] = []
    queries = [None] if args.include_broad_pool else []
    source_words: list[str] = []
    for word in normalize(f"{constraints.vibe} {constraints.direction} {constraints.notes}").split():
        if len(word) >= 4 and word not in VIBE_STOP_WORDS:
            source_words.append(word)
    queries.extend(source_words[: args.query_count])
    if args.remix_focus:
        queries.extend(REMIX_QUERY_LANES)
    queries.extend(DEFAULT_QUERY_LANES)

    def add_pool_rows(rows: list[dict[str, Any]], *, reason: str | None = None) -> None:
        for row in rows:
            key = str(row.get("duplicate_key") or row.get("preferred_path"))
            if reason:
                row.setdefault("reasons", []).append(reason)
            if key in seen:
                if reason and key in by_key:
                    by_key[key].setdefault("reasons", []).append(reason)
                continue
            seen.add(key)
            by_key[key] = row
            pool.append(row)

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
        add_pool_rows(rows, reason=f"rhythm lane query: {query}" if args.remix_focus and query in REMIX_RHYTHM_LANES else None)
    band_min = getattr(args, "min_bpm", None)
    band_max = getattr(args, "max_bpm", None)
    target_bpm = getattr(args, "target_bpm", None)
    if target_bpm is not None and band_min is None and band_max is None:
        bands = master_tempo_bands(float(target_bpm), float(getattr(args, "max_tempo_stretch_pct", 16.0)))
    elif band_min is not None or band_max is not None:
        bands = [(band_min, band_max)]
    else:
        bands = []
    for low, high in bands:
        # Tempo-column browsing: pull the whole analyzed BPM band directly so a
        # requested tempo range never depends on random pool draws. A master
        # tempo pulls its double/half-time octave bands too — a 175 BPM track
        # is in-band for a 90 BPM set at half-time feel.
        rows = candidate_rows(
            conn,
            constraints,
            history_path=args.history,
            recent_limit=args.recent_limit,
            limit=args.sql_pool_limit,
            query=None,
            pool_limit=args.sql_pool_limit,
            bpm_range=(low, high),
        )
        add_pool_rows(rows, reason=f"tempo band {low or 0:g}-{high or 999:g} bpm")
    if args.stem_aware_remix:
        stem_filters = ["t.preferred_path IS NOT NULL", "lower(t.preferred_path) NOT LIKE '%/separated/%'"]
        stem_params: list[Any] = []
        stem_rows = conn.execute(
            f"""
            SELECT
                t.duplicate_key,
                t.title_guess,
                t.artist_guess,
                t.album_guess,
                t.preferred_path,
                t.preferred_server,
                t.copies,
                t.server_count,
                t.preferred_quality_score,
                t.tunebat_bpm,
                t.tunebat_key,
                t.tunebat_mode,
                t.tunebat_camelot,
                t.tunebat_energy,
                t.tunebat_danceability,
                t.tunebat_happiness
            FROM tracks t
            JOIN track_stem_sets ss ON ss.duplicate_key = t.duplicate_key OR ss.source_path = t.preferred_path
            JOIN track_stems st ON st.stem_set_id = ss.id
            WHERE ss.status = 'ready'
              AND {" AND ".join(stem_filters)}
            GROUP BY t.duplicate_key
            HAVING COUNT(DISTINCT st.stem_name) >= 4
            ORDER BY t.preferred_quality_score DESC, t.copies DESC, t.server_count DESC
            LIMIT ?
            """,
            stem_params + [args.sql_pool_limit],
        ).fetchall()
        stem_candidates: list[dict[str, Any]] = []
        for row in rows_to_dicts(stem_rows):
            play_meta = recent_play_index(conn, args.history, args.recent_limit).get(str(row.get("duplicate_key"))) or {}
            row["last_played_at"] = play_meta.get("last_played_at")
            row["plays_seen"] = play_meta.get("plays_seen", 0)
            score, reasons = score_candidate(row, constraints, play_meta=play_meta)
            row["score"] = score
            row["reasons"] = reasons
            stem_candidates.append(row)
        add_pool_rows(stem_candidates, reason="stem-ready inventory lane")
    pool.sort(key=lambda item: float(item.get("score") or 0.0), reverse=True)
    return pool


def selected_tracks_from_paths(paths: list[Path], args: argparse.Namespace) -> list[SelectedTrack]:
    """Hand-picked arrangement: the DJ chose these tracks in this order. They
    still go through analysis, arrangement, the planner, and the guards."""
    conn = connect(args.db)
    selected: list[SelectedTrack] = []
    for path in paths:
        resolved = str(path)
        if not Path(resolved).exists():
            raise SystemExit(f"picked track does not exist: {resolved}")
        row = conn.execute(
            "SELECT artist_guess, title_guess, album_guess FROM tracks WHERE preferred_path = ?",
            (resolved,),
        ).fetchone()
        artist, title, album = (row if row else ("", Path(resolved).stem, ""))
        selected.append(
            SelectedTrack(
                path=resolved,
                artist=str(artist or ""),
                title=str(title or Path(resolved).stem),
                album=str(album or ""),
                score=1.0,
                duration_ms=probe_duration_ms(resolved),
                last_played_at=None,
                plays_seen=0,
                reasons=["hand-picked by the DJ"],
            )
        )
    if len(selected) < 2:
        raise SystemExit("a hand-picked set needs at least 2 tracks")
    return selected


def apply_arrangement_default(args: argparse.Namespace) -> None:
    if (getattr(args, "remix_focus", False) or getattr(args, "stem_aware_remix", False)) and "--arrangement" not in sys.argv:
        args.arrangement = "sections"


def select_tracks(args: argparse.Namespace, *, exclude_paths: set[str] | None = None) -> list[SelectedTrack]:
    apply_arrangement_default(args)
    picked = [Path(p) for p in (getattr(args, "track", None) or [])]
    if picked:
        return selected_tracks_from_paths(picked, args)
    pool = candidate_pool(args)
    if exclude_paths:
        pool = [row for row in pool if str(row.get("preferred_path") or "") not in exclude_paths]
    if not pool:
        raise SystemExit("no candidates available")
    if bool(getattr(args, "stem_aware_remix", False)):
        # Prefer tracks with ready stems, but never let stem coverage narrow
        # taste: selection is driven by the library, history, and constraints,
        # and missing stems are queued for background splitting instead.
        ready_paths = ready_stem_source_paths(Path(getattr(args, "db", DEFAULT_DB)))
        for row in pool:
            if str(row.get("preferred_path") or "") in ready_paths:
                row["stem_ready"] = True
                row.setdefault("reasons", []).append("ready stem artifacts")
    pool = apply_scratch_material_policy(pool, args)
    pool = apply_recent_material_policy(pool, args)
    min_bpm = getattr(args, "min_bpm", None)
    max_bpm = getattr(args, "max_bpm", None)
    target_bpm = getattr(args, "target_bpm", None)
    if target_bpm is not None and min_bpm is None and max_bpm is None:
        bands = master_tempo_bands(float(target_bpm), float(getattr(args, "max_tempo_stretch_pct", 16.0)))
    elif min_bpm is not None or max_bpm is not None:
        bands = [(min_bpm, max_bpm)]
    else:
        bands = []
    if bands:
        # Tempo-column browsing: the band prefilters the pool so ranking
        # windows only ever see in-band tracks (a master tempo accepts its
        # double/half-time octaves too).
        def in_band(bpm: float) -> bool:
            return any((low is None or bpm >= low) and (high is None or bpm <= high) for low, high in bands)

        pool = [row for row in pool if row.get("tunebat_bpm") is not None and in_band(float(row["tunebat_bpm"]))]
        if not pool:
            raise SystemExit("no candidates inside the requested BPM band")
    rng = random.SystemRandom()
    taste_profile = load_taste_profile(args.taste_profile)
    artist_counts: Counter[str] = Counter()
    title_counts: Counter[str] = Counter()
    selected: list[SelectedTrack] = []
    selected_keys: set[str] = set()
    runway_ms = 0
    top_window = min(len(pool), max(args.max_tracks * 12, 120))
    max_per_artist = int(getattr(args, "max_per_artist", 1) or 1)
    stem_remix_mode = bool(getattr(args, "stem_aware_remix", False) and getattr(args, "remix_focus", False))
    if stem_remix_mode:
        max_per_artist = max(max_per_artist, 3)
    # Per-track structure rejection is the norm now (no fallback windows), so
    # always carry a small surplus past min_tracks or a single rejected track
    # fails the whole selection.
    target_tracks_before_runway_stop = min(args.max_tracks, args.min_tracks + 2 + (2 if stem_remix_mode else 0))
    for row in pool:
        edm_bed = is_edm_bed_candidate(row)
        row["edm_bed_discretion"] = edm_bed
        if edm_bed:
            row.setdefault("reasons", []).append("edm bed discretion lane")
        affinity = 0.0 if edm_bed else taste_affinity(row, taste_profile)
        row["spotify_taste_affinity"] = affinity
        if affinity:
            row.setdefault("reasons", []).append(f"spotify taste affinity {affinity:.2f}")
        if is_downloaded_candidate(row):
            row["downloaded_material"] = True
            row.setdefault("reasons", []).append("downloaded material lane")
            if taste_profile.available and not edm_bed and not is_taste_anchor(row, taste_profile):
                row["spotify_leftfield_download"] = True
                row.setdefault("reasons", []).append("spotify left-field download lane")
    def selection_score(item: dict[str, Any]) -> float:
        # Small readiness bonus acts as a tie-break inside a taste band; it
        # must stay well below typical score gaps so stems never drive taste.
        return (
            float(item.get("score") or 0.0)
            + float(item.get("spotify_taste_affinity") or 0.0)
            + (0.15 if item.get("stem_ready") else 0.0)
            + rng.uniform(0.0, args.selection_jitter)
        )

    quota_ranked = list(pool)
    rng.shuffle(quota_ranked)
    quota_ranked.sort(key=selection_score, reverse=True)
    ranked = list(pool[:top_window])
    rng.shuffle(ranked)
    ranked.sort(key=selection_score, reverse=True)

    def try_select(row: dict[str, Any]) -> bool:
        nonlocal runway_ms
        key = str(row.get("duplicate_key") or row.get("preferred_path") or "")
        if key in selected_keys:
            return False
        if len(selected) >= args.max_tracks:
            return False
        path = str(row.get("preferred_path") or "")
        if not path or not Path(path).exists():
            return False
        haystack = normalize(
            " ".join(str(row.get(key) or "") for key in ("title_guess", "artist_guess", "album_guess", "preferred_path"))
        )
        if any(normalize(term) in haystack for term in args.skip_term):
            return False
        if args.require_analysis and not (row.get("tunebat_bpm") or row.get("tunebat_energy") is not None):
            return False

        if args.min_score is not None and float(row.get("score") or 0.0) < args.min_score:
            return False
        artist = str(row.get("artist_guess") or "").strip()
        artist_key = normalize(artist) or artist.casefold()
        title_key = normalize(str(row.get("title_guess") or ""))
        if artist_key and artist_counts[artist_key] >= max_per_artist:
            return False
        if title_key and title_counts[title_key] >= 1:
            return False
        duration_ms = probe_duration_ms(path)
        if duration_ms is not None and duration_ms < args.min_track_ms:
            return False
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
        selected_keys.add(key)
        if artist_key:
            artist_counts[artist_key] += 1
        if title_key:
            title_counts[title_key] += 1
        # Runway must approximate scheduled timeline time: full tracks in the
        # default arrangement, capped section windows in the remix lanes.
        if str(getattr(args, "arrangement", "full")) == "full":
            runway_ms += min(duration_ms or args.default_track_ms, int(getattr(args, "max_full_lead_ms", 480_000)))
        else:
            runway_ms += min(duration_ms or args.default_track_ms, args.max_lead_clip_ms)
        return True

    download_target = 0
    if args.downloaded_track_ratio > 0 and args.max_tracks >= 10:
        download_target = max(1, round(args.max_tracks * args.downloaded_track_ratio))
    leftfield_download_target = 0
    if taste_profile.available and download_target > 0 and args.leftfield_download_ratio > 0:
        leftfield_download_target = max(1, round(download_target * args.leftfield_download_ratio))

    for row in quota_ranked:
        if sum(1 for track in selected if "spotify left-field download lane" in track.reasons) >= leftfield_download_target:
            break
        if row.get("spotify_leftfield_download"):
            try_select(row)
    for row in quota_ranked:
        if sum(1 for track in selected if "downloaded material lane" in track.reasons) >= download_target:
            break
        if row.get("downloaded_material"):
            try_select(row)
    runway_stop_ms = args.min_runway_ms
    for row in ranked:
        if try_select(row):
            if len(selected) >= args.max_tracks:
                break
            if len(selected) >= target_tracks_before_runway_stop and runway_ms >= runway_stop_ms:
                break

    if len(selected) < args.min_tracks or runway_ms < args.min_runway_ms:
        raise WeakSelectionError(selected, runway_ms)
    selected.sort(key=lambda track: track.score, reverse=True)
    return selected


def apply_recent_material_policy(rows: list[dict[str, Any]], args: argparse.Namespace) -> list[dict[str, Any]]:
    policy = str(getattr(args, "recent_material_policy", "penalty") or "penalty")
    if policy in {"off", "penalty"}:
        return rows
    fresh_rows = [row for row in rows if int(row.get("plays_seen") or 0) <= 0]
    if fresh_rows:
        return fresh_rows
    raise SystemExit("recent material policy banned every candidate; refusing repeat-heavy autodj set")


def ready_stem_source_paths(db_path: Path) -> set[str]:
    try:
        with sqlite3.connect(db_path) as db:
            rows = db.execute(
                """
                SELECT s.source_path AS path
                FROM track_stem_sets s
                JOIN track_stems st ON st.stem_set_id = s.id
                WHERE s.status = 'ready'
                GROUP BY s.id, s.source_path
                HAVING COUNT(DISTINCT st.stem_name) >= 4
                """
            ).fetchall()
            preferred_rows = db.execute(
                """
                SELECT t.preferred_path AS path
                FROM track_stem_sets s
                JOIN track_stems st ON st.stem_set_id = s.id
                JOIN tracks t ON t.duplicate_key = s.duplicate_key
                WHERE s.status = 'ready'
                  AND t.preferred_path IS NOT NULL
                GROUP BY s.id, t.preferred_path
                HAVING COUNT(DISTINCT st.stem_name) >= 4
                """
            ).fetchall()
        return {str(row[0]) for row in rows + preferred_rows}
    except sqlite3.Error:
        return set()


def _scratch_paths_from_json(value: Any) -> set[str]:
    paths: set[str] = set()
    if isinstance(value, dict):
        for key in ("path", "source_path", "track_path", "preferred_path", "resolved_track", "track"):
            item = value.get(key)
            if isinstance(item, str) and item.startswith("/"):
                paths.add(item)
        for item in value.get("paths") or []:
            if isinstance(item, str) and item.startswith("/"):
                paths.add(item)
        for item in value.values():
            paths.update(_scratch_paths_from_json(item))
    elif isinstance(value, list):
        for item in value:
            paths.update(_scratch_paths_from_json(item))
    return paths


def load_scratch_material_index(db_path: Path, source_files: list[Path]) -> tuple[set[str], set[str]]:
    paths: set[str] = set()
    for source_file in source_files:
        try:
            text = source_file.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            paths.update(_scratch_paths_from_json(json.loads(text)))
            continue
        except json.JSONDecodeError:
            pass
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("/"):
                paths.add(line)
    duplicate_keys: set[str] = set()
    if paths:
        try:
            conn = connect(db_path)
            placeholders = ",".join("?" for _ in paths)
            rows = conn.execute(
                f"""
                SELECT duplicate_key
                FROM files
                WHERE path IN ({placeholders})
                UNION
                SELECT duplicate_key
                FROM tracks
                WHERE preferred_path IN ({placeholders})
                """,
                [*paths, *paths],
            ).fetchall()
            duplicate_keys = {str(row["duplicate_key"]) for row in rows if row["duplicate_key"]}
        except (sqlite3.Error, OSError):
            duplicate_keys = set()
    return paths, duplicate_keys


def apply_scratch_material_policy(pool: list[dict[str, Any]], args: argparse.Namespace) -> list[dict[str, Any]]:
    policy = str(getattr(args, "scratch_material_policy", "ban") or "ban")
    if policy == "off" or not pool:
        return pool
    source_files = [Path(item) for item in (getattr(args, "scratch_source_file", None) or DEFAULT_SCRATCH_SOURCE_FILES)]
    scratch_paths, scratch_keys = load_scratch_material_index(Path(getattr(args, "db", DEFAULT_DB)), source_files)
    if not scratch_paths and not scratch_keys:
        return pool
    penalty = float(getattr(args, "scratch_material_penalty", 0.8) or 0.8)
    fresh: list[dict[str, Any]] = []
    scratch: list[dict[str, Any]] = []
    for row in pool:
        path = str(row.get("preferred_path") or "")
        key = str(row.get("duplicate_key") or "")
        if path in scratch_paths or key in scratch_keys:
            row = dict(row)
            row["scratch_material"] = True
            row.setdefault("reasons", []).append(f"scratch/proof material policy: {policy}")
            if policy == "penalty":
                row["score"] = round(float(row.get("score") or 0.0) - penalty, 4)
            scratch.append(row)
        else:
            fresh.append(row)
    min_required = max(1, int(getattr(args, "min_tracks", 1) or 1))
    if policy == "ban" and len(fresh) >= min_required:
        return fresh
    return [*fresh, *scratch]


def material_score(track: SelectedTrack, words: tuple[str, ...]) -> int:
    haystack = normalize(" ".join([track.path, track.artist, track.title, track.album, *track.reasons]))
    return sum(1 for word in words if word in haystack)


def track_bpm(track: SelectedTrack) -> float | None:
    for reason in track.reasons:
        if not reason.startswith("bpm "):
            continue
        try:
            return float(reason.split(" ", 1)[1])
        except (IndexError, ValueError):
            return None
    return None


def rhythm_bed_score(track: SelectedTrack) -> int:
    score = material_score(
        track,
        (
            "techno",
            "electronic",
            "breakbeat",
            "dubstep",
            "industrial",
            "garage",
            "drum and bass",
            "jungle",
            "bass",
            "hard techno",
            "riddim",
            "neurofunk",
            "dnb",
        ),
    )
    bpm = track_bpm(track)
    has_rhythm_lane = material_score(track, ("rhythm lane query",)) > 0
    if bpm is not None and (128 <= bpm <= 180) and has_rhythm_lane:
        score += 1
    if score > 0 and track.duration_ms and track.duration_ms >= 120_000:
        score += 1
    return score


def lead_score(track: SelectedTrack) -> int:
    score = material_score(track, ("vocal", "rap", "hip hop", "punk", "feat", "with", "song", "acapella", "hook"))
    if rhythm_bed_score(track) > 1:
        score -= 2
    if track.duration_ms and track.duration_ms >= 90_000:
        score += 1
    return score


def stem_readiness_report(selected: list[SelectedTrack], args: argparse.Namespace) -> dict[str, Any]:
    if not args.stem_aware_remix:
        return {"required": False}
    try:
        conn = connect(args.db)
        conn.execute("SELECT 1 FROM track_stem_sets LIMIT 1")
    except (sqlite3.Error, OSError) as exc:
        return {"required": True, "ready": 0, "checked": 0, "error": str(exc)}
    tracks: list[dict[str, Any]] = []
    ready_count = 0
    for track in selected:
        stem_set = conn.execute(
            """
            SELECT id, artifact_root
            FROM track_stem_sets
            WHERE source_path = ? AND status = 'ready'
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (track.path,),
        ).fetchone()
        windows: list[dict[str, Any]] = []
        if stem_set is not None:
            ready_count += 1
            rows = conn.execute(
                """
                SELECT stem_name, kind, start_ms, end_ms, confidence
                FROM track_stem_windows
                WHERE stem_set_id = ?
                  AND kind IN ('vocal_present', 'vocal_absent', 'instrumental_pocket', 'bass_active', 'drums_active')
                ORDER BY start_ms
                LIMIT 24
                """,
                (stem_set["id"],),
            ).fetchall()
            windows = [dict(row) for row in rows]
        tracks.append(
            {
                "path": track.path,
                "artist": track.artist,
                "title": track.title,
                "ready": stem_set is not None,
                "stem_set_id": stem_set["id"] if stem_set is not None else None,
                "windows": windows,
            }
        )
    return {
        "required": True,
        "checked": len(selected),
        "ready": ready_count,
        "policy": "use ready vocal/bass/drum stem windows for vocal remixes; avoid vocal-on-vocal and keep one bass stem active unless chopping/trading",
        "tracks": tracks,
    }


def load_or_analyze_selected(selected: list[SelectedTrack], args: argparse.Namespace) -> dict[str, TrackAnalysis]:
    analyses: dict[str, TrackAnalysis] = {}
    missing: list[Path] = []
    for track in selected:
        path = Path(track.path)
        analysis = load_analysis_from_db(args.db, path)
        if analysis is None or not has_full_track_key_metadata(analysis):
            missing.append(path)
        else:
            analyses[track.path] = analysis
    if missing:
        for analysis in analyze_with_cache(
            missing,
            args.analysis_cache,
            args.analysis_backend,
            args.analysis_sample_rate,
            args.db,
            args.tunebat_analyzer,
        ):
            analyses[analysis.path] = analysis
    return analyses


def full_track_window(track: SelectedTrack, analysis: TrackAnalysis | None, args: argparse.Namespace) -> SourceWindow:
    """A set is full songs mixed into each other. Play the whole track (capped
    only for marathon recordings); the planner shapes the transitions."""
    source_duration_ms = track.duration_ms or args.default_track_ms
    duration_ms = min(source_duration_ms, int(getattr(args, "max_full_lead_ms", 480_000)))
    return SourceWindow(0, duration_ms, "full-track", None)


def source_window_for_track(track: SelectedTrack, analysis: TrackAnalysis | None, args: argparse.Namespace, *, fast_mode: bool) -> SourceWindow:
    if str(getattr(args, "arrangement", "full")) == "full":
        return full_track_window(track, analysis, args)
    source_duration_ms = track.duration_ms or args.default_track_ms
    max_clip_ms = args.max_fast_lead_clip_ms if fast_mode else args.max_lead_clip_ms
    min_clip_ms = min(args.min_section_clip_ms, max_clip_ms)
    min_anchor_ms = min(args.min_anchor_section_ms, min_clip_ms)
    if analysis is not None:
        windows = [
            window
            for window in coerce_structure(analysis.structure)
            if window.kind not in {"outro"}
            and window.confidence >= args.min_section_confidence
            and window.end_ms > window.start_ms
        ]
        candidates: list[SourceWindow] = []
        for window in windows:
            duration_ms = window.end_ms - window.start_ms
            required_ms = min_clip_ms if window.kind == "intro" else min_anchor_ms
            if duration_ms < required_ms:
                continue
            source_remaining_ms = source_duration_ms - window.start_ms
            clipped_duration_ms = min(source_remaining_ms, max_clip_ms)
            if clipped_duration_ms < min_clip_ms:
                continue
            candidates.append(
                SourceWindow(
                    trim_start_ms=window.start_ms,
                    duration_ms=clipped_duration_ms,
                    reason=f"structure:{window.kind}",
                    structure_kind=window.kind,
                )
            )
        if candidates:
            priority = {"drop": 0, "build": 1, "breakdown": 2, "intro": 3}
            candidates.sort(key=lambda item: (priority.get(str(item.structure_kind), 9), item.trim_start_ms))
            return candidates[0]
        cues = [
            cue
            for cue in cue_points_for_analysis(analysis)
            if cue.kind in {"drop", "hook", "clean_intro", "safe_loop"}
            and cue.end_ms is not None
            and cue.end_ms > cue.at_ms
            and cue.end_ms - cue.at_ms >= (min_clip_ms if cue.kind in {"clean_intro", "safe_loop"} else min_anchor_ms)
        ]
        if cues:
            cue = sorted(cues, key=lambda item: (item.kind not in {"drop", "hook"}, item.at_ms))[0]
            source_remaining_ms = source_duration_ms - cue.at_ms
            duration_ms = min(source_remaining_ms, max_clip_ms)
            if duration_ms < min_clip_ms:
                raise SystemExit(f"no defensible structure window for {track.artist} - {track.title}")
            return SourceWindow(
                trim_start_ms=cue.at_ms,
                duration_ms=duration_ms,
                reason=f"cue:{cue.kind}",
                structure_kind=cue.kind,
            )
    # No fallback window. A lead without a defensible analyzed section is
    # rejected per-track by filter_defensible_source_tracks, and selection
    # simply picks other candidates; "play the first 90 seconds from 0:00" is
    # not a DJ move.
    raise SystemExit(f"no defensible structure window for {track.artist} - {track.title}")


def fast_section_mode_for(tracks: list[SelectedTrack]) -> bool:
    rhythm_sources = [track for track in tracks if rhythm_bed_score(track) > 0]
    return any(material_score(track, ("dubstep", "bass", "drum and bass", "dnb", "riddim", "heavy")) > 0 for track in rhythm_sources)


def filter_defensible_source_tracks(
    selected: list[SelectedTrack],
    analyses: dict[str, TrackAnalysis],
    args: argparse.Namespace,
) -> tuple[list[SelectedTrack], list[dict[str, str]]]:
    if str(getattr(args, "arrangement", "full")) == "full":
        return selected, []
    fast_mode = fast_section_mode_for(selected)
    accepted: list[SelectedTrack] = []
    rejected: list[dict[str, str]] = []
    for track in selected:
        try:
            source_window_for_track(track, analyses.get(track.path), args, fast_mode=fast_mode)
        except SystemExit as exc:
            rejected.append(
                {
                    "path": track.path,
                    "artist": track.artist,
                    "title": track.title,
                    "reason": str(exc),
                }
            )
            continue
        accepted.append(track)
    if len(accepted) < args.min_tracks:
        summary = "; ".join(f"{item['artist']} - {item['title']}" for item in rejected[:5])
        raise SystemExit(f"only {len(accepted)} defensible structured track(s); rejected {len(rejected)} ({summary})")
    return accepted, rejected


def queue_stem_splits(paths: list[str], args: argparse.Namespace, *, reason: str) -> None:
    if not paths:
        return
    queue_path = Path(getattr(args, "runtime", DEFAULT_RUNTIME)) / "stem-split-queue.jsonl"
    queued: set[str] = set()
    if queue_path.exists():
        for line in queue_path.read_text(encoding="utf-8").splitlines():
            try:
                queued.add(str(json.loads(line).get("path") or ""))
            except (json.JSONDecodeError, AttributeError):
                continue
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    with queue_path.open("a", encoding="utf-8") as handle:
        for path in paths:
            if path in queued:
                continue
            handle.write(json.dumps({"path": path, "queued_at": iso_now(), "reason": reason}, sort_keys=True) + "\n")
            queued.add(path)


def key_alignment_cost(left: TrackAnalysis | None, right: TrackAnalysis | None, max_shift: int = 2) -> int:
    if (
        left is None or right is None
        or left.tonic is None or left.mode not in {"major", "minor"}
        or right.tonic is None or right.mode not in {"major", "minor"}
    ):
        return 50
    base = major_equivalent_tonic(left.tonic % 12, left.mode)
    for magnitude in range(0, max_shift + 1):
        for shift in ({0} if magnitude == 0 else {magnitude, -magnitude}):
            if major_equivalent_tonic((right.tonic + shift) % 12, right.mode) == base:
                return magnitude
    return 40


def order_by_key_chain(selected: list[SelectedTrack], analyses: dict[str, TrackAnalysis]) -> list[SelectedTrack]:
    """Harmonic sequencing: greedily chain tracks so adjacent keys are
    alignable, turning would-be cuts into blends. What a DJ does with the
    Camelot wheel before touching the decks."""
    if len(selected) < 3:
        return selected
    remaining = list(selected[1:])
    ordered = [selected[0]]
    while remaining:
        current = analyses.get(ordered[-1].path)
        best = min(remaining, key=lambda track: (key_alignment_cost(current, analyses.get(track.path)), -track.score))
        ordered.append(best)
        remaining.remove(best)
    return ordered


def clip_tempo_factor_for(action: dict[str, Any], analysis: TrackAnalysis | None, args: argparse.Namespace) -> float:
    """Rendered tempo factor of a lead under the master (mirrors the session
    layer's warp so beat math can run at authoring time)."""
    target = getattr(args, "target_bpm", None)
    if target is None or analysis is None or not analysis.bpm:
        return 1.0
    from slime_audio_session import master_tempo_shift_pct

    shift = master_tempo_shift_pct(float(analysis.bpm), float(target), abs(float(getattr(args, "max_tempo_stretch_pct", 16.0))))
    return 1.0 + (shift or 0.0) / 100.0


def snap_to_host_beat(
    timeline_ms: int,
    host_action: dict[str, Any],
    host_analysis: TrackAnalysis | None,
    args: argparse.Namespace,
    *,
    bars: bool = True,
) -> int:
    """Snap a timeline instant onto the host record's rendered beat grid.

    Entries and toggles authored at raw millisecond arithmetic land off the
    beat and sound like stumbles; every woven layer must enter where the host
    record's grid says beats live.
    """
    grid = getattr(host_analysis, "beatgrid", None) if host_analysis else None
    if grid is None or not grid.bpm or grid.beat_offset_ms is None:
        return timeline_ms
    factor = clip_tempo_factor_for(host_action, host_analysis, args)
    start = int(host_action.get("at_ms") or 0)
    trim = int(host_action.get("trim_start_ms") or 0)
    beat_src = 60_000.0 / float(grid.bpm)
    step = beat_src * (4 if bars else 1)
    source_pos = trim + (timeline_ms - start) * factor
    k = round((source_pos - grid.beat_offset_ms) / step)
    snapped_source = grid.beat_offset_ms + k * step
    return int(round(start + (snapped_source - trim) / factor))


def phase_aligned_trim(
    entry_timeline_ms: int,
    host_action: dict[str, Any],
    host_analysis: TrackAnalysis | None,
    guest_analysis: TrackAnalysis | None,
    base_trim_ms: int,
    args: argparse.Namespace,
) -> int:
    """Trim for a guest layer so its beats land on the host's beats.

    Two records at the same rendered BPM but arbitrary phase produce flams;
    a guest stem layer must start ON one of its own beats at an instant the
    host is also on a beat. Entry time should already be host-beat-snapped."""
    grid = getattr(guest_analysis, "beatgrid", None) if guest_analysis else None
    if grid is None or not grid.bpm or grid.beat_offset_ms is None:
        return base_trim_ms
    beat = 60_000.0 / float(grid.bpm)
    k = max(0, -(-(base_trim_ms - grid.beat_offset_ms) // beat))  # ceil to next beat at/after base trim
    import math

    k = math.ceil((base_trim_ms - grid.beat_offset_ms) / beat)
    return int(round(grid.beat_offset_ms + max(0, k) * beat))


def weave_arrangement(
    lead_actions: list[dict[str, Any]],
    analyses: dict[str, TrackAnalysis],
    args: argparse.Namespace,
    *,
    occupancy: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Weave the set: the body is remixed, not just the junctions.

    Leads group into chapters; each chapter's opening record donates its
    drums as a foundation groove that keeps running under the following
    leads, and mid-chapter the NEXT record's vocal rides over the current
    record's instrumental for two phrases (its own vocal toggled out) — the
    tease. Songs happen over a persistent groove instead of in sequence.
    A playlist with transitions is not the assignment.

    Only stems-ready material participates; sparse coverage degrades toward
    the plain sequence gracefully. Requires a master tempo (the weave only
    makes sense on one grid).
    """
    if getattr(args, "target_bpm", None) is None or len(lead_actions) < 2:
        return []
    ready_paths = ready_stem_source_paths(Path(getattr(args, "db", DEFAULT_DB)))
    woven: list[dict[str, Any]] = []

    def deck_free(deck: str, start_ms: int, end_ms: int) -> bool:
        for event in (occupancy or []) + woven:
            if event.get("type") != "load_track" or str(event.get("deck")) != deck:
                continue
            ev_start = int(event.get("at_ms") or 0)
            ev_end = ev_start + int(event.get("duration_ms") or 0)
            if ev_start < end_ms and start_ms < ev_end:
                return False
        return True
    chapter_size = max(2, int(getattr(args, "chapter_size", 3) or 3))
    for chapter_start in range(0, len(lead_actions), chapter_size):
        chapter = lead_actions[chapter_start : chapter_start + chapter_size]
        if len(chapter) < 2:
            continue
        foundation = chapter[0]
        foundation_path = str(foundation.get("source_path") or "")
        foundation_analysis = analyses.get(foundation_path)
        if foundation_path in ready_paths:
            # The groove is a DRUM SWAP, per lead: the host record's own drums
            # step out while the foundation's drums carry — audible by
            # construction, at real volume, never a -7dB stack a listener
            # cannot hear. One segment per following lead, beat-snapped to
            # the host's grid and phase-aligned, kept a phrase clear of the
            # junction windows on both ends.
            for host in chapter[1:]:
                host_path = str(host.get("source_path") or "")
                if host_path not in ready_paths:
                    continue
                host_analysis = analyses.get(host_path)
                host_start = int(host.get("at_ms") or 0)
                host_end = host_start + int(host.get("duration_ms") or 0)
                window_start = snap_to_host_beat(host_start + 40_000, host, host_analysis, args)
                window_end = snap_to_host_beat(host_end - 40_000, host, host_analysis, args)
                if window_end - window_start < 32_000 or not deck_free("deck-1", window_start, window_end):
                    continue
                base_trim = int(foundation.get("trim_start_ms") or 0)
                bed = {
                    "type": "load_track",
                    "id": f"{host['id']}-groove-swap",
                    "deck": "deck-1",
                    "source_path": foundation_path,
                    "at_ms": window_start,
                    "trim_start_ms": phase_aligned_trim(window_start, host, host_analysis, foundation_analysis, base_trim, args),
                    "duration_ms": window_end - window_start,
                    "gain_db": -3.0,
                    "play_stems": ["drums"],
                    "fade_in_ms": 400,
                    "fade_out_ms": 400,
                    "planner_role": "arrangement-groove",
                    "kind": "bed",
                }
                if foundation_analysis is not None and foundation_analysis.bpm:
                    bed["source_bpm"] = float(foundation_analysis.bpm)
                woven.append(bed)
                woven.append({
                    "type": "stem_toggle",
                    "id": f"{host['id']}-drums-out-for-groove",
                    "target": host["id"],
                    "stem": "drums",
                    "enabled": False,
                    "at_ms": window_start,
                    "planner_role": "arrangement-groove",
                })
                woven.append({
                    "type": "stem_toggle",
                    "id": f"{host['id']}-drums-back",
                    "target": host["id"],
                    "stem": "drums",
                    "enabled": True,
                    "at_ms": window_end,
                    "planner_role": "arrangement-groove",
                })
        # The tease: next record's vocal over this record's instrumental tail,
        # pitched into the host's tonal center (relative-major alignment, max
        # 2 st) — or skipped when the keys genuinely don't reach.
        for current, following in zip(chapter, chapter[1:]):
            follow_path = str(following.get("source_path") or "")
            current_path = str(current.get("source_path") or "")
            if follow_path not in ready_paths or current_path not in ready_paths:
                continue
            current_analysis = analyses.get(current_path)
            follow_analysis_key = analyses.get(follow_path)
            if (
                current_analysis is None or follow_analysis_key is None
                or current_analysis.tonic is None or follow_analysis_key.tonic is None
                or current_analysis.mode not in ("major", "minor") or follow_analysis_key.mode not in ("major", "minor")
            ):
                continue
            def _rel(tonic, mode):
                return (int(tonic) + 3) % 12 if mode == "minor" else int(tonic) % 12
            tease_delta = (_rel(current_analysis.tonic, current_analysis.mode) - _rel(follow_analysis_key.tonic, follow_analysis_key.mode)) % 12
            if tease_delta > 6:
                tease_delta -= 12
            if abs(tease_delta) > 2:
                continue
            current_end = int(current.get("at_ms") or 0) + int(current.get("duration_ms") or 0)
            tease_ms = 24_000
            tease_at = max(int(current.get("at_ms") or 0), current_end - tease_ms - 8_000)
            tease_at = snap_to_host_beat(tease_at, current, current_analysis, args)
            if current_end - tease_at < 16_000 or not deck_free("deck-4", tease_at, current_end):
                continue
            follow_analysis = analyses.get(follow_path)
            tease = {
                "type": "load_track",
                "id": f"{following['id']}-tease",
                "deck": "deck-4",
                "source_path": follow_path,
                "at_ms": tease_at,
                "trim_start_ms": phase_aligned_trim(tease_at, current, current_analysis, follow_analysis_key, int(following.get("trim_start_ms") or 0), args),
                "duration_ms": min(tease_ms, current_end - tease_at),
                "gain_db": -3.0,
                "play_stems": ["vocals"],
                "fade_in_ms": 1_500,
                "fade_out_ms": 1_500,
                "planner_role": "arrangement-tease",
                "kind": "tease",
                "pitch_shift_semitones": tease_delta,
                "keymatch": False,
            }
            if follow_analysis is not None:
                if follow_analysis.bpm:
                    tease["source_bpm"] = float(follow_analysis.bpm)
                for key in ("key", "tonic", "mode", "camelot"):
                    value = getattr(follow_analysis, key, None)
                    if value is not None:
                        tease[key] = value
            woven.append(tease)
            # The host record steps its own vocal out under the tease.
            woven.append({
                "type": "stem_toggle",
                "id": f"{current['id']}-vocal-out-for-tease",
                "target": current["id"],
                "stem": "vocals",
                "enabled": False,
                "at_ms": tease_at,
                "planner_role": "arrangement-tease",
            })
    return woven


def session_payload(selected: list[SelectedTrack], args: argparse.Namespace, analyses: dict[str, TrackAnalysis] | None = None) -> dict[str, Any]:
    analyses = analyses or {}
    if getattr(args, "target_bpm", None) is not None and not getattr(args, "track", None):
        selected = order_by_key_chain(selected, analyses)
    rhythm_sources = [track for track in selected if rhythm_bed_score(track) > 0]
    leads = [track for track in selected if track not in rhythm_sources]
    if len(leads) < args.min_tracks:
        leads = sorted(selected, key=lead_score, reverse=True)

    fast_mode = fast_section_mode_for(selected)
    # A hand-picked tracklist is the arrangement; never silently truncate it.
    lead_cap = len(leads) if getattr(args, "track", None) else args.max_tracks
    cursor_ms = 0
    lead_clips: list[dict[str, Any]] = []
    lead_actions: list[dict[str, Any]] = []
    stem_ready_paths = ready_stem_source_paths(Path(getattr(args, "db", DEFAULT_DB)))
    stem_split_queued: list[str] = []
    transition_plans: list[dict[str, Any]] = []
    previous_track: SelectedTrack | None = None
    for index, track in enumerate(leads[:lead_cap]):
        source_window = source_window_for_track(track, analyses.get(track.path), args, fast_mode=fast_mode)
        event_id = f"lead-{index + 1:03d}-{slugify(track.title)[:40]}"
        deck = "deck-2" if index % 2 == 0 else "deck-3"
        analysis = analyses.get(track.path)
        # The session owns tempo, DAW-style: leads carry their analyzed
        # source_bpm and the session layer warps them to master_bpm (straight
        # or double/half-time) on every load. Leads render neutral when there
        # is no master or no analysis. Pairwise arrangement-time warping
        # (chasing each neighbor's tempo/key) chained into +/-16% tempo and
        # 4-semitone swings between songs; corrections belong to the planner,
        # small and clamped, on real overlaps only.
        transform: dict[str, Any] = {"tempo_shift_pct": 0.0, "pitch_shift_semitones": 0}
        plan_payload: dict[str, Any] | None = None
        if analysis is not None and analysis.bpm:
            transform["source_bpm"] = float(analysis.bpm)
        base_event = {
            "id": event_id,
            "deck": deck,
            "start_ms": cursor_ms,
            "trim_start_ms": source_window.trim_start_ms,
            "duration_ms": source_window.duration_ms,
            "fade_in_ms": 0 if index == 0 else args.fade_in_ms,
            "fade_out_ms": args.fade_out_ms,
            "planner_role": "lead",
            "source_window_reason": source_window.reason,
            "source_structure_kind": source_window.structure_kind,
            **transform,
        }
        if analysis is not None:
            for key in ("key", "tonic", "mode", "camelot"):
                value = getattr(analysis, key, None)
                if value is not None:
                    base_event[key] = value
        # Loading is how songs get onto decks: every lead is a load_track
        # action on the deck clock. A plain load plays the record whole; stem
        # selection is the explicit opt-in on top. Raw session clips are the
        # fallback representation (imports, legacy sets), never the product.
        action = {
            "type": "load_track",
            **{key: value for key, value in base_event.items() if key != "start_ms"},
            "source_path": track.path,
            "at_ms": cursor_ms,
        }
        if bool(getattr(args, "stem_aware_remix", False)) and track.path in stem_ready_paths:
            action["play_stems"] = ["vocals", "drums", "bass", "other"]
            # prepare_stems=False: readiness was checked above, and generation
            # must never block on Demucs. Missing stems load as plain records
            # and get queued for background splitting instead.
            action = prepare_load_track_action_stems(
                action, db_path=Path(getattr(args, "db", DEFAULT_DB)), prepare_stems=False
            )
        elif track.path not in stem_ready_paths:
            # Every loaded record should have stems ready — beds, bass swaps,
            # drums-only intros, and acapella tags are only playable moves if
            # the artifacts exist. Queue the split in every mode; the backfill
            # cron churns through it without ever blocking live audio.
            stem_split_queued.append(track.path)
        if track.path in stem_ready_paths:
            action["stems_ready"] = True
        lead_actions.append(action)
        cursor_ms += max(16_000, source_window.duration_ms - args.base_overlap_ms)
        previous_track = track

    queue_stem_splits(stem_split_queued, args, reason="lead loaded without ready stems; every loaded record should have stems prepared")
    timeline_events = lead_clips
    actions = lead_actions
    lead_starts = [
        int(event.get("at_ms", event.get("start_ms", 0)) or 0)
        for event in (lead_actions or lead_clips)
    ]
    # No canned mic text. Publish hosting slots with track context so the DJ
    # agent can author its own lean-ins over the handoffs it cares about.
    mic_lean_ins: list[dict[str, Any]] = []
    commentary_slots: list[dict[str, Any]] = []
    arranged = leads[:lead_cap]
    for index, start_ms in enumerate(lead_starts[1:], start=1):
        incoming = arranged[index] if index < len(arranged) else None
        if incoming is None:
            continue
        incoming_analysis = analyses.get(incoming.path)
        commentary_slots.append(
            {
                "at_ms": start_ms + 12_000,
                "reason": "incoming lead handoff",
                "incoming": {
                    "artist": incoming.artist,
                    "title": incoming.title,
                    "path": incoming.path,
                    "bpm": incoming_analysis.bpm if incoming_analysis else None,
                    "camelot": incoming_analysis.camelot if incoming_analysis else None,
                    "energy": incoming_analysis.energy if incoming_analysis else None,
                },
            }
        )

    payload = {
        "version": 1,
        "timeline_mode": "autodj-arrangement",
        "decks": ["deck-1", "deck-2", "deck-3", VOCAL_DECK],
        **(
            {
                "master_bpm": float(args.target_bpm),
                "max_tempo_stretch_pct": abs(float(getattr(args, "max_tempo_stretch_pct", 16.0))),
            }
            if getattr(args, "target_bpm", None) is not None
            else {}
        ),
        **(
            {
                "master_key": str(args.target_key),
                "max_key_shift_semitones": abs(int(getattr(args, "max_key_shift_semitones", 2))),
            }
            if getattr(args, "target_key", None)
            else {}
        ),
        "clips": sorted(timeline_events, key=lambda clip: (int(clip.get("start_ms") or 0), str(clip.get("id") or ""))),
        "actions": sorted(actions, key=lambda action: (int(action.get("at_ms", action.get("start_ms", 0)) or 0), str(action.get("id") or ""))),
        "transition_plans": transition_plans,
        "mic_lean_ins": mic_lean_ins,
        "automations": [],
        "deck_automations": [],
        "fader_routing": {"deck_assignments": {"deck-1": "A", "deck-2": "A", "deck-3": "B", VOCAL_DECK: "THRU"}},
    }
    payload["title"] = args.title
    payload["notes"] = {
        "created_at": iso_now(),
        "intent": args.intent,
        "selection_process": "database candidates plus play-history freshness penalties; arranged as short lead sections plus real handoffs/beds/effects",
        "scratch_material_policy": str(getattr(args, "scratch_material_policy", "ban") or "ban"),
        "scratch_source_files": [str(path) for path in (getattr(args, "scratch_source_file", None) or DEFAULT_SCRATCH_SOURCE_FILES)],
        "scratch_material_selected": sum(
            1 for track in selected if any("scratch/proof material policy" in reason for reason in track.reasons)
        ),
        "remix_focus": bool(args.remix_focus),
        "remix_policy": (
            "hard-techno/dnb/dubstep vocal remix lane: pair vocal/hook leads with rhythm/bass beds, prefer drop/build anchors, avoid vocal clashes, keep one sub/bass source active"
            if args.remix_focus
            else None
        ),
        "selected_material": [asdict(track) for track in selected],
        "lead_count": len(lead_actions or lead_clips),
        "bed_count": 0,
        "commentary_slots": commentary_slots,
        "max_lead_clip_ms": args.max_lead_clip_ms,
        "fast_section_mode": fast_mode,
        "stem_aware_load_tracks": bool(getattr(args, "stem_aware_remix", False)),
        "stem_split_queued": stem_split_queued,
    }
    parse_session(payload)
    return payload


def run_planner(session_path: Path, args: argparse.Namespace, *, lock_before_ms: int | None = None) -> dict[str, Any]:
    command = [
        sys.executable,
        "scripts/slime_audio_mix_planner.py",
        "--session",
        str(session_path),
        "--cached-analysis-only",
        "--apply",
    ]
    if getattr(args, "target_bpm", None) is not None:
        command.extend(["--target-bpm", str(args.target_bpm)])
    if lock_before_ms is not None:
        command.extend(["--lock-before-ms", str(lock_before_ms)])
    result = subprocess.run(command, cwd=REPO_ROOT, text=True, capture_output=True, check=False)
    return {"command": command, "returncode": result.returncode, "stdout": result.stdout[-4000:], "stderr": result.stderr[-4000:]}


def load_session_payload(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def append_selection_history(selected: list[SelectedTrack], args: argparse.Namespace, *, session_path: Path, dry_run: bool) -> None:
    if args.history is None:
        return
    args.history.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "event": "autodj_material_selected",
        "dry_run": dry_run,
        "paths": [track.path for track in selected],
        "session": str(session_path),
        "timestamp": iso_now(),
        "title": args.title,
    }
    with args.history.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")


def structural_bed_balance_profile(play_stems: list[str], args: argparse.Namespace) -> dict[str, Any]:
    stems = set(play_stems or [])
    base_gain = float(getattr(args, "bed_gain_db", -3.0) or -3.0)
    base_lowpass = float(getattr(args, "bed_lowpass_hz", 1800.0) or 1800.0)
    base_highpass = float(getattr(args, "bed_highpass_hz", 90.0) or 90.0)
    if stems == {"drums"}:
        drum_gain = min(base_gain + 2.0, -1.0)
        return {
            "strategy": "component-aware drum bed: fader first, preserve kick/snare/hat attack",
            "gain_db": drum_gain,
            "gain_offsets": [-1.0, 0.5, 0.0, -2.0],
            "lowpass_points": [
                max(4200.0, base_lowpass * 2.5),
                max(9000.0, base_lowpass * 5.0),
                max(6500.0, base_lowpass * 3.5),
            ],
            "highpass_points": [
                max(70.0, min(base_highpass, 110.0)),
                max(55.0, min(base_highpass, 90.0)),
                max(90.0, min(base_highpass + 40.0, 150.0)),
            ],
        }
    return {
        "strategy": "component-aware rhythm bed: carve bass/mids/highs after fader balance",
        "gain_db": base_gain,
        "gain_offsets": [-2.0, 0.0, -0.75, -2.5],
        "lowpass_points": [
            max(900.0, base_lowpass * 0.65),
            max(base_lowpass + 1200.0, base_lowpass * 2.25),
            max(base_lowpass, 2400.0),
        ],
        "highpass_points": [
            max(base_highpass, 130.0),
            base_highpass,
            max(base_highpass + 90.0, 150.0),
        ],
    }


def lead_phrase_snapper(lead: dict[str, Any], lead_analysis: TrackAnalysis | None):
    """Map the lead's source beat grid onto the mix timeline.

    Returns (snap, bar_ms): snap(mix_ms) moves a timestamp to the nearest lead
    phrase boundary in mix time. Both are None when no usable grid exists.
    """
    if lead_analysis is None or lead_analysis.beatgrid is None:
        return None, None
    grid = lead_analysis.beatgrid
    phrase_ms = int(grid.phrase_ms or 0)
    if phrase_ms <= 0:
        return None, None
    lead_start = int(lead.get("start_ms", lead.get("start", 0)) or 0)
    trim_start = int(lead.get("trim_start_ms", lead.get("trim_start", 0)) or 0)
    beat_offset = int(grid.beat_offset_ms or 0)
    phrase_beats = int(grid.phrase_beats or 32) or 32
    bar_ms = max(1, round(phrase_ms * 4 / phrase_beats))

    def snap(mix_ms: int) -> int:
        source_ms = trim_start + (mix_ms - lead_start)
        k = round((source_ms - beat_offset) / phrase_ms)
        snapped_source = beat_offset + k * phrase_ms
        return lead_start + (snapped_source - trim_start)

    return snap, bar_ms


def lead_drop_anchor_ms(
    lead: dict[str, Any],
    lead_analysis: TrackAnalysis | None,
    window_start_ms: int,
    window_end_ms: int,
) -> int | None:
    """Mix-time start of the first drop/build in the lead inside the window."""
    if lead_analysis is None:
        return None
    lead_start = int(lead.get("start_ms", lead.get("start", 0)) or 0)
    trim_start = int(lead.get("trim_start_ms", lead.get("trim_start", 0)) or 0)
    anchors = []
    for window in coerce_structure(lead_analysis.structure):
        if window.kind not in {"drop", "build"}:
            continue
        mix_ms = lead_start + (window.start_ms - trim_start)
        if window_start_ms <= mix_ms <= window_end_ms:
            anchors.append((0 if window.kind == "drop" else 1, mix_ms))
    if not anchors:
        return None
    anchors.sort()
    return anchors[0][1]


def plan_structural_bed_layer(
    lead: dict[str, Any],
    source: SelectedTrack,
    play_stems: list[str],
    args: argparse.Namespace,
    lead_analysis: TrackAnalysis | None = None,
) -> dict[str, Any] | None:
    lead_start = int(lead.get("start_ms", lead.get("start", 0)) or 0)
    lead_duration = int(lead.get("duration_ms", lead.get("duration", 0)) or 0)
    if lead_duration < 48_000:
        return None
    source_duration = source.duration_ms or args.default_track_ms
    desired_duration = min(int(args.bed_duration_ms), max(32_000, min(64_000, lead_duration // 3)))
    latest_start = lead_start + max(0, lead_duration - desired_duration - 24_000)
    entry_offset = min(max(16_000, lead_duration // 4), max(0, latest_start - lead_start))
    bed_start = lead_start + entry_offset
    snap, bar_ms = lead_phrase_snapper(lead, lead_analysis)
    phrase_snapped = False
    if snap is not None:
        snapped = snap(bed_start)
        if lead_start <= snapped <= lead_start + max(0, latest_start - lead_start):
            bed_start = snapped
            phrase_snapped = True
    bed_duration = min(desired_duration, max(32_000, lead_start + lead_duration - bed_start - 24_000))
    if bed_duration < 32_000:
        return None
    bed_end = bed_start + bed_duration
    trim_start = min(int(args.bed_trim_start_ms), max(0, source_duration - bed_duration - 1_000))
    balance_profile = structural_bed_balance_profile(play_stems, args)
    ramp_ms = (bar_ms or 4_000) * 2
    entrance_ms = bed_start + min(max(ramp_ms, 4_000), bed_duration // 4)
    # Open the bed filter into the lead's next drop/build when one lands inside
    # the bed window; otherwise open at the midpoint. This is what makes the
    # move read as intentional instead of a static carve.
    drop_anchor = lead_drop_anchor_ms(lead, lead_analysis, entrance_ms + 1, bed_end - 8_000)
    midpoint_ms = drop_anchor if drop_anchor is not None else bed_start + max(1, bed_duration // 2)
    end_ms = bed_end
    gain_offsets = balance_profile["gain_offsets"]
    lowpass_points = balance_profile["lowpass_points"]
    highpass_points = balance_profile["highpass_points"]
    return {
        "strategy": "planned-stem-layer",
        "role": "drum-bed" if set(play_stems) == {"drums"} else "rhythm-bed",
        "source_stems": play_stems,
        "target_stems": ["vocals", "drums", "bass", "other"],
        "entry_intent": "mid-section groove injection; not a terminal next-track drum preview",
        "exit_intent": "fade and filter out before the lead exit unless a transition plan explicitly takes over",
        "beatmatch_evidence": {
            "status": "pending-analysis",
            "requirement": "populate local BPM, beat offset, phrase anchor, target tempo, and drift check before launch",
        },
        "keymatch_evidence": {
            "status": "pending-analysis",
            "requirement": "populate local key/Camelot relationship, pitch shift, or drums-only/atonal exemption before launch",
        },
        "lead_action_id": lead.get("id"),
        "source_path": source.path,
        "start_ms": bed_start,
        "duration_ms": bed_duration,
        "end_ms": bed_end,
        "trim_start_ms": trim_start,
        "balance_profile": balance_profile["strategy"],
        "gain_db": balance_profile["gain_db"],
        "automation_intent": {
            "gain_db": "audible fader entrance, hold, then exit fade",
            "lowpass_hz": "open enough for the selected stems to read, then soften the exit",
            "highpass_hz": "carve mud without deleting kick/snare identity",
        },
        "motion": {
            "phrase_snapped": phrase_snapped,
            "bar_ms": bar_ms,
            "drop_aligned_ms": drop_anchor,
        },
        "automation_points": {
            "gain_db": [
                {"at_ms": bed_start, "value": balance_profile["gain_db"] + gain_offsets[0]},
                {"at_ms": entrance_ms, "value": balance_profile["gain_db"] + gain_offsets[1]},
                *(
                    [{"at_ms": drop_anchor, "value": balance_profile["gain_db"] + gain_offsets[1] + 1.0}]
                    if drop_anchor is not None and entrance_ms < drop_anchor < end_ms - 8_000
                    else []
                ),
                {"at_ms": max(bed_start, end_ms - 8_000), "value": balance_profile["gain_db"] + gain_offsets[2]},
                {"at_ms": end_ms, "value": balance_profile["gain_db"] + gain_offsets[3]},
            ],
            "lowpass_hz": [
                {"at_ms": bed_start, "value": lowpass_points[0]},
                {"at_ms": midpoint_ms, "value": lowpass_points[1]},
                {"at_ms": end_ms, "value": lowpass_points[2]},
            ],
            "highpass_hz": [
                {"at_ms": bed_start, "value": highpass_points[0]},
                {"at_ms": midpoint_ms, "value": highpass_points[1]},
                {"at_ms": end_ms, "value": highpass_points[2]},
            ],
        },
    }


def add_structural_beds(
    session_path: Path,
    selected: list[SelectedTrack],
    args: argparse.Namespace,
    *,
    min_start_ms: int = 0,
) -> dict[str, Any]:
    payload = load_session_payload(session_path)
    compiled = compile_actions_payload(payload)
    actions_by_id = {
        str(action.get("id")): action
        for action in payload.get("actions", [])
        if isinstance(action, dict) and action.get("id")
    }
    compiled_events = [
        *compiled.get("clips", []),
        *[stem_group_to_guard_event(group, actions_by_id) for group in compiled.get("stem_groups", [])],
    ]
    rhythm_sources = sorted(
        [track for track in selected if rhythm_bed_score(track) > 0],
        key=rhythm_bed_score,
        reverse=True,
    )
    if not rhythm_sources and bool(getattr(args, "remix_focus", False)):
        rhythm_sources = sorted(selected, key=lead_score, reverse=True)
    leads = sorted(
        [
            event
            for event in compiled_events
            if event.get("planner_role") == "lead"
            and int(event.get("start_ms", event.get("start", 0)) or 0) >= min_start_ms
        ],
        key=lambda event: int(event.get("start_ms", event.get("start", 0)) or 0),
    )
    if not rhythm_sources or not leads:
        return {"added": 0, "reason": "not enough lead/rhythm material"}

    decks = [str(deck) for deck in payload.get("decks", []) if str(deck)]
    if "deck-4" not in decks:
        decks.append("deck-4")
    payload["decks"] = decks
    routing = payload.setdefault("fader_routing", {}).setdefault("deck_assignments", {})
    routing.setdefault("deck-1", "A")
    routing.setdefault("deck-2", "A")
    routing.setdefault("deck-3", "B")
    routing["deck-4"] = "THRU"

    max_structural_beds = int(getattr(args, "max_structural_beds", 4) or 0)
    if max_structural_beds <= 0:
        return {"added": 0, "reason": "structural beds disabled"}
    target_count = min(max_structural_beds, len(leads))
    target_stride = max(1, len(leads) // max(1, target_count))
    first_target_index = 0 if target_count >= len(leads) else (1 if len(leads) > 1 else 0)
    target_indices = list(range(first_target_index, len(leads), target_stride))[:target_count]
    if not target_indices:
        target_indices = [0]

    lead_paths = [event_source(lead) for lead in leads]
    used_source_paths: set[str] = set()
    # Bed stems are rendered from real stem artifacts; a bed source without
    # ready stems would either block generation on Demucs or silently play the
    # full track (vocals included) under the lead. Prefer ready material and
    # skip the bed when none exists.
    ready_paths = ready_stem_source_paths(Path(getattr(args, "db", DEFAULT_DB)))

    def choose_bed_source(target_index: int, lead: dict[str, Any]) -> SelectedTrack | None:
        current_path = event_source(lead)
        adjacent_paths = {
            path
            for path in lead_paths[max(0, target_index - 1) : min(len(lead_paths), target_index + 2)]
            if path
        }
        candidates = [
            source
            for source in rhythm_sources
            if source.path not in used_source_paths
            and source.path != current_path
            and source.path not in adjacent_paths
        ]
        if not candidates:
            candidates = [
                source
                for source in rhythm_sources
                if source.path not in used_source_paths
                and source.path != current_path
            ]
        if not candidates:
            candidates = [
                source
                for source in rhythm_sources
                if source.path != current_path
                and source.path not in adjacent_paths
            ]
        if not candidates:
            candidates = [
                source
                for source in rhythm_sources
                if source.path != current_path
            ]
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda source: (
                source.path in ready_paths,
                rhythm_bed_score(source),
                float(source.score or 0.0),
                source.duration_ms or 0,
            ),
        )

    added: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    bed_windows: list[tuple[int, int]] = []
    bed_targets = [(target_index, leads[target_index]) for target_index in target_indices]
    for bed_number, (target_index, lead) in enumerate(bed_targets, start=1):
        source = choose_bed_source(target_index, lead)
        if source is None:
            continue
        if source.path not in ready_paths:
            skipped.append(
                {
                    "source": source.path,
                    "under": lead.get("id"),
                    "reason": "no ready stem artifacts; run scripts/slime_audio_stems.py split on the bed source first",
                }
            )
            continue
        used_source_paths.add(source.path)
        bed_id = f"bed-{bed_number:03d}-{slugify(source.title)[:40]}"
        play_stems = ["drums", "bass", "other"]
        bed_event: dict[str, Any] = {
            "id": bed_id,
            "deck": "deck-4",
            "fade_in_ms": args.bed_fade_in_ms,
            "fade_out_ms": args.bed_fade_out_ms,
            "planner_role": "rhythm-bed",
            "bed_under": lead.get("id"),
            "bed_selection_reason": "sparse planned stem layer; avoids current, previous, and next lead when possible",
        }
        if rhythm_bed_score(source) <= 0:
            bed_event["planner_role"] = "drum-bed"
            play_stems = ["drums"]
            bed_event["play_stems"] = play_stems
        source_analysis = load_analysis_from_db(args.db, Path(source.path))
        lead_analysis = load_analysis_from_db(args.db, Path(event_source(lead)))
        if source_analysis is not None:
            for key in ("key", "tonic", "mode", "camelot"):
                value = getattr(source_analysis, key, None)
                if value is not None:
                    bed_event[key] = value
        if source_analysis is not None and lead_analysis is not None:
            plan = transition_plan(source_analysis, lead_analysis, max_pitch_shift=6)
            lead_pitch_shift = int(lead.get("pitch_shift_semitones") or 0)
            bed_event["tempo_shift_pct"] = float(plan.target_tempo_shift_pct or 0.0)
            bed_event["pitch_shift_semitones"] = int(plan.pitch_shift_semitones or 0) + lead_pitch_shift
            bed_event["transition_decision"] = {
                "tempo_shift_pct": bed_event["tempo_shift_pct"],
                "pitch_shift_semitones": bed_event["pitch_shift_semitones"],
                "decision": plan.key_relation,
                "score": plan.score,
                "reason": "; ".join(plan.notes) or "explicit bed beat/key match",
                "source_bpm": source_analysis.bpm,
                "lead_bpm": lead_analysis.bpm,
                "source_beat_offset_ms": source_analysis.beat_offset_ms,
                "lead_beat_offset_ms": lead_analysis.beat_offset_ms,
                "source_phrase_ms": source_analysis.beatgrid.phrase_ms if source_analysis.beatgrid else None,
                "lead_phrase_ms": lead_analysis.beatgrid.phrase_ms if lead_analysis.beatgrid else None,
            }
            payload.setdefault("transition_plans", []).append(
                {
                    "id": f"transition-{bed_id}",
                    "planner_role": "mix-planner-transition-plan",
                    "from_clip_id": str(lead.get("id") or ""),
                    "to_clip_id": bed_id,
                    "to_action_id": bed_id,
                    "decision": plan.key_relation,
                    "tempo_shift_pct": bed_event["tempo_shift_pct"],
                    "pitch_shift_semitones": bed_event["pitch_shift_semitones"],
                    "score": plan.score,
                    "reason": "; ".join(plan.notes) or "explicit bed beat/key match",
                }
            )
            if (
                source_analysis.tonic is not None
                and source_analysis.mode in {"major", "minor"}
                and lead_analysis.tonic is not None
                and lead_analysis.mode in {"major", "minor"}
            ):
                source_relative = major_equivalent_tonic(
                    (source_analysis.tonic + int(bed_event["pitch_shift_semitones"] or 0)) % 12,
                    source_analysis.mode,
                )
                lead_relative = major_equivalent_tonic((lead_analysis.tonic + lead_pitch_shift) % 12, lead_analysis.mode)
                if source_relative != lead_relative:
                    bed_event["planner_role"] = "drum-bed"
                    play_stems = ["drums"]
                    bed_event["play_stems"] = play_stems
        if not isinstance(bed_event.get("transition_decision"), dict):
            bed_event["planner_role"] = "drum-bed"
            play_stems = ["drums"]
            bed_event["play_stems"] = play_stems
            bed_event["tempo_shift_pct"] = 0.0
            bed_event["pitch_shift_semitones"] = 0
            bed_event["transition_decision"] = {
                "tempo_shift_pct": 0.0,
                "pitch_shift_semitones": 0,
                "decision": "drums-only fallback",
                "reason": "missing local analysis for full beat/key plan; restrict to drums-only layer and require proof-listening before launch",
            }
        plan = plan_structural_bed_layer(lead, source, play_stems, args, lead_analysis=lead_analysis)
        if plan is None:
            continue
        if isinstance(bed_event.get("transition_decision"), dict):
            decision = bed_event["transition_decision"]
            plan["beatmatch_evidence"] = {
                "status": "analyzed",
                "source_bpm": decision.get("source_bpm"),
                "lead_bpm": decision.get("lead_bpm"),
                "source_beat_offset_ms": decision.get("source_beat_offset_ms"),
                "lead_beat_offset_ms": decision.get("lead_beat_offset_ms"),
                "source_phrase_ms": decision.get("source_phrase_ms"),
                "lead_phrase_ms": decision.get("lead_phrase_ms"),
                "target_tempo_shift_pct": decision.get("tempo_shift_pct"),
                "drift_check": "required in proof render start/middle/exit",
            }
            plan["keymatch_evidence"] = {
                "status": "analyzed",
                "decision": decision.get("decision"),
                "pitch_shift_semitones": decision.get("pitch_shift_semitones"),
                "reason": decision.get("reason"),
                "drums_only_exemption": set(play_stems) == {"drums"},
            }
        bed_start = int(plan["start_ms"])
        bed_duration = int(plan["duration_ms"])
        bed_end = int(plan["end_ms"])
        if any(overlaps(bed_start, bed_end, start, end) for start, end in bed_windows):
            continue
        bed_event["start_ms"] = bed_start
        bed_event["trim_start_ms"] = int(plan["trim_start_ms"])
        bed_event["duration_ms"] = bed_duration
        bed_event["gain_db"] = float(plan["gain_db"])
        bed_event["component_balance_strategy"] = str(plan["balance_profile"])
        bed_event["stem_layer_plan"] = plan
        # Beds always load as stem-resolved load_track actions. Clip-level
        # play_stems used to be dashboard-only decoration that the renderer
        # ignored, which is how "drums-only" beds ended up playing full songs.
        payload = add_action(
            payload,
            action={
                "type": "load_track",
                **bed_event,
                "source_path": source.path,
                "at_ms": bed_start,
                "play_stems": play_stems,
            },
            db_path=args.db,
            lock_before_ms=None,
            force=True,
        )
        bed_windows.append((bed_start, bed_end))
        automation_points = plan["automation_points"]
        # add_action returns a fresh payload, so resolve the automation list
        # each time instead of holding a reference from before the copy.
        payload.setdefault("deck_automations", []).extend(
            [
                {
                    "target": "deck-4",
                    "param": "gain_db",
                    "source_clip_id": bed_id,
                    "planner_role": "planned-stem-layer-automation",
                    "stem_layer_plan_ref": bed_id,
                    "points": automation_points["gain_db"],
                },
                {
                    "target": "deck-4",
                    "param": "lowpass_hz",
                    "source_clip_id": bed_id,
                    "planner_role": "planned-stem-layer-automation",
                    "stem_layer_plan_ref": bed_id,
                    "points": automation_points["lowpass_hz"],
                },
                {
                    "target": "deck-4",
                    "param": "highpass_hz",
                    "source_clip_id": bed_id,
                    "planner_role": "planned-stem-layer-automation",
                    "stem_layer_plan_ref": bed_id,
                    "points": automation_points["highpass_hz"],
                },
            ]
        )
        # Bass ownership handoff: when the bed brings bass, the lead's low end
        # steps aside at the bed entrance and returns as the bed exits, ramped
        # over roughly a bar so the move is audible but not a volume cliff.
        lead_deck = str(lead.get("deck") or "")
        if "bass" in set(play_stems) and lead_deck:
            handoff_ramp_ms = int((plan.get("motion") or {}).get("bar_ms") or 2_000)
            payload.setdefault("deck_automations", []).append(
                {
                    "target": lead_deck,
                    "param": "eq_low_db",
                    "source_clip_id": bed_id,
                    "planner_role": "planned-bed-bass-handoff",
                    "stem_layer_plan_ref": bed_id,
                    "points": [
                        {"at_ms": max(0, bed_start - handoff_ramp_ms), "value": 0.0},
                        {"at_ms": bed_start, "value": -3.5},
                        {"at_ms": max(bed_start, bed_end - handoff_ramp_ms), "value": -3.5},
                        {"at_ms": bed_end, "value": 0.0},
                    ],
                }
            )
        added.append({"id": bed_id, "source": source.path, "under": lead.get("id"), "start_ms": bed_start, "duration_ms": bed_duration})

    payload["clips"] = sorted(
        payload.get("clips", []),
        key=lambda clip: (int(clip.get("start_ms", clip.get("start", 0)) or 0), str(clip.get("deck") or ""), str(clip.get("id") or "")),
    )
    payload["actions"] = sorted(
        payload.get("actions", []),
        key=lambda action: (int(action.get("at_ms", action.get("start_ms", 0)) or 0), str(action.get("id") or "")),
    )
    notes = payload.setdefault("notes", {})
    notes["bed_count"] = int(notes.get("bed_count") or 0) + len(added)
    notes["structural_bed_strategy"] = "sparse non-adjacent planned stem layers with component-aware fader/EQ balance"
    notes["structural_bed_target_count"] = target_count
    write_payload(session_path, payload)
    return {"added": len(added), "beds": added, "skipped": skipped}


def clip_start_ms(clip: dict[str, Any]) -> int:
    return int(clip.get("start_ms", clip.get("start", 0)) or 0)


def clip_duration_ms(clip: dict[str, Any]) -> int:
    return int(clip.get("duration_ms", clip.get("duration", 0)) or 0)


def clip_end_ms(clip: dict[str, Any]) -> int:
    return clip_start_ms(clip) + clip_duration_ms(clip)


def event_start_ms(event: dict[str, Any]) -> int | None:
    for key in ("start_ms", "start", "at_ms", "at"):
        value = event.get(key)
        if value is not None:
            return int(value)
    points = event.get("points")
    if isinstance(points, list):
        starts = [event_start_ms(point) for point in points if isinstance(point, dict)]
        starts = [start for start in starts if start is not None]
        if starts:
            return min(starts)
    return None


def event_end_ms(event: dict[str, Any]) -> int | None:
    start = event_start_ms(event)
    duration = event.get("duration_ms", event.get("duration"))
    if start is not None and duration is not None:
        return start + int(duration)
    points = event.get("points")
    if isinstance(points, list):
        ends = [event_start_ms(point) for point in points if isinstance(point, dict)]
        ends = [end for end in ends if end is not None]
        if ends:
            return max(ends)
    tail = event.get("tail_ms")
    if start is not None and tail is not None:
        return start + int(tail)
    return start


def overlaps(left_start: int, left_end: int, right_start: int | None, right_end: int | None) -> bool:
    if right_start is None or right_end is None:
        return False
    return left_start < right_end and right_start < left_end


def record_move_window(windows: list[tuple[int, int]], start: int | None, end: int | None, lead_start: int, lead_end: int) -> None:
    if start is None or end is None or not overlaps(lead_start, lead_end, start, end):
        return
    windows.append((max(lead_start, start), min(lead_end, max(start + 1, end))))


def max_gap_ms(lead_start: int, lead_end: int, windows: list[tuple[int, int]]) -> int:
    if not windows:
        return lead_end - lead_start
    merged: list[tuple[int, int]] = []
    for start, end in sorted(windows):
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    cursor = lead_start
    largest = 0
    for start, end in merged:
        largest = max(largest, start - cursor)
        cursor = max(cursor, end)
    return max(largest, lead_end - cursor)


def enabled_stems_for_event(event: dict[str, Any]) -> set[str]:
    stems = event.get("stems") if isinstance(event.get("stems"), dict) else {}
    return {name for name, stem in stems.items() if isinstance(stem, dict) and stem.get("enabled", True)}


def is_harmonic_layer(event: dict[str, Any]) -> bool:
    role = str(event.get("planner_role") or "")
    if "drum" in role or "scratch" in role or "stab" in role:
        return False
    stems = enabled_stems_for_event(event)
    if stems:
        return bool(stems & {"vocals", "bass", "other"})
    return True


def event_key_metadata(
    event: dict[str, Any],
    *,
    db_path: Path,
    cache: dict[str, TrackAnalysis | None],
) -> tuple[int | None, str | None, str | None]:
    tonic = event.get("tonic")
    mode = event.get("mode")
    key_name = event.get("key")
    try:
        parsed_tonic = int(tonic) if tonic is not None else None
    except (TypeError, ValueError):
        parsed_tonic = None
    parsed_mode = str(mode) if mode in {"major", "minor"} else None
    if parsed_tonic is not None and parsed_mode is not None:
        return parsed_tonic, parsed_mode, str(key_name or "")

    source_path = str(event.get("source_path") or event.get("path") or "").strip()
    if not source_path:
        return None, None, None
    if source_path not in cache:
        cache[source_path] = load_analysis_from_db(db_path, Path(source_path))
    analysis = cache[source_path]
    if analysis is None or analysis.tonic is None or analysis.mode not in {"major", "minor"}:
        return None, None, None
    return analysis.tonic, analysis.mode, analysis.key


def shifted_relative_tonic(tonic: int, mode: str, event: dict[str, Any]) -> int:
    try:
        shift = int(event.get("pitch_shift_semitones") or 0)
    except (TypeError, ValueError):
        shift = 0
    return major_equivalent_tonic((tonic + shift) % 12, mode)


def validate_harmonic_overlaps(session_path: Path, args: argparse.Namespace) -> dict[str, Any]:
    payload = load_session_payload(session_path)
    compiled = compile_actions_payload(payload)
    actions_by_id = {
        str(action.get("id")): action
        for action in payload.get("actions", [])
        if isinstance(action, dict) and action.get("id")
    }
    clips = [clip for clip in compiled.get("clips", []) if clip.get("id") and clip_duration_ms(clip) > 0]
    stem_groups = [
        stem_group_to_guard_event(group, actions_by_id)
        for group in compiled.get("stem_groups", [])
        if group.get("id") and clip_duration_ms(group) > 0
    ]
    events = [event for event in [*clips, *stem_groups] if is_harmonic_layer(event)]
    min_overlap_ms = int(getattr(args, "min_harmonic_overlap_ms", DEFAULT_MIN_HARMONIC_OVERLAP_MS))
    # Already-played music cannot be unplayed: an extend must not fail over an
    # overlap that ended behind the live playhead (found 2026-07-03 when a
    # keyless bed pair from hour one blocked an hour-three extension).
    guard_after_ms = int(getattr(args, "harmonic_guard_after_ms", 0) or 0)
    db_path = Path(getattr(args, "db", DEFAULT_DB))
    key_cache: dict[str, TrackAnalysis | None] = {}
    failures: list[dict[str, Any]] = []
    checked = 0

    for index, left in enumerate(events):
        left_start = clip_start_ms(left)
        left_end = clip_end_ms(left)
        for right in events[index + 1 :]:
            right_start = clip_start_ms(right)
            right_end = clip_end_ms(right)
            overlap_ms = min(left_end, right_end) - max(left_start, right_start)
            if overlap_ms < min_overlap_ms:
                continue
            if min(left_end, right_end) <= guard_after_ms:
                continue
            if str(left.get("source_path") or left.get("path") or "") == str(right.get("source_path") or right.get("path") or ""):
                continue
            checked += 1
            left_tonic, left_mode, left_key = event_key_metadata(left, db_path=db_path, cache=key_cache)
            right_tonic, right_mode, right_key = event_key_metadata(right, db_path=db_path, cache=key_cache)
            if left_tonic is None or left_mode is None or right_tonic is None or right_mode is None:
                failures.append(
                    {
                        "left": str(left.get("id") or ""),
                        "right": str(right.get("id") or ""),
                        "overlap_ms": overlap_ms,
                        "reason": "missing key metadata for harmonic overlap",
                    }
                )
                continue
            left_relative = shifted_relative_tonic(left_tonic, left_mode, left)
            right_relative = shifted_relative_tonic(right_tonic, right_mode, right)
            if left_relative != right_relative:
                failures.append(
                    {
                        "left": str(left.get("id") or ""),
                        "right": str(right.get("id") or ""),
                        "overlap_ms": overlap_ms,
                        "left_key": left_key,
                        "right_key": right_key,
                        "left_pitch_shift": int(left.get("pitch_shift_semitones") or 0),
                        "right_pitch_shift": int(right.get("pitch_shift_semitones") or 0),
                        "reason": "effective keys do not share a relative-major tonic",
                    }
                )

    if failures:
        raise SystemExit(f"harmonic overlap guard failed: {json.dumps(failures[:5], sort_keys=True)}")
    return {"checked": checked, "min_overlap_ms": min_overlap_ms}


def event_source(event: dict[str, Any]) -> str:
    return str(event.get("source_path") or event.get("path") or "").strip()


def is_transition_subject(event: dict[str, Any]) -> bool:
    if clip_duration_ms(event) <= 0:
        return False
    role = str(event.get("planner_role") or "")
    if any(token in role for token in ("stab", "scratch", "effect", "drop", "lean-in")):
        return False
    if not event_source(event):
        return False
    return is_harmonic_layer(event)


def transition_plan_for_event(payload: dict[str, Any], event: dict[str, Any]) -> dict[str, Any] | None:
    event_ids = {
        str(event.get("id") or ""),
        str(event.get("source_action_id") or ""),
        str(event.get("load_id") or ""),
    }
    event_ids.discard("")
    for plan in payload.get("transition_plans", []):
        if not isinstance(plan, dict):
            continue
        targets = {
            str(plan.get("to_clip_id") or ""),
            str(plan.get("to_event_id") or ""),
            str(plan.get("to_action_id") or ""),
            str(plan.get("to_load_id") or ""),
            str(plan.get("incoming_id") or ""),
        }
        targets.discard("")
        if event_ids & targets:
            return plan
    return None


def has_explicit_transform_decision(event: dict[str, Any], source_action: dict[str, Any]) -> bool:
    if any(key in source_action for key in ("tempo_shift_pct", "pitch_shift_semitones")):
        try:
            tempo_shift = float(source_action.get("tempo_shift_pct", event.get("tempo_shift_pct", 0.0)) or 0.0)
        except (TypeError, ValueError):
            tempo_shift = 0.0
        try:
            pitch_shift = int(source_action.get("pitch_shift_semitones", event.get("pitch_shift_semitones", 0)) or 0)
        except (TypeError, ValueError):
            pitch_shift = 0
        return bool(tempo_shift or pitch_shift)
    for key in ("beatmatch", "keymatch", "transition_decision", "transition_plan"):
        value = source_action.get(key) or event.get(key)
        if isinstance(value, dict) and ("tempo_shift_pct" in value or "pitch_shift_semitones" in value):
            return True
    return False


def validate_transition_decisions(session_path: Path, args: argparse.Namespace) -> dict[str, Any]:
    payload = load_session_payload(session_path)
    compiled = compile_actions_payload(payload)
    actions_by_id = {
        str(action.get("id")): action
        for action in payload.get("actions", [])
        if isinstance(action, dict) and action.get("id")
    }
    clips = [clip for clip in compiled.get("clips", []) if clip.get("id")]
    stem_groups = [
        stem_group_to_guard_event(group, actions_by_id)
        for group in compiled.get("stem_groups", [])
        if group.get("id")
    ]
    events = sorted(
        [event for event in [*clips, *stem_groups] if is_transition_subject(event)],
        key=lambda event: (clip_start_ms(event), str(event.get("deck") or ""), str(event.get("id") or "")),
    )
    failures: list[dict[str, Any]] = []
    checked = 0
    previous: dict[str, Any] | None = None

    for event in events:
        source = event_source(event)
        if previous is None:
            previous = event
            continue
        previous_source = event_source(previous)
        if source == previous_source:
            previous = event
            continue
        checked += 1
        source_action_id = str(event.get("source_action_id") or event.get("id") or "")
        source_action = actions_by_id.get(source_action_id) or {}
        plan = transition_plan_for_event(payload, event)
        plan_has_transform = (
            isinstance(plan, dict)
            and "tempo_shift_pct" in plan
            and "pitch_shift_semitones" in plan
            and str(plan.get("decision") or "")
        )
        if not plan_has_transform and not has_explicit_transform_decision(event, source_action):
            failures.append(
                {
                    "from": str(previous.get("id") or ""),
                    "to": str(event.get("id") or ""),
                    "from_source": previous_source,
                    "to_source": source,
                    "start_ms": clip_start_ms(event),
                    "reason": "missing explicit beat/key transition decision",
                }
            )
        previous = event

    if failures:
        raise SystemExit(f"transition decision guard failed: {json.dumps(failures[:5], sort_keys=True)}")
    return {"checked": checked}


def is_non_musical_clip(event: dict[str, Any]) -> bool:
    role = str(event.get("planner_role") or event.get("role") or event.get("kind") or "").lower()
    if any(token in role for token in ("sample", "drop", "scratch", "effect", "stab", "lean-in", "sfx")):
        return True
    if bool(event.get("non_musical", False)) or bool(event.get("sample", False)):
        return True
    path = event_source(event).lower()
    return any(token in path for token in ("/samples/", "/sfx/", "/drops/", "/one-shots/", "/oneshots/"))


def path_in_library_db(path: str, db_path: Path) -> bool:
    if not path:
        return False
    try:
        with sqlite3.connect(db_path) as db:
            row = db.execute("SELECT 1 FROM files WHERE path = ? LIMIT 1", (path,)).fetchone()
        return row is not None
    except sqlite3.Error:
        return False


def validate_stem_load_usage(session_path: Path, args: argparse.Namespace) -> dict[str, Any]:
    if not bool(getattr(args, "require_stem_loads", False)):
        return {"required": False, "checked": 0}
    payload = load_session_payload(session_path)
    db_path = Path(getattr(args, "db", DEFAULT_DB))
    clips = [
        clip
        for clip in payload.get("clips", [])
        if isinstance(clip, dict)
        and clip.get("path")
        and clip_duration_ms(clip) > 0
        and not is_non_musical_clip(clip)
        and path_in_library_db(str(clip.get("path") or ""), db_path)
    ]
    load_actions = [
        action
        for action in payload.get("actions", [])
        if isinstance(action, dict) and action.get("type") == "load_track" and action.get("source_path")
    ]
    failures: list[dict[str, Any]] = []
    if clips:
        failures.extend(
            {
                "id": str(clip.get("id") or ""),
                "path": str(clip.get("path") or ""),
                "reason": "database music tracks must use load_track actions with resolved stems; clip events are reserved for short non-musical samples/effects",
            }
            for clip in clips[:5]
        )
    if not load_actions:
        failures.append({"reason": "stem-aware session has no load_track actions"})
    if failures:
        raise SystemExit(f"stem load guard failed: {json.dumps(failures[:5], sort_keys=True)}")
    return {"required": True, "checked": len(load_actions)}


def validate_vocal_guards(session_path: Path, args: argparse.Namespace) -> dict[str, Any]:
    payload = load_session_payload(session_path)
    overlap = audit_vocal_overlap_payload(payload)
    if not bool(overlap.get("ok")):
        raise SystemExit(f"vocal overlap guard failed: {json.dumps(overlap, sort_keys=True)}")
    alignment = audit_vocal_alignment_payload(payload, db_path=Path(getattr(args, "db", DEFAULT_DB)), cache_path=Path(getattr(args, "analysis_cache", DEFAULT_ANALYSIS_CACHE)))
    if not bool(alignment.get("ok")):
        raise SystemExit(f"vocal alignment guard failed: {json.dumps(alignment, sort_keys=True)}")
    return {"overlap": overlap, "alignment": alignment}


def validate_no_vanilla_leads(session_path: Path, args: argparse.Namespace) -> dict[str, Any]:
    payload = load_session_payload(session_path)
    compiled = compile_actions_payload(payload)
    actions_by_id = {
        str(action.get("id")): action
        for action in payload.get("actions", [])
        if isinstance(action, dict) and action.get("id")
    }
    clips = [clip for clip in compiled.get("clips", []) if clip.get("id") and clip_duration_ms(clip) > 0]
    stem_groups = [
        stem_group_to_guard_event(group, actions_by_id)
        for group in compiled.get("stem_groups", [])
        if group.get("id") and clip_duration_ms(group) > 0
    ]
    events = clips + stem_groups
    leads = [event for event in events if is_guard_lead(event) and clip_duration_ms(event) >= args.min_vanilla_check_ms]
    automations = list(compiled.get("automations", [])) + list(compiled.get("deck_automations", []))
    effects = list(compiled.get("effects", []))
    failures: list[dict[str, Any]] = []

    for lead in leads:
        lead_id = str(lead["id"])
        lead_start = clip_start_ms(lead)
        lead_end = clip_end_ms(lead)
        windows: list[tuple[int, int]] = []
        material_windows: list[tuple[int, int]] = []
        if any(lead.get(key) for key in ("tempo_shift_pct", "pitch_shift_semitones", "reverse")) or lead.get("playback_rate") not in {None, 1, 1.0}:
            windows.append((lead_start, lead_end))
        for event in events:
            if event is lead:
                continue
            role = str(event.get("planner_role") or "")
            before = len(material_windows)
            if is_guard_lead(event) and clip_duration_ms(event) > 45_000:
                move_end = min(clip_end_ms(event), clip_start_ms(event) + 32_000)
                record_move_window(material_windows, clip_start_ms(event), move_end, lead_start, lead_end)
            elif role != "lead":
                record_move_window(material_windows, clip_start_ms(event), clip_end_ms(event), lead_start, lead_end)
            if len(material_windows) > before:
                windows.append(material_windows[-1])
        for effect in effects:
            if effect.get("target") == lead_id:
                record_move_window(windows, event_start_ms(effect), event_end_ms(effect), lead_start, lead_end)
            elif str(effect.get("target") or "") in {str(lead.get("deck") or ""), "master", "all"}:
                record_move_window(windows, event_start_ms(effect), event_end_ms(effect), lead_start, lead_end)
        for effect in effects:
            if effect.get("target") == lead_id:
                record_move_window(windows, event_start_ms(effect), event_end_ms(effect), lead_start, lead_end)
        for automation in automations:
            if automation.get("planner_role") == "autodj-lead-filter-ride":
                continue
            target = str(automation.get("target") or "")
            if target in {lead_id, str(lead.get("deck") or ""), "crossfader", "master", "all"}:
                start = event_start_ms(automation)
                end = event_end_ms(automation)
                if start is not None:
                    record_move_window(windows, start - 2_000, (end or start) + 2_000, lead_start, lead_end)
        gap = max_gap_ms(lead_start, lead_end, windows)
        if gap > args.max_vanilla_lead_ms or not material_windows:
            failure = {"id": lead_id, "max_vanilla_gap_ms": gap, "duration_ms": lead_end - lead_start}
            if not material_windows:
                failure["reason"] = "no material DJ move overlaps this lead; filter/eq rides alone do not count"
            failures.append(failure)

    if failures:
        raise SystemExit(f"vanilla lead guard failed: {json.dumps(failures[:5], sort_keys=True)}")
    return {"checked": len(leads), "max_allowed_gap_ms": args.max_vanilla_lead_ms}


def validate_component_bed_balance(session_path: Path, args: argparse.Namespace) -> dict[str, Any]:
    payload = load_session_payload(session_path)
    candidates = [
        item
        for item in [*payload.get("clips", []), *payload.get("actions", [])]
        if isinstance(item, dict)
        and item.get("type") in {None, "load_track"}
        and ("bed" in str(item.get("planner_role") or "") or item.get("play_stems") == ["drums"])
    ]
    failures: list[dict[str, Any]] = []
    checked = 0
    candidates_by_id = {str(item.get("id") or ""): item for item in candidates if isinstance(item, dict) and item.get("id")}
    for item in candidates:
        play_stems = item.get("play_stems") if isinstance(item.get("play_stems"), list) else []
        role = str(item.get("planner_role") or "")
        if role != "drum-bed" and set(play_stems) != {"drums"}:
            continue
        checked += 1
        try:
            gain_db = float(item.get("gain_db", 0.0) or 0.0)
        except (TypeError, ValueError):
            gain_db = 0.0
        strategy = str(item.get("component_balance_strategy") or "")
        stem_layer_plan = item.get("stem_layer_plan")
        if not isinstance(stem_layer_plan, dict):
            failures.append(
                {
                    "id": str(item.get("id") or ""),
                    "planner_role": role,
                    "reason": "bed has no explicit stem-layer plan for stems, timing, exit, beat/key evidence, and automation intent",
                }
            )
        if gain_db <= -6.0 and "component-aware drum bed" not in strategy:
            failures.append(
                {
                    "id": str(item.get("id") or ""),
                    "gain_db": gain_db,
                    "planner_role": role,
                    "reason": "drum bed uses old buried gain without component-aware fader/EQ balance",
                }
            )
        if isinstance(stem_layer_plan, dict):
            if not isinstance(stem_layer_plan.get("beatmatch_evidence"), dict):
                failures.append(
                    {
                        "id": str(item.get("id") or ""),
                        "planner_role": role,
                        "reason": "bed stem-layer plan lacks beatmatch evidence",
                    }
                )
            if set(play_stems) != {"drums"} and not isinstance(stem_layer_plan.get("keymatch_evidence"), dict):
                failures.append(
                    {
                        "id": str(item.get("id") or ""),
                        "planner_role": role,
                        "reason": "tonal bed stem-layer plan lacks keymatch evidence",
                    }
                )
            source_duration_ms = item.get("source_duration_ms") or stem_layer_plan.get("source_duration_ms")
            if source_duration_ms is not None:
                try:
                    source_duration = int(source_duration_ms)
                    trim_start = int(item.get("trim_start_ms", stem_layer_plan.get("trim_start_ms", 0)) or 0)
                    duration = int(item.get("duration_ms", stem_layer_plan.get("duration_ms", 0)) or 0)
                except (TypeError, ValueError):
                    source_duration = 0
                    trim_start = 0
                    duration = 0
                terminal_start = max(0, source_duration - 45_000)
                entry_intent = str(stem_layer_plan.get("entry_intent") or "").casefold()
                if duration >= 20_000 and trim_start >= terminal_start and "outro" not in entry_intent and "terminal" not in entry_intent:
                    failures.append(
                        {
                            "id": str(item.get("id") or ""),
                            "planner_role": role,
                            "trim_start_ms": trim_start,
                            "source_duration_ms": source_duration,
                            "reason": "bed uses a terminal source window without an explicit outro/terminal handoff reason",
                        }
                    )
        if strategy and "bed" in role and not isinstance(stem_layer_plan, dict):
            failures.append(
                {
                    "id": str(item.get("id") or ""),
                    "planner_role": role,
                    "reason": "bed has component balance but no explicit stem-layer plan for stems, timing, exit, and automation intent",
                }
            )
    gain_ramp_patterns: Counter[tuple[str, tuple[float, ...]]] = Counter()
    for automation in payload.get("deck_automations", []):
        if not isinstance(automation, dict) or automation.get("param") != "gain_db":
            continue
        target = str(automation.get("target") or "")
        if target not in {"deck-3", "deck-4"}:
            continue
        values: list[float] = []
        for point in automation.get("points") or []:
            if not isinstance(point, dict):
                continue
            try:
                values.append(round(float(point.get("value")), 1))
            except (TypeError, ValueError):
                continue
        if len(values) < 2:
            continue
        pattern = (target, tuple(values))
        gain_ramp_patterns[pattern] += 1
        starts_magic = (target == "deck-4" and abs(values[0] - -5.4) <= 0.15) or (target == "deck-3" and abs(values[0] - -4.8) <= 0.15)
        ends_at_unity = abs(values[-1] - 0.0) <= 0.15
        ref = str(automation.get("source_clip_id") or automation.get("stem_layer_plan_ref") or "")
        referenced = candidates_by_id.get(ref) or {}
        plan = referenced.get("stem_layer_plan") if isinstance(referenced, dict) else None
        has_measurement = isinstance(plan, dict) and (
            isinstance(plan.get("balance_measurement"), dict)
            or isinstance(plan.get("measured_balance"), dict)
            or isinstance(plan.get("measurement"), dict)
        )
        if starts_magic and ends_at_unity and not has_measurement:
            failures.append(
                {
                    "target": target,
                    "source_clip_id": ref,
                    "values": values,
                    "reason": "deck gain automation matches a known canned ramp without measured balance metadata",
                }
            )
    for (target, values), count in gain_ramp_patterns.items():
        if count >= 3:
            failures.append(
                {
                    "target": target,
                    "values": list(values),
                    "count": count,
                    "reason": "same deck gain ramp repeats across multiple beds; automation must be per-layer musical balance",
                }
            )
    if failures:
        raise SystemExit(f"component bed balance guard failed: {json.dumps(failures[:5], sort_keys=True)}")
    return {"checked": checked}


REQUIRED_DECISION_AUDIT_FIELDS = (
    "request_summary",
    "acquisition_summary",
    "candidate_pool",
    "analysis_source",
    "tempo_key_decisions",
    "stem_role_plan",
    "source_windows",
    "entry_exit_plan",
    "balance_proof",
    "render_proof_checks",
    "launch_facts",
)


def write_generation_failure_audit(
    args: argparse.Namespace,
    session_path: Path,
    *,
    stage: str,
    error: BaseException,
    selected: list[SelectedTrack] | None = None,
    structure_rejections: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    audit_path = args.runtime / f"{args.slug}-failure-audit.json"
    selected = selected or getattr(error, "selected", None) or []
    if isinstance(error, SystemExit):
        error_text = str(error)
        error_type = "SystemExit"
    else:
        error_text = str(error)
        error_type = type(error).__name__
    audit = {
        "request_summary": {
            "title": args.title,
            "slug": args.slug,
            "intent": args.intent,
            "live": not bool(args.dry_run),
            "created_at": iso_now(),
        },
        "failure": {
            "stage": stage,
            "type": error_type,
            "message": error_text,
            "traceback": traceback.format_exception_only(type(error), error),
        },
        "session": {
            "path": str(session_path),
            "exists": session_path.exists(),
        },
        "candidate_pool": {
            "selected": [asdict(track) for track in selected],
            "structure_rejections": structure_rejections or [],
        },
        "launch_facts": {
            "target": list(getattr(args, "target", []) or []),
            "dry_run": bool(args.dry_run),
        },
    }
    audit_path.write_text(json.dumps(audit, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"path": str(audit_path), "stage": stage, "error": error_text}


def stem_group_to_guard_event(group: dict[str, Any], actions_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    event = dict(group)
    source_action_id = str(group.get("source_action_id") or group.get("id") or "")
    source_action = actions_by_id.get(source_action_id) or {}
    role = str(group.get("planner_role") or source_action.get("planner_role") or "")
    if not role:
        stems = group.get("stems") if isinstance(group.get("stems"), dict) else {}
        enabled_stems = {name for name, stem in stems.items() if isinstance(stem, dict) and stem.get("enabled", True)}
        if enabled_stems == {"vocals", "drums", "bass", "other"}:
            role = "lead"
        elif "vocals" not in enabled_stems and enabled_stems:
            role = "rhythm-bed"
        elif enabled_stems == {"vocals"}:
            role = "vocal"
    event["planner_role"] = role
    for key in ("key", "tonic", "mode", "camelot"):
        if key not in event and key in source_action:
            event[key] = source_action[key]
    if "path" not in event and event.get("source_path"):
        event["path"] = event["source_path"]
    return event


def is_guard_lead(event: dict[str, Any]) -> bool:
    role = str(event.get("planner_role") or "")
    if role == "lead" or "full" in role:
        return True
    if role in {"rhythm-bed", "vocal"} or "bed" in role or "stab" in role:
        return False
    stems = event.get("stems") if isinstance(event.get("stems"), dict) else {}
    enabled_stems = {name for name, stem in stems.items() if isinstance(stem, dict) and stem.get("enabled", True)}
    return enabled_stems == {"vocals", "drums", "bass", "other"}


def run_session_edit(command_args: list[str]) -> dict[str, Any]:
    command = [sys.executable, "scripts/slime_audio_session.py", *command_args]
    result = subprocess.run(command, cwd=REPO_ROOT, text=True, capture_output=True, check=False)
    return {
        "command": command,
        "returncode": result.returncode,
        "stdout": result.stdout[-2000:],
        "stderr": result.stderr[-2000:],
    }


def launch_runner(session_path: Path, state_path: Path, args: argparse.Namespace) -> int:
    if getattr(args, "force", False):
        # A forced takeover must actually take over: the incumbent runner holds
        # the FIFO writer lock until it exits, and two writers interleave PCM.
        incumbents = live_session_runner_pids()
        for pid in incumbents:
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                continue
        deadline = time.time() + 10.0
        while live_session_runner_pids() and time.time() < deadline:
            time.sleep(0.3)
        remaining = live_session_runner_pids()
        if remaining:
            raise SystemExit(f"forced takeover could not stop live runners (pids {remaining}); stop them manually")
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
    if args.ignore_pause:
        command.append("--ignore-pause")
    log = log_path.open("ab")
    process = subprocess.Popen(command, cwd=REPO_ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    pid_path.write_text(f"{process.pid}\n", encoding="utf-8")
    return process.pid


def prefix_block_ids(block: dict[str, Any], prefix: str) -> None:
    def rewrite(value: Any) -> str:
        return f"{prefix}-{value}"

    for collection in ("clips", "actions", "mic_lean_ins"):
        for item in block.get(collection, []) or []:
            if isinstance(item, dict) and item.get("id"):
                item["id"] = rewrite(item["id"])
    for plan in block.get("transition_plans", []) or []:
        if not isinstance(plan, dict):
            continue
        for key in ("id", "from_clip_id", "to_clip_id", "to_action_id"):
            if plan.get(key):
                plan[key] = rewrite(plan[key])
    for automation in block.get("deck_automations", []) or []:
        if not isinstance(automation, dict):
            continue
        for key in ("source_clip_id", "stem_layer_plan_ref"):
            if automation.get(key):
                automation[key] = rewrite(automation[key])


def shift_block_times(block: dict[str, Any], offset_ms: int) -> None:
    for clip in block.get("clips", []) or []:
        clip["start_ms"] = int(clip.get("start_ms") or 0) + offset_ms
    for action in block.get("actions", []) or []:
        if "at_ms" in action:
            action["at_ms"] = int(action.get("at_ms") or 0) + offset_ms
        if "start_ms" in action:
            action["start_ms"] = int(action.get("start_ms") or 0) + offset_ms
    for lean in block.get("mic_lean_ins", []) or []:
        lean["start"] = format_ms(parse_ms(lean.get("start"), "extension lean-in start") + offset_ms)
        for key in ("ducking", "lowpass"):
            envelope = lean.get(key)
            if not isinstance(envelope, dict):
                continue
            for point in envelope.get("points", []) or []:
                if isinstance(point, dict) and "at" in point:
                    point["at"] = int(point["at"]) + offset_ms
    for automation in block.get("deck_automations", []) or []:
        for point in automation.get("points", []) or []:
            for key in ("at", "at_ms"):
                if isinstance(point, dict) and key in point:
                    point[key] = int(point[key]) + offset_ms


def merge_block_into_payload(payload: dict[str, Any], block: dict[str, Any], offset_ms: int) -> dict[str, Any]:
    merged = json.loads(json.dumps(payload))
    addition = json.loads(json.dumps(block))
    shift_block_times(addition, offset_ms)
    merged.setdefault("clips", []).extend(addition.get("clips", []) or [])
    merged.setdefault("actions", []).extend(addition.get("actions", []) or [])
    merged.setdefault("transition_plans", []).extend(addition.get("transition_plans", []) or [])
    merged.setdefault("mic_lean_ins", []).extend(addition.get("mic_lean_ins", []) or [])
    merged.setdefault("deck_automations", []).extend(addition.get("deck_automations", []) or [])
    decks = [str(deck) for deck in merged.get("decks", []) if str(deck)]
    for deck in addition.get("decks", []) or []:
        if str(deck) and str(deck) not in decks:
            decks.append(str(deck))
    merged["decks"] = decks
    routing = merged.setdefault("fader_routing", {}).setdefault("deck_assignments", {})
    for deck, side in ((addition.get("fader_routing") or {}).get("deck_assignments") or {}).items():
        routing.setdefault(deck, side)
    merged["clips"] = sorted(
        merged.get("clips", []),
        key=lambda clip: (int(clip.get("start_ms", clip.get("start", 0)) or 0), str(clip.get("deck") or ""), str(clip.get("id") or "")),
    )
    merged["actions"] = sorted(
        merged.get("actions", []),
        # load_track must sort before same-instant actions that reference the
        # load (e.g. a stem_toggle at the load's own start), or
        # compile_actions_payload walks the toggle before the load exists and
        # rejects the whole session.
        key=lambda action: (
            int(action.get("at_ms", action.get("start_ms", 0)) or 0),
            0 if str(action.get("type") or "") == "load_track" else 1,
            str(action.get("id") or ""),
        ),
    )
    return merged


def resolve_live_session_paths(args: argparse.Namespace) -> tuple[Path, Path]:
    if args.session is not None:
        if args.state is not None:
            return args.session, args.state
        return args.session, args.session.with_name(args.session.stem + "-state.json")
    pointer_path = args.active_pointer
    if not pointer_path.exists():
        raise SystemExit(f"no active set pointer at {pointer_path}; pass --session or start a set first")
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    session_value = str(pointer.get("active_session_path") or "")
    if not session_value:
        raise SystemExit(f"active set pointer {pointer_path} has no active_session_path")
    session_path = Path(session_value)
    state_value = str(pointer.get("active_state_path") or "")
    state_path = Path(state_value) if state_value else session_path.with_name(session_path.stem + "-state.json")
    if args.state is not None:
        state_path = args.state
    return session_path, state_path


def extend_set(args: argparse.Namespace) -> int:
    from slime_audio_session_mixdown import session_duration_ms

    args.runtime.mkdir(parents=True, exist_ok=True)
    if args.pause_file.exists() and not args.ignore_pause:
        print(
            json.dumps(
                {
                    "status": "paused",
                    "pause_file": str(args.pause_file),
                    "reason": args.pause_file.read_text(encoding="utf-8", errors="replace").strip(),
                },
                sort_keys=True,
            )
        )
        return 0
    inherit_room_masters(args)
    # extend is the heartbeat, so it also heals the room: receivers whose
    # shared-stream client crashed never restart it themselves, and a silent
    # receiver under a healthy server is otherwise invisible until a human
    # notices. Never let healing block the timeline work.
    try:
        from slime_audio_stream import heal_shared_stream_listeners

        kicked = heal_shared_stream_listeners()
        if kicked and args.history is not None:
            args.history.parent.mkdir(parents=True, exist_ok=True)
            with args.history.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {"event": "receiver_listener_restarted", "endpoints": kicked, "timestamp": iso_now()},
                        sort_keys=True,
                    )
                    + "\n"
                )
    except Exception as error:
        print(f"receiver heal skipped: {error}", file=sys.stderr)
    lock_fd = acquire_lock()
    try:
        session_path, state_path = resolve_live_session_paths(args)
        if not session_path.exists():
            raise SystemExit(f"live session not found: {session_path}; run autodj continue first")
        source_text = session_path.read_text(encoding="utf-8")
        payload = json.loads(source_text)
        total_ms = session_duration_ms(parse_session(payload))
        playhead_ms = playhead_ms_from_state(state_path) if state_path.exists() else 0
        remaining_ms = max(0, total_ms - playhead_ms)
        base_status = {
            "session": str(session_path),
            "state": str(state_path),
            "total_ms": total_ms,
            "playhead_ms": playhead_ms,
            "remaining_ms": remaining_ms,
        }
        if not args.force:
            if args.target_length_ms > 0 and total_ms >= args.target_length_ms:
                print(json.dumps({"status": "ok", "reason": "target length reached", **base_status}, sort_keys=True))
                return 0
            if remaining_ms >= args.ahead_ms:
                print(json.dumps({"status": "ok", "reason": "enough runway ahead", **base_status}, sort_keys=True))
                return 0

        args.min_runway_ms = args.block_ms
        work_path = args.runtime / f"{args.slug}.json"
        plan_path = args.runtime / f"{args.slug}-plan.json"
        stage = "select_tracks"
        selected: list[SelectedTrack] = []
        analyses: dict[str, TrackAnalysis] = {}
        structure_rejections: list[dict[str, Any]] = []
        try:
            existing_paths = {str(clip.get("path") or "") for clip in payload.get("clips", []) if isinstance(clip, dict)}
            existing_paths |= {
                str(action.get("source_path") or action.get("path") or "")
                for action in payload.get("actions", [])
                if isinstance(action, dict)
            }
            existing_paths.discard("")
            selected = select_tracks(args, exclude_paths=existing_paths)
            stage = "analysis"
            analyses = load_or_analyze_selected(selected, args)
            stage = "filter_defensible_sources"
            selected, structure_rejections = filter_defensible_source_tracks(selected, analyses, args)
            stage = "session_payload"
            block = session_payload(selected, args, analyses)
            prefix_block_ids(block, f"ext-{time.strftime('%m%d%H%M%S')}")
            stage = "merge"
            merged = merge_block_into_payload(payload, block, offset_ms=total_ms)
            notes = merged.setdefault("notes", {})
            block_commentary_slots = [
                {**slot, "at_ms": int(slot.get("at_ms") or 0) + total_ms}
                for slot in (block.get("notes") or {}).get("commentary_slots", [])
            ]
            notes.setdefault("commentary_slots", []).extend(block_commentary_slots)
            extensions = notes.setdefault("extensions", [])
            extensions.append(
                {
                    "at_ms": total_ms,
                    "block_ms": args.block_ms,
                    "created_at": iso_now(),
                    "intent": args.intent,
                    "slug": args.slug,
                    "tracks": [track.path for track in selected],
                }
            )
            write_payload(work_path, merged)
            append_selection_history(selected, args, session_path=session_path, dry_run=args.dry_run)
            # Lock everything at or before the window the runner may already be
            # prerendering; the planner may restitch anything after that,
            # including the junction between old tail and new block.
            state_payload = load_json_file(state_path) if state_path.exists() else {}
            window_end_ms = int(state_payload.get("window_end_ms") or 0)
            lock_before_ms = max(playhead_ms, window_end_ms) + args.prerender_lead_ms + 5_000
            stage = "mix_planner"
            planner = run_planner(work_path, args, lock_before_ms=min(lock_before_ms, total_ms))
            if planner["returncode"] != 0:
                raise SystemExit(planner["stderr"] or planner["stdout"] or "mix planner failed")
            stage = "structural_beds"
            structural = add_structural_beds(work_path, selected, args, min_start_ms=total_ms)
            stage = "harmonic_guard"
            # Overlaps that already played are history, not defects to block on.
            args.harmonic_guard_after_ms = playhead_ms
            harmonic_guard = validate_harmonic_overlaps(work_path, args)
            advisories: list[dict[str, Any]] = []
            args.require_stem_loads = bool(args.stem_aware_remix) and not (block.get("notes") or {}).get("stem_split_queued")
            guards = {
                "harmonic_guard": harmonic_guard,
                "vanilla_guard": run_advisory_guard("vanilla", validate_no_vanilla_leads, work_path, args, advisories),
                "stem_load_guard": run_advisory_guard("stem-load", validate_stem_load_usage, work_path, args, advisories),
                "transition_decision_guard": run_advisory_guard(
                    "transition-decisions", validate_transition_decisions, work_path, args, advisories
                ),
                "vocal_guards": run_advisory_guard("vocal", validate_vocal_guards, work_path, args, advisories),
                "component_bed_balance_guard": run_advisory_guard(
                    "bed-balance", validate_component_bed_balance, work_path, args, advisories
                ),
            }
            stage = "session_validate"
            validate = subprocess.run(
                [sys.executable, "scripts/slime_audio_session.py", "validate", str(work_path)],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            if validate.returncode != 0:
                raise SystemExit(validate.stderr or validate.stdout or "session validation failed")
            plan = {
                "created_at": iso_now(),
                "status": "dry_run" if args.dry_run else "extended",
                "session": str(session_path),
                "state": str(state_path),
                "work_session": str(work_path),
                "title": args.title,
                "slug": args.slug,
                "intent": args.intent,
                "extension_at_ms": total_ms,
                "playhead_ms": playhead_ms,
                "lock_before_ms": lock_before_ms,
                "commentary_slots": block_commentary_slots,
                "planner": planner,
                "structural": structural,
                "guards": guards,
                "advisories": advisories,
                "tracks": [asdict(track) for track in selected],
                "structure_rejections": structure_rejections,
            }
            if not args.dry_run:
                stage = "publish"
                if session_path.read_text(encoding="utf-8") != source_text:
                    raise SystemExit(
                        "live session changed while the extension was being built; rerun autodj extend"
                    )
                write_payload(session_path, load_session_payload(work_path))
                if args.history is not None:
                    args.history.parent.mkdir(parents=True, exist_ok=True)
                    with args.history.open("a", encoding="utf-8") as handle:
                        handle.write(
                            json.dumps(
                                {
                                    "event": "autodj_set_extended",
                                    "session": str(session_path),
                                    "extension_at_ms": total_ms,
                                    "slug": args.slug,
                                    "timestamp": iso_now(),
                                    "tracks": [track.path for track in selected],
                                },
                                sort_keys=True,
                            )
                            + "\n"
                        )
            plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            print(json.dumps(plan, indent=2, sort_keys=True))
            return 0
        except (SystemExit, Exception) as error:
            failure_audit = write_generation_failure_audit(
                args,
                work_path,
                stage=stage,
                error=error,
                selected=selected,
                structure_rejections=structure_rejections,
            )
            print(json.dumps({"status": "failed", "failure_audit": failure_audit}, sort_keys=True), file=sys.stderr)
            raise
    finally:
        os.close(lock_fd)


def load_json_file(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def continue_set(args: argparse.Namespace) -> int:
    args.runtime.mkdir(parents=True, exist_ok=True)
    if args.pause_file.exists() and not args.ignore_pause:
        print(
            json.dumps(
                {
                    "status": "paused",
                    "pause_file": str(args.pause_file),
                    "reason": args.pause_file.read_text(encoding="utf-8", errors="replace").strip(),
                },
                sort_keys=True,
            )
        )
        return 0
    inherit_room_masters(args)
    lock_fd = acquire_lock()
    try:
        if not args.force and playback_healthy():
            print(json.dumps({"status": "ok", "reason": "playback healthy; not stomping"}))
            return 0
        if not args.force and (runner_pids := live_session_runner_pids()):
            # Fail closed: a transient stale/unreadable dashboard state must
            # never read as permission to stomp a live runner. Two runners
            # interleave FIFO writes into audible jumps (heard 2026-07-03).
            raise SystemExit(
                f"live session runner present (pids {runner_pids}) but dashboard state looks unhealthy/stale; "
                "refusing to stomp the stream. Investigate the runner, or pass --force to replace it deliberately."
            )

        session_path = args.runtime / f"{args.slug}.json"
        state_path = args.runtime / f"{args.slug}-state.json"
        plan_path = args.runtime / f"{args.slug}-plan.json"
        stage = "select_tracks"
        selected: list[SelectedTrack] = []
        analyses: dict[str, TrackAnalysis] = {}
        structure_rejections: list[dict[str, Any]] = []
        try:
            selected = select_tracks(args)
            stage = "analysis"
            slow_start_advisory = slow_start_advisory_for(selected, args)
            if slow_start_advisory is not None:
                print(slow_start_advisory["warning"], file=sys.stderr, flush=True)
            analyses = load_or_analyze_selected(selected, args)
            stage = "filter_defensible_sources"
            selected, structure_rejections = filter_defensible_source_tracks(selected, analyses, args)
            stage = "session_payload"
            payload = session_payload(selected, args, analyses)
            write_payload(session_path, payload)
            append_selection_history(selected, args, session_path=session_path, dry_run=args.dry_run)
            stage = "mix_planner"
            planner = run_planner(session_path, args)
            if planner["returncode"] != 0:
                raise SystemExit(planner["stderr"] or planner["stdout"] or "mix planner failed")
            stage = "weave"
            # Weave on FINAL positions: the planner re-times every lead (deep
            # overlaps, bar snapping), so grooves and teases authored earlier
            # would reference a timeline that no longer exists.
            planned_payload = load_session_payload(session_path)
            planned_leads = sorted(
                [a for a in planned_payload.get("actions", []) if a.get("type") == "load_track" and a.get("planner_role") == "lead"],
                key=lambda a: int(a.get("at_ms") or 0),
            )
            woven = weave_arrangement(planned_leads, analyses, args, occupancy=planned_payload.get("actions", []))
            if woven:
                planned_payload.setdefault("actions", []).extend(woven)
                write_payload(session_path, planned_payload)
            stage = "structural_beds"
            structural = add_structural_beds(session_path, selected, args)
            if not structural.get("added"):
                # Bed material selection is genre-keyword gated, so quiet crates
                # produce none mechanically. Surface it for the DJ to decide on
                # purpose instead of shipping an unlayered set by accident.
                advisory_note = {
                    "guard": "beds",
                    "warning": (
                        "no stem/bed layers in this set (mechanical bed selection found no rhythm material). "
                        "If the vibe allows, layer a key/tempo-matched bed under a long lead or two with "
                        "live_edit add-action play_stems; if restraint fits the vibe, keep it sparse on purpose."
                    ),
                }
            else:
                advisory_note = None
            # Two things gate playback: the session must render (below) and
            # overlapping music must not audibly clash (here). Everything else
            # is advice for the DJ agent, reported in the plan and dashboard.
            stage = "harmonic_guard"
            harmonic_guard = validate_harmonic_overlaps(session_path, args)
            advisories: list[dict[str, Any]] = []
            if slow_start_advisory is not None:
                advisories.append(slow_start_advisory)
            if advisory_note is not None:
                advisories.append(advisory_note)
            args.require_stem_loads = bool(args.stem_aware_remix) and not payload.get("notes", {}).get("stem_split_queued")
            guards = {
                "harmonic_guard": harmonic_guard,
                "vanilla_guard": run_advisory_guard("vanilla", validate_no_vanilla_leads, session_path, args, advisories),
                "stem_load_guard": run_advisory_guard("stem-load", validate_stem_load_usage, session_path, args, advisories),
                "transition_decision_guard": run_advisory_guard(
                    "transition-decisions", validate_transition_decisions, session_path, args, advisories
                ),
                "vocal_guards": run_advisory_guard("vocal", validate_vocal_guards, session_path, args, advisories),
                "component_bed_balance_guard": run_advisory_guard(
                    "bed-balance", validate_component_bed_balance, session_path, args, advisories
                ),
            }
            if args.remix_focus and int(structural.get("added") or 0) < 1:
                advisories.append({"guard": "remix-focus", "warning": "no rhythm/stem bed landed; add beds live"})
            stage = "stem_readiness"
            stem_readiness = stem_readiness_report(selected, args)
            payload = load_session_payload(session_path)
            stage = "session_validate"
            validate = subprocess.run(
                [sys.executable, "scripts/slime_audio_session.py", "validate", str(session_path)],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            if validate.returncode != 0:
                raise SystemExit(validate.stderr or validate.stdout or "session validation failed")
            stage = "launch"
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
                "structural": structural,
                "guards": guards,
                "advisories": advisories,
                "stem_readiness": stem_readiness,
                "commentary_slots": (payload.get("notes") or {}).get("commentary_slots", []),
                "tracks": [asdict(track) for track in selected],
                "structure_rejections": structure_rejections,
                "analysis_coverage": {"selected": len(selected), "available": len(analyses)},
                "selection_policy": {
                    "taste_profile": str(args.taste_profile),
                    "taste_profile_available": load_taste_profile(args.taste_profile).available,
                    "downloaded_track_ratio": args.downloaded_track_ratio,
                    "leftfield_download_ratio": args.leftfield_download_ratio,
                    "edm_beds_use_spotify_taste": False,
                },
            }
            plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            print(json.dumps(plan, indent=2, sort_keys=True))
            return 0
        except (SystemExit, Exception) as error:
            failure_audit = write_generation_failure_audit(
                args,
                session_path,
                stage=stage,
                error=error,
                selected=selected,
                structure_rejections=structure_rejections,
            )
            print(json.dumps({"status": "failed", "failure_audit": failure_audit}, sort_keys=True), file=sys.stderr)
            raise
    finally:
        os.close(lock_fd)


def slow_start_advisory_for(selected: list[SelectedTrack], args: argparse.Namespace, *, threshold: int = 8) -> dict[str, Any] | None:
    """A big hand-picked list of unanalyzed tracks means minutes of silence
    before first audio. Advise the two-phase start before the wait begins."""
    if not getattr(args, "track", None):
        return None
    conn = connect(args.db)
    missing = [
        track.path
        for track in selected
        if (row := conn.execute("SELECT tunebat_bpm FROM tracks WHERE preferred_path = ?", (track.path,)).fetchone()) is None
        or row[0] is None
    ]
    if len(missing) < threshold:
        return None
    return {
        "guard": "time-to-audio",
        "warning": (
            f"{len(missing)} hand-picked tracks need fresh BPM/key analysis before any audio starts. "
            "Two-phase start beats a silent room: launch a short opener from analyzed material now, "
            "then append this list behind the playhead with extend --track."
        ),
    }


def run_advisory_guard(name: str, guard, session_path: Path, args: argparse.Namespace, advisories: list[dict[str, Any]]) -> dict[str, Any]:
    """Taste guards advise; they do not gate playback. Only render validity
    and audible harm (key clashes on real overlaps) stay fatal."""
    try:
        return guard(session_path, args)
    except SystemExit as error:
        advisories.append({"guard": name, "warning": str(error)})
        return {"advisory": str(error)}


def validate_dj_session(args: argparse.Namespace) -> int:
    validate = subprocess.run(
        [sys.executable, "scripts/slime_audio_session.py", "validate", str(args.session)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if validate.returncode != 0:
        raise SystemExit(validate.stderr or validate.stdout or "session validation failed")
    report = {
        "session": str(args.session),
        "vanilla_guard": validate_no_vanilla_leads(args.session, args),
        "stem_load_guard": validate_stem_load_usage(args.session, args),
        "transition_decision_guard": validate_transition_decisions(args.session, args),
        "harmonic_guard": validate_harmonic_overlaps(args.session, args),
        "vocal_guards": validate_vocal_guards(args.session, args),
        "component_bed_balance_guard": validate_component_bed_balance(args.session, args),
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Start a normal database-backed SlimeAudio DJ continuation set.")
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate-session", help="Run DJ-specific guards against an existing session.")
    validate.add_argument("session", type=Path)
    validate.add_argument("--db", type=Path, default=DEFAULT_DB)
    validate.add_argument("--analysis-cache", type=Path, default=DEFAULT_ANALYSIS_CACHE)
    validate.add_argument("--min-vanilla-check-ms", type=int, default=90_000)
    validate.add_argument("--max-vanilla-lead-ms", type=int, default=90_000)
    validate.add_argument("--min-harmonic-overlap-ms", type=int, default=DEFAULT_MIN_HARMONIC_OVERLAP_MS)
    validate.add_argument("--require-stem-loads", action=argparse.BooleanOptionalAction, default=False)
    stop = sub.add_parser("stop", help="Stop the live set cleanly (terminates runners, logs why, leaves no pause).")
    stop.add_argument("--reason", default="operator stop")
    stop.add_argument("--history", type=Path, default=DEFAULT_RUNTIME / "play-history.jsonl")
    cont = sub.add_parser("continue", help="Start a fresh database-backed set when nothing is playing.")
    add_generation_arguments(cont)
    cont.add_argument("--title", default=f"Autodj Continuation {time.strftime('%Y-%m-%d %H%M')}")
    cont.add_argument("--slug", default=f"autodj-continuation-{time.strftime('%Y%m%d-%H%M%S')}")
    cont.add_argument("--intent", default="continue the room from fresh database-backed candidates without hardcoded tracks")
    cont.add_argument("--target", action="append", default=list(DEFAULT_TARGETS))
    cont.add_argument("--min-runway-ms", type=int, default=DEFAULT_MIN_RUNWAY_MS)
    cont.add_argument("--min-tracks", type=int, default=5)
    cont.add_argument("--max-tracks", type=int, default=DEFAULT_MAX_TRACKS)
    cont.add_argument("--window-ms", type=int, default=180_000)
    cont.add_argument("--prerender-lead-ms", type=int, default=60_000)
    cont.add_argument("--discover-timeout-ms", type=int, default=4000)
    cont.add_argument("--force", action="store_true", help="Generate and launch even when playback looks healthy.")
    extend = sub.add_parser("extend", help="Append a planned, guarded block to the live session behind the playhead.")
    add_generation_arguments(extend)
    extend.add_argument("--session", type=Path, help="Live session to extend. Defaults to the active-set pointer.")
    extend.add_argument("--state", type=Path, help="Runner state for the live session. Defaults next to --session or from the pointer.")
    extend.add_argument("--active-pointer", type=Path, default=DEFAULT_ACTIVE_SET)
    extend.add_argument("--title", default=f"Autodj Extension {time.strftime('%Y-%m-%d %H%M')}")
    extend.add_argument("--slug", default=f"autodj-extension-{time.strftime('%Y%m%d-%H%M%S')}")
    extend.add_argument("--intent", default="extend the live set with fresh database-backed material behind the playhead")
    extend.add_argument(
        "--ahead-ms",
        type=int,
        default=DEFAULT_EXTEND_AHEAD_MS,
        help="No-op when at least this much scheduled music remains ahead of the playhead.",
    )
    extend.add_argument(
        "--block-ms",
        type=int,
        default=DEFAULT_EXTEND_BLOCK_MS,
        help="Target amount of new timeline to append per invocation.",
    )
    extend.add_argument(
        "--target-length-ms",
        type=int,
        default=DEFAULT_TARGET_LENGTH_MS,
        help="Stop extending once the session reaches this total length. 0 keeps extending forever (cron mode).",
    )
    extend.add_argument("--min-tracks", type=int, default=2)
    extend.add_argument("--max-tracks", type=int, default=8)
    extend.add_argument("--prerender-lead-ms", type=int, default=60_000)
    extend.add_argument("--force", action="store_true", help="Append a block even when runway/target checks would skip.")
    return parser.parse_args()


def add_generation_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--constraints", type=Path, default=DEFAULT_CONSTRAINTS)
    parser.add_argument("--history", type=Path, default=DEFAULT_HISTORY)
    parser.add_argument("--runtime", type=Path, default=DEFAULT_RUNTIME)
    parser.add_argument("--taste-profile", type=Path, default=DEFAULT_TASTE_PROFILE)
    parser.add_argument("--pause-file", type=Path, default=DEFAULT_AUTODJ_PAUSE_FILE)
    parser.add_argument("--ignore-pause", action="store_true")
    parser.add_argument("--analysis-cache", type=Path, default=DEFAULT_ANALYSIS_CACHE)
    parser.add_argument("--analysis-backend", choices=["auto", "ffmpeg"], default="ffmpeg")
    parser.add_argument("--analysis-sample-rate", type=int, default=44_100)
    parser.add_argument("--tunebat-analyzer", type=Path, default=DEFAULT_TUNEBAT_LOCAL_ANALYZER)
    parser.add_argument("--max-per-artist", type=int, default=1)
    parser.add_argument("--recent-limit", type=int, default=120)
    parser.add_argument("--recent-material-policy", choices=["penalty", "ban", "off"], default="penalty")
    parser.add_argument("--pool-per-query", type=int, default=60)
    parser.add_argument("--sql-pool-limit", type=int, default=600)
    parser.add_argument("--query-count", type=int, default=0)
    parser.add_argument("--include-broad-pool", action="store_true")
    parser.add_argument("--remix-focus", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--stem-aware-remix", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--downloaded-track-ratio", type=float, default=0.10)
    parser.add_argument("--leftfield-download-ratio", type=float, default=0.10)
    parser.add_argument("--selection-jitter", type=float, default=0.12)
    parser.add_argument("--scratch-source-file", type=Path, action="append")
    parser.add_argument("--scratch-material-policy", choices=["ban", "penalty", "off"], default="ban")
    parser.add_argument("--scratch-material-penalty", type=float, default=0.8)
    parser.add_argument("--skip-term", action="append", default=list(DEFAULT_SKIP_TERMS))
    parser.add_argument("--require-analysis", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--min-bpm", type=float, help="Only select tracks with analyzed BPM at or above this (tempo-column browsing).")
    parser.add_argument("--max-bpm", type=float, help="Only select tracks with analyzed BPM at or below this (tempo-column browsing).")
    parser.add_argument(
        "--track",
        type=Path,
        action="append",
        help="Hand-pick this track (repeatable, in play order). Skips mechanical selection; analysis, arrangement, planning, and guards still run.",
    )
    parser.add_argument(
        "--target-bpm",
        type=float,
        help="Tempo-lock the set: select tracks whose analyzed BPM can stretch to this rendered tempo and author the shift on every lead. Slowing interesting tracks down (or speeding them up) is the point.",
    )
    parser.add_argument(
        "--max-tempo-stretch-pct",
        type=float,
        default=16.0,
        help="How far a lead may be stretched to reach --target-bpm (turntable wide mode is 16).",
    )
    parser.add_argument(
        "--target-key",
        default=None,
        help='Master key for the set (e.g. "A minor"): every keyed lead pitch-matches its relative-major tonic to it, within --max-key-shift-semitones; out-of-reach material plays native. Per-track opt-out via live_edit set-warp --no-keymatch.',
    )
    parser.add_argument(
        "--max-key-shift-semitones",
        type=int,
        default=2,
        help="Keymatch pitch limit; material further than this from the master key plays native.",
    )
    parser.add_argument("--min-score", type=float, default=None)
    parser.add_argument("--default-track-ms", type=int, default=240_000)
    parser.add_argument("--min-track-ms", type=int, default=90_000)
    parser.add_argument(
        "--arrangement",
        choices=["full", "sections"],
        default="full",
        help="full: play whole songs mixed into each other (the normal set). sections: chop leads to anchored windows (rapid remix work; implied by --remix-focus/--stem-aware-remix).",
    )
    parser.add_argument("--max-full-lead-ms", type=int, default=480_000)
    parser.add_argument("--max-lead-clip-ms", type=int, default=90_000)
    parser.add_argument("--max-fast-lead-clip-ms", type=int, default=64_000)
    parser.add_argument("--min-section-clip-ms", type=int, default=32_000)
    parser.add_argument("--min-anchor-section-ms", type=int, default=8_000)
    parser.add_argument("--min-section-confidence", type=float, default=0.45)
    parser.add_argument("--base-overlap-ms", type=int, default=0)
    parser.add_argument("--fade-in-ms", type=int, default=0)
    parser.add_argument("--fade-out-ms", type=int, default=0)
    parser.add_argument("--bed-duration-ms", type=int, default=96_000)
    parser.add_argument("--bed-trim-start-ms", type=int, default=60_000)
    parser.add_argument("--bed-gain-db", type=float, default=-3.0)
    parser.add_argument("--bed-fade-in-ms", type=int, default=3_000)
    parser.add_argument("--bed-fade-out-ms", type=int, default=1_500)
    parser.add_argument("--bed-lowpass-hz", type=float, default=1_800.0)
    parser.add_argument("--bed-highpass-hz", type=float, default=90.0)
    parser.add_argument("--max-structural-beds", type=int, default=4)
    parser.add_argument("--min-vanilla-check-ms", type=int, default=90_000)
    parser.add_argument("--max-vanilla-lead-ms", type=int, default=90_000)
    parser.add_argument("--min-harmonic-overlap-ms", type=int, default=DEFAULT_MIN_HARMONIC_OVERLAP_MS)
    parser.add_argument("--dry-run", action="store_true")


def inherit_room_masters(args: argparse.Namespace) -> None:
    """The room's musical identity survives relaunches.

    A continue with explicit --target-bpm/--target-key records them in the
    constraints file; a bare continue (the watchdog's dead-air fallback, a
    minimal operator command) inherits them back — so even a blind relaunch
    keeps the set's tempo and key instead of drifting off-vibe.
    """
    constraints_path = Path(getattr(args, "constraints", None) or (DEFAULT_RUNTIME / "live-set-constraints.json"))
    try:
        payload = json.loads(constraints_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        payload = {}
    changed = False
    if getattr(args, "target_bpm", None) is not None:
        if payload.get("master_bpm") != float(args.target_bpm):
            payload["master_bpm"] = float(args.target_bpm)
            changed = True
    elif payload.get("master_bpm"):
        args.target_bpm = float(payload["master_bpm"])
    if getattr(args, "target_key", None):
        if payload.get("master_key") != str(args.target_key):
            payload["master_key"] = str(args.target_key)
            changed = True
    elif payload.get("master_key"):
        args.target_key = str(payload["master_key"])
    if changed:
        constraints_path.parent.mkdir(parents=True, exist_ok=True)
        constraints_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def stop_set(args: argparse.Namespace) -> int:
    """Stop the live set cleanly: terminate runners, log why, leave no pause.

    The room goes quiet on purpose (operator request, cold-test reset) and the
    next `continue` starts fresh without needing --force or an unpause.
    """
    pids = live_session_runner_pids()
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            continue
    deadline = time.time() + 10.0
    while live_session_runner_pids() and time.time() < deadline:
        time.sleep(0.3)
    remaining = live_session_runner_pids()
    if args.history is not None:
        args.history.parent.mkdir(parents=True, exist_ok=True)
        with args.history.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {"event": "set_stopped", "pids": pids, "reason": args.reason, "timestamp": iso_now()},
                    sort_keys=True,
                )
                + "\n"
            )
    print(json.dumps({"status": "stopped" if not remaining else "failed", "stopped_pids": pids, "still_running": remaining, "reason": args.reason}, sort_keys=True))
    return 0 if not remaining else 1


def main() -> int:
    args = parse_args()
    if args.command == "validate-session":
        return validate_dj_session(args)
    if args.command == "continue":
        return continue_set(args)
    if args.command == "extend":
        return extend_set(args)
    if args.command == "stop":
        return stop_set(args)
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
