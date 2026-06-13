#!/usr/bin/env python3
from __future__ import annotations

import argparse
import audioop
import copy
import json
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from slime_audio_dj import (
    DEFAULT_CACHE,
    DEFAULT_LIBRARY_DB,
    DEFAULT_TUNEBAT_LOCAL_ANALYZER,
    BeatGrid,
    analyze_with_cache,
    beat_grid,
    load_analysis_from_db,
)


def action_at_ms(action: dict[str, Any]) -> int:
    return int(action.get("at_ms", action.get("at", 0)) or 0)


def parse_ms(value: Any) -> int:
    return int(value or 0)


def snap_ms_to_grid(ms: int, grid: BeatGrid | None, *, quantum: str = "beat") -> int:
    if grid is None or grid.beat_offset_ms is None or not grid.bpm:
        return int(ms)
    if quantum == "phrase" and grid.phrase_ms:
        unit = float(grid.phrase_ms)
    else:
        unit = 60_000.0 / float(grid.bpm)
    steps = round((int(ms) - int(grid.beat_offset_ms)) / unit)
    return max(0, int(round(int(grid.beat_offset_ms) + steps * unit)))


def snap_duration_to_grid(duration_ms: int, grid: BeatGrid | None, *, beats: int | None = None) -> int:
    if grid is None or not grid.bpm:
        return int(duration_ms)
    beat_ms = 60_000.0 / float(grid.bpm)
    beat_count = beats if beats is not None else max(1, round(int(duration_ms) / beat_ms))
    return max(1, int(round(beat_count * beat_ms)))


def local_peak(values: list[float], index: int, radius: int) -> float:
    lo = max(0, index - radius)
    hi = min(len(values), index + radius + 1)
    return max(values[lo:hi] or [0.0])


def local_mean(values: list[float], index: int, width: int) -> float:
    lo = max(0, index)
    hi = min(len(values), index + width)
    window = values[lo:hi]
    return sum(window) / len(window) if window else 0.0


def refine_loop_anchor_from_envelope(
    envelope: list[float],
    onsets: list[float],
    *,
    current_ms: int,
    length_ms: int,
    frame_ms: int,
    search_ms: int = 1400,
) -> int:
    if not envelope or not onsets or length_ms <= 0 or frame_ms <= 0:
        return current_ms
    best_score: float | None = None
    best_ms = current_ms
    peak_radius = max(1, round(100 / frame_ms))
    mean_width = max(1, round(120 / frame_ms))
    for candidate_ms in range(max(0, current_ms - search_ms), current_ms + search_ms + 1, frame_ms):
        start_index = max(0, round(candidate_ms / frame_ms))
        end_index = max(0, round((candidate_ms + length_ms) / frame_ms))
        score = local_peak(onsets, start_index, peak_radius) + local_peak(onsets, end_index, peak_radius)
        score -= abs(local_mean(envelope, start_index, mean_width) - local_mean(envelope, end_index, mean_width)) * 1.2
        score -= abs(candidate_ms - current_ms) * 0.4
        if best_score is None or score > best_score:
            best_score = score
            best_ms = candidate_ms
    return best_ms


def drums_stem_path(load_action: dict[str, Any] | None) -> str | None:
    if not isinstance(load_action, dict):
        return None
    stem = (load_action.get("stems") or {}).get("drums")
    if isinstance(stem, dict):
        path = str(stem.get("path") or "").strip()
        return path or None
    if isinstance(stem, str):
        return stem
    return None


