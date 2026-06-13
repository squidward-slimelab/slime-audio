#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
import math
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

from slime_audio_dj import DEFAULT_CACHE, DEFAULT_LIBRARY_DB, decoded_wav, load_analysis_from_db, read_wav_mono


FRAME_MS = 46
DEFAULT_MAX_BEAT_ERROR_MS = 80
DEFAULT_MAX_PHRASE_ERROR_MS = 220
DEFAULT_MAX_VOCAL_OVERLAP_MS = 500


@dataclass(frozen=True)
class VocalCue:
    kind: str
    at_ms: int
    end_ms: int
    confidence: float
    reason: str


@dataclass(frozen=True)
class ActionBeatgrid:
    bpm: float
    beat_offset_ms: int
    phrase_beats: int = 32
    phrase_ms: int | None = None
    confidence: float = 0.0
    source: str = "action"

    @property
    def beat_ms(self) -> float:
        return 60_000.0 / self.bpm

    @property
    def resolved_phrase_ms(self) -> float:
        return float(self.phrase_ms if self.phrase_ms is not None else round(self.beat_ms * self.phrase_beats))


def dbfs(value: float) -> float:
    return 20.0 * math.log10(max(value / 32768.0, 1e-9))


def vocal_envelope_db(path: Path, *, backend: str = "ffmpeg", sample_rate: int = 48_000, frame_ms: int = FRAME_MS) -> list[tuple[int, int, float]]:
    with decoded_wav(path, backend, sample_rate) as wav_path:
        rate, _channels, frames = read_wav_mono(wav_path)
    sample_width = 2
    frame_samples = max(1, int(rate * frame_ms / 1000))
    frame_bytes = frame_samples * sample_width
    windows: list[tuple[int, int, float]] = []
    for offset in range(0, len(frames), frame_bytes):
        chunk = frames[offset : offset + frame_bytes]
        if not chunk:
            continue
        total = 0
        samples = len(chunk) // sample_width
        for index in range(0, len(chunk) - 1, sample_width):
            sample = int.from_bytes(chunk[index : index + sample_width], "little", signed=True)
            total += sample * sample
        rms = math.sqrt(total / max(1, samples))
        start_ms = int(round((offset / sample_width) / rate * 1000))
        end_ms = int(round(((offset + len(chunk)) / sample_width) / rate * 1000))
        windows.append((start_ms, end_ms, dbfs(rms)))
    return windows


def stem_envelope_db(path: Path, *, backend: str = "ffmpeg", sample_rate: int = 48_000, frame_ms: int = FRAME_MS) -> list[tuple[int, int, float]]:
    return vocal_envelope_db(path, backend=backend, sample_rate=sample_rate, frame_ms=frame_ms)


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return -120.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * fraction)))
    return ordered[index]


def active_regions(
    windows: list[tuple[int, int, float]],
    *,
    min_region_ms: int = 300,
    merge_gap_ms: int = 250,
) -> list[tuple[int, int, float]]:
    if not windows:
        return []
    values = [db for _start, _end, db in windows]
    threshold = max(percentile(values, 0.20) + 10.0, percentile(values, 0.95) - 24.0, -52.0)
    regions: list[tuple[int, int, list[float]]] = []
    current_start: int | None = None
    current_end: int | None = None
    current_values: list[float] = []
    last_active_end: int | None = None
    for start, end, db in windows:
        active = db >= threshold
        if active:
            if current_start is None:
                current_start = start
            elif last_active_end is not None and start - last_active_end > merge_gap_ms:
                regions.append((current_start, current_end or last_active_end, current_values))
                current_start = start
                current_values = []
            current_end = end
            last_active_end = end
            current_values.append(db)
    if current_start is not None and current_end is not None:
        regions.append((current_start, current_end, current_values))
    result: list[tuple[int, int, float]] = []
    peak = max(values)
    for start, end, region_values in regions:
        if end - start < min_region_ms:
            continue
        mean = sum(region_values) / max(1, len(region_values))
        confidence = max(0.0, min(1.0, 0.45 + ((mean - threshold) / max(1.0, peak - threshold + 6.0))))
        result.append((start, end, confidence))
    return result


