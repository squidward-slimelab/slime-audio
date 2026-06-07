#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from slime_audio_dj import (
    DEFAULT_CACHE,
    DEFAULT_LIBRARY_DB,
    DEFAULT_TUNEBAT_LOCAL_ANALYZER,
    TrackAnalysis,
    analyze_with_cache,
    coerce_analysis,
    coerce_structure,
    load_analysis_from_db,
    select_cue,
    transition_plan,
)
from slime_audio_session import load_payload, parse_session, playhead_ms_from_state, write_payload
from slime_audio_session import add_instant_double_routine

DECK_ORDER = ["deck-2", "deck-3", "deck-1", "deck-4"]
DEFAULT_LOCK_LEAD_MS = 20_000
DEFAULT_DOUBLE_DURATION_MS = 12_000
MIN_OVERLAY_SCORE = 0.72
MAX_RENDER_TEMPO_SHIFT_PCT = 4.0
MAX_RENDER_PITCH_SHIFT_SEMITONES = 2
AUTO_ROUTINE_RECIPES = ["echo-stabs", "loop-roll", "scratch-cuts", "one-beat-trades"]
FILTER_OPEN_HZ = 22_050


@dataclass(frozen=True)
class PlannedMove:
    kind: str
    clip_id: str
    start_ms: int
    reason: str
    related_clip_id: str | None = None


def clip_end(clip: dict[str, Any]) -> int:
    return int(clip["start_ms"]) + int(clip.get("duration_ms") or 0)


def clip_overlaps(clip: dict[str, Any], start_ms: int, end_ms: int) -> bool:
    return int(clip["start_ms"]) < end_ms and start_ms < clip_end(clip)


def normalize_clip_times(payload: dict[str, Any]) -> None:
    for clip in payload.get("clips", []):
        if "start" in clip and "start_ms" not in clip:
            clip["start_ms"] = clip.pop("start")
        if "duration" in clip and "duration_ms" not in clip:
            clip["duration_ms"] = clip.pop("duration")
        if "trim_start" in clip and "trim_start_ms" not in clip:
            clip["trim_start_ms"] = clip.pop("trim_start")


def analyzed_remaining_ms(clip: dict[str, Any], analysis: TrackAnalysis | None) -> int | None:
    if analysis is None or analysis.duration_s <= 0:
        return None
    trim_start_ms = int(clip.get("trim_start_ms") or 0)
    remaining_ms = int(round(analysis.duration_s * 1000)) - trim_start_ms
    return max(1, remaining_ms)


def sync_placeholder_duration_to_analysis(clip: dict[str, Any], analysis: TrackAnalysis | None) -> bool:
    remaining_ms = analyzed_remaining_ms(clip, analysis)
    if remaining_ms is None:
        return False
    current_ms = int(clip.get("duration_ms") or 0)
    if current_ms <= 0:
        return False
    if abs(current_ms - remaining_ms) <= 5_000:
        return False
    # Base imported/live playlists often use a generic 240s placeholder.
    # Correct those from analysis so songs neither fade/cut too early nor
    # leave dead scheduled tails. Intentional short clips, doubles, beds, and
    # cue gestures keep their authored durations.
    if current_ms != 240_000 or clip.get("planner_role"):
        return False
    clip["duration_ms"] = remaining_ms
    return True


def phrase_ms(analysis: TrackAnalysis | None) -> int:
    if analysis and analysis.beatgrid and analysis.beatgrid.phrase_ms:
        return max(8_000, min(32_000, int(analysis.beatgrid.phrase_ms)))
    return 16_000


def safe_overlay_plan(
    source: TrackAnalysis | None,
    target: TrackAnalysis | None,
    *,
    max_tempo_shift_pct: float = MAX_RENDER_TEMPO_SHIFT_PCT,
    max_pitch_shift_semitones: int = MAX_RENDER_PITCH_SHIFT_SEMITONES,
) -> tuple[Any | None, str]:
    if source is None or target is None:
        return None, "missing analysis"
    plan = transition_plan(source, target)
    tempo_shift = abs(plan.target_tempo_shift_pct or 0.0)
    if plan.score < MIN_OVERLAY_SCORE:
        return None, f"transition score {plan.score} below overlay threshold"
    if plan.key_relation == "clash":
        return None, "key clash"
    if abs(plan.pitch_shift_semitones) > max_pitch_shift_semitones:
        return None, f"pitch shift {plan.pitch_shift_semitones:+d} exceeds allowed render shift"
    if tempo_shift > max_tempo_shift_pct:
        return None, f"tempo shift {tempo_shift:.2f}% too large for current renderer"
    pitch_note = f"; pitch {plan.pitch_shift_semitones:+d}" if plan.pitch_shift_semitones else ""
    return plan, f"score {plan.score}; {plan.key_relation}; tempo {plan.target_tempo_shift_pct or 0.0:+.2f}%{pitch_note}"


