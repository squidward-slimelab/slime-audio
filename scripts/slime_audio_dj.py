#!/usr/bin/env python3
from __future__ import annotations

import argparse
import audioop
import json
import math
import sqlite3
import tempfile
import wave
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from slime_audio_session import Clip, MixSession, load_session, parse_session, playhead_ms_from_state
from slime_audio_stream import convert_to_stream_wav
from slime_music_library import DEFAULT_DB as DEFAULT_LIBRARY_DB
from slime_music_library import DEFAULT_TUNEBAT_LOCAL_ANALYZER, store_tunebat_local_payload, tunebat_local_payload

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CACHE = REPO_ROOT / "runtime" / "dj-analysis-cache.json"
NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
MAJOR_PROFILE = (6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88)
MINOR_PROFILE = (6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17)

# Camelot numbers for pitch class 0..11. 8B is C major, 8A is A minor.
MAJOR_CAMELOT = (8, 3, 10, 5, 12, 7, 2, 9, 4, 11, 6, 1)
MINOR_CAMELOT = (5, 12, 7, 2, 9, 4, 11, 6, 1, 8, 3, 10)


@dataclass(frozen=True)
class StructureWindow:
    kind: str
    start_ms: int
    end_ms: int
    confidence: float
    reason: str


@dataclass(frozen=True)
class BeatGrid:
    bpm: float | None
    beat_offset_ms: int | None
    phrase_beats: int
    phrase_ms: int | None


@dataclass(frozen=True)
class TrackAnalysis:
    path: str
    duration_s: float
    sample_rate: int
    channels: int
    bpm: float | None
    beat_offset_ms: int | None
    key: str | None
    tonic: int | None
    mode: str | None
    camelot: str | None
    energy: float
    loudness_db: float
    confidence: dict[str, float]
    beatgrid: BeatGrid | None = None
    structure: list[StructureWindow] | None = None


@dataclass(frozen=True)
class TransitionPlan:
    source: str
    target: str
    score: float
    bpm_ratio: float | None
    target_tempo_shift_pct: float | None
    pitch_shift_semitones: int
    key_relation: str
    phrase_wait_beats: int
    notes: list[str]


@dataclass(frozen=True)
class TensionWindow:
    start_ms: int
    end_ms: int
    kind: str
    confidence: float
    reason: str
    talking_points: list[str]
    clip_id: str | None = None
    track: str | None = None
    next_clip_id: str | None = None
    next_track: str | None = None
    source_start_ms: int | None = None
    source_end_ms: int | None = None


def load_cache(path: Path) -> dict[str, dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_cache(path: Path, cache: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def cache_key(path: Path) -> str:
    stat = path.stat()
    return f"{path.resolve()}:{stat.st_size}:{int(stat.st_mtime)}"


def read_wav_mono(path: Path) -> tuple[int, int, bytes]:
    with wave.open(str(path), "rb") as audio:
        channels = audio.getnchannels()
        width = audio.getsampwidth()
        rate = audio.getframerate()
        frames = audio.readframes(audio.getnframes())
    if width != 2:
        raise SystemExit(f"expected 16-bit PCM wav after decode: {path}")
    if channels > 1:
        frames = audioop.tomono(frames, width, 0.5, 0.5)
    return rate, channels, frames


@contextmanager
def decoded_wav(path: Path, backend: str, sample_rate: int):
    if path.suffix.lower() == ".wav":
        yield path
        return
    with tempfile.TemporaryDirectory(prefix="slime-dj-") as temp_dir:
        wav_path = Path(temp_dir) / "decoded.wav"
        convert_to_stream_wav(path, wav_path, backend, sample_rate, 2)
        yield wav_path


def rms_envelope(frames: bytes, rate: int, frame_ms: int = 46) -> tuple[list[float], float]:
    width = 2
    frame_bytes = max(1, rate * frame_ms // 1000) * width
    values: list[float] = []
    total_square = 0.0
    total_samples = 0
    for offset in range(0, len(frames), frame_bytes):
        chunk = frames[offset : offset + frame_bytes]
        if not chunk:
            continue
        rms = float(audioop.rms(chunk, width))
        values.append(rms)
        samples = len(chunk) // width
        total_square += (rms * rms) * samples
        total_samples += samples
    whole_rms = math.sqrt(total_square / total_samples) if total_samples else 0.0
    return values, whole_rms


def estimate_bpm(envelope: list[float], frame_ms: int = 46) -> tuple[float | None, int | None, float]:
    if len(envelope) < 80:
        return None, None, 0.0
    mean = sum(envelope) / len(envelope)
    centered = [max(0.0, value - mean) for value in envelope]
    onsets = [0.0]
    for previous, current in zip(centered, centered[1:]):
        onsets.append(max(0.0, current - previous))
    if max(onsets, default=0.0) <= 0:
        return None, None, 0.0

    min_lag = max(1, round(60_000 / 200 / frame_ms))
    max_lag = max(min_lag + 1, round(60_000 / 60 / frame_ms))
    scores: list[tuple[float, int]] = []
    for lag in range(min_lag, max_lag + 1):
        score = sum(onsets[index] * onsets[index - lag] for index in range(lag, len(onsets)))
        scores.append((score, lag))
    best_score, best_lag = max(scores)
    if best_score <= 0:
        return None, None, 0.0
    bpm = 60_000 / (best_lag * frame_ms)
    while bpm < 80:
        bpm *= 2
    while bpm > 160:
        bpm /= 2
    sorted_scores = sorted((score for score, _lag in scores), reverse=True)
    runner_up = sorted_scores[1] if len(sorted_scores) > 1 else 0.0
    confidence = min(1.0, max(0.0, (best_score - runner_up) / best_score))

    threshold = (sum(onsets) / len(onsets)) * 1.5
    first_peak = next((index for index, value in enumerate(onsets) if value >= threshold), 0)
    beat_offset_ms = int(first_peak * frame_ms)
    return round(bpm, 2), beat_offset_ms, round(confidence, 3)


def beat_grid(bpm: float | None, beat_offset_ms: int | None, phrase_beats: int = 32) -> BeatGrid:
    phrase_ms = None
    if bpm:
        phrase_ms = int(round((60_000 / bpm) * phrase_beats))
    return BeatGrid(bpm=bpm, beat_offset_ms=beat_offset_ms, phrase_beats=phrase_beats, phrase_ms=phrase_ms)


def smooth(values: list[float], window: int) -> list[float]:
    if not values or window <= 1:
        return values[:]
    half = window // 2
    smoothed = []
    for index in range(len(values)):
        start = max(0, index - half)
        end = min(len(values), index + half + 1)
        smoothed.append(sum(values[start:end]) / (end - start))
    return smoothed


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * fraction)))
    return ordered[index]