def detect_vocal_cues(
    path: Path,
    *,
    backend: str = "ffmpeg",
    sample_rate: int = 48_000,
    min_hook_ms: int = 2_000,
) -> list[VocalCue]:
    windows = vocal_envelope_db(path, backend=backend, sample_rate=sample_rate)
    regions = active_regions(windows)
    cues: list[VocalCue] = []
    for index, (start, end, confidence) in enumerate(regions):
        cues.append(VocalCue("line_onset", start, min(end, start + 1_500), confidence, "vocal stem rises above adaptive threshold"))
        if index == 0:
            cues.append(VocalCue("vocal_entry", start, end, confidence, "first detected vocal phrase"))
        if end - start >= min_hook_ms:
            cues.append(VocalCue("hook_candidate", start, end, confidence, "sustained vocal phrase usable as a hook alignment point"))
    cues.sort(key=lambda cue: (cue.at_ms, {"vocal_entry": 0, "hook_candidate": 1, "line_onset": 2}.get(cue.kind, 9)))
    return cues


def detect_drum_hits(
    path: Path,
    *,
    backend: str = "ffmpeg",
    sample_rate: int = 48_000,
    min_hit_ms: int = 40,
) -> list[VocalCue]:
    windows = stem_envelope_db(path, backend=backend, sample_rate=sample_rate)
    if not windows:
        return []
    values = [db for _start, _end, db in windows]
    threshold = max(percentile(values, 0.75), percentile(values, 0.95) - 18.0, -48.0)
    hits: list[VocalCue] = []
    current_start: int | None = None
    current_end: int | None = None
    current_peak = -120.0
    for start, end, db in windows:
        active = db >= threshold
        if active:
            if current_start is None:
                current_start = start
                current_peak = db
            current_end = end
            current_peak = max(current_peak, db)
            continue
        if current_start is not None and current_end is not None:
            if current_end - current_start >= min_hit_ms:
                confidence = max(0.0, min(1.0, 0.5 + ((current_peak - threshold) / 24.0)))
                hits.append(VocalCue("drum_hit", current_start, current_end, confidence, "drum stem transient above adaptive threshold"))
            current_start = None
            current_end = None
            current_peak = -120.0
    if current_start is not None and current_end is not None and current_end - current_start >= min_hit_ms:
        confidence = max(0.0, min(1.0, 0.5 + ((current_peak - threshold) / 24.0)))
        hits.append(VocalCue("drum_hit", current_start, current_end, confidence, "drum stem transient above adaptive threshold"))
    return hits


def phrase_anchor_for_vocal_cue(
    cue: VocalCue,
    drum_hits: list[VocalCue],
    *,
    max_pickup_ms: int = 4_000,
) -> VocalCue:
    candidates = [
        hit
        for hit in drum_hits
        if cue.at_ms <= hit.at_ms <= cue.at_ms + max_pickup_ms
    ]
    if candidates:
        return min(candidates, key=lambda hit: (hit.at_ms - cue.at_ms, -hit.confidence))
    nearby = [
        hit
        for hit in drum_hits
        if abs(hit.at_ms - cue.at_ms) <= max_pickup_ms
    ]
    if nearby:
        return min(nearby, key=lambda hit: (abs(hit.at_ms - cue.at_ms), -hit.confidence))
    return VocalCue("vocal_cue_fallback", cue.at_ms, cue.end_ms, cue.confidence, "no nearby drum phrase anchor detected; falling back to vocal cue")


