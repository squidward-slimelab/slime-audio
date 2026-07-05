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
    major_equivalent_tonic,
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
from slime_audio_session import (
    DEFAULT_LIBRARY_DB as SESSION_LIBRARY_DB,
    DEFAULT_MAX_WARP_STRETCH_PCT,
    action_type,
    compile_actions_payload,
    edit_lock_ms_from_state,
    load_payload,
    parse_ms,
    master_bpm_at,
    master_tempo_shift_pct,
    parse_session,
    playhead_ms_from_state,
    ready_stem_artifacts,
    write_payload,
)

DECK_ORDER = ["deck-2", "deck-3", "deck-1", "deck-4"]
DEFAULT_LOCK_LEAD_MS = 20_000
DEFAULT_DOUBLE_DURATION_MS = 12_000
MIN_OVERLAY_SCORE = 0.72
MAX_RENDER_TEMPO_SHIFT_PCT = 4.0
MAX_RENDER_PITCH_SHIFT_SEMITONES = 2
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


def warp_aware_duration_ms(payload: dict[str, Any], clip: dict[str, Any], duration_ms: int, at_ms_estimate: int) -> int:
    """Timeline length of a full-track lead under the master tempo knob.

    The renderer maps timeline time to source time as elapsed * tempo_factor,
    so a lead whose duration_ms is its source length plays wrong once warped:
    sped-up clips exhaust their file early into scheduled dead air, slowed
    clips chop mid-note at their end. Author the warped timeline length into
    duration_ms instead, keeping the source-domain length in
    source_duration_ms so the derivation stays idempotent across replans.
    Only planner leads are converted: short authored windows (beds, doubles,
    cue gestures) declare timeline intent, not source length.
    """
    if str(clip.get("planner_role") or "") != "lead":
        return duration_ms
    source_ms = int(clip.get("source_duration_ms") or 0)
    if source_ms <= 0:
        source_ms = duration_ms
        clip["source_duration_ms"] = source_ms
    if not clip.get("warp", True) or not clip.get("source_bpm"):
        return source_ms
    master = master_bpm_at(payload, max(0, int(at_ms_estimate)))
    if master is None:
        return source_ms
    max_stretch = abs(float(payload.get("max_tempo_stretch_pct", DEFAULT_MAX_WARP_STRETCH_PCT)))
    shift = master_tempo_shift_pct(float(clip["source_bpm"]), master, max_stretch)
    factor = 1 + (shift or 0.0) / 100
    if factor <= 0:
        return source_ms
    return max(1, int(round(source_ms / factor)))


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
    # The repo key-fit policy (and the autodj harmonic guard) requires the
    # overlapped keys to share a relative-major tonic after the chosen shift.
    # Camelot-adjacent pairs score well but are not alignable, so they cut.
    if (
        source.tonic is not None
        and source.mode in {"major", "minor"}
        and target.tonic is not None
        and target.mode in {"major", "minor"}
    ):
        source_relative = major_equivalent_tonic(source.tonic % 12, source.mode)
        target_relative = major_equivalent_tonic((target.tonic + int(plan.pitch_shift_semitones or 0)) % 12, target.mode)
        if source_relative != target_relative:
            return None, "keys are not relative-tonic alignable within render limits"
    pitch_note = f"; pitch {plan.pitch_shift_semitones:+d}" if plan.pitch_shift_semitones else ""
    return plan, f"score {plan.score}; {plan.key_relation}; tempo {plan.target_tempo_shift_pct or 0.0:+.2f}%{pitch_note}"


class TempoLockedKeyPlan:
    def __init__(self, pitch_shift_semitones: int):
        self.pitch_shift_semitones = pitch_shift_semitones
        self.target_tempo_shift_pct = None
        self.score = 1.0
        self.key_relation = "tempo-locked"
        self.bpm_ratio = 1.0
        self.phrase_wait_beats = 0
        self.notes: list[str] = []


