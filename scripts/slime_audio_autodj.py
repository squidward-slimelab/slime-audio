#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sqlite3
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
from slime_audio_dj import (
    DEFAULT_CACHE as DEFAULT_ANALYSIS_CACHE,
    DEFAULT_TUNEBAT_LOCAL_ANALYZER,
    TrackAnalysis,
    analyze_with_cache,
    coerce_structure,
    cue_points_for_analysis,
    load_analysis_from_db,
)
from slime_audio_session import parse_session, probe_duration_ms, write_payload
from slime_music_library import DEFAULT_DB, connect, normalize

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNTIME = REPO_ROOT / "runtime"
DEFAULT_TARGETS = ["192.168.0.123:47777", "192.168.0.163:47777"]
DEFAULT_MIN_RUNWAY_MS = 35 * 60 * 1000
DEFAULT_MAX_TRACKS = 24
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


@dataclass(frozen=True)
class SourceWindow:
    trim_start_ms: int
    duration_ms: int
    reason: str
    structure_kind: str | None


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
    by_key: dict[str, dict[str, Any]] = {}
    pool: list[dict[str, Any]] = []
    queries = [None] if args.include_broad_pool else []
    source_words: list[str] = []
    for word in normalize(f"{constraints.direction} {constraints.notes}").split():
        if len(word) >= 4 and word not in VIBE_STOP_WORDS:
            source_words.append(word)
    queries.extend(source_words[: args.query_count])
    if args.remix_focus:
        queries.extend(REMIX_QUERY_LANES)
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
            require_structure=args.structured_source_only,
        )
        for row in rows:
            key = str(row.get("duplicate_key") or row.get("preferred_path"))
            if args.remix_focus and query in REMIX_RHYTHM_LANES:
                row.setdefault("reasons", []).append(f"rhythm lane query: {query}")
            if key in seen:
                if args.remix_focus and query in REMIX_RHYTHM_LANES and key in by_key:
                    by_key[key].setdefault("reasons", []).append(f"rhythm lane query: {query}")
                continue
            seen.add(key)
            by_key[key] = row
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
        if len(selected) >= args.max_tracks:
            break
        if not args.structured_source_only and runway_ms >= args.min_runway_ms:
            break

    if len(selected) < args.min_tracks and runway_ms < args.min_runway_ms:
        raise SystemExit(
            f"only selected {len(selected)} tracks / {round(runway_ms / 60000, 1)} min; refusing weak autodj set"
        )
    return selected


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
        if analysis is None:
            missing.append(path)
        else:
            analyses[track.path] = analysis
    if missing and args.analyze_missing_sections:
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