def alignment_plan(
    cues: list[VocalCue],
    *,
    target_drop_ms: int,
    cue_kind: str = "hook_candidate",
    pre_roll_ms: int = 1_200,
    drum_hits: list[VocalCue] | None = None,
    max_pickup_ms: int = 4_000,
    tempo_shift_pct: float = 0.0,
    preferred_cue_ms: int | None = None,
) -> dict[str, int | float | str]:
    matching = [cue for cue in cues if cue.kind == cue_kind]
    if not matching:
        matching = [cue for cue in cues if cue.kind == "vocal_entry"] or cues
    if not matching:
        raise ValueError("no vocal cues detected")
    if preferred_cue_ms is None:
        cue = matching[0]
    else:
        cue = min(matching, key=lambda item: (abs(item.at_ms - preferred_cue_ms), item.at_ms))
    anchor = phrase_anchor_for_vocal_cue(cue, drum_hits or [], max_pickup_ms=max_pickup_ms)
    tempo_factor = 1.0 + (tempo_shift_pct / 100.0)
    if tempo_factor <= 0:
        raise ValueError("tempo_shift_pct produces non-positive tempo factor")
    trim_start_ms = max(0, cue.at_ms - pre_roll_ms)
    rendered_anchor_offset_ms = int(round((anchor.at_ms - trim_start_ms) / tempo_factor))
    at_ms = target_drop_ms - rendered_anchor_offset_ms
    return {
        "cue_kind": cue.kind,
        "cue_at_ms": cue.at_ms,
        "cue_end_ms": cue.end_ms,
        "phrase_anchor_kind": anchor.kind,
        "phrase_anchor_ms": anchor.at_ms,
        "phrase_anchor_confidence": anchor.confidence,
        "confidence": cue.confidence,
        "target_drop_ms": target_drop_ms,
        "recommended_at_ms": max(0, at_ms),
        "recommended_trim_start_ms": trim_start_ms,
        "pre_roll_ms": cue.at_ms - trim_start_ms,
        "preferred_cue_ms": preferred_cue_ms if preferred_cue_ms is not None else "",
        "vocal_lead_in_ms": max(0, anchor.at_ms - cue.at_ms),
        "tempo_shift_pct": tempo_shift_pct,
        "tempo_factor": tempo_factor,
        "rendered_anchor_offset_ms": rendered_anchor_offset_ms,
        "reason": "start the vocal load so the original phrase-start drum anchor lands on the backing phrase start, allowing pickup vocals to enter early",
    }


def action_at_ms(action: dict) -> int:
    return int(action.get("at_ms", action.get("at", 0)) or 0)


def parse_action_ms(action: dict, field: str, default: int = 0) -> int:
    return int(action.get(field, default) or default)


def action_tempo_factor(action: dict) -> float:
    factor = 1.0 + (float(action.get("tempo_shift_pct", 0.0) or 0.0) / 100.0)
    if factor <= 0:
        raise ValueError(f"{action.get('id') or '<unknown>'} has non-positive tempo factor")
    return factor


def timeline_ms_for_source_ms(action: dict, source_ms: int) -> int:
    rendered_offset_ms = (source_ms - parse_action_ms(action, "trim_start_ms")) / action_tempo_factor(action)
    return int(round(action_at_ms(action) + rendered_offset_ms))


def source_ms_at_timeline_ms(action: dict, timeline_ms: int) -> float:
    return parse_action_ms(action, "trim_start_ms") + ((timeline_ms - action_at_ms(action)) * action_tempo_factor(action))


def align_vocal_action(
    action: dict,
    *,
    target_drop_ms: int,
    cue_kind: str = "hook_candidate",
    pre_roll_ms: int = 1_200,
    max_pickup_ms: int = 4_000,
    backend: str = "ffmpeg",
    sample_rate: int = 48_000,
) -> tuple[dict, dict[str, int | float | str]]:
    stems = action.get("stems") or {}
    vocal_stem = stems.get("vocals")
    if isinstance(vocal_stem, dict):
        vocal_path = vocal_stem.get("path")
    else:
        vocal_path = vocal_stem
    if not vocal_path:
        raise ValueError(f"vocal load {action.get('id') or '<unknown>'} has no vocal stem path")
    drum_stem = stems.get("drums")
    if isinstance(drum_stem, dict):
        drum_path = drum_stem.get("path")
    else:
        drum_path = drum_stem
    cues = detect_vocal_cues(Path(vocal_path), backend=backend, sample_rate=sample_rate)
    drum_hits = detect_drum_hits(Path(drum_path), backend=backend, sample_rate=sample_rate) if drum_path else []
    plan = alignment_plan(
        cues,
        target_drop_ms=target_drop_ms,
        cue_kind=cue_kind,
        pre_roll_ms=pre_roll_ms,
        drum_hits=drum_hits,
        max_pickup_ms=max_pickup_ms,
        tempo_shift_pct=float(action.get("tempo_shift_pct", 0.0) or 0.0),
        preferred_cue_ms=int(action.get("trim_start_ms", action.get("trim_start", 0)) or 0) + pre_roll_ms,
    )
    aligned = copy.deepcopy(action)
    aligned["at_ms"] = int(plan["recommended_at_ms"])
    aligned["trim_start_ms"] = int(plan["recommended_trim_start_ms"])
    aligned["vocal_alignment"] = plan
    return aligned, plan


def action_belongs_to_vocal(action: dict, vocal_id: str) -> bool:
    action_id = str(action.get("id") or "")
    target = str(action.get("target") or "")
    return action_id.startswith(f"{vocal_id}-") or target == vocal_id