def tempo_locked_key_plan(
    source: TrackAnalysis | None,
    target: TrackAnalysis | None,
    max_pitch_shift_semitones: int,
    *,
    source_pitch_shift: int = 0,
) -> tuple[TempoLockedKeyPlan | None, str]:
    """Key alignment for a tempo-locked pair: tempos are already identical by
    authored shifts, so the only decision is the smallest pitch shift that
    aligns the incoming track's EFFECTIVE key (including the outgoing clip's
    own authored shift) to the outgoing one. No alignment within the render
    limit means cut."""
    if source is None or target is None:
        return None, "missing analysis"
    if (
        source.tonic is None
        or source.mode not in {"major", "minor"}
        or target.tonic is None
        or target.mode not in {"major", "minor"}
    ):
        return None, "missing key metadata"
    source_relative = major_equivalent_tonic((source.tonic + source_pitch_shift) % 12, source.mode)
    for magnitude in range(0, max_pitch_shift_semitones + 1):
        for shift in ({0} if magnitude == 0 else {magnitude, -magnitude}):
            if major_equivalent_tonic((target.tonic + shift) % 12, target.mode) == source_relative:
                pitch_note = f"; pitch {shift:+d}" if shift else ""
                return TempoLockedKeyPlan(shift), f"tempo-locked key fit{pitch_note}"
    return None, "keys are not relative-tonic alignable within render limits"