def source_window_for_track(track: SelectedTrack, analysis: TrackAnalysis | None, args: argparse.Namespace, *, fast_mode: bool) -> SourceWindow:
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
        phrase_ms = analysis.beatgrid.phrase_ms if analysis.beatgrid and analysis.beatgrid.phrase_ms else None
        if phrase_ms and not args.require_section_analysis:
            phrases = max(1, max_clip_ms // phrase_ms)
            duration_ms = min(source_duration_ms, phrases * phrase_ms)
            if duration_ms >= min_clip_ms:
                return SourceWindow(0, duration_ms, "phrase-aligned-fallback", None)
    if args.require_section_analysis:
        raise SystemExit(f"no defensible structure window for {track.artist} - {track.title}")
    return SourceWindow(0, min(source_duration_ms, max_clip_ms), "duration-fallback", None)


def fast_section_mode_for(tracks: list[SelectedTrack]) -> bool:
    rhythm_sources = [track for track in tracks if rhythm_bed_score(track) > 0]
    return any(material_score(track, ("dubstep", "bass", "drum and bass", "dnb", "riddim", "heavy")) > 0 for track in rhythm_sources)


def filter_defensible_source_tracks(
    selected: list[SelectedTrack],
    analyses: dict[str, TrackAnalysis],
    args: argparse.Namespace,
) -> tuple[list[SelectedTrack], list[dict[str, str]]]:
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


def session_payload(selected: list[SelectedTrack], args: argparse.Namespace, analyses: dict[str, TrackAnalysis] | None = None) -> dict[str, Any]:
    analyses = analyses or {}
    rhythm_sources = [track for track in selected if rhythm_bed_score(track) > 0]
    leads = [track for track in selected if track not in rhythm_sources]
    if len(leads) < args.min_tracks:
        leads = sorted(selected, key=lead_score, reverse=True)

    fast_mode = fast_section_mode_for(selected)
    cursor_ms = 0
    lead_clips: list[dict[str, Any]] = []
    for index, track in enumerate(leads[: args.max_tracks]):
        source_window = source_window_for_track(track, analyses.get(track.path), args, fast_mode=fast_mode)
        clip = {
            "id": f"lead-{index + 1:03d}-{slugify(track.title)[:40]}",
            "deck": "deck-2" if index % 2 == 0 else "deck-3",
            "path": track.path,
            "start_ms": cursor_ms,
            "trim_start_ms": source_window.trim_start_ms,
            "duration_ms": source_window.duration_ms,
            "fade_in_ms": 0 if index == 0 else args.fade_in_ms,
            "fade_out_ms": args.fade_out_ms,
            "planner_role": "lead",
            "source_window_reason": source_window.reason,
            "source_structure_kind": source_window.structure_kind,
        }
        lead_clips.append(clip)
        cursor_ms += max(1, source_window.duration_ms - args.base_overlap_ms)

    payload = {
        "version": 1,
        "timeline_mode": "autodj-arrangement",
        "decks": ["deck-1", "deck-2", "deck-3"],
        "clips": sorted(lead_clips, key=lambda clip: (int(clip.get("start_ms") or 0), str(clip.get("id") or ""))),
        "mic_lean_ins": [],
        "automations": [],
        "deck_automations": [],
        "fader_routing": {"deck_assignments": {"deck-1": "A", "deck-2": "A", "deck-3": "B"}},
    }
    payload["title"] = args.title
    payload["notes"] = {
        "created_at": iso_now(),
        "intent": args.intent,
        "selection_process": "database candidates plus play-history freshness penalties; arranged as short lead sections plus real handoffs/beds/effects",
        "remix_focus": bool(args.remix_focus),
        "remix_policy": (
            "hard-techno/dnb/dubstep vocal remix lane: pair vocal/hook leads with rhythm/bass beds, prefer drop/build anchors, avoid vocal clashes, keep one sub/bass source active"
            if args.remix_focus
            else None
        ),
        "selected_material": [asdict(track) for track in selected],
        "lead_count": len(lead_clips),
        "bed_count": 0,
        "max_lead_clip_ms": args.max_lead_clip_ms,
        "fast_section_mode": fast_mode,
    }
    parse_session(payload)
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


def add_structural_beds(session_path: Path, selected: list[SelectedTrack], args: argparse.Namespace) -> dict[str, Any]:
    payload = load_session_payload(session_path)
    lead_paths = {str(clip.get("path") or "") for clip in payload.get("clips", []) if clip.get("planner_role") == "lead"}
    rhythm_sources = [track for track in selected if rhythm_bed_score(track) > 0 and track.path not in lead_paths]
    leads = sorted(
        [clip for clip in payload.get("clips", []) if clip.get("planner_role") == "lead"],
        key=lambda clip: int(clip.get("start_ms", clip.get("start", 0)) or 0),
    )
    if not rhythm_sources or len(leads) < 2:
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

    added: list[dict[str, Any]] = []
    deck_automations = payload.setdefault("deck_automations", [])
    bed_targets = leads[1::2][: len(rhythm_sources)]
    for index, lead in enumerate(bed_targets, start=1):
        source = rhythm_sources[(index - 1) % len(rhythm_sources)]
        lead_start = int(lead.get("start_ms", lead.get("start", 0)) or 0)
        lead_duration = int(lead.get("duration_ms", lead.get("duration", 0)) or 0)
        if lead_duration <= 0:
            continue
        bed_start = lead_start + min(24_000, max(0, lead_duration // 5))
        bed_duration = min(args.bed_duration_ms, max(32_000, lead_duration - (bed_start - lead_start) - 4_000))
        if bed_duration < 32_000:
            continue
        source_duration = source.duration_ms or args.default_track_ms
        trim_start = min(args.bed_trim_start_ms, max(0, source_duration - bed_duration - 1_000))
        bed_id = f"bed-{index:03d}-{slugify(source.title)[:40]}"
        payload.setdefault("clips", []).append(
            {
                "id": bed_id,
                "deck": "deck-4",
                "path": source.path,
                "start_ms": bed_start,
                "trim_start_ms": trim_start,
                "duration_ms": bed_duration,
                "gain_db": args.bed_gain_db,
                "fade_in_ms": args.bed_fade_in_ms,
                "fade_out_ms": args.bed_fade_out_ms,
                "planner_role": "rhythm-bed",
                "bed_under": lead.get("id"),
            }
        )
        end_ms = bed_start + bed_duration
        for param, value in (
            ("gain_db", args.bed_gain_db),
            ("lowpass_hz", args.bed_lowpass_hz),
            ("highpass_hz", args.bed_highpass_hz),
        ):
            deck_automations.append(
                {
                    "target": "deck-4",
                    "param": param,
                    "source_clip_id": bed_id,
                    "planner_role": "bed-filter-carve",
                    "points": [{"at_ms": bed_start, "value": value}, {"at_ms": end_ms, "value": value}],
                }
            )
        added.append({"id": bed_id, "source": source.path, "under": lead.get("id"), "start_ms": bed_start, "duration_ms": bed_duration})

    payload["clips"] = sorted(
        payload.get("clips", []),
        key=lambda clip: (int(clip.get("start_ms", clip.get("start", 0)) or 0), str(clip.get("deck") or ""), str(clip.get("id") or "")),
    )
    notes = payload.setdefault("notes", {})
    notes["bed_count"] = int(notes.get("bed_count") or 0) + len(added)
    write_payload(session_path, payload)
    return {"added": len(added), "beds": added}


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


def validate_no_vanilla_leads(session_path: Path, args: argparse.Namespace) -> dict[str, Any]:
    payload = load_session_payload(session_path)
    clips = [clip for clip in payload.get("clips", []) if clip.get("id") and clip_duration_ms(clip) > 0]
    leads = [clip for clip in clips if clip.get("planner_role") == "lead" and clip_duration_ms(clip) >= args.min_vanilla_check_ms]
    automations = list(payload.get("automations", [])) + list(payload.get("deck_automations", []))
    effects = list(payload.get("effects", []))
    failures: list[dict[str, Any]] = []

    for lead in leads:
        lead_id = str(lead["id"])
        lead_start = clip_start_ms(lead)
        lead_end = clip_end_ms(lead)
        windows: list[tuple[int, int]] = []
        if any(lead.get(key) for key in ("tempo_shift_pct", "pitch_shift_semitones", "reverse")) or lead.get("playback_rate") not in {None, 1, 1.0}:
            windows.append((lead_start, lead_end))
        for clip in clips:
            if clip is lead:
                continue
            role = str(clip.get("planner_role") or "")
            if role == "lead" and clip_duration_ms(clip) > 45_000:
                move_end = min(clip_end_ms(clip), clip_start_ms(clip) + 32_000)
                record_move_window(windows, clip_start_ms(clip), move_end, lead_start, lead_end)
            elif role != "lead":
                record_move_window(windows, clip_start_ms(clip), clip_end_ms(clip), lead_start, lead_end)
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
        if gap > args.max_vanilla_lead_ms:
            failures.append({"id": lead_id, "max_vanilla_gap_ms": gap, "duration_ms": lead_end - lead_start})

    if failures:
        raise SystemExit(f"vanilla lead guard failed: {json.dumps(failures[:5], sort_keys=True)}")
    return {"checked": len(leads), "max_allowed_gap_ms": args.max_vanilla_lead_ms}


def run_session_edit(command_args: list[str]) -> dict[str, Any]:
    command = [sys.executable, "scripts/slime_audio_session.py", *command_args]
    result = subprocess.run(command, cwd=REPO_ROOT, text=True, capture_output=True, check=False)
    return {
        "command": command,
        "returncode": result.returncode,
        "stdout": result.stdout[-2000:],
        "stderr": result.stderr[-2000:],
    }


def ensure_utility_deck(session_path: Path, deck: str = "deck-4") -> None:
    payload = load_session_payload(session_path)
    decks = [str(item) for item in payload.get("decks", []) if str(item)]
    if deck not in decks:
        decks.append(deck)
        payload["decks"] = decks
    routing = payload.setdefault("fader_routing", {}).setdefault("deck_assignments", {})
    routing.setdefault("deck-1", "A")
    routing.setdefault("deck-2", "A")
    routing.setdefault("deck-3", "B")
    routing.setdefault(deck, "THRU")
    write_payload(session_path, payload)


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
    for clip in clips:
        if clip.get("planner_role") == "rhythm-bed":
            moves.append({"kind": "structural-rhythm-bed", "source": clip.get("id"), "target": clip.get("bed_under")})

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
        ensure_utility_deck(session_path, "deck-4")
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
        analyses = load_or_analyze_selected(selected, args)
        selected, structure_rejections = filter_defensible_source_tracks(selected, analyses, args)
        payload = session_payload(selected, args, analyses)
        write_payload(session_path, payload)
        append_selection_history(selected, args, session_path=session_path, dry_run=args.dry_run)
        planner = run_planner(session_path, args)
        structural = add_structural_beds(session_path, selected, args)
        creative = apply_creative_pass(session_path, args)
        vanilla_guard = validate_no_vanilla_leads(session_path, args)
        stem_readiness = stem_readiness_report(selected, args)
        if args.remix_focus:
            move_kinds = {str(move.get("kind") or "") for move in creative.get("moves", [])}
            if int(structural.get("added") or 0) < 1 and not ({"rhythm-bed", "structural-rhythm-bed", "bed-filter-carve"} & move_kinds):
                raise SystemExit("remix-focus set has no rhythm/stem bed; refusing generic handoff-only mix")
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
            "structural": structural,
            "creative": creative,
            "vanilla_guard": vanilla_guard,
            "stem_readiness": stem_readiness,
            "tracks": [asdict(track) for track in selected],
            "structure_rejections": structure_rejections,
            "analysis_coverage": {"selected": len(selected), "available": len(analyses)},
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
    cont.add_argument("--analysis-cache", type=Path, default=DEFAULT_ANALYSIS_CACHE)
    cont.add_argument("--analysis-backend", choices=["auto", "ffmpeg"], default="ffmpeg")
    cont.add_argument("--analysis-sample-rate", type=int, default=44_100)
    cont.add_argument("--tunebat-analyzer", type=Path, default=DEFAULT_TUNEBAT_LOCAL_ANALYZER)
    cont.add_argument("--analyze-missing-sections", action=argparse.BooleanOptionalAction, default=True)
    cont.add_argument("--require-section-analysis", action=argparse.BooleanOptionalAction, default=True)
    cont.add_argument("--structured-source-only", action=argparse.BooleanOptionalAction, default=False)
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
    cont.add_argument("--remix-focus", action=argparse.BooleanOptionalAction, default=False)
    cont.add_argument("--stem-aware-remix", action=argparse.BooleanOptionalAction, default=False)
    cont.add_argument("--selection-jitter", type=float, default=0.12)
    cont.add_argument("--skip-term", action="append", default=list(DEFAULT_SKIP_TERMS))
    cont.add_argument("--require-analysis", action=argparse.BooleanOptionalAction, default=False)
    cont.add_argument("--min-score", type=float, default=None)
    cont.add_argument("--default-track-ms", type=int, default=240_000)
    cont.add_argument("--min-track-ms", type=int, default=90_000)
    cont.add_argument("--max-lead-clip-ms", type=int, default=90_000)
    cont.add_argument("--max-fast-lead-clip-ms", type=int, default=64_000)
    cont.add_argument("--min-section-clip-ms", type=int, default=32_000)
    cont.add_argument("--min-anchor-section-ms", type=int, default=8_000)
    cont.add_argument("--min-section-confidence", type=float, default=0.45)
    cont.add_argument("--base-overlap-ms", type=int, default=8_000)
    cont.add_argument("--fade-in-ms", type=int, default=2_500)
    cont.add_argument("--fade-out-ms", type=int, default=5_000)
    cont.add_argument("--bed-duration-ms", type=int, default=96_000)
    cont.add_argument("--bed-trim-start-ms", type=int, default=60_000)
    cont.add_argument("--bed-gain-db", type=float, default=-6.0)
    cont.add_argument("--bed-fade-in-ms", type=int, default=3_000)
    cont.add_argument("--bed-fade-out-ms", type=int, default=1_500)
    cont.add_argument("--bed-lowpass-hz", type=float, default=1_800.0)
    cont.add_argument("--bed-highpass-hz", type=float, default=90.0)
    cont.add_argument("--routine-every", type=int, default=3)
    cont.add_argument("--min-creative-moves", type=int, default=2)
    cont.add_argument("--min-vanilla-check-ms", type=int, default=90_000)
    cont.add_argument("--max-vanilla-lead-ms", type=int, default=90_000)
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