def drum_envelope(path: str, *, backend: str, sample_rate: int, duration_s: int = 140, frame_ms: int = 10) -> tuple[list[float], list[float], int] | None:
    source = Path(path)
    if not source.exists():
        return None
    with tempfile.TemporaryDirectory(prefix="slime-loop-anchor-") as temp_dir:
        wav_path = Path(temp_dir) / "drums.wav"
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source),
                "-t",
                str(duration_s),
                "-ac",
                "1",
                "-ar",
                str(sample_rate),
                str(wav_path),
            ],
            check=True,
        )
        with wave.open(str(wav_path), "rb") as audio:
            rate = audio.getframerate()
            width = audio.getsampwidth()
            frame_count = audio.getnframes()
            frames = audio.readframes(frame_count)
    samples_per_frame = max(1, int(rate * frame_ms / 1000))
    envelope: list[float] = []
    for index in range(0, frame_count, samples_per_frame):
        chunk = frames[index * width : (index + samples_per_frame) * width]
        envelope.append(float(audioop.rms(chunk, width)) if chunk else 0.0)
    onsets = [0.0] + [max(0.0, current - previous) for previous, current in zip(envelope, envelope[1:])]
    return envelope, onsets, frame_ms


@dataclass
class RewriteReport:
    id: str
    field: str
    old_ms: int
    new_ms: int
    reason: str


def session_grid(payload: dict[str, Any]) -> BeatGrid | None:
    bpm = payload.get("bpm")
    if bpm is None:
        return None
    beat_offset_ms = int(payload.get("beat_offset_ms", payload.get("beat_offset", 0)) or 0)
    phrase_beats = int(payload.get("phrase_beats", 32) or 32)
    return beat_grid(float(bpm), beat_offset_ms, phrase_beats)


def source_analysis(path: str, *, db: Path, cache: Path, analyze_missing: bool, backend: str, sample_rate: int) -> BeatGrid | None:
    source = Path(path)
    analysis = load_analysis_from_db(db, source)
    if analysis is None and analyze_missing and source.exists():
        analysis = analyze_with_cache([source], cache, backend, sample_rate, db, DEFAULT_TUNEBAT_LOCAL_ANALYZER)[0]
    return analysis.beatgrid if analysis and analysis.beatgrid else None