def action_matches_target_role(action: dict, target_role: str) -> bool:
    role = str(action.get("planner_role") or "")
    if role == target_role:
        return True
    return target_role == "vocal-hook" and "vocal-hook" in role


def is_vocal_action(action: dict, target_role: str = "vocal-hook") -> bool:
    if action.get("type") != "load_track":
        return False
    if action_matches_target_role(action, target_role):
        return True
    play_stems = action.get("play_stems", action.get("enabled_stems"))
    return isinstance(play_stems, list) and play_stems == ["vocals"]


def is_backing_action(action: dict, target_role: str = "vocal-hook") -> bool:
    if action.get("type") != "load_track" or is_vocal_action(action, target_role=target_role):
        return False
    play_stems = action.get("play_stems", action.get("enabled_stems"))
    if isinstance(play_stems, list) and play_stems == ["vocals"]:
        return False
    return True


def action_end_ms(action: dict) -> int | None:
    duration_ms = action.get("duration_ms", action.get("duration"))
    if duration_ms is None:
        return None
    return action_at_ms(action) + int(duration_ms)


def event_start_ms(event: dict) -> int:
    return int(event.get("start_ms", event.get("start", event.get("at_ms", event.get("at", 0)))) or 0)


def event_end_ms(event: dict) -> int | None:
    duration_ms = event.get("duration_ms", event.get("duration"))
    if duration_ms is None:
        return None
    return event_start_ms(event) + int(duration_ms)


def vocal_overlap_exempt(event: dict) -> bool:
    if bool(event.get("allow_vocal_overlap", event.get("vocal_overlap_allowed", False))):
        return True
    role = str(event.get("planner_role") or event.get("role") or event.get("kind") or "").lower()
    return any(token in role for token in ("vocal-trade", "call-response", "scratch", "stab"))


def path_looks_instrumental(path: str) -> bool:
    lowered = path.lower()
    return any(token in lowered for token in ("instrumental", "no vocals", "withoutvocals", "without vocals", "/drums.", "/bass.", "/other."))


def event_is_vocal_bearing(event: dict, *, target_role: str = "vocal-hook") -> bool:
    if bool(event.get("instrumental", False)) or str(event.get("vocal_policy", "")).lower() in {"none", "no_vocals", "instrumental"}:
        return False
    play_stems = event.get("play_stems", event.get("enabled_stems"))
    if isinstance(play_stems, list):
        return "vocals" in {str(stem) for stem in play_stems}
    stems = event.get("stems") if isinstance(event.get("stems"), dict) else {}
    if stems:
        vocal_stem = stems.get("vocals")
        if isinstance(vocal_stem, dict):
            return bool(vocal_stem.get("enabled", True)) and not bool(vocal_stem.get("mute", False))
        if isinstance(vocal_stem, bool):
            return vocal_stem
        if isinstance(vocal_stem, str):
            return bool(vocal_stem.strip())
    if event.get("type") == "load_track" and action_matches_target_role(event, target_role):
        return True
    path = str(event.get("path") or event.get("source_path") or "")
    if path_looks_instrumental(path):
        return False
    # Full-song clips/actions are vocal-bearing by default. Mark instrumental
    # sources explicitly instead of letting hidden vocal overlaps slip through.
    return True


def vocal_overlap_events(payload: dict, *, target_role: str = "vocal-hook") -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for collection, kind in (("clips", "clip"), ("actions", "action"), ("stem_groups", "stem-group")):
        items = payload.get(collection, [])
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            if collection == "actions" and item.get("type") != "load_track":
                continue
            if collection == "stem_groups" and payload.get("actions"):
                continue
            if not event_is_vocal_bearing(item, target_role=target_role):
                continue
            start_ms = event_start_ms(item)
            end_ms = event_end_ms(item)
            events.append(
                {
                    "id": str(item.get("id") or item.get("source_action_id") or ""),
                    "kind": kind,
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "path": str(item.get("path") or item.get("source_path") or ""),
                    "exempt": vocal_overlap_exempt(item),
                }
            )
    return sorted(events, key=lambda item: (int(item["start_ms"]), str(item["id"])))