def transition_overlap_ms(
    source: TrackAnalysis | None,
    target: TrackAnalysis | None,
    shorter_ms: int,
    *,
    max_tempo_shift_pct: float = MAX_RENDER_TEMPO_SHIFT_PCT,
    max_pitch_shift_semitones: int = MAX_RENDER_PITCH_SHIFT_SEMITONES,
) -> int:
    plan, _reason = safe_overlay_plan(
        source,
        target,
        max_tempo_shift_pct=max_tempo_shift_pct,
        max_pitch_shift_semitones=max_pitch_shift_semitones,
    )
    if plan is None:
        return 0
    base = phrase_ms(source)
    if plan.score >= 0.84:
        base *= 2
    return max(4_000, min(base, shorter_ms // 3, 32_000))


def first_structure(analysis: TrackAnalysis | None, kinds: set[str], *, before_ms: int = 150_000, after_ms: int = 8_000) -> Any | None:
    if analysis is None:
        return None
    kind_priority = {"drop": 0, "build": 1}
    windows = [
        window
        for window in coerce_structure(analysis.structure)
        if window.kind in kinds and after_ms <= window.start_ms < before_ms
    ]
    windows.sort(key=lambda item: (kind_priority.get(item.kind, 9), -item.confidence, item.start_ms))
    return windows[0] if windows else None


def deck_available(clips: list[dict[str, Any]], deck: str, start_ms: int, end_ms: int) -> bool:
    return not any(str(clip.get("deck")) == deck and clip_overlaps(clip, start_ms, end_ms) for clip in clips)


def choose_deck(
    clips: list[dict[str, Any]],
    start_ms: int,
    end_ms: int,
    *,
    deck_order: list[str] | None = None,
    avoid: set[str] | None = None,
) -> str:
    avoid = avoid or set()
    deck_order = deck_order or DECK_ORDER
    for deck in deck_order:
        if deck in avoid:
            continue
        if deck_available(clips, deck, start_ms, end_ms):
            return deck
    for deck in deck_order:
        if deck_available(clips, deck, start_ms, end_ms):
            return deck
    return deck_order[0]


def auto_routine_start_ms(clip: dict[str, Any], analysis: TrackAnalysis | None, *, lock_before_ms: int) -> int | None:
    start_ms = int(clip.get("start_ms", 0))
    duration_ms = int(clip.get("duration_ms") or 0)
    if duration_ms < 36_000:
        return None
    phrase = phrase_ms(analysis)
    routine_start = start_ms + phrase
    if routine_start < lock_before_ms:
        routine_start = lock_before_ms + 4_000
    latest_start = start_ms + duration_ms - 10_000
    if routine_start > latest_start:
        return None
    return routine_start


def add_deck_automation(
    payload: dict[str, Any],
    *,
    target: str,
    param: str,
    points: list[dict[str, float | int]],
    role: str,
    related_clip_id: str,
) -> None:
    payload.setdefault("deck_automations", []).append(
        {
            "target": target,
            "param": param,
            "planner_role": role,
            "related_clip_id": related_clip_id,
            "points": points,
        }
    )


def transition_anchor_ms(incoming: dict[str, Any], analysis: TrackAnalysis | None, overlap_ms: int) -> int:
    start_ms = int(incoming["start_ms"])
    end_ms = start_ms + overlap_ms
    phrase = phrase_ms(analysis)
    cue = select_cue(analysis, {"drop", "hook"}, before_ms=overlap_ms + phrase, after_ms=4_000)
    if cue is None:
        return end_ms
    return start_ms + int(cue.at_ms)


def add_transition_filter_automation(
    payload: dict[str, Any],
    outgoing: dict[str, Any],
    incoming: dict[str, Any],
    overlap_ms: int,
    incoming_analysis: TrackAnalysis | None = None,
) -> None:
    if overlap_ms <= 0:
        return
    start_ms = int(incoming["start_ms"])
    end_ms = start_ms + overlap_ms
    if end_ms <= start_ms:
        return
    middle_ms = start_ms + max(1, overlap_ms // 2)
    outgoing_deck = str(outgoing.get("deck") or "")
    incoming_deck = str(incoming.get("deck") or "")
    if not outgoing_deck or not incoming_deck or outgoing_deck == incoming_deck:
        return
    incoming_anchor_ms = transition_anchor_ms(incoming, incoming_analysis, overlap_ms)
    incoming_middle_ms = start_ms + max(1, (incoming_anchor_ms - start_ms) // 2)
    add_deck_automation(
        payload,
        target=outgoing_deck,
        param="lowpass_hz",
        role="mix-planner-filter-carve",
        related_clip_id=str(incoming.get("id")),
        points=[
            {"at_ms": start_ms, "value": FILTER_OPEN_HZ},
            {"at_ms": middle_ms, "value": 6_500},
            {"at_ms": end_ms, "value": 2_200},
        ],
    )
    add_deck_automation(
        payload,
        target=outgoing_deck,
        param="eq_low_db",
        role="mix-planner-eq-carve",
        related_clip_id=str(incoming.get("id")),
        points=[
            {"at_ms": start_ms, "value": 0.0},
            {"at_ms": end_ms, "value": -4.0},
        ],
    )
    add_deck_automation(
        payload,
        target=outgoing_deck,
        param="eq_high_db",
        role="mix-planner-eq-carve",
        related_clip_id=str(incoming.get("id")),
        points=[
            {"at_ms": start_ms, "value": 0.0},
            {"at_ms": end_ms, "value": -2.0},
        ],
    )
    add_deck_automation(
        payload,
        target=incoming_deck,
        param="highpass_hz",
        role="mix-planner-filter-carve",
        related_clip_id=str(outgoing.get("id")),
        points=[
            {"at_ms": start_ms, "value": 420},
            {"at_ms": incoming_middle_ms, "value": 180},
            {"at_ms": incoming_anchor_ms, "value": 20},
        ],
    )
    add_deck_automation(
        payload,
        target=incoming_deck,
        param="eq_low_db",
        role="mix-planner-eq-carve",
        related_clip_id=str(outgoing.get("id")),
        points=[
            {"at_ms": start_ms, "value": -5.0},
            {"at_ms": incoming_anchor_ms, "value": 0.0},
        ],
    )


def transition_plan_record(
    *,
    outgoing: dict[str, Any] | None,
    incoming: dict[str, Any],
    incoming_analysis: TrackAnalysis | None,
    plan: Any | None,
    overlap_ms: int,
    reason: str,
) -> dict[str, Any]:
    decision = "blend" if plan is not None and overlap_ms > 0 else "cut"
    record: dict[str, Any] = {
        "id": f"transition-{incoming.get('id')}",
        "planner_role": "mix-planner-transition-plan",
        "from_clip_id": str(outgoing.get("id")) if outgoing else None,
        "to_clip_id": str(incoming.get("id")),
        "start_ms": int(incoming.get("start_ms") or 0),
        "end_ms": int(incoming.get("start_ms") or 0) + max(0, int(overlap_ms)),
        "overlap_ms": max(0, int(overlap_ms)),
        "decision": decision,
        "reason": reason,
        "tempo_shift_pct": float(incoming.get("tempo_shift_pct") or 0.0),
        "pitch_shift_semitones": int(incoming.get("pitch_shift_semitones") or 0),
        "analysis_path": str(incoming_analysis.path) if incoming_analysis is not None else str(incoming.get("path") or ""),
    }
    if plan is not None:
        record.update(
            {
                "score": plan.score,
                "bpm_ratio": plan.bpm_ratio,
                "key_relation": plan.key_relation,
                "phrase_wait_beats": plan.phrase_wait_beats,
                "notes": plan.notes,
            }
        )
    return record


def plan_future_mix(
    payload: dict[str, Any],
    analyses_by_path: dict[str, TrackAnalysis | dict],
    *,
    lock_before_ms: int,
    double_every: int = 0,
    routine_every: int = 2,
    routine_cache_path: Path | None = None,
    routine_db_path: Path = DEFAULT_LIBRARY_DB,
    max_tempo_shift_pct: float = MAX_RENDER_TEMPO_SHIFT_PCT,
    max_pitch_shift_semitones: int = MAX_RENDER_PITCH_SHIFT_SEMITONES,
    plan_until_ms: int | None = None,
) -> tuple[dict[str, Any], list[PlannedMove]]:
    next_payload = copy.deepcopy(payload)
    normalize_clip_times(next_payload)
    next_payload["deck_automations"] = [
        automation
        for automation in next_payload.get("deck_automations", [])
        if automation.get("planner_role") not in {"mix-planner-filter-carve", "mix-planner-eq-carve"}
    ]
    next_payload["transition_plans"] = [
        plan
        for plan in next_payload.get("transition_plans", [])
        if not (
            plan.get("planner_role") == "mix-planner-transition-plan"
            and int(plan.get("start_ms") or 0) >= lock_before_ms
            and (plan_until_ms is None or int(plan.get("start_ms") or 0) < plan_until_ms)
        )
    ]
    analyses = {path: coerce_analysis(analysis) for path, analysis in analyses_by_path.items()}
    original_clips = sorted(next_payload.get("clips", []), key=lambda clip: (int(clip.get("start_ms", 0)), str(clip.get("deck")), str(clip.get("id"))))
    locked = [clip for clip in original_clips if int(clip.get("start_ms", 0)) < lock_before_ms]
    after_horizon = [
        clip
        for clip in original_clips
        if plan_until_ms is not None and int(clip.get("start_ms", 0)) >= plan_until_ms
    ]
    protected = [*locked, *after_horizon]
    future = [
        clip
        for clip in original_clips
        if int(clip.get("start_ms", 0)) >= lock_before_ms
        and (plan_until_ms is None or int(clip.get("start_ms", 0)) < plan_until_ms)
        and clip.get("kind") != "planner-double"
    ]
    if not future:
        return next_payload, []
    declared_decks = [str(deck) for deck in next_payload.get("decks", [])]
    deck_order = [deck for deck in DECK_ORDER if deck in declared_decks] + [deck for deck in declared_decks if deck not in DECK_ORDER]
    if not deck_order:
        deck_order = DECK_ORDER

    planned: list[PlannedMove] = []
    rebuilt: list[dict[str, Any]] = protected[:]
    previous = max(locked, key=clip_end, default=None)
    cursor = max(lock_before_ms, clip_end(previous) - 12_000 if previous else lock_before_ms)
    previous_analysis = analyses.get(str(previous.get("path"))) if previous else None
    previous_deck = str(previous.get("deck")) if previous else ""

    for index, clip in enumerate(future):
        duration_ms = int(clip.get("duration_ms") or 0)
        if duration_ms <= 0:
            continue
        analysis = analyses.get(str(clip.get("path")))
        if sync_placeholder_duration_to_analysis(clip, analysis):
            duration_ms = int(clip.get("duration_ms") or 0)
        shorter = min(duration_ms, int(previous.get("duration_ms") or duration_ms) if previous else duration_ms)
        overlap = transition_overlap_ms(
            previous_analysis,
            analysis,
            shorter,
            max_tempo_shift_pct=max_tempo_shift_pct,
            max_pitch_shift_semitones=max_pitch_shift_semitones,
        )
        start_ms = cursor if previous is None else max(lock_before_ms, clip_end(previous) - overlap)
        end_ms = start_ms + duration_ms
        deck = choose_deck(rebuilt, start_ms, end_ms, deck_order=deck_order, avoid={previous_deck} if previous_deck else set())

        clip["start_ms"] = start_ms
        clip["deck"] = deck
        # Keep clip fades as click/entry protection. Long automatic fade-outs
        # make the lead record audibly sag even when no replacement move is
        # obvious; transition shape belongs in EQ/filter/crossfader automation.
        clip["fade_in_ms"] = min(overlap, 8_000) if overlap else 0
        clip["fade_out_ms"] = 0
        plan, overlay_reason = safe_overlay_plan(
            previous_analysis,
            analysis,
            max_tempo_shift_pct=max_tempo_shift_pct,
            max_pitch_shift_semitones=max_pitch_shift_semitones,
        )
        if plan is not None:
            clip["tempo_shift_pct"] = plan.target_tempo_shift_pct or 0.0
            clip["pitch_shift_semitones"] = plan.pitch_shift_semitones
            reason = f"overlap {overlap}ms; {overlay_reason}"
        else:
            clip["tempo_shift_pct"] = 0.0
            clip["pitch_shift_semitones"] = 0
            reason = f"cut; {overlay_reason}"
        rebuilt.append(clip)
        planned.append(PlannedMove("blend", str(clip.get("id")), start_ms, reason, str(previous.get("id")) if previous else None))
        next_payload.setdefault("transition_plans", []).append(
            transition_plan_record(
                outgoing=previous,
                incoming=clip,
                incoming_analysis=analysis,
                plan=plan,
                overlap_ms=overlap if previous is not None else 0,
                reason=reason,
            )
        )
        if previous is not None and overlap and plan is not None:
            actual_overlap_ms = max(0, min(overlap, clip_end(previous) - start_ms))
            add_transition_filter_automation(next_payload, previous, clip, actual_overlap_ms, analysis)

        if double_every > 0 and previous is not None and overlap and plan is not None and index % double_every == 0:
            cue = select_cue(analysis, {"drop", "hook", "build"}, before_ms=150_000, after_ms=8_000)
            drop = first_structure(analysis, {"drop", "build"}) if cue is None else None
            cue_ms = int(cue.at_ms if cue is not None else drop.start_ms) if cue is not None or drop is not None else None
            if cue_ms is not None:
                double_duration = min(DEFAULT_DOUBLE_DURATION_MS, duration_ms - cue_ms)
                double_start = max(lock_before_ms, start_ms - double_duration)
                double_duration = start_ms - double_start
                if double_duration >= 4_000:
                    double_end = double_start + double_duration
                    double_deck = choose_deck(rebuilt, double_start, double_end, deck_order=deck_order, avoid={deck, previous_deck})
                    double_clip = {
                        "id": f"double-{clip.get('id')}",
                        "deck": double_deck,
                        "path": clip.get("path"),
                        "start_ms": double_start,
                        "trim_start_ms": cue_ms,
                        "duration_ms": double_duration,
                        "gain_db": -6.0,
                        "fade_in_ms": min(1500, double_duration // 3),
                        "fade_out_ms": min(2500, double_duration // 2),
                        "kind": "planner-double",
                        "planner_role": "drop-double",
                        "source_clip_id": clip.get("id"),
                        "cue_kind": cue.kind if cue is not None else drop.kind,
                    }
                    rebuilt.append(double_clip)
                    planned.append(PlannedMove("double", str(double_clip["id"]), double_start, f"{double_clip['cue_kind']} tease from {clip.get('id')}", str(clip.get("id"))))

        previous = clip
        previous_analysis = analysis
        previous_deck = deck
        cursor = end_ms

    next_payload["clips"] = sorted(rebuilt, key=lambda clip: (int(clip.get("start_ms", 0)), str(clip.get("deck")), str(clip.get("id"))))
    next_payload["automations"] = [
        automation
        for automation in next_payload.get("automations", [])
        if not (automation.get("target") == "master" and automation.get("param") == "duck_volume" and automation.get("planner_role") == "mix-planner")
    ]
    if routine_cache_path is not None and routine_every > 0:
        routine_targets = [
            (clip, analyses.get(str(clip.get("path"))))
            for clip in sorted(next_payload.get("clips", []), key=lambda item: int(item.get("start_ms", 0)))
            if clip.get("kind") not in {"planner-double", "effect-track"} and int(clip.get("start_ms", 0)) >= lock_before_ms
        ]
        routine_index = 0
        for clip, analysis in routine_targets:
            if routine_index % routine_every != 0:
                routine_index += 1
                continue
            start_ms = auto_routine_start_ms(clip, analysis, lock_before_ms=lock_before_ms)
            if start_ms is None:
                routine_index += 1
                continue
            recipe = AUTO_ROUTINE_RECIPES[(routine_index // routine_every) % len(AUTO_ROUTINE_RECIPES)]
            routine_id = f"auto-{recipe}-{clip.get('id')}"
            try:
                next_payload = add_instant_double_routine(
                    next_payload,
                    source_id=str(clip.get("id")),
                    routine_id=routine_id,
                    recipe=recipe,
                    start=str(start_ms),
                    cache_path=routine_cache_path,
                    cue_db=routine_db_path,
                    lock_before_ms=lock_before_ms,
                    force=False,
                )
            except ValueError as error:
                planned.append(PlannedMove("routine-skip", routine_id, start_ms, str(error), str(clip.get("id"))))
            else:
                planned.append(PlannedMove("routine", routine_id, start_ms, recipe, str(clip.get("id"))))
            routine_index += 1
    parse_session(next_payload)
    return next_payload, planned


def analyze_session_paths(
    payload: dict[str, Any],
    cache: Path,
    backend: str,
    sample_rate: int,
    *,
    lock_before_ms: int,
    db_path: Path = DEFAULT_LIBRARY_DB,
    tunebat_analyzer: Path = DEFAULT_TUNEBAT_LOCAL_ANALYZER,
    analyze_missing: bool = True,
    plan_until_ms: int | None = None,
) -> dict[str, TrackAnalysis]:
    paths = []
    seen = set()
    for clip in payload.get("clips", []):
        start_ms = int(clip.get("start_ms", clip.get("start", 0)))
        duration_ms = int(clip.get("duration_ms", clip.get("duration", 0)) or 0)
        if start_ms + duration_ms < lock_before_ms:
            continue
        if plan_until_ms is not None and start_ms >= plan_until_ms:
            continue
        path = str(clip.get("path") or "")
        if path and path not in seen:
            seen.add(path)
            paths.append(Path(path))
    if analyze_missing:
        return {analysis.path: analysis for analysis in analyze_with_cache(paths, cache, backend, sample_rate, db_path, tunebat_analyzer)}
    analyses: dict[str, TrackAnalysis] = {}
    for path in paths:
        stored = load_analysis_from_db(db_path, path)
        if stored is not None:
            analyses[stored.path] = stored
    return analyses


def state_lock_ms(state_path: Path | None, lead_ms: int) -> int:
    if state_path is None or not state_path.exists():
        return 0
    payload = load_payload(state_path)
    window_end = payload.get("window_end_ms")
    playhead = playhead_ms_from_state(state_path)
    if isinstance(window_end, (int, float)):
        return max(int(window_end), playhead + lead_ms)
    return playhead + lead_ms


def main() -> int:
    parser = argparse.ArgumentParser(description="Plan phrase-aware SlimeAudio doubles, drops, and blends into future mix-session clips.")
    parser.add_argument("--session", type=Path, default=Path("runtime/mix-session.json"))
    parser.add_argument("--state", type=Path)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--db", type=Path, default=DEFAULT_LIBRARY_DB)
    parser.add_argument("--tunebat-analyzer", type=Path, default=DEFAULT_TUNEBAT_LOCAL_ANALYZER)
    parser.add_argument("--backend", choices=["auto", "ffmpeg"], default="auto")
    parser.add_argument("--sample-rate", type=int, default=44_100)
    parser.add_argument("--lock-before-ms", type=int)
    parser.add_argument("--lock-lead-ms", type=int, default=DEFAULT_LOCK_LEAD_MS)
    parser.add_argument("--double-every", type=int, default=0)
    parser.add_argument("--routine-every", type=int, default=2)
    parser.add_argument("--no-routines", action="store_true")
    parser.add_argument("--max-render-tempo-shift-pct", type=float, default=MAX_RENDER_TEMPO_SHIFT_PCT)
    parser.add_argument("--max-render-pitch-shift-semitones", type=int, default=MAX_RENDER_PITCH_SHIFT_SEMITONES)
    parser.add_argument("--cached-analysis-only", action="store_true", help="Use only cached DB analysis; missing tracks become explicit cut decisions.")
    parser.add_argument("--horizon-ms", type=int, help="Only rewrite future clips that begin before lock-before plus this horizon.")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    payload = load_payload(args.session)
    normalize_clip_times(payload)
    lock_before_ms = args.lock_before_ms if args.lock_before_ms is not None else state_lock_ms(args.state, args.lock_lead_ms)
    plan_until_ms = lock_before_ms + args.horizon_ms if args.horizon_ms is not None else None
    analyses = analyze_session_paths(
        payload,
        args.cache,
        args.backend,
        args.sample_rate,
        lock_before_ms=lock_before_ms,
        db_path=args.db,
        tunebat_analyzer=args.tunebat_analyzer,
        analyze_missing=not args.cached_analysis_only,
        plan_until_ms=plan_until_ms,
    )
    planned_payload, moves = plan_future_mix(
        payload,
        analyses,
        lock_before_ms=lock_before_ms,
        double_every=args.double_every,
        routine_every=0 if args.no_routines else args.routine_every,
        routine_cache_path=None if args.no_routines else args.cache,
        routine_db_path=args.db,
        max_tempo_shift_pct=args.max_render_tempo_shift_pct,
        max_pitch_shift_semitones=args.max_render_pitch_shift_semitones,
        plan_until_ms=plan_until_ms,
    )
    result = {
        "lock_before_ms": lock_before_ms,
        "moves": [asdict(move) for move in moves],
        "clip_count": len(planned_payload.get("clips", [])),
        "transition_plan_count": len(planned_payload.get("transition_plans", [])),
        "cached_analysis_only": args.cached_analysis_only,
        "plan_until_ms": plan_until_ms,
    }
    if args.apply:
        write_payload(args.session, planned_payload)
        result["applied"] = True
    else:
        result["applied"] = False
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