def rewrite_payload(
    payload: dict[str, Any],
    *,
    db: Path = DEFAULT_LIBRARY_DB,
    cache: Path = DEFAULT_CACHE,
    analyze_missing: bool = False,
    backend: str = "ffmpeg",
    sample_rate: int = 48_000,
    mix_quantum: str = "beat",
    source_quantum: str = "beat",
    refine_loop_transients: bool = False,
) -> tuple[dict[str, Any], list[RewriteReport]]:
    actions = payload.get("actions")
    if not isinstance(actions, list):
        raise ValueError("session has no actions[] to rewrite")
    result = copy.deepcopy(payload)
    result_actions = result["actions"]
    mix_grid = session_grid(result)
    loads: dict[str, dict[str, Any]] = {}
    source_grids: dict[str, BeatGrid | None] = {}
    drum_envelopes: dict[str, tuple[list[float], list[float], int] | None] = {}
    reports: list[RewriteReport] = []

    def set_ms(action: dict[str, Any], field: str, new_ms: int, reason: str) -> None:
        old_ms = parse_ms(action.get(field))
        if old_ms != new_ms:
            action[field] = new_ms
            reports.append(RewriteReport(str(action.get("id") or action.get("target") or "<unknown>"), field, old_ms, new_ms, reason))

    for action in result_actions:
        if not isinstance(action, dict):
            continue
        if action.get("type") == "load_track":
            load_id = str(action.get("id") or "")
            if load_id:
                loads[load_id] = action
                source_grids[load_id] = source_analysis(str(action.get("source_path") or ""), db=db, cache=cache, analyze_missing=analyze_missing, backend=backend, sample_rate=sample_rate)
                stem_path = drums_stem_path(action) if refine_loop_transients else None
                if stem_path:
                    drum_envelopes[load_id] = drum_envelope(stem_path, backend=backend, sample_rate=sample_rate)

    for action in result_actions:
        if not isinstance(action, dict):
            continue
        kind = action.get("type")
        if kind in {"jump_to_cue", "loop_start", "loop_exit"}:
            set_ms(action, "at_ms", snap_ms_to_grid(action_at_ms(action), mix_grid, quantum=mix_quantum), "snap action time to session beat grid")
        if kind == "loop_start" and action.get("exit_ms") is not None:
            set_ms(action, "exit_ms", snap_ms_to_grid(parse_ms(action.get("exit_ms")), mix_grid, quantum=mix_quantum), "snap loop exit time to session beat grid")
        target = str(action.get("target") or action.get("load_id") or action.get("group_id") or "")
        source_grid = source_grids.get(target)
        if kind == "set_cue" and action.get("position_ms") is not None:
            quantum = "phrase" if str(action.get("cue_id") or "").lower() in {"drop", "hook"} else source_quantum
            set_ms(action, "position_ms", snap_ms_to_grid(parse_ms(action.get("position_ms")), source_grid, quantum=quantum), "snap cue source position to source beat grid")
        elif kind in {"loop_start", "loop_exit"} and action.get("position_ms") is not None:
            set_ms(action, "position_ms", snap_ms_to_grid(parse_ms(action.get("position_ms")), source_grid, quantum=source_quantum), "snap source position to source beat grid")
        if kind == "loop_start" and action.get("length_ms") is not None:
            set_ms(action, "length_ms", snap_duration_to_grid(parse_ms(action.get("length_ms")), source_grid), "snap loop length to source beat length")
            if refine_loop_transients and action.get("position_ms") is not None:
                current = parse_ms(action.get("position_ms"))
                length = parse_ms(action.get("length_ms"))
                envelope_payload = drum_envelopes.get(target)
                if envelope_payload is not None:
                    envelope, onsets, frame_ms = envelope_payload
                    refined = refine_loop_anchor_from_envelope(envelope, onsets, current_ms=current, length_ms=length, frame_ms=frame_ms)
                    set_ms(action, "position_ms", refined, "refine loop source position to matching drum transients")
                    for cue_action in result_actions:
                        if (
                            isinstance(cue_action, dict)
                            and cue_action.get("type") == "set_cue"
                            and cue_action.get("target") == target
                            and str(cue_action.get("cue_id") or "").lower() == "loop"
                        ):
                            set_ms(cue_action, "position_ms", refined, "match refined loop source position")

    result_actions.sort(key=lambda item: (action_at_ms(item) if isinstance(item, dict) else 0, str(item.get("id") if isinstance(item, dict) else "")))
    result.setdefault("notes", {})["beat_jump_alignment"] = {
        "mix_quantum": mix_quantum,
        "source_quantum": source_quantum,
        "analyze_missing": analyze_missing,
        "refine_loop_transients": refine_loop_transients,
        "rewrites": len(reports),
    }
    return result, reports


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Snap SlimeAudio cue/loop/jump actions to beat grids.")
    parser.add_argument("session", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--db", type=Path, default=DEFAULT_LIBRARY_DB)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--analyze-missing", action="store_true")
    parser.add_argument("--backend", default="ffmpeg")
    parser.add_argument("--sample-rate", type=int, default=48_000)
    parser.add_argument("--mix-quantum", choices=["beat", "phrase"], default="beat")
    parser.add_argument("--source-quantum", choices=["beat", "phrase"], default="beat")
    parser.add_argument("--refine-loop-transients", action="store_true", help="Refine loop_start source positions against drum-stem transient boundaries after grid snapping.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    payload = json.loads(args.session.read_text())
    rewritten, reports = rewrite_payload(
        payload,
        db=args.db,
        cache=args.cache,
        analyze_missing=args.analyze_missing,
        backend=args.backend,
        sample_rate=args.sample_rate,
        mix_quantum=args.mix_quantum,
        source_quantum=args.source_quantum,
        refine_loop_transients=args.refine_loop_transients,
    )
    args.output.write_text(json.dumps(rewritten, indent=2) + "\n")
    print(json.dumps({"output": str(args.output), "rewrites": [report.__dict__ for report in reports]}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