def audit_vocal_overlap_payload(
    payload: dict,
    *,
    target_role: str = "vocal-hook",
    max_overlap_ms: int = DEFAULT_MAX_VOCAL_OVERLAP_MS,
) -> dict[str, object]:
    events = vocal_overlap_events(payload, target_role=target_role)
    overlaps: list[dict[str, object]] = []
    unknown_duration = [event for event in events if event["end_ms"] is None]
    for index, left in enumerate(events):
        left_end = left["end_ms"]
        if left_end is None:
            continue
        for right in events[index + 1 :]:
            right_start = int(right["start_ms"])
            if right_start >= int(left_end):
                break
            right_end = right["end_ms"]
            if right_end is None:
                continue
            overlap_ms = min(int(left_end), int(right_end)) - max(int(left["start_ms"]), right_start)
            if overlap_ms <= max_overlap_ms:
                continue
            status = "warn" if left["exempt"] or right["exempt"] else "fail"
            overlaps.append(
                {
                    "status": status,
                    "overlap_ms": overlap_ms,
                    "left_id": left["id"],
                    "right_id": right["id"],
                    "start_ms": max(int(left["start_ms"]), right_start),
                    "end_ms": min(int(left_end), int(right_end)),
                    "reason": "simultaneous vocal-bearing sources; use cuts, pauses, beat jumps, or call-response trades instead",
                }
            )
    failures = [item for item in overlaps if item["status"] == "fail"]
    return {
        "ok": not failures and not unknown_duration,
        "checked": len(events),
        "failed": len(failures),
        "unknown_duration": unknown_duration,
        "overlaps": overlaps,
        "max_overlap_ms": max_overlap_ms,
    }


def action_beatgrid_from_cache(action: dict, cache_path: Path | None) -> ActionBeatgrid | None:
    source_path = action.get("source_path") or action.get("path")
    if cache_path is None or not source_path:
        return None
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    normalized = str(Path(str(source_path)))
    for value in cache.values() if isinstance(cache, dict) else []:
        if not isinstance(value, dict):
            continue
        path = str(value.get("path") or "")
        if path != source_path and str(Path(path)) != normalized:
            continue
        raw = value.get("beatgrid") if isinstance(value.get("beatgrid"), dict) else value
        bpm = raw.get("bpm")
        beat_offset_ms = raw.get("beat_offset_ms")
        if bpm is None or beat_offset_ms is None:
            return None
        confidence_payload = value.get("confidence") if isinstance(value.get("confidence"), dict) else {}
        return ActionBeatgrid(
            bpm=float(bpm),
            beat_offset_ms=int(beat_offset_ms),
            phrase_beats=int(raw.get("phrase_beats") or 32),
            phrase_ms=int(raw["phrase_ms"]) if raw.get("phrase_ms") is not None else None,
            confidence=float(confidence_payload.get("bpm", value.get("bpm_confidence", 0.0)) or 0.0),
            source="cache",
        )
    return None


def action_beatgrid(action: dict, *, db_path: Path | None = None, cache_path: Path | None = DEFAULT_CACHE) -> ActionBeatgrid | None:
    raw = action.get("beatgrid") if isinstance(action.get("beatgrid"), dict) else action
    bpm = raw.get("bpm")
    beat_offset_ms = raw.get("beat_offset_ms")
    confidence_payload = raw.get("confidence") if isinstance(raw.get("confidence"), dict) else {}
    if bpm is not None and beat_offset_ms is not None:
        phrase_beats = int(raw.get("phrase_beats") or 32)
        phrase_ms = raw.get("phrase_ms")
        return ActionBeatgrid(
            bpm=float(bpm),
            beat_offset_ms=int(beat_offset_ms),
            phrase_beats=phrase_beats,
            phrase_ms=int(phrase_ms) if phrase_ms is not None else None,
            confidence=float(confidence_payload.get("bpm", raw.get("bpm_confidence", 0.0)) or 0.0),
            source="action",
        )
    source_path = action.get("source_path") or action.get("path")
    if db_path is None or not source_path:
        return action_beatgrid_from_cache(action, cache_path)
    try:
        analysis = load_analysis_from_db(db_path, Path(str(source_path)))
    except (FileNotFoundError, sqlite3.Error, OSError):
        return action_beatgrid_from_cache(action, cache_path)
    if analysis is None or analysis.bpm is None or analysis.beat_offset_ms is None:
        return action_beatgrid_from_cache(action, cache_path)
    grid = analysis.beatgrid
    return ActionBeatgrid(
        bpm=float(analysis.bpm),
        beat_offset_ms=int(analysis.beat_offset_ms),
        phrase_beats=int(grid.phrase_beats if grid else 32),
        phrase_ms=int(grid.phrase_ms) if grid and grid.phrase_ms is not None else None,
        confidence=float((analysis.confidence or {}).get("bpm", 0.0) or 0.0),
        source="db",
    )


