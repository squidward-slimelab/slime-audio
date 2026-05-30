#!/usr/bin/env python3
from __future__ import annotations

import argparse
import audioop
import json
import math
import tempfile
import wave
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from slime_audio_stream import convert_to_stream_wav

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


def analyze_with_cache(paths: list[Path], cache_path: Path, backend: str, sample_rate: int) -> list[TrackAnalysis]:
    cache = load_cache(cache_path)
    analyses: list[TrackAnalysis] = []
    changed = False
    for path in paths:
        key = cache_key(path)
        if key in cache:
            analyses.append(TrackAnalysis(**cache[key]))
            continue
        analysis = analyze_track(path, backend=backend, sample_rate=sample_rate)
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze tracks and plan Rekordbox-ish SlimeAudio transitions.")
    sub = parser.add_subparsers(dest="command", required=True)

    analyze_parser = sub.add_parser("analyze")
    analyze_parser.add_argument("tracks", nargs="*")
    analyze_parser.add_argument("--playlist", type=Path)
    analyze_parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    analyze_parser.add_argument("--backend", choices=["auto", "ffmpeg"], default="auto")
    analyze_parser.add_argument("--sample-rate", type=int, default=44_100)

    plan_parser = sub.add_parser("plan")
    plan_parser.add_argument("tracks", nargs="*")
    plan_parser.add_argument("--playlist", type=Path)
    plan_parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    plan_parser.add_argument("--backend", choices=["auto", "ffmpeg"], default="auto")
    plan_parser.add_argument("--sample-rate", type=int, default=44_100)
    plan_parser.add_argument("--max-pitch-shift", type=int, default=2)

    rank_parser = sub.add_parser("rank")
    rank_parser.add_argument("source")
    rank_parser.add_argument("tracks", nargs="*")
    rank_parser.add_argument("--playlist", type=Path)
    rank_parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    rank_parser.add_argument("--backend", choices=["auto", "ffmpeg"], default="auto")
    rank_parser.add_argument("--sample-rate", type=int, default=44_100)
    rank_parser.add_argument("--max-pitch-shift", type=int, default=2)
    rank_parser.add_argument("--limit", type=int, default=10)

    structure_parser = sub.add_parser("structure")
    structure_parser.add_argument("tracks", nargs="*")
    structure_parser.add_argument("--playlist", type=Path)
    structure_parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    structure_parser.add_argument("--backend", choices=["auto", "ffmpeg"], default="auto")
    structure_parser.add_argument("--sample-rate", type=int, default=44_100)

    args = parser.parse_args()
    tracks = parse_track_list(args.tracks, args.playlist)
    if args.command == "rank":
        tracks = [Path(args.source)] + tracks
    analyses = analyze_with_cache(tracks, args.cache, args.backend, args.sample_rate)
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

    plans = [
        transition_plan(left, right, max_pitch_shift=args.max_pitch_shift)
        for left, right in zip(analyses, analyses[1:])
    ]
    print(json.dumps([asdict(plan) for plan in plans], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
