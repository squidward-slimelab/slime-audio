#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import sqlite3
import subprocess
import struct
import tempfile
import wave
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from slime_music_library import DEFAULT_DB, connect

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STEM_ROOT = REPO_ROOT / "runtime" / "stems"
CANONICAL_STEMS = ("vocals", "drums", "bass", "other")
WINDOW_MS = 1000


@dataclass(frozen=True)
class TrackIdentity:
    source_path: Path
    duplicate_key: str | None
    source_size: int
    source_mtime: float
    model: str
    profile: str
    sample_rate: int
    channels: int

    @property
    def stem_set_id(self) -> str:
        payload = {
            "source_path": str(self.source_path),
            "duplicate_key": self.duplicate_key,
            "source_size": self.source_size,
            "source_mtime": self.source_mtime,
            "model": self.model,
            "profile": self.profile,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:24]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def resolve_track(conn: sqlite3.Connection, value: str) -> tuple[Path, str | None]:
    path = Path(value).expanduser()
    if path.exists():
        return path.resolve(), None
    row = conn.execute("SELECT duplicate_key, preferred_path FROM tracks WHERE duplicate_key = ?", (value,)).fetchone()
    if row:
        return Path(row["preferred_path"]).resolve(), str(row["duplicate_key"])
    rows = conn.execute(
        """
        SELECT duplicate_key, preferred_path
        FROM tracks
        WHERE normalized_title LIKE '%' || lower(?) || '%'
           OR normalized_artist LIKE '%' || lower(?) || '%'
        ORDER BY preferred_quality_score DESC, preferred_path ASC
        LIMIT 2
        """,
        (value, value),
    ).fetchall()
    if len(rows) == 1:
        return Path(rows[0]["preferred_path"]).resolve(), str(rows[0]["duplicate_key"])
    if len(rows) > 1:
        raise SystemExit(f"track query is ambiguous: {value}")
    raise SystemExit(f"track not found: {value}")