def nearest_grid_delta_ms(source_ms: float, grid: ActionBeatgrid, quantum_ms: float) -> float:
    if quantum_ms <= 0:
        return 0.0
    steps = round((source_ms - grid.beat_offset_ms) / quantum_ms)
    nearest = grid.beat_offset_ms + (steps * quantum_ms)
    return source_ms - nearest


def active_backing_for_anchor(actions: list[dict], anchor_timeline_ms: int, *, target_role: str = "vocal-hook") -> dict | None:
    candidates = []
    for action in actions:
        if not isinstance(action, dict) or not is_backing_action(action, target_role=target_role):
            continue
        start_ms = action_at_ms(action)
        end_ms = action_end_ms(action)
        if start_ms <= anchor_timeline_ms and (end_ms is None or anchor_timeline_ms < end_ms):
            candidates.append(action)
    if candidates:
        return max(candidates, key=lambda item: action_at_ms(item))
    previous = [action for action in actions if isinstance(action, dict) and is_backing_action(action, target_role=target_role) and action_at_ms(action) <= anchor_timeline_ms]
    return max(previous, key=lambda item: action_at_ms(item)) if previous else None


def audit_vocal_alignment_payload(
    payload: dict,
    *,
    target_role: str = "vocal-hook",
    db_path: Path | None = DEFAULT_LIBRARY_DB,
    cache_path: Path | None = DEFAULT_CACHE,
    max_beat_error_ms: int = DEFAULT_MAX_BEAT_ERROR_MS,
    max_phrase_error_ms: int = DEFAULT_MAX_PHRASE_ERROR_MS,
) -> dict[str, object]:
    actions = [action for action in payload.get("actions", []) if isinstance(action, dict)]
    reports: list[dict[str, object]] = []
    for action in actions:
        if not is_vocal_action(action, target_role=target_role):
            continue
        alignment = action.get("vocal_alignment") if isinstance(action.get("vocal_alignment"), dict) else None
        if not alignment:
            reports.append(
                {
                    "id": str(action.get("id") or ""),
                    "status": "fail",
                    "reason": "missing vocal_alignment metadata",
                }
            )
            continue
        anchor_source_ms = int(alignment.get("phrase_anchor_ms", alignment.get("cue_at_ms")))
        anchor_timeline_ms = timeline_ms_for_source_ms(action, anchor_source_ms)
        backing = active_backing_for_anchor(actions, anchor_timeline_ms, target_role=target_role)
        if backing is None:
            reports.append(
                {
                    "id": str(action.get("id") or ""),
                    "status": "fail",
                    "anchor_timeline_ms": anchor_timeline_ms,
                    "reason": "no active backing load at vocal anchor",
                }
            )
            continue
        grid = action_beatgrid(backing, db_path=db_path, cache_path=cache_path)
        if grid is None:
            reports.append(
                {
                    "id": str(action.get("id") or ""),
                    "status": "fail",
                    "anchor_timeline_ms": anchor_timeline_ms,
                    "backing_id": str(backing.get("id") or ""),
                    "reason": "backing load has no usable beatgrid",
                }
            )
            continue
        backing_source_ms = source_ms_at_timeline_ms(backing, anchor_timeline_ms)
        backing_factor = action_tempo_factor(backing)
        beat_delta_ms = nearest_grid_delta_ms(backing_source_ms, grid, grid.beat_ms) / backing_factor
        phrase_delta_ms = nearest_grid_delta_ms(backing_source_ms, grid, grid.resolved_phrase_ms) / backing_factor
        status = "pass" if abs(beat_delta_ms) <= max_beat_error_ms and abs(phrase_delta_ms) <= max_phrase_error_ms else "fail"
        reports.append(
            {
                "id": str(action.get("id") or ""),
                "status": status,
                "anchor_timeline_ms": anchor_timeline_ms,
                "anchor_source_ms": anchor_source_ms,
                "backing_id": str(backing.get("id") or ""),
                "backing_source_ms": int(round(backing_source_ms)),
                "backing_grid_source": grid.source,
                "bpm": grid.bpm,
                "beat_delta_ms": int(round(beat_delta_ms)),
                "phrase_delta_ms": int(round(phrase_delta_ms)),
                "max_beat_error_ms": max_beat_error_ms,
                "max_phrase_error_ms": max_phrase_error_ms,
                "reason": "vocal phrase anchor compared against active backing beat and phrase grid",
            }
        )
    failed = [item for item in reports if item.get("status") != "pass"]
    return {"ok": not failed, "checked": len(reports), "failed": len(failed), "vocals": reports}