def frame_to_ms(index: int, frame_ms: int) -> int:
    return int(index * frame_ms)


def align_to_phrase(ms: int, grid: BeatGrid) -> int:
    if grid.beat_offset_ms is None or grid.phrase_ms is None:
        return max(0, ms)
    if ms <= grid.beat_offset_ms:
        return max(0, grid.beat_offset_ms)
    phrases = round((ms - grid.beat_offset_ms) / grid.phrase_ms)
    return max(0, grid.beat_offset_ms + (phrases * grid.phrase_ms))


def detect_structure_windows(
    envelope: list[float],
    bpm: float | None,
    beat_offset_ms: int | None,
    duration_s: float,
    frame_ms: int = 46,
) -> list[StructureWindow]:
    if not envelope or duration_s <= 0:
        return []
    grid = beat_grid(bpm, beat_offset_ms)
    phrase_ms = grid.phrase_ms or 16_000
    min_window_ms = max(4_000, phrase_ms // 2)
    max_ms = int(duration_s * 1000)
    normalized = []
    high = max(percentile(envelope, 0.95), 1.0)
    for value in envelope:
        normalized.append(min(1.0, value / high))
    window_frames = max(3, round(min_window_ms / frame_ms))
    energy = smooth(normalized, window_frames)
    low_threshold = max(0.10, percentile(energy, 0.25))
    high_threshold = max(low_threshold + 0.10, percentile(energy, 0.72))
    windows: list[StructureWindow] = []

    def add(kind: str, start_ms: int, end_ms: int, confidence: float, reason: str) -> None:
        start_ms = max(0, min(max_ms, align_to_phrase(start_ms, grid)))
        aligned_end = align_to_phrase(end_ms, grid) or end_ms
        end_ms = max(start_ms + min_window_ms, min(max_ms, aligned_end))
        end_ms = min(max_ms, end_ms)
        if end_ms - start_ms >= 1000:
            windows.append(StructureWindow(kind, start_ms, end_ms, round(max(0.0, min(1.0, confidence)), 3), reason))

    intro_end = min(max_ms, phrase_ms if max_ms < phrase_ms * 4 else phrase_ms * 2)
    if intro_end > 0:
        add("intro", 0, intro_end, 0.55, "opening phrase region")

    outro_start = max(0, max_ms - intro_end)
    if outro_start > 0:
        add("outro", outro_start, max_ms, 0.5, "ending phrase region")

    for index in range(1, len(energy) - 1):
        previous = energy[index - 1]
        current = energy[index]
        nxt = energy[index + 1]
        if previous < low_threshold and current < low_threshold and nxt > current:
            start_ms = frame_to_ms(index, frame_ms)
            add("breakdown", start_ms, start_ms + min_window_ms, 0.6 + (low_threshold - current), "sustained lower-energy section")
        rise = nxt - previous
        if current < high_threshold and rise > 0.08:
            start_ms = frame_to_ms(index, frame_ms)
            add("build", start_ms, start_ms + min_window_ms, 0.55 + rise, "energy rising into a likely transition")
        if current >= high_threshold and previous < high_threshold:
            start_ms = frame_to_ms(index, frame_ms)
            add("drop", start_ms, start_ms + min_window_ms, 0.65 + (current - high_threshold), "energy crosses high threshold")

    if not any(window.kind == "build" for window in windows) and len(energy) > window_frames + 1:
        rises = [
            (energy[index + window_frames] - energy[index], index)
            for index in range(0, len(energy) - window_frames)
        ]
        strongest_rise, rise_index = max(rises, default=(0.0, 0))
        if strongest_rise > 0.04:
            start_ms = frame_to_ms(rise_index, frame_ms)
            add("build", start_ms, start_ms + min_window_ms, 0.55 + strongest_rise, "strongest sustained energy rise")

    deduped: list[StructureWindow] = []
    seen: set[tuple[str, int]] = set()
    for window in sorted(windows, key=lambda item: (item.start_ms, item.kind, -item.confidence)):
        bucket = (window.kind, window.start_ms // max(1, phrase_ms))
        if bucket in seen:
            continue
        seen.add(bucket)
        deduped.append(window)
    return deduped[:24]


def suggested_lean_in_windows(structure: list[StructureWindow]) -> list[dict[str, object]]:
    suggestions = []
    for window in structure:
        if window.kind in {"breakdown", "build", "intro"}:
            suggestions.append(
                {
                    "at_ms": window.start_ms,
                    "kind": window.kind,
                    "confidence": window.confidence,
                    "reason": f"{window.kind} window: {window.reason}",
                }
            )
        elif window.kind == "drop":
            suggestions.append(
                {
                    "at_ms": max(0, window.start_ms - 1500),
                    "kind": "pre-drop",
                    "confidence": window.confidence,
                    "reason": "speak just before the detected drop, then get out",
                }
            )
    return suggestions[:12]


def coerce_structure(values: list[StructureWindow] | list[dict] | None) -> list[StructureWindow]:
    result = []
    for value in values or []:
        if isinstance(value, StructureWindow):
            result.append(value)
        elif isinstance(value, dict):
            result.append(StructureWindow(**value))
    return result


def coerce_analysis(value: TrackAnalysis | dict) -> TrackAnalysis:
    if isinstance(value, TrackAnalysis):
        return value
    payload = dict(value)
    if isinstance(payload.get("beatgrid"), dict):
        payload["beatgrid"] = BeatGrid(**payload["beatgrid"])
    payload["structure"] = coerce_structure(payload.get("structure"))
    return TrackAnalysis(**payload)


def beatgrid_asdict(value: BeatGrid | dict | None) -> dict | None:
    if value is None:
        return None
    if isinstance(value, BeatGrid):
        return asdict(value)
    return value


def goertzel_power(samples: list[float], rate: int, frequency: float) -> float:
    if not samples:
        return 0.0
    omega = 2.0 * math.pi * frequency / rate
    coeff = 2.0 * math.cos(omega)
    q0 = q1 = q2 = 0.0
    for sample in samples:
        q0 = coeff * q1 - q2 + sample
        q2 = q1
        q1 = q0
    return q1 * q1 + q2 * q2 - coeff * q1 * q2


def estimate_chroma(frames: bytes, rate: int, max_seconds: int = 90) -> list[float]:
    width = 2
    max_bytes = min(len(frames), rate * max_seconds * width)
    stride = max(1, rate // 4000)
    raw = frames[:max_bytes]
    samples = [
        audioop.getsample(raw, width, index) / 32768.0
        for index in range(0, len(raw) // width, stride)
    ]
    effective_rate = rate / stride
    chroma = [0.0] * 12
    for midi in range(36, 85):
        frequency = 440.0 * (2 ** ((midi - 69) / 12))
        if frequency >= effective_rate / 2:
            continue
        chroma[midi % 12] += goertzel_power(samples, int(effective_rate), frequency)
    total = sum(chroma)
    return [value / total for value in chroma] if total else chroma


def rotate(values: tuple[float, ...], amount: int) -> list[float]:
    return [values[(index - amount) % 12] for index in range(12)]


def correlation(a: list[float], b: list[float]) -> float:
    a_mean = sum(a) / len(a)
    b_mean = sum(b) / len(b)
    numerator = sum((left - a_mean) * (right - b_mean) for left, right in zip(a, b))
    a_den = math.sqrt(sum((value - a_mean) ** 2 for value in a))
    b_den = math.sqrt(sum((value - b_mean) ** 2 for value in b))
    return numerator / (a_den * b_den) if a_den and b_den else 0.0


def estimate_key(frames: bytes, rate: int) -> tuple[int | None, str | None, float]:
    chroma = estimate_chroma(frames, rate)
    if not any(chroma):
        return None, None, 0.0
    candidates: list[tuple[float, int, str]] = []
    for tonic in range(12):
        candidates.append((correlation(chroma, rotate(MAJOR_PROFILE, tonic)), tonic, "major"))
        candidates.append((correlation(chroma, rotate(MINOR_PROFILE, tonic)), tonic, "minor"))
    candidates.sort(reverse=True)
    best_score, tonic, mode = candidates[0]
    runner_up = candidates[1][0] if len(candidates) > 1 else 0.0
    confidence = min(1.0, max(0.0, best_score - runner_up))
    return tonic, mode, round(confidence, 3)


def camelot(tonic: int | None, mode: str | None) -> str | None:
    if tonic is None or mode is None:
        return None
    number = MAJOR_CAMELOT[tonic] if mode == "major" else MINOR_CAMELOT[tonic]
    return f"{number}{'B' if mode == 'major' else 'A'}"


def tonic_mode_from_camelot(value: str | None) -> tuple[int | None, str | None]:
    if not value:
        return None, None
    text = value.strip().upper()
    if len(text) < 2 or text[-1] not in {"A", "B"}:
        return None, None
    try:
        number = int(text[:-1])
    except ValueError:
        return None, None
    if text[-1] == "B" and number in MAJOR_CAMELOT:
        return MAJOR_CAMELOT.index(number), "major"
    if text[-1] == "A" and number in MINOR_CAMELOT:
        return MINOR_CAMELOT.index(number), "minor"
    return None, None


def key_name(tonic: int | None, mode: str | None) -> str | None:
    if tonic is None or mode is None:
        return None
    return f"{NOTE_NAMES[tonic]} {mode}"


def analyze_track(path: Path, backend: str = "auto", sample_rate: int = 44_100) -> TrackAnalysis:
    with decoded_wav(path, backend, sample_rate) as wav_path:
        rate, channels, frames = read_wav_mono(wav_path)
    duration_s = (len(frames) // 2) / rate if rate else 0.0
    envelope, rms = rms_envelope(frames, rate)
    bpm, beat_offset_ms, bpm_confidence = estimate_bpm(envelope)
    tonic, mode, key_confidence = estimate_key(frames, rate)
    loudness_db = 20 * math.log10(max(rms, 1.0) / 32768.0)
    grid = beat_grid(bpm, beat_offset_ms)
    structure = detect_structure_windows(envelope, bpm, beat_offset_ms, duration_s)
    return TrackAnalysis(
        path=str(path),
        duration_s=round(duration_s, 3),
        sample_rate=rate,
        channels=channels,
        bpm=bpm,
        beat_offset_ms=beat_offset_ms,
        key=key_name(tonic, mode),
        tonic=tonic,
        mode=mode,
        camelot=camelot(tonic, mode),
        energy=round(min(1.0, rms / 32768.0), 4),
        loudness_db=round(loudness_db, 2),
        confidence={"bpm": bpm_confidence, "key": key_confidence},
        beatgrid=grid,
        structure=structure,
    )


def library_tunebat_row(conn: sqlite3.Connection, path: Path) -> sqlite3.Row | None:
    path_text = str(path)
    return conn.execute(
        """
        SELECT
            files.duplicate_key,
            tracks.preferred_path,
            tracks.tunebat_key,
            tracks.tunebat_mode,
            tracks.tunebat_camelot,
            tracks.tunebat_bpm,
            tracks.tunebat_energy
        FROM files
        JOIN tracks ON tracks.duplicate_key = files.duplicate_key
        WHERE files.path = ? OR tracks.preferred_path = ?
        LIMIT 1
        """,
        (path_text, path_text),
    ).fetchone()


def ensure_library_tunebat(path: Path, db_path: Path, analyzer: Path) -> sqlite3.Row | None:
    if not db_path.exists():
        return None
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = library_tunebat_row(conn, path)
    if row is None:
        conn.close()
        return None
    if row["tunebat_bpm"] is not None and row["tunebat_camelot"]:
        conn.close()
        return row
    target_path = Path(row["preferred_path"] or path)
    if not target_path.exists():
        target_path = path
    payload = tunebat_local_payload(analyzer, target_path)
    store_tunebat_local_payload(conn, row["duplicate_key"], target_path, payload)
    conn.commit()
    refreshed = library_tunebat_row(conn, path)
    conn.close()
    return refreshed


def with_library_tunebat(analysis: TrackAnalysis, row: sqlite3.Row | None) -> TrackAnalysis:
    if row is None or row["tunebat_bpm"] is None:
        return analysis
    camelot_value = str(row["tunebat_camelot"] or "")
    tonic, mode = tonic_mode_from_camelot(camelot_value)
    if row["tunebat_mode"]:
        mode = str(row["tunebat_mode"])
    key_value = str(row["tunebat_key"] or "") or key_name(tonic, mode)
    confidence = dict(analysis.confidence)
    confidence["bpm"] = 1.0
    if camelot_value or key_value:
        confidence["key"] = 1.0
    bpm = float(row["tunebat_bpm"])
    grid = beat_grid(bpm, analysis.beat_offset_ms)
    return replace(
        analysis,
        bpm=bpm,
        key=key_value,
        tonic=tonic,
        mode=mode,
        camelot=camelot_value or camelot(tonic, mode),
        energy=float(row["tunebat_energy"]) if row["tunebat_energy"] is not None else analysis.energy,
        confidence=confidence,
        beatgrid=grid,
    )


def semitone_distance(source: int, target: int) -> int:
    diff = (target - source) % 12
    return diff - 12 if diff > 6 else diff


def relative_tonic(tonic: int, mode: str) -> int:
    return (tonic + 3) % 12 if mode == "minor" else (tonic - 3) % 12


def key_match(source: TrackAnalysis, target: TrackAnalysis, max_pitch_shift: int) -> tuple[float, int, str, list[str]]:
    notes: list[str] = []
    if source.tonic is None or target.tonic is None or source.mode is None or target.mode is None:
        return 0.25, 0, "unknown", ["missing key metadata"]
    if source.tonic == target.tonic and source.mode == target.mode:
        return 1.0, 0, "same key", notes
    if source.mode != target.mode and relative_tonic(source.tonic, source.mode) == target.tonic:
        notes.append("mode-rotation match: same pitch set, reinterpret minor/major root")
        return 0.96, 0, "relative major/minor rotation", notes

    same_mode_shift = semitone_distance(target.tonic, source.tonic)
    if source.mode == target.mode and abs(same_mode_shift) <= max_pitch_shift:
        return 0.86 - (abs(same_mode_shift) * 0.04), same_mode_shift, "pitch-shift same mode", notes

    rotated_target = relative_tonic(target.tonic, target.mode)
    rotation_shift = semitone_distance(rotated_target, source.tonic)
    if source.mode != target.mode and abs(rotation_shift) <= max_pitch_shift:
        notes.append("pitch then rotate mode against the relative major/minor")
        return 0.80 - (abs(rotation_shift) * 0.04), rotation_shift, "pitch-shift mode rotation", notes

    camelot_score = camelot_compatibility(source.camelot, target.camelot)
    return camelot_score, 0, "camelot neighbor" if camelot_score >= 0.65 else "clash", notes


def camelot_compatibility(source: str | None, target: str | None) -> float:
    if not source or not target:
        return 0.25
    source_number, source_mode = int(source[:-1]), source[-1]
    target_number, target_mode = int(target[:-1]), target[-1]
    if source == target:
        return 1.0
    if source_number == target_number and source_mode != target_mode:
        return 0.92
    if source_mode == target_mode and ((source_number - target_number) % 12 in {1, 11}):
        return 0.82
    return 0.35


def transition_plan(source: TrackAnalysis, target: TrackAnalysis, max_pitch_shift: int = 2) -> TransitionPlan:
    notes: list[str] = []
    bpm_ratio = None
    tempo_shift = None
    tempo_score = 0.35
    if source.bpm and target.bpm:
        candidates = [target.bpm, target.bpm * 2, target.bpm / 2]
        closest = min(candidates, key=lambda value: abs(value - source.bpm))
        bpm_ratio = closest / source.bpm
        tempo_shift = (bpm_ratio - 1.0) * 100
        tempo_score = max(0.0, 1.0 - abs(tempo_shift) / 12.0)
        if abs(tempo_shift) > 6:
            notes.append("tempo stretch is audible; prefer a longer transition or a bridge track")
    key_score, pitch_shift, relation, key_notes = key_match(source, target, max_pitch_shift)
    notes.extend(key_notes)
    energy_delta = abs(source.energy - target.energy)
    energy_score = max(0.0, 1.0 - energy_delta * 3.0)
    score = (tempo_score * 0.40) + (key_score * 0.45) + (energy_score * 0.15)
    phrase_wait = 32
    return TransitionPlan(
        source=source.path,
        target=target.path,
        score=round(score, 3),
        bpm_ratio=round(bpm_ratio, 5) if bpm_ratio is not None else None,
        target_tempo_shift_pct=round(tempo_shift, 2) if tempo_shift is not None else None,
        pitch_shift_semitones=pitch_shift,
        key_relation=relation,
        phrase_wait_beats=phrase_wait,
        notes=notes,
    )


def clip_end_ms(clip: Clip, analysis: TrackAnalysis | None = None) -> int:
    if clip.duration_ms is not None:
        return clip.start_ms + clip.duration_ms
    if analysis is not None:
        remaining_ms = max(0, int(round(analysis.duration_s * 1000)) - clip.trim_start_ms)
        return clip.start_ms + remaining_ms
    return clip.start_ms


def track_label(path_text: str | None) -> str:
    if not path_text:
        return "unknown track"
    stem = Path(path_text).stem
    return stem or Path(path_text).name or path_text


def talking_points_for_suggestion(kind: str, track: str, reason: str, analysis: TrackAnalysis) -> list[str]:
    points = []
    if kind == "pre-drop":
        points.append(f"detected release point in {track}; speak briefly before it and clear the drop")
    elif kind == "build":
        points.append(f"{track} is gaining energy; use it to set up the next musical move")
    elif kind == "breakdown":
        points.append(f"{track} has a lower-energy pocket with room for a short vocal")
    elif kind == "intro":
        points.append(f"{track} is in an opening phrase, useful for framing without stepping on a hook")
    points.append(reason)
    if analysis.bpm:
        points.append(f"analysis estimate: {analysis.bpm:g} bpm")
    if analysis.camelot:
        points.append(f"analysis estimate: {analysis.camelot} camelot")
    return points[:4]


def session_tension_windows(
    session: MixSession,
    analyses_by_path: dict[str, TrackAnalysis | dict],
    *,
    max_pitch_shift: int = 2,
    transition_lead_ms: int = 8_000,
) -> list[TensionWindow]:
    analyses = {str(Path(path)): coerce_analysis(analysis) for path, analysis in analyses_by_path.items()}
    analyses.update({path: coerce_analysis(analysis) for path, analysis in analyses_by_path.items()})
    clips = sorted(session.clips, key=lambda clip: (clip.start_ms, clip.deck, clip.id))
    windows: list[TensionWindow] = []

    for clip in clips:
        analysis = analyses.get(clip.path) or analyses.get(str(Path(clip.path)))
        if analysis is None:
            continue
        clip_end = clip_end_ms(clip, analysis)
        track = track_label(clip.path)
        for suggestion in suggested_lean_in_windows(coerce_structure(analysis.structure)):
            source_ms = int(suggestion["at_ms"])
            absolute_ms = clip.start_ms + max(0, source_ms - clip.trim_start_ms)
            if absolute_ms < clip.start_ms or absolute_ms >= clip_end:
                continue
            kind = str(suggestion["kind"])
            confidence = float(suggestion["confidence"])
            source_end_ms = source_ms + 6_000
            end_ms = min(clip_end, absolute_ms + 6_000)
            reason = f"{track}: {suggestion['reason']}"
            windows.append(
                TensionWindow(
                    start_ms=absolute_ms,
                    end_ms=end_ms,
                    kind=kind,
                    confidence=round(confidence, 3),
                    reason=reason,
                    talking_points=talking_points_for_suggestion(kind, track, str(suggestion["reason"]), analysis),
                    clip_id=clip.id,
                    track=clip.path,
                    source_start_ms=source_ms,
                    source_end_ms=source_end_ms,
                )
            )

    for left, right in zip(clips, clips[1:]):
        left_analysis = analyses.get(left.path) or analyses.get(str(Path(left.path)))
        right_analysis = analyses.get(right.path) or analyses.get(str(Path(right.path)))
        if left_analysis is None or right_analysis is None:
            continue
        plan = transition_plan(left_analysis, right_analysis, max_pitch_shift=max_pitch_shift)
        start_ms = max(left.start_ms, right.start_ms - transition_lead_ms)
        end_ms = min(right.start_ms, start_ms + transition_lead_ms)
        if end_ms <= start_ms:
            end_ms = start_ms + 4_000
        notes = plan.notes[:] or []
        if plan.target_tempo_shift_pct is not None:
            notes.append(f"tempo shift {plan.target_tempo_shift_pct:+.2f}%")
        notes.append(f"key relation: {plan.key_relation}")
        if plan.pitch_shift_semitones:
            notes.append(f"pitch target {plan.pitch_shift_semitones:+d} semitones")
        reason = f"transition into {track_label(right.path)}: {', '.join(notes[:3])}"
        windows.append(
            TensionWindow(
                start_ms=start_ms,
                end_ms=end_ms,
                kind="transition",
                confidence=plan.score,
                reason=reason,
                talking_points=[
                    f"{track_label(left.path)} into {track_label(right.path)}",
                    *notes[:3],
                ],
                clip_id=left.id,
                track=left.path,
                next_clip_id=right.id,
                next_track=right.path,
            )
        )

    return sorted(windows, key=lambda item: (item.start_ms, -item.confidence, item.kind))


def analyze_with_cache(
    paths: list[Path],
    cache_path: Path,
    backend: str,
    sample_rate: int,
    db_path: Path = DEFAULT_LIBRARY_DB,
    tunebat_analyzer: Path = DEFAULT_TUNEBAT_LOCAL_ANALYZER,
) -> list[TrackAnalysis]:
    cache = load_cache(cache_path)
    analyses: list[TrackAnalysis] = []
    changed = False
    for path in paths:
        key = cache_key(path)
        if key in cache:
            analysis = coerce_analysis(cache[key])
            row = ensure_library_tunebat(path, db_path, tunebat_analyzer)
            analysis = with_library_tunebat(analysis, row)
            cache[key] = asdict(analysis)
            changed = True
            analyses.append(analysis)
            continue
        analysis = analyze_track(path, backend=backend, sample_rate=sample_rate)
        row = ensure_library_tunebat(path, db_path, tunebat_analyzer)
        analysis = with_library_tunebat(analysis, row)
        cache[key] = asdict(analysis)
        analyses.append(analysis)
        changed = True
    if changed:
        write_cache(cache_path, cache)
    return analyses


def parse_track_list(values: list[str], playlist: Path | None) -> list[Path]:
    paths = [Path(value) for value in values]
    if playlist is not None:
        paths.extend(Path(line.strip()) for line in playlist.read_text(encoding="utf-8").splitlines() if line.strip())
    if not paths:
        raise SystemExit("provide tracks as args or --playlist")
    return paths


def session_tracks(session: MixSession, *, from_ms: int | None = None, horizon_ms: int | None = None) -> list[Path]:
    end_ms = from_ms + horizon_ms if from_ms is not None and horizon_ms is not None else None
    paths: list[Path] = []
    seen: set[str] = set()
    for clip in sorted(session.clips, key=lambda item: (item.start_ms, item.deck, item.id)):
        if from_ms is not None and clip.duration_ms is not None and clip.start_ms + clip.duration_ms < from_ms:
            continue
        if end_ms is not None and clip.start_ms > end_ms:
            continue
        if clip.path in seen:
            continue
        seen.add(clip.path)
        paths.append(Path(clip.path))
    if not paths:
        raise SystemExit("session has no clips to analyze")
    return paths


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze tracks and plan Rekordbox-ish SlimeAudio transitions.")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_analysis_source_args(command_parser: argparse.ArgumentParser) -> None:
        command_parser.add_argument("--db", type=Path, default=DEFAULT_LIBRARY_DB)
        command_parser.add_argument("--tunebat-analyzer", type=Path, default=DEFAULT_TUNEBAT_LOCAL_ANALYZER)

    analyze_parser = sub.add_parser("analyze")
    analyze_parser.add_argument("tracks", nargs="*")
    analyze_parser.add_argument("--playlist", type=Path)
    analyze_parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    analyze_parser.add_argument("--backend", choices=["auto", "ffmpeg"], default="auto")
    analyze_parser.add_argument("--sample-rate", type=int, default=44_100)
    add_analysis_source_args(analyze_parser)

    plan_parser = sub.add_parser("plan")
    plan_parser.add_argument("tracks", nargs="*")
    plan_parser.add_argument("--playlist", type=Path)
    plan_parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    plan_parser.add_argument("--backend", choices=["auto", "ffmpeg"], default="auto")
    plan_parser.add_argument("--sample-rate", type=int, default=44_100)
    plan_parser.add_argument("--max-pitch-shift", type=int, default=2)
    add_analysis_source_args(plan_parser)

    rank_parser = sub.add_parser("rank")
    rank_parser.add_argument("source")
    rank_parser.add_argument("tracks", nargs="*")
    rank_parser.add_argument("--playlist", type=Path)
    rank_parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    rank_parser.add_argument("--backend", choices=["auto", "ffmpeg"], default="auto")
    rank_parser.add_argument("--sample-rate", type=int, default=44_100)
    rank_parser.add_argument("--max-pitch-shift", type=int, default=2)
    rank_parser.add_argument("--limit", type=int, default=10)
    add_analysis_source_args(rank_parser)

    structure_parser = sub.add_parser("structure")
    structure_parser.add_argument("tracks", nargs="*")
    structure_parser.add_argument("--playlist", type=Path)
    structure_parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    structure_parser.add_argument("--backend", choices=["auto", "ffmpeg"], default="auto")
    structure_parser.add_argument("--sample-rate", type=int, default=44_100)
    add_analysis_source_args(structure_parser)

    tension_parser = sub.add_parser("tension")
    tension_parser.add_argument("tracks", nargs="*")
    tension_parser.add_argument("--playlist", type=Path)
    tension_parser.add_argument("--session", type=Path)
    tension_parser.add_argument("--state", type=Path)
    tension_parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    tension_parser.add_argument("--backend", choices=["auto", "ffmpeg"], default="auto")
    tension_parser.add_argument("--sample-rate", type=int, default=44_100)
    tension_parser.add_argument("--max-pitch-shift", type=int, default=2)
    tension_parser.add_argument("--transition-lead-ms", type=int, default=8_000)
    tension_parser.add_argument("--from-ms", type=int)
    tension_parser.add_argument("--horizon-ms", type=int)
    tension_parser.add_argument("--min-confidence", type=float, default=0.0)
    tension_parser.add_argument("--limit", type=int, default=24)
    add_analysis_source_args(tension_parser)

    args = parser.parse_args()
    session = None
    from_ms = args.from_ms if getattr(args, "command", None) == "tension" and args.from_ms is not None else None
    if getattr(args, "command", None) == "tension" and args.state is not None and from_ms is None and args.state.exists():
        from_ms = playhead_ms_from_state(args.state)
    if getattr(args, "command", None) == "tension" and args.session is not None:
        session = load_session(args.session)
        tracks = session_tracks(session, from_ms=from_ms, horizon_ms=args.horizon_ms)
    else:
        tracks = parse_track_list(args.tracks, args.playlist)
    if args.command == "rank":
        tracks = [Path(args.source)] + tracks
    analyses = analyze_with_cache(tracks, args.cache, args.backend, args.sample_rate, args.db, args.tunebat_analyzer)
    if args.command == "analyze":
        print(json.dumps([asdict(analysis) for analysis in analyses], indent=2, sort_keys=True))
        return 0
    if args.command == "rank":
        source = analyses[0]
        plans = [
            transition_plan(source, candidate, max_pitch_shift=args.max_pitch_shift)
            for candidate in analyses[1:]
        ]
        plans.sort(key=lambda plan: plan.score, reverse=True)
        print(json.dumps([asdict(plan) for plan in plans[: args.limit]], indent=2, sort_keys=True))
        return 0
    if args.command == "structure":
        print(
            json.dumps(
                [
                    {
                        "path": analysis.path,
                        "duration_s": analysis.duration_s,
                        "bpm": analysis.bpm,
                        "beat_offset_ms": analysis.beat_offset_ms,
                        "beatgrid": beatgrid_asdict(analysis.beatgrid),
                        "structure": [asdict(window) for window in coerce_structure(analysis.structure)],
                        "lean_in_suggestions": suggested_lean_in_windows(coerce_structure(analysis.structure)),
                    }
                    for analysis in analyses
                ],
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if args.command == "tension":
        if session is None:
            clips_payload = []
            cursor_ms = 0
            for index, analysis in enumerate(analyses):
                duration_ms = int(round(analysis.duration_s * 1000))
                clips_payload.append(
                    {
                        "id": f"track-{index + 1}",
                        "deck": f"deck-{(index % 4) + 1}",
                        "path": analysis.path,
                        "start": cursor_ms,
                        "duration": duration_ms,
                    }
                )
                cursor_ms += duration_ms
            session = parse_session({"version": 1, "decks": ["deck-1", "deck-2", "deck-3", "deck-4"], "clips": clips_payload, "mic_lean_ins": []})
        analyses_by_path = {analysis.path: analysis for analysis in analyses}
        windows = session_tension_windows(
            session,
            analyses_by_path,
            max_pitch_shift=args.max_pitch_shift,
            transition_lead_ms=args.transition_lead_ms,
        )
        if from_ms is not None:
            windows = [window for window in windows if window.end_ms >= from_ms]
        if args.horizon_ms is not None and from_ms is not None:
            windows = [window for window in windows if window.start_ms <= from_ms + args.horizon_ms]
        windows = [window for window in windows if window.confidence >= args.min_confidence]
        print(json.dumps([asdict(window) for window in windows[: args.limit]], indent=2, sort_keys=True))
        return 0

    plans = [
        transition_plan(left, right, max_pitch_shift=args.max_pitch_shift)
        for left, right in zip(analyses, analyses[1:])
    ]
    print(json.dumps([asdict(plan) for plan in plans], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