def probe_audio(path: Path) -> dict[str, int | float]:
    result = subprocess.run(
        [
            "ffprobe",
            "-hide_banner",
            "-loglevel",
            "error",
            "-show_entries",
            "format=duration:stream=sample_rate,channels",
            "-select_streams",
            "a:0",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    stream = (payload.get("streams") or [{}])[0]
    duration_s = float((payload.get("format") or {}).get("duration") or 0)
    return {
        "duration_ms": int(round(duration_s * 1000)),
        "sample_rate": int(stream.get("sample_rate") or 0),
        "channels": int(stream.get("channels") or 0),
    }


def identity_for(path: Path, duplicate_key: str | None, *, model: str, profile: str, sample_rate: int, channels: int) -> TrackIdentity:
    stat = path.stat()
    return TrackIdentity(
        source_path=path,
        duplicate_key=duplicate_key,
        source_size=stat.st_size,
        source_mtime=stat.st_mtime,
        model=model,
        profile=profile,
        sample_rate=sample_rate,
        channels=channels,
    )


def fresh_stem_set(conn: sqlite3.Connection, identity: TrackIdentity) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT *
        FROM track_stem_sets
        WHERE source_path = ?
          AND source_size = ?
          AND source_mtime = ?
          AND model = ?
          AND profile = ?
          AND status = 'ready'
        """,
        (str(identity.source_path), identity.source_size, identity.source_mtime, identity.model, identity.profile),
    ).fetchone()


def upsert_stem_set(conn: sqlite3.Connection, identity: TrackIdentity, artifact_root: Path, *, status: str, error: str | None, audio: dict[str, Any] | None = None) -> None:
    timestamp = now_iso()
    existing = conn.execute("SELECT created_at FROM track_stem_sets WHERE id = ?", (identity.stem_set_id,)).fetchone()
    created_at = existing["created_at"] if existing else timestamp
    conn.execute(
        """
        INSERT INTO track_stem_sets(
            id, duplicate_key, source_path, source_size, source_mtime, model, profile, artifact_root,
            sample_rate, channels, duration_ms, status, error, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            duplicate_key = excluded.duplicate_key,
            artifact_root = excluded.artifact_root,
            sample_rate = excluded.sample_rate,
            channels = excluded.channels,
            duration_ms = excluded.duration_ms,
            status = excluded.status,
            error = excluded.error,
            updated_at = excluded.updated_at
        """,
        (
            identity.stem_set_id,
            identity.duplicate_key,
            str(identity.source_path),
            identity.source_size,
            identity.source_mtime,
            identity.model,
            identity.profile,
            str(artifact_root),
            int((audio or {}).get("sample_rate") or identity.sample_rate),
            int((audio or {}).get("channels") or identity.channels),
            int((audio or {}).get("duration_ms") or 0) or None,
            status,
            error,
            created_at,
            timestamp,
        ),
    )


def measure_wav(path: Path, window_ms: int = WINDOW_MS) -> tuple[dict[str, float | int], list[tuple[int, int, float]]]:
    with wave.open(str(path), "rb") as audio:
        channels = audio.getnchannels()
        sample_rate = audio.getframerate()
        sample_width = audio.getsampwidth()
        frames = audio.getnframes()
        if sample_width != 2:
            return {"loudness_db": None, "peak_db": None, "duration_ms": int(round(frames / sample_rate * 1000)), "sample_rate": sample_rate, "channels": channels}, []
        samples_per_window = max(1, int(sample_rate * channels * window_ms / 1000))
        total_samples = 0
        total_square = 0.0
        peak = 0.0
        window_start_sample = 0
        window_samples = 0
        window_square = 0.0
        windows: list[tuple[int, int, float]] = []
        while True:
            raw = audio.readframes(max(1, sample_rate))
            if not raw:
                break
            for (sample,) in struct.iter_unpack("<h", raw):
                value = sample / 32768.0
                total_samples += 1
                total_square += value * value
                peak = max(peak, abs(value))
                window_samples += 1
                window_square += value * value
                if window_samples >= samples_per_window:
                    start_ms = int(round((window_start_sample / channels) / sample_rate * 1000))
                    end_ms = int(round(((window_start_sample + window_samples) / channels) / sample_rate * 1000))
                    chunk_rms = math.sqrt(window_square / window_samples) or 1e-9
                    windows.append((start_ms, end_ms, 20 * math.log10(chunk_rms)))
                    window_start_sample += window_samples
                    window_samples = 0
                    window_square = 0.0
        if window_samples:
            start_ms = int(round((window_start_sample / channels) / sample_rate * 1000))
            end_ms = int(round(((window_start_sample + window_samples) / channels) / sample_rate * 1000))
            chunk_rms = math.sqrt(window_square / window_samples) or 1e-9
            windows.append((start_ms, end_ms, 20 * math.log10(chunk_rms)))
    if total_samples <= 0:
        return {"loudness_db": -120.0, "peak_db": -120.0, "duration_ms": 0, "sample_rate": sample_rate, "channels": channels}, []
    rms = math.sqrt(total_square / total_samples) or 1e-9
    peak = peak or 1e-9
    return (
        {
            "loudness_db": 20 * math.log10(rms),
            "peak_db": 20 * math.log10(peak),
            "duration_ms": int(round((total_samples / channels) / sample_rate * 1000)),
            "sample_rate": sample_rate,
            "channels": channels,
        },
        windows,
    )


def copy_stems(source_dir: Path, artifact_root: Path) -> dict[str, Path]:
    artifact_root.mkdir(parents=True, exist_ok=True)
    copied: dict[str, Path] = {}
    for stem_name in CANONICAL_STEMS:
        source = source_dir / f"{stem_name}.wav"
        if not source.exists():
            raise ValueError(f"missing {stem_name}.wav in {source_dir}")
        target = artifact_root / f"{stem_name}.wav"
        shutil.copy2(source, target)
        copied[stem_name] = target
    return copied


def run_demucs(source_path: Path, temp_dir: Path, *, demucs_bin: str, model: str, jobs: int) -> Path:
    command = [demucs_bin, "-n", model, "-j", str(jobs), "-o", str(temp_dir), str(source_path)]
    subprocess.run(command, check=True)
    candidates = list(temp_dir.glob(f"{model}/*"))
    if not candidates:
        candidates = list(temp_dir.glob("*/*"))
    for candidate in candidates:
        if all((candidate / f"{stem}.wav").exists() for stem in CANONICAL_STEMS):
            return candidate
    raise RuntimeError(f"demucs did not produce canonical stems under {temp_dir}")


def write_manifest(path: Path, identity: TrackIdentity, audio: dict[str, Any], stem_metrics: dict[str, dict[str, Any]]) -> None:
    manifest = {
        "version": 1,
        "id": identity.stem_set_id,
        "source_path": str(identity.source_path),
        "duplicate_key": identity.duplicate_key,
        "source_size": identity.source_size,
        "source_mtime": identity.source_mtime,
        "model": identity.model,
        "profile": identity.profile,
        "created_at": now_iso(),
        "sample_rate": audio.get("sample_rate"),
        "channels": audio.get("channels"),
        "duration_ms": audio.get("duration_ms"),
        "stems": {
            stem_name: {
                "path": f"{stem_name}.wav",
                "loudness_db": metrics.get("loudness_db"),
                "peak_db": metrics.get("peak_db"),
            }
            for stem_name, metrics in stem_metrics.items()
        },
        "analysis_source": {"analysis_path": str(identity.source_path)},
    }
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_stem_rows(conn: sqlite3.Connection, stem_set_id: str, stems: dict[str, Path], metrics: dict[str, dict[str, Any]], windows: dict[str, list[tuple[int, int, float]]]) -> None:
    conn.execute("DELETE FROM track_stems WHERE stem_set_id = ?", (stem_set_id,))
    for stem_name, stem_path in stems.items():
        item = metrics[stem_name]
        conn.execute(
            """
            INSERT INTO track_stems(stem_set_id, stem_name, path, loudness_db, peak_db, vocal_presence_score, artifact_score)
            VALUES (?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                stem_set_id,
                stem_name,
                str(stem_path),
                item.get("loudness_db"),
                item.get("peak_db"),
                vocal_presence_score(windows.get(stem_name, [])) if stem_name == "vocals" else None,
            ),
        )
    write_window_rows(conn, stem_set_id, windows)


def vocal_presence_score(windows: list[tuple[int, int, float]]) -> float:
    if not windows:
        return 0.0
    threshold = adaptive_threshold(windows)
    active_ms = sum(end - start for start, end, db in windows if db >= threshold)
    total_ms = sum(end - start for start, end, _db in windows) or 1
    return max(0.0, min(1.0, active_ms / total_ms))


def adaptive_threshold(windows: list[tuple[int, int, float]]) -> float:
    if not windows:
        return -120.0
    values = sorted(db for _start, _end, db in windows)
    median = values[len(values) // 2]
    peak = max(values)
    return max(median + 6.0, peak - 18.0, -48.0)


def contiguous_windows(source: list[tuple[int, int, float]], predicate: Any, kind: str, reason: str) -> list[tuple[str, int, int, float, str]]:
    result: list[tuple[str, int, int, float, str]] = []
    current_start: int | None = None
    current_end: int | None = None
    confidence_values: list[float] = []
    for start, end, db in source:
        if predicate(db):
            if current_start is None:
                current_start = start
            current_end = end
            confidence_values.append(max(0.0, min(1.0, (db + 60.0) / 60.0)))
        elif current_start is not None and current_end is not None:
            result.append((kind, current_start, current_end, sum(confidence_values) / max(1, len(confidence_values)), reason))
            current_start = None
            current_end = None
            confidence_values = []
    if current_start is not None and current_end is not None:
        result.append((kind, current_start, current_end, sum(confidence_values) / max(1, len(confidence_values)), reason))
    return result


def write_window_rows(conn: sqlite3.Connection, stem_set_id: str, windows: dict[str, list[tuple[int, int, float]]]) -> None:
    conn.execute("DELETE FROM track_stem_windows WHERE stem_set_id = ?", (stem_set_id,))
    for stem_name, stem_windows in windows.items():
        if not stem_windows:
            continue
        threshold = adaptive_threshold(stem_windows)
        kinds: list[tuple[str, int, int, float, str]] = []
        if stem_name == "vocals":
            kinds.extend(contiguous_windows(stem_windows, lambda db: db >= threshold, "vocal_present", "vocal stem energy above adaptive threshold"))
            kinds.extend(contiguous_windows(stem_windows, lambda db: db < threshold, "vocal_absent", "vocal stem energy below adaptive threshold"))
            kinds.extend(contiguous_windows(stem_windows, lambda db: db < threshold, "instrumental_pocket", "vocal absence implies room for overlay"))
        elif stem_name == "bass":
            kinds.extend(contiguous_windows(stem_windows, lambda db: db >= threshold, "bass_active", "bass stem energy above adaptive threshold"))
        elif stem_name == "drums":
            kinds.extend(contiguous_windows(stem_windows, lambda db: db >= threshold, "drums_active", "drums stem energy above adaptive threshold"))
        for kind, start_ms, end_ms, confidence, reason in kinds:
            if end_ms - start_ms < WINDOW_MS:
                continue
            conn.execute(
                """
                INSERT OR REPLACE INTO track_stem_windows(stem_set_id, stem_name, kind, start_ms, end_ms, confidence, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (stem_set_id, stem_name, kind, start_ms, end_ms, confidence, reason),
            )


def command_status(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    source_path, duplicate_key = resolve_track(conn, args.track)
    audio = probe_audio(source_path)
    identity = identity_for(source_path, duplicate_key, model=args.model, profile=args.profile, sample_rate=args.sample_rate or int(audio["sample_rate"]), channels=args.channels or int(audio["channels"]))
    rows = conn.execute("SELECT * FROM track_stem_sets WHERE source_path = ? ORDER BY updated_at DESC", (str(source_path),)).fetchall()
    print(json.dumps({"track": str(source_path), "fresh_id": identity.stem_set_id, "stem_sets": [dict(row) for row in rows]}, indent=2, sort_keys=True))
    return 0


def command_split(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    source_path, duplicate_key = resolve_track(conn, args.track)
    source_audio = probe_audio(source_path)
    sample_rate = args.sample_rate or int(source_audio["sample_rate"])
    channels = args.channels or int(source_audio["channels"])
    identity = identity_for(source_path, duplicate_key, model=args.model, profile=args.profile, sample_rate=sample_rate, channels=channels)
    artifact_root = args.stem_root / identity.stem_set_id
    if not args.force and fresh_stem_set(conn, identity):
        print(json.dumps({"status": "ready", "id": identity.stem_set_id, "artifact_root": str(artifact_root)}, indent=2))
        return 0
    upsert_stem_set(conn, identity, artifact_root, status="running", error=None, audio=source_audio)
    conn.commit()
    try:
        with tempfile.TemporaryDirectory(prefix="slime-audio-stems-") as temp:
            if args.source_stems_dir:
                demucs_dir = args.source_stems_dir
            else:
                demucs_dir = run_demucs(source_path, Path(temp), demucs_bin=args.demucs_bin, model=args.model, jobs=args.jobs)
            stems = copy_stems(demucs_dir, artifact_root)
        metrics: dict[str, dict[str, Any]] = {}
        windows: dict[str, list[tuple[int, int, float]]] = {}
        for stem_name, stem_path in stems.items():
            metrics[stem_name], windows[stem_name] = measure_wav(stem_path)
        write_manifest(artifact_root / "manifest.json", identity, source_audio, metrics)
        upsert_stem_set(conn, identity, artifact_root, status="ready", error=None, audio=source_audio)
        write_stem_rows(conn, identity.stem_set_id, stems, metrics, windows)
        conn.commit()
    except Exception as exc:
        upsert_stem_set(conn, identity, artifact_root, status="failed", error=f"{exc.__class__.__name__}: {exc}", audio=source_audio)
        conn.commit()
        raise
    print(json.dumps({"status": "ready", "id": identity.stem_set_id, "manifest": str(artifact_root / "manifest.json")}, indent=2))
    return 0


def command_analyze(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    row = conn.execute("SELECT * FROM track_stem_sets WHERE id = ? OR source_path = ? ORDER BY updated_at DESC LIMIT 1", (args.track, args.track)).fetchone()
    if row is None:
        source_path, duplicate_key = resolve_track(conn, args.track)
        audio = probe_audio(source_path)
        identity = identity_for(source_path, duplicate_key, model=args.model, profile=args.profile, sample_rate=args.sample_rate or int(audio["sample_rate"]), channels=args.channels or int(audio["channels"]))
        row = fresh_stem_set(conn, identity)
    if row is None:
        raise SystemExit(f"no ready stem set found: {args.track}")
    stems = {stem["stem_name"]: Path(stem["path"]) for stem in conn.execute("SELECT * FROM track_stems WHERE stem_set_id = ?", (row["id"],))}
    metrics: dict[str, dict[str, Any]] = {}
    windows: dict[str, list[tuple[int, int, float]]] = {}
    for stem_name, stem_path in stems.items():
        metrics[stem_name], windows[stem_name] = measure_wav(stem_path)
    write_stem_rows(conn, row["id"], stems, metrics, windows)
    conn.commit()
    print(json.dumps({"status": "analyzed", "id": row["id"], "windows": sum(len(items) for items in windows.values())}, indent=2))
    return 0


def command_verify(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    row = conn.execute("SELECT * FROM track_stem_sets WHERE id = ? OR source_path = ? ORDER BY updated_at DESC LIMIT 1", (args.track, args.track)).fetchone()
    if row is None:
        raise SystemExit(f"stem set not found: {args.track}")
    artifact_root = Path(row["artifact_root"])
    errors: list[str] = []
    if row["status"] != "ready":
        errors.append(f"status is {row['status']}")
    if not (artifact_root / "manifest.json").exists():
        errors.append("manifest missing")
    for stem_name in CANONICAL_STEMS:
        if not (artifact_root / f"{stem_name}.wav").exists():
            errors.append(f"{stem_name}.wav missing")
    window_count = conn.execute("SELECT COUNT(*) AS count FROM track_stem_windows WHERE stem_set_id = ?", (row["id"],)).fetchone()["count"]
    print(json.dumps({"status": "ok" if not errors else "failed", "id": row["id"], "errors": errors, "window_count": window_count}, indent=2))
    return 1 if errors else 0


def command_gc(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    rows = conn.execute("SELECT * FROM track_stem_sets WHERE status = 'failed'").fetchall()
    removed = 0
    for row in rows:
        artifact_root = Path(row["artifact_root"])
        if artifact_root.exists():
            shutil.rmtree(artifact_root)
            removed += 1
    print(json.dumps({"removed_failed_artifact_roots": removed}, indent=2))
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage SlimeAudio stem artifacts and analysis.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--stem-root", type=Path, default=DEFAULT_STEM_ROOT)
    parser.add_argument("--model", default="htdemucs")
    parser.add_argument("--profile", default="4stem")
    parser.add_argument("--sample-rate", type=int)
    parser.add_argument("--channels", type=int)
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status")
    status.add_argument("track")
    status.set_defaults(func=command_status)

    split = sub.add_parser("split")
    split.add_argument("track")
    split.add_argument("--demucs-bin", default="demucs")
    split.add_argument("--jobs", type=int, default=1)
    split.add_argument("--force", action=argparse.BooleanOptionalAction, default=False)
    split.add_argument("--source-stems-dir", type=Path)
    split.set_defaults(func=command_split)

    analyze = sub.add_parser("analyze")
    analyze.add_argument("track")
    analyze.set_defaults(func=command_analyze)

    verify = sub.add_parser("verify")
    verify.add_argument("track")
    verify.set_defaults(func=command_verify)

    gc = sub.add_parser("gc")
    gc.add_argument("--older-than-days", type=int, default=30)
    gc.set_defaults(func=command_gc)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    args.stem_root.mkdir(parents=True, exist_ok=True)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