def align_session_payload(
    payload: dict,
    *,
    target_role: str = "vocal-hook",
    block_ms: int | None = None,
    drop_offset_ms: int | None = None,
    cue_kind: str = "hook_candidate",
    pre_roll_ms: int = 1_200,
    max_pickup_ms: int = 4_000,
    backend: str = "ffmpeg",
    sample_rate: int = 48_000,
) -> tuple[dict, list[dict[str, int | float | str]]]:
    actions = payload.get("actions")
    if not isinstance(actions, list):
        raise ValueError("session has no actions[] to align")
    aligned_payload = copy.deepcopy(payload)
    aligned_actions = aligned_payload["actions"]
    reports: list[dict[str, int | float | str]] = []
    deltas: dict[str, int] = {}
    for index, action in enumerate(list(aligned_actions)):
        if not isinstance(action, dict) or action.get("type") != "load_track" or not action_matches_target_role(action, target_role):
            continue
        target_drop_ms = action.get("align_to_ms", action.get("target_drop_ms"))
        if target_drop_ms is None:
            if block_ms is None or drop_offset_ms is None:
                continue
            target_drop_ms = (action_at_ms(action) // block_ms) * block_ms + drop_offset_ms
        new_action, plan = align_vocal_action(
            action,
            target_drop_ms=int(target_drop_ms),
            cue_kind=str(action.get("vocal_cue_kind") or cue_kind),
            pre_roll_ms=int(action.get("vocal_pre_roll_ms") or pre_roll_ms),
            max_pickup_ms=int(action.get("vocal_max_pickup_ms") or max_pickup_ms),
            backend=backend,
            sample_rate=sample_rate,
        )
        old_at = action_at_ms(action)
        new_at = action_at_ms(new_action)
        deltas[str(action.get("id"))] = new_at - old_at
        aligned_actions[index] = new_action
        reports.append({"id": str(action.get("id")), "old_at_ms": old_at, "new_at_ms": new_at, "delta_ms": new_at - old_at, **plan})

    for action in aligned_actions:
        if not isinstance(action, dict):
            continue
        for vocal_id, delta in deltas.items():
            if not delta or str(action.get("id") or "") == vocal_id:
                continue
            if action_belongs_to_vocal(action, vocal_id):
                action["at_ms"] = action_at_ms(action) + delta
    aligned_actions.sort(key=lambda item: (action_at_ms(item) if isinstance(item, dict) else 0, str(item.get("id") if isinstance(item, dict) else "")))
    aligned_payload.setdefault("notes", {})["vocal_alignment"] = {
        "target_role": target_role,
        "block_ms": block_ms,
        "drop_offset_ms": drop_offset_ms,
        "cue_kind": cue_kind,
        "pre_roll_ms": pre_roll_ms,
        "max_pickup_ms": max_pickup_ms,
        "aligned_loads": len(reports),
    }
    return aligned_payload, reports


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Detect vocal-stem cue points and plan drop-aligned vocal loads.")
    sub = parser.add_subparsers(dest="command")

    detect = sub.add_parser("detect")
    detect.add_argument("vocal_stem", type=Path)
    detect.add_argument("--backend", default="ffmpeg")
    detect.add_argument("--sample-rate", type=int, default=48_000)
    detect.add_argument("--target-drop-ms", type=int)
    detect.add_argument("--cue-kind", default="hook_candidate")
    detect.add_argument("--pre-roll-ms", type=int, default=1_200)
    detect.add_argument("--max-pickup-ms", type=int, default=4_000)
    detect.add_argument("--min-hook-ms", type=int, default=2_000)

    align = sub.add_parser("align-session")
    align.add_argument("session", type=Path)
    align.add_argument("--output", type=Path, required=True)
    align.add_argument("--target-role", default="vocal-hook")
    align.add_argument("--block-ms", type=int)
    align.add_argument("--drop-offset-ms", type=int)
    align.add_argument("--cue-kind", default="hook_candidate")
    align.add_argument("--pre-roll-ms", type=int, default=1_200)
    align.add_argument("--max-pickup-ms", type=int, default=4_000)
    align.add_argument("--backend", default="ffmpeg")
    align.add_argument("--sample-rate", type=int, default=48_000)
    audit = sub.add_parser("audit-session")
    audit.add_argument("session", type=Path)
    audit.add_argument("--target-role", default="vocal-hook")
    audit.add_argument("--db", type=Path, default=DEFAULT_LIBRARY_DB)
    audit.add_argument("--no-db", action="store_true")
    audit.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    audit.add_argument("--no-cache", action="store_true")
    audit.add_argument("--max-beat-error-ms", type=int, default=DEFAULT_MAX_BEAT_ERROR_MS)
    audit.add_argument("--max-phrase-error-ms", type=int, default=DEFAULT_MAX_PHRASE_ERROR_MS)
    audit.add_argument("--fail", action=argparse.BooleanOptionalAction, default=True)
    overlap = sub.add_parser("audit-vocal-overlap")
    overlap.add_argument("session", type=Path)
    overlap.add_argument("--target-role", default="vocal-hook")
    overlap.add_argument("--max-overlap-ms", type=int, default=DEFAULT_MAX_VOCAL_OVERLAP_MS)
    overlap.add_argument("--fail", action=argparse.BooleanOptionalAction, default=True)
    # Backward-compatible shorthand: `script stem.flac --target-drop-ms ...`.
    parser.add_argument("legacy_vocal_stem", nargs="?", type=Path)
    parser.add_argument("--backend", default="ffmpeg")
    parser.add_argument("--sample-rate", type=int, default=48_000)
    parser.add_argument("--target-drop-ms", type=int)
    parser.add_argument("--cue-kind", default="hook_candidate")
    parser.add_argument("--pre-roll-ms", type=int, default=1_200)
    parser.add_argument("--max-pickup-ms", type=int, default=4_000)
    parser.add_argument("--min-hook-ms", type=int, default=2_000)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "align-session":
        payload = json.loads(args.session.read_text())
        aligned, report = align_session_payload(
            payload,
            target_role=args.target_role,
            block_ms=args.block_ms,
            drop_offset_ms=args.drop_offset_ms,
            cue_kind=args.cue_kind,
            pre_roll_ms=args.pre_roll_ms,
            max_pickup_ms=args.max_pickup_ms,
            backend=args.backend,
            sample_rate=args.sample_rate,
        )
        args.output.write_text(json.dumps(aligned, indent=2) + "\n")
        print(json.dumps({"output": str(args.output), "aligned": report}, indent=2, sort_keys=True))
        return 0
    if args.command == "audit-session":
        payload = json.loads(args.session.read_text())
        report = audit_vocal_alignment_payload(
            payload,
            target_role=args.target_role,
            db_path=None if args.no_db else args.db,
            cache_path=None if args.no_cache else args.cache,
            max_beat_error_ms=args.max_beat_error_ms,
            max_phrase_error_ms=args.max_phrase_error_ms,
        )
        print(json.dumps(report, indent=2, sort_keys=True))
        if args.fail and not report["ok"]:
            return 1
        return 0
    if args.command == "audit-vocal-overlap":
        payload = json.loads(args.session.read_text())
        report = audit_vocal_overlap_payload(
            payload,
            target_role=args.target_role,
            max_overlap_ms=args.max_overlap_ms,
        )
        print(json.dumps(report, indent=2, sort_keys=True))
        if args.fail and not report["ok"]:
            return 1
        return 0

    vocal_stem = args.vocal_stem if args.command == "detect" else args.legacy_vocal_stem
    if vocal_stem is None:
        raise SystemExit("vocal stem is required")
    cues = detect_vocal_cues(vocal_stem, backend=args.backend, sample_rate=args.sample_rate, min_hook_ms=args.min_hook_ms)
    payload: dict[str, object] = {
        "vocal_stem": str(vocal_stem),
        "cues": [asdict(cue) for cue in cues],
    }
    if args.target_drop_ms is not None:
        payload["alignment"] = alignment_plan(
            cues,
            target_drop_ms=args.target_drop_ms,
            cue_kind=args.cue_kind,
            pre_roll_ms=args.pre_roll_ms,
            max_pickup_ms=args.max_pickup_ms,
        )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