def tempo_locked_overlap_ms(
    source: TrackAnalysis | None,
    target: TrackAnalysis | None,
    shorter_ms: int,
    max_pitch_shift_semitones: int,
    *,
    source_pitch_shift: int = 0,
    stems_ready: bool = False,
) -> int:
    plan, _reason = tempo_locked_key_plan(source, target, max_pitch_shift_semitones, source_pitch_shift=source_pitch_shift)
    if plan is None:
        return 0
    phrase = int(phrase_ms(source) or 16_000)
    if stems_ready:
        # With split material on both sides the junction is an extended
        # same-record runway: the incoming record's OWN drums enter early and
        # the outgoing's OWN tail lingers — no cross-record feel gambling, so
        # the passage can run long. This is where dual-source time lives when
        # the taste-risky moves are reserved for agents.
        return max(32_000, min(6 * phrase, shorter_ms // 2, 96_000))
    return max(6_000, min(phrase, shorter_ms // 3))


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


def rendered_clip_factor(payload: dict[str, Any], clip: dict[str, Any], analysis: TrackAnalysis | None) -> float:
    """The rate the renderer actually consumes this clip's source at.

    Under a master tempo the warp lives in the knob, not tempo_shift_pct —
    beat math run at the authored shift (usually 0) drifted off the real grid
    by seconds at mid-record junctions."""
    if clip.get("warp", True):
        source_bpm = clip.get("source_bpm") or (analysis.bpm if analysis and analysis.bpm else None)
        if source_bpm:
            master = master_bpm_at(payload, int(clip.get("start_ms") or 0))
            if master:
                max_stretch = abs(float(payload.get("max_tempo_stretch_pct", DEFAULT_MAX_WARP_STRETCH_PCT)))
                shift = master_tempo_shift_pct(float(source_bpm), float(master), max_stretch)
                if shift is not None:
                    return 1.0 + shift / 100.0
    factor = 1.0 + float(clip.get("tempo_shift_pct") or 0.0) / 100.0
    return factor if factor > 0 else 1.0


def snap_to_clip_beat(
    timeline_ms: int,
    clip: dict[str, Any],
    analysis: TrackAnalysis | None,
    *,
    bars: bool = True,
    factor: float | None = None,
) -> int:
    """Snap a timeline instant onto a rendered clip's beat grid.

    Junctions placed by raw arithmetic land off the beat; an incoming record
    must arrive on the outgoing record's bar. Callers under a master tempo
    must pass the rendered factor (rendered_clip_factor)."""
    grid = getattr(analysis, "beatgrid", None) if analysis else None
    if grid is None or not grid.bpm or grid.beat_offset_ms is None:
        return timeline_ms
    if factor is None:
        factor = 1.0 + float(clip.get("tempo_shift_pct") or 0.0) / 100.0
    if factor <= 0:
        return timeline_ms
    start = int(clip.get("start_ms") or 0)
    trim = int(clip.get("trim_start_ms") or 0)
    step = (60_000.0 / float(grid.bpm)) * (4 if bars else 1)
    source_pos = trim + (timeline_ms - start) * factor
    k = round((source_pos - grid.beat_offset_ms) / step)
    return int(round(start + (grid.beat_offset_ms + k * step - trim) / factor))


def align_incoming_beat_phase(
    start_ms: int,
    incoming: dict[str, Any],
    incoming_analysis: TrackAnalysis | None,
    incoming_factor: float,
    outgoing: dict[str, Any],
    outgoing_analysis: TrackAnalysis | None,
    outgoing_factor: float,
) -> int:
    """Micro-shift the incoming record so its BEATS interlock with the
    outgoing's grid.

    Bar-snapping the file start is not enough: the incoming's first beat
    sits beat_offset_ms into the file, so its drums rode the whole runway up
    to half a beat off the outgoing's grid (heard live as 'simply very
    offbeat drums whenever multiple tracks play'). Both records render at
    the master tempo, so aligning one beat aligns them all."""
    import math

    grid = getattr(incoming_analysis, "beatgrid", None) if incoming_analysis else None
    if grid is None or not grid.bpm or grid.beat_offset_ms is None or incoming_factor <= 0:
        return start_ms
    trim = int(incoming.get("trim_start_ms") or 0)
    beat_src = 60_000.0 / float(grid.bpm)
    k = math.ceil((trim - float(grid.beat_offset_ms)) / beat_src)
    first_beat_src = float(grid.beat_offset_ms) + k * beat_src
    first_beat_tl = int(round(start_ms + (first_beat_src - trim) / incoming_factor))
    snapped = snap_to_clip_beat(first_beat_tl, outgoing, outgoing_analysis, bars=False, factor=outgoing_factor)
    return start_ms + (snapped - first_beat_tl)


def author_stem_handoff(
    planner_actions: list[dict[str, Any]],
    *,
    outgoing: dict[str, Any],
    incoming: dict[str, Any],
    overlap_start_ms: int,
    overlap_end_ms: int,
    modulated: bool = False,
) -> list[str]:
    """The remix handoff, authored by default on every blend with split material.

    The outgoing record's vocal steps out as the overlap begins (no stacked
    choruses, ever) and the incoming record enters drums+bass, opening to the
    full song at the end of the overlap — the classic bring-it-in-on-the-drums
    move. Toggles segment the loads, so everything outside the handoff still
    renders from the original files. Only deck loads with ready stem artifacts
    participate; everything else keeps the plain blend.
    """
    notes: list[str] = []

    def ready(clip: dict[str, Any]) -> bool:
        if not clip.get("source_action_id") or not clip.get("deck_clock_segment"):
            return False
        return ready_stem_artifacts(SESSION_LIBRARY_DB, str(clip.get("path") or "")) is not None

    def toggle(load_id: str, stem: str, enabled: bool, at_ms: int, suffix: str) -> None:
        planner_actions.append(
            {
                "type": "stem_toggle",
                "id": f"mix-{load_id}-{stem}-{suffix}",
                "target": load_id,
                "stem": stem,
                "enabled": enabled,
                "at_ms": int(at_ms),
                "planner_role": "mix-planner-stem-handoff",
            }
        )

    out_ready = ready(outgoing)
    in_ready = ready(incoming)
    if in_ready and overlap_end_ms - overlap_start_ms >= 24_000:
        # The extended double-deck runway, staged in quarters: incoming enters
        # drums-only under the outgoing (vocal already out); at 1/2 the bass
        # swaps hands (one low end at a time) and the outgoing strips its
        # melodics; at 3/4 the incoming opens melodics over the outgoing's
        # bare drum tail; full song on the boundary as the outgoing ends.
        # Every layered element is the record's OWN material — feel-safe.
        in_id = str(incoming["source_action_id"])
        span = overlap_end_ms - overlap_start_ms
        half_ms = overlap_start_ms + span // 2
        three_quarter_ms = overlap_start_ms + (3 * span) // 4
        if out_ready:
            out_id = str(outgoing["source_action_id"])
            toggle(out_id, "vocals", False, overlap_start_ms, "out")
            toggle(out_id, "bass", False, half_ms, "swap")
            toggle(out_id, "other", False, half_ms, "strip")
        toggle(in_id, "vocals", False, overlap_start_ms, "intro")
        toggle(in_id, "other", False, overlap_start_ms, "intro")
        toggle(in_id, "bass", False, overlap_start_ms, "intro")
        if modulated and not out_ready:
            # A modulated junction over an unsplit outgoing: the incoming's
            # tonal stems (in the NEW center) must not sound under the
            # outgoing's full song (old center) — drums carry the whole
            # runway and everything opens where the outgoing ends.
            toggle(in_id, "bass", True, overlap_end_ms, "swap")
            toggle(in_id, "other", True, overlap_end_ms, "melodics")
            notes.append("modulated runway: drums-only entry, full song opens with the new key on the boundary")
        else:
            toggle(in_id, "bass", True, half_ms, "swap")
            toggle(in_id, "other", True, three_quarter_ms, "melodics")
            notes.append(
                ("modulated " if modulated else "") + "extended runway: drums-first entry, bass swap at half, melodics at three-quarters, open on the boundary"
                + ("" if out_ready else " (incoming-only: outgoing unsplit)")
            )
        toggle(in_id, "vocals", True, overlap_end_ms, "open")
        return notes
    if out_ready:
        out_id = str(outgoing["source_action_id"])
        toggle(out_id, "vocals", False, overlap_start_ms, "out")
        notes.append("outgoing vocal steps out for the overlap")
    if in_ready:
        in_id = str(incoming["source_action_id"])
        toggle(in_id, "vocals", False, overlap_start_ms, "intro")
        toggle(in_id, "other", False, overlap_start_ms, "intro")
        toggle(in_id, "vocals", True, overlap_end_ms, "open")
        toggle(in_id, "other", True, overlap_end_ms, "open")
        notes.append("incoming enters drums+bass, opens at the phrase")
    return notes


def plan_future_mix(
    payload: dict[str, Any],
    analyses_by_path: dict[str, TrackAnalysis | dict],
    *,
    lock_before_ms: int,
    double_every: int = 0,
    max_tempo_shift_pct: float = MAX_RENDER_TEMPO_SHIFT_PCT,
    max_pitch_shift_semitones: int = MAX_RENDER_PITCH_SHIFT_SEMITONES,
    plan_until_ms: int | None = None,
    target_bpm: float | None = None,
) -> tuple[dict[str, Any], list[PlannedMove]]:
    original_payload = payload
    has_deck_loads = any(action_type(action) == "load_track" for action in payload.get("actions", []) or [])
    if has_deck_loads:
        # Loading is how songs play: the planner works on the compiled deck
        # clock, and its corrections flow back into the load_track actions
        # afterwards (write_back_deck_loads). The actions are consumed by the
        # compile — leaving them in the working copy would compile them a
        # second time at validation and duplicate every lead.
        payload = compile_actions_payload(copy.deepcopy(payload))
        payload.pop("actions", None)
        payload.pop("performance_actions", None)
    next_payload = copy.deepcopy(payload)
    if target_bpm is None and next_payload.get("master_bpm"):
        # The session owns tempo: a master_bpm session is tempo-locked whether
        # or not the caller passed the flag.
        target_bpm = float(next_payload["master_bpm"])
    normalize_clip_times(next_payload)
    next_payload["deck_automations"] = [
        automation
        for automation in next_payload.get("deck_automations", [])
        if automation.get("planner_role") not in {"mix-planner-filter-carve", "mix-planner-eq-carve"}
        or planner_entry_start_ms(automation) < lock_before_ms
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
    # Planner-authored key modulations beyond the lock rebuild each pass;
    # aired ones stay (they already pitched what the room heard).
    next_payload["master_key_automation"] = [
        point
        for point in next_payload.get("master_key_automation", []) or []
        if str(point.get("planner_role") or "") != "mix-planner-modulation"
        or parse_ms(point.get("at", point.get("at_ms")), "master key point") < lock_before_ms
    ]
    if not next_payload["master_key_automation"]:
        next_payload.pop("master_key_automation")
    analyses = {path: coerce_analysis(analysis) for path, analysis in analyses_by_path.items()}
    original_clips = sorted(next_payload.get("clips", []), key=lambda clip: (int(clip.get("start_ms", 0)), str(clip.get("deck")), str(clip.get("id"))))
    # Multi-segment loads (stem-toggled records) are IMMOVABLE in replans:
    # write_back keeps their actions authoritative and discards planner edits
    # to their segments, so any placement computed against a re-placed segment
    # never sticks — new leads planned that way landed inside the authoritative
    # segments and validation crashed (the tail-merge crash of tests 35-37).
    # Plan around them at their real positions instead.
    segment_owner_counts: dict[str, int] = {}
    for event in list(next_payload.get("clips", [])) + list(next_payload.get("stem_groups", []) or []):
        source = str(event.get("source_action_id") or "")
        if source and event.get("deck_clock_segment"):
            segment_owner_counts[source] = segment_owner_counts.get(source, 0) + 1

    def immovable(clip: dict[str, Any]) -> bool:
        source = str(clip.get("source_action_id") or "")
        return bool(source) and segment_owner_counts.get(source, 0) > 1

    locked = [clip for clip in original_clips if int(clip.get("start_ms", 0)) < lock_before_ms or immovable(clip)]
    after_horizon = [
        clip
        for clip in original_clips
        if plan_until_ms is not None and int(clip.get("start_ms", 0)) >= plan_until_ms and not immovable(clip)
    ]
    protected = [*locked, *after_horizon]
    future = [
        clip
        for clip in original_clips
        if int(clip.get("start_ms", 0)) >= lock_before_ms
        and (plan_until_ms is None or int(clip.get("start_ms", 0)) < plan_until_ms)
        and clip.get("kind") != "planner-double"
        and not immovable(clip)
    ]
    if not future:
        if has_deck_loads:
            return write_back_deck_loads(original_payload, next_payload, lock_before_ms=lock_before_ms), []
        return next_payload, []
    declared_decks = [str(deck) for deck in next_payload.get("decks", [])]
    deck_order = [deck for deck in DECK_ORDER if deck in declared_decks] + [deck for deck in declared_decks if deck not in DECK_ORDER]
    if not deck_order:
        deck_order = DECK_ORDER

    planned: list[PlannedMove] = []
    rebuilt: list[dict[str, Any]] = protected[:]
    planner_actions: list[dict[str, Any]] = []
    # Deck occupancy must include everything that sounds — the weave's groove
    # beds and teases are stem groups, invisible to a clips-only scan, and
    # deep overlaps exhaust the lead decks into whatever the scan can't see.
    stem_occupancy: list[dict[str, Any]] = [
        {"start_ms": int(g.get("start_ms") or 0), "duration_ms": int(g.get("duration_ms") or 0), "deck": g.get("deck"), "id": g.get("id")}
        for g in next_payload.get("stem_groups", [])
        if g.get("duration_ms")
    ]
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
        duration_ms = warp_aware_duration_ms(
            next_payload, clip, duration_ms, clip_end(previous) if previous is not None else cursor
        )
        clip["duration_ms"] = duration_ms
        shorter = min(duration_ms, int(previous.get("duration_ms") or duration_ms) if previous else duration_ms)
        # Blend vs cut is decided per pair: transition_overlap_ms returns 0
        # whenever analysis is missing or the pair cannot be made compatible
        # within the render limits, which keeps unsafe handoffs as hard cuts.
        # In a tempo-locked set every clip is authored to the same rendered
        # tempo, so tempo compatibility is free and only key fit decides.
        modulation_key: dict[str, Any] | None = None
        if target_bpm is not None:
            previous_shift = int(previous.get("pitch_shift_semitones") or 0) if previous else 0
            # The extended runway needs only the INCOMING record split (its own
            # drums carry the entry); the outgoing side adds its vocal-out and
            # strip moves when it too is ready. Requiring both sides halved
            # runway coverage for no musical reason.
            pair_stems_ready = (
                previous is not None
                and bool(clip.get("source_action_id"))
                and bool(clip.get("deck_clock_segment"))
                and ready_stem_artifacts(SESSION_LIBRARY_DB, str(clip.get("path") or "")) is not None
            )
            overlap = tempo_locked_overlap_ms(
                previous_analysis, analysis, shorter, max_pitch_shift_semitones,
                source_pitch_shift=previous_shift, stems_ready=pair_stems_ready,
            )
            # The master key is a ride, not a fence: a pair that won't align
            # within the render limit is the cue to MODULATE the center to the
            # incoming record, not to fall back to a hard cut. With split
            # material the junction still blends — the incoming enters on its
            # own drums (atonal) and its tonal stems wait for the modulation
            # boundary, so the two centers never sound together.
            if (
                overlap == 0
                and previous is not None
                and analysis is not None
                and analysis.tonic is not None
                and analysis.mode in ("major", "minor")
            ):
                modulation_key = {"tonic": int(analysis.tonic) % 12, "mode": str(analysis.mode)}
                if pair_stems_ready:
                    phrase = int(phrase_ms(previous_analysis) or 16_000)
                    overlap = max(32_000, min(6 * phrase, shorter // 2, 96_000))
        else:
            overlap = transition_overlap_ms(
                previous_analysis,
                analysis,
                shorter,
                max_tempo_shift_pct=max_tempo_shift_pct,
                max_pitch_shift_semitones=max_pitch_shift_semitones,
            )
        start_ms = cursor if previous is None else max(lock_before_ms, clip_end(previous) - overlap)
        if previous is not None:
            # The incoming record arrives on the outgoing record's bar — at
            # the RENDERED rate — and then micro-shifts so its beats (not its
            # file start) interlock with the outgoing's grid.
            out_factor = rendered_clip_factor(next_payload, previous, previous_analysis)
            in_factor = rendered_clip_factor(next_payload, clip, analysis)
            start_ms = max(lock_before_ms, snap_to_clip_beat(start_ms, previous, previous_analysis, factor=out_factor))
            start_ms = max(
                lock_before_ms,
                align_incoming_beat_phase(start_ms, clip, analysis, in_factor, previous, previous_analysis, out_factor),
            )
        end_ms = start_ms + duration_ms
        deck = choose_deck(rebuilt + stem_occupancy, start_ms, end_ms, deck_order=deck_order, avoid={previous_deck} if previous_deck else set())

        clip["start_ms"] = start_ms
        clip["deck"] = deck
        # Keep clip fades as click/entry protection. Long automatic fade-outs
        # make the lead record audibly sag even when no replacement move is
        # obvious; transition shape belongs in EQ/filter/crossfader automation.
        # No automatic fades, ever: the planner injecting fades on blends made
        # every boundary a surprise for the DJ (operator order 2026-07-04).
        # Blends are carried by the stem runway choreography and EQ carves;
        # fades are a deliberate DJ move on specific clips only.
        if target_bpm is not None:
            # Authored tempo locks are the arrangement; the planner only picks
            # the key alignment for overlaps and never rewrites tempo.
            if modulation_key is not None:
                key_label = f"{modulation_key['tonic']} {modulation_key['mode']}"
                if overlap:
                    plan = TempoLockedKeyPlan(0)
                    plan.key_relation = "modulated"
                    clip["pitch_shift_semitones"] = 0
                    reason = f"overlap {overlap}ms; tempo-locked; master key modulates to {key_label} with the incoming record"
                else:
                    plan = None
                    clip["start_ms"] = cursor if previous is None else max(lock_before_ms, clip_end(previous))
                    reason = f"cut; keys out of reach and incoming unsplit; master key modulates to {key_label} at the cut"
                # The center steps where the incoming record starts; keymatched
                # events pin the center at their own start, so the outgoing
                # keeps its pitch and everything after aligns to the new key.
                next_payload.setdefault("master_key_automation", []).append(
                    {
                        "at_ms": int(clip["start_ms"]),
                        "value": modulation_key,
                        "planner_role": "mix-planner-modulation",
                    }
                )
            else:
                plan, overlay_reason = (None, "tempo-locked cut") if not overlap else tempo_locked_key_plan(
                    previous_analysis, analysis, max_pitch_shift_semitones, source_pitch_shift=previous_shift
                )
                if plan is not None:
                    clip["pitch_shift_semitones"] = plan.pitch_shift_semitones
                    reason = f"overlap {overlap}ms; tempo-locked; {overlay_reason}"
                else:
                    overlap = 0
                    clip["start_ms"] = cursor if previous is None else max(lock_before_ms, clip_end(previous))
                    reason = f"cut; {overlay_reason}"
        else:
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
                # A cut is not permission to erase authored rendered
                # corrections; leave the clip's own tempo/pitch alone.
                reason = f"cut; {overlay_reason}"
        rebuilt.append(clip)
        planned.append(PlannedMove("blend" if overlap and plan is not None else "cut", str(clip.get("id")), start_ms, reason, str(previous.get("id")) if previous else None))
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
            if actual_overlap_ms >= 8_000:
                author_stem_handoff(
                    planner_actions,
                    outgoing=previous,
                    incoming=clip,
                    overlap_start_ms=start_ms,
                    overlap_end_ms=start_ms + actual_overlap_ms,
                    modulated=modulation_key is not None,
                )

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
                    double_deck = choose_deck(rebuilt + stem_occupancy, double_start, double_end, deck_order=deck_order, avoid={deck, previous_deck})
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
        # The NEXT record blends into whatever actually ends last: an
        # immovable (stem-toggled) load can outlast the clip just placed, and
        # ignoring it left replans planning against the wrong tail — a moved
        # lead landed 375ms over an immovable one as an accidental near-cut.
        in_play = [c for c in rebuilt if int(c.get("start_ms", 0)) <= end_ms]
        latest = max(in_play + [clip], key=clip_end)
        if latest is not clip and clip_end(latest) > clip_end(clip):
            previous = latest
            previous_analysis = analyses.get(str(latest.get("path")))
            previous_deck = str(latest.get("deck") or "")
        cursor = end_ms

    next_payload["clips"] = sorted(rebuilt, key=lambda clip: (int(clip.get("start_ms", 0)), str(clip.get("deck")), str(clip.get("id"))))
    next_payload["automations"] = [
        automation
        for automation in next_payload.get("automations", [])
        if not (automation.get("target") == "master" and automation.get("param") == "duck_volume" and automation.get("planner_role") == "mix-planner")
    ]
    parse_session(copy.deepcopy(next_payload))
    if planner_actions and has_deck_loads:
        # Attached AFTER validating the planned timeline: these toggles
        # reference the original load actions, which the compiled working
        # copy consumed — they only become parseable once write_back merges
        # them next to the loads they target (validated there).
        next_payload["actions"] = planner_actions
    if has_deck_loads:
        return write_back_deck_loads(original_payload, next_payload, lock_before_ms=lock_before_ms), planned
    return next_payload, planned


PLANNED_LOAD_WRITEBACK_FIELDS = (
    "deck",
    "duration_ms",
    "trim_start_ms",
    "fade_in_ms",
    "fade_out_ms",
    "pitch_shift_semitones",
    "tempo_shift_pct",
    "source_duration_ms",
)


def planner_entry_start_ms(entry: dict[str, Any]) -> int:
    for key in ("start_ms", "start", "at_ms", "at"):
        value = entry.get(key)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                try:
                    return parse_ms(value, key)
                except Exception:
                    return 0
    points = entry.get("points")
    if isinstance(points, list) and points:
        starts = [planner_entry_start_ms(p) for p in points if isinstance(p, dict)]
        if starts:
            return min(starts)
    return 0


def write_back_deck_loads(original: dict[str, Any], planned: dict[str, Any], *, lock_before_ms: int = 0) -> dict[str, Any]:
    """Planned corrections flow back into the load_track actions owning the leads.

    The planner computes on the compiled deck clock; the session's canonical
    authoring stays "loads on decks". Single-segment loads take the planned
    clip's fields verbatim; multi-segment loads (live performance moves) keep
    their actions authoritative and drop planner edits to their segments.
    Only the planner's own mix-planner-* transition plans and carves merge
    back — compiled materializations (knob rides, stem segments) must not
    duplicate into the stored session.
    """
    result = copy.deepcopy(original)
    actions_by_id: dict[str, dict[str, Any]] = {}
    for action in result.get("actions", []) or []:
        aid = str(action.get("id") or "")
        if aid and action_type(action) == "load_track":
            actions_by_id[aid] = action
    segment_counts: dict[str, int] = {}
    for event in list(planned.get("clips", []) or []) + list(planned.get("stem_groups", []) or []):
        source = str(event.get("source_action_id") or "")
        if source:
            segment_counts[source] = segment_counts.get(source, 0) + 1
    kept_clips: list[dict[str, Any]] = []
    for clip in planned.get("clips", []) or []:
        source = str(clip.get("source_action_id") or "")
        if clip.get("deck_clock_segment") and source in actions_by_id:
            if segment_counts.get(source, 0) == 1:
                action = actions_by_id[source]
                action["at_ms"] = int(clip.get("start_ms") or 0)
                action.pop("at", None)
                action.pop("duration", None)
                for field in PLANNED_LOAD_WRITEBACK_FIELDS:
                    if clip.get(field) is not None:
                        action[field] = clip[field]
            continue
        kept_clips.append(clip)
    result["clips"] = kept_clips
    for key, role_prefix in (
        ("transition_plans", "mix-planner"),
        ("deck_automations", "mix-planner"),
        ("automations", "mix-planner"),
        ("actions", "mix-planner"),
        # Weave teases pin to a host's position; a replan re-times the hosts
        # but kept the teases verbatim, so they landed over different records
        # in different keys (extend's guard caught two 24s clashes). Post-lock
        # arrangement entries drop with the replan — a missing tease beats a
        # stale one.
        ("actions", "arrangement-"),
        ("master_key_automation", "mix-planner"),
    ):
        # The planner may only rewrite its OWN entries at or beyond the lock:
        # dropping everything role-tagged erased the locked front junctions'
        # vocal choreography on every extend — five cold sets aired
        # stacked-vocal openings that had been authored correctly at launch.
        # Immovable (multi-segment) loads are not re-planned, so their
        # existing choreography must survive the strip too — the lock-only
        # rule erased every immovable lead's junction toggles on each extend
        # and nothing re-authored them (four bare junctions aired).
        immovable_targets = {source for source, count in segment_counts.items() if count > 1}
        original_entries = [
            entry
            for entry in result.get(key, []) or []
            if not str(entry.get("planner_role") or "").startswith(role_prefix)
            or planner_entry_start_ms(entry) < lock_before_ms
            or str(entry.get("target") or "") in immovable_targets
        ]
        planner_entries = [
            entry
            for entry in planned.get(key, []) or []
            if str(entry.get("planner_role") or "").startswith(role_prefix)
            and planner_entry_start_ms(entry) >= lock_before_ms
        ]
        result[key] = original_entries + planner_entries
    parse_session(result)
    return result


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
    # Loading is how songs play: analyze the compiled deck clock so
    # action-authored leads get their analyses too.
    if any(action_type(action) == "load_track" for action in payload.get("actions", []) or []):
        payload = compile_actions_payload(copy.deepcopy(payload))
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
    playhead = playhead_ms_from_state(state_path)
    return max(edit_lock_ms_from_state(state_path), playhead + lead_ms)


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
    parser.add_argument("--max-render-tempo-shift-pct", type=float, default=MAX_RENDER_TEMPO_SHIFT_PCT)
    parser.add_argument("--max-render-pitch-shift-semitones", type=int, default=MAX_RENDER_PITCH_SHIFT_SEMITONES)
    parser.add_argument("--cached-analysis-only", action="store_true", help="Use only cached DB analysis; missing tracks become explicit cut decisions.")
    parser.add_argument("--horizon-ms", type=int, help="Only rewrite future clips that begin before lock-before plus this horizon.")
    parser.add_argument(
        "--target-bpm",
        type=float,
        help="Tempo-locked set: clips are authored to one rendered tempo; the planner preserves those shifts and blends on key fit alone.",
    )
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
        max_tempo_shift_pct=args.max_render_tempo_shift_pct,
        max_pitch_shift_semitones=args.max_render_pitch_shift_semitones,
        plan_until_ms=plan_until_ms,
        target_bpm=args.target_bpm,
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
