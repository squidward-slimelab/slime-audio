#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import math
import os
import re
import subprocess
import tempfile
import urllib.request
from dataclasses import replace
from pathlib import Path
from typing import Any

from slime_audio_session import (
    DEFAULT_LIBRARY_DB,
    Automation,
    AutomationPoint,
    Clip,
    EffectEvent,
    MicLeanIn,
    MixSession,
    SlipEvent,
    StemGroup,
    StemState,
    load_payload,
    load_session,
    parse_ms,
    ready_stem_artifacts,
)


BED_ROLE_NAMES = {"rhythm-bed", "bed", "mashup-bed"}
DEFAULT_KOKORO_URL = os.environ.get("SLIME_AUDIO_KOKORO_URL", "http://robokrabs.tail4cb51.ts.net:7862")


def seconds(ms: int) -> str:
    return f"{ms / 1000:.3f}"


def shell_escape_filter(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def gain_multiplier(db: float) -> float:
    return 10 ** (db / 20)


def tempo_factor(clip: Clip) -> float:
    factor = 1 + (clip.tempo_shift_pct / 100)
    if factor <= 0:
        raise ValueError(f"clip {clip.id} tempo_shift_pct produces non-positive tempo")
    return factor


def atempo_filters(factor: float) -> list[str]:
    if factor <= 0:
        raise ValueError("tempo factor must be positive")
    filters = []
    remaining = factor
    while remaining > 2.0:
        filters.append("atempo=2.000000")
        remaining /= 2.0
    while remaining < 0.5:
        filters.append("atempo=0.500000")
        remaining /= 0.5
    filters.append(f"atempo={remaining:.6f}")
    return filters


def time_pitch_filters(clip: Clip, sample_rate: int) -> list[str]:
    filters: list[str] = []
    if clip.playback_rate != 1.0:
        filters.append(f"asetrate={max(1, int(round(sample_rate * clip.playback_rate)))}")
        filters.append(f"aresample={sample_rate}")
    if clip.pitch_shift_semitones:
        pitch_factor = 2 ** (clip.pitch_shift_semitones / 12)
        filters.append(f"asetrate={max(1, int(round(sample_rate * pitch_factor)))}")
        filters.append(f"aresample={sample_rate}")
        filters.extend(atempo_filters(1 / pitch_factor))
    if clip.tempo_shift_pct:
        filters.extend(atempo_filters(tempo_factor(clip)))
    return filters


def source_duration_ms(clip: Clip) -> int | None:
    if clip.duration_ms is None:
        return None
    return max(1, int(round(clip.duration_ms * tempo_factor(clip) * clip.playback_rate)))


def shift_automation_window(automation: Automation, from_ms: int) -> Automation | None:
    points = sorted(automation.points, key=lambda point: point.at_ms)
    shifted: list[AutomationPoint] = []
    last_before: AutomationPoint | None = None
    for point in points:
        if point.at_ms < from_ms:
            last_before = point
            continue
        if last_before is not None and not shifted:
            shifted.append(AutomationPoint(at_ms=0, value=last_before.value, curve=last_before.curve))
        shifted.append(replace(point, at_ms=point.at_ms - from_ms))
    if not shifted:
        return None
    return replace(automation, points=shifted)


def shift_session_window(session: MixSession, from_ms: int, duration_ms: int | None = None) -> MixSession:
    if from_ms <= 0 and duration_ms is None:
        return session
    window_end_ms = from_ms + duration_ms if duration_ms is not None else None

    clips: list[Clip] = []
    for clip in session.clips:
        if clip.end_ms is not None and clip.end_ms <= from_ms:
            continue
        if window_end_ms is not None and clip.start_ms >= window_end_ms:
            continue
        if clip.start_ms < from_ms and clip.duration_ms is None:
            continue
        overlap_ms = max(0, from_ms - clip.start_ms)
        duration_ms = clip.duration_ms - overlap_ms if clip.duration_ms is not None else None
        if duration_ms is not None and window_end_ms is not None:
            duration_ms = min(duration_ms, max(0, window_end_ms - max(clip.start_ms, from_ms)))
        if duration_ms is not None and duration_ms <= 0:
            continue
        shifted_automations = [
            shifted for automation in clip.automations if (shifted := shift_automation_window(automation, from_ms)) is not None
        ]
        clips.append(
            replace(
                clip,
                start_ms=max(0, clip.start_ms - from_ms),
                trim_start_ms=clip.trim_start_ms + int(round(overlap_ms * tempo_factor(clip))),
                duration_ms=duration_ms,
                automations=shifted_automations,
            )
        )

    stem_groups: list[StemGroup] = []
    for group in session.stem_groups:
        if group.end_ms is not None and group.end_ms <= from_ms:
            continue
        if window_end_ms is not None and group.start_ms >= window_end_ms:
            continue
        if group.start_ms < from_ms and group.duration_ms is None:
            continue
        overlap_ms = max(0, from_ms - group.start_ms)
        group_duration_ms = group.duration_ms - overlap_ms if group.duration_ms is not None else None
        if group_duration_ms is not None and window_end_ms is not None:
            group_duration_ms = min(group_duration_ms, max(0, window_end_ms - max(group.start_ms, from_ms)))
        if group_duration_ms is not None and group_duration_ms <= 0:
            continue
        shifted_automations = [
            shifted for automation in group.automations if (shifted := shift_automation_window(automation, from_ms)) is not None
        ]
        stem_groups.append(
            replace(
                group,
                start_ms=max(0, group.start_ms - from_ms),
                trim_start_ms=group.trim_start_ms + int(round(overlap_ms * tempo_factor(group))),
                duration_ms=group_duration_ms,
                automations=shifted_automations,
            )
        )

    mic_lean_ins = [
        replace(
            lean_in,
            start_ms=lean_in.start_ms - from_ms,
            effects=[
                shifted for effect in lean_in.effects if (shifted := shift_automation_window(effect, from_ms)) is not None
            ],
        )
        for lean_in in session.mic_lean_ins
        if lean_in.start_ms >= from_ms and (window_end_ms is None or lean_in.start_ms < window_end_ms)
    ]
    effects = [
        replace(
            effect,
            start_ms=max(0, effect.start_ms - from_ms),
            duration_ms=min(effect.duration_ms, max(0, (window_end_ms if window_end_ms is not None else effect.start_ms + effect.duration_ms) - max(effect.start_ms, from_ms))),
        )
        for effect in session.effects
        if effect.end_ms > from_ms and (window_end_ms is None or effect.start_ms < window_end_ms)
    ]
    effects = [effect for effect in effects if effect.duration_ms > 0]
    slip_events = [
        replace(
            event,
            start_ms=event.start_ms - from_ms,
        )
        for event in session.slip_events
        if event.end_ms > from_ms and (window_end_ms is None or event.start_ms < window_end_ms)
    ]
    automations = [
        shifted for automation in session.automations if (shifted := shift_automation_window(automation, from_ms)) is not None
    ]
    deck_automations = [
        shifted for automation in session.deck_automations if (shifted := shift_automation_window(automation, from_ms)) is not None
    ]
    return MixSession(
        version=session.version,
        decks=session.decks,
        clips=clips,
        stem_groups=stem_groups,
        mic_lean_ins=mic_lean_ins,
        effects=effects,
        automations=automations,
        deck_automations=deck_automations,
        slip_events=slip_events,
        fader_routing=session.fader_routing,
    )


def synthesize_kokoro(base_url: str, voice: str, text: str, output_path: Path, timeout: int) -> None:
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/tts",
        data=json.dumps({"text": text, "voice": voice}).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    output_path.write_bytes(base64.b64decode(payload["audio"]))


def normalize_tts(input_path: Path, output_path: Path, sample_rate: int, channels: int) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(input_path),
            "-af",
            "loudnorm=I=-15:TP=-1.5:LRA=8",
            "-acodec",
            "pcm_s16le",
            "-ac",
            str(channels),
            "-ar",
            str(sample_rate),
            str(output_path),
        ],
        check=True,
    )


def automation_window(lean_in: MicLeanIn, param: str, fallback_value: float) -> tuple[int, int, float]:
    effect = next((item for item in lean_in.effects if item.param == param), None)
    if effect is None or not effect.points:
        return max(0, lean_in.start_ms - 250), lean_in.start_ms + 3500, fallback_value
    start = effect.points[0].at_ms
    end = effect.points[-1].at_ms
    value = float(effect.points[0].value)
    return start, end, value


def collect_master_automation(session: MixSession, param: str) -> list[tuple[int, int, float]]:
    windows = []
    for lean_in in session.mic_lean_ins:
        windows.append(automation_window(lean_in, param, 0.45 if param == "duck_volume" else 1400.0))
    for automation in session.automations:
        if automation.target == "master" and automation.param == param:
            windows.extend(window_from_automation(automation))
    return sorted(windows, key=lambda item: item[0])


def session_duration_ms(session: MixSession, lean_in_default_ms: int = 5000) -> int:
    ends = [clip.end_ms for clip in session.clips if clip.end_ms is not None]
    ends.extend(group.end_ms for group in session.stem_groups if group.end_ms is not None)
    ends.extend(lean_in.start_ms + lean_in_default_ms for lean_in in session.mic_lean_ins)
    ends.extend(effect.end_ms for effect in session.effects)
    ends.extend(event.end_ms for event in session.slip_events)
    for param in ("duck_volume", "lowpass_hz"):
        ends.extend(end for _start, end, _value in collect_master_automation(session, param))
    for automation in session.deck_automations:
        if automation.points:
            ends.append(max(point.at_ms for point in automation.points))
    return max(ends, default=1000)


# Automation ramps render as piecewise-constant filter windows, so subdivide
# finely enough that steps are inaudible. 120 ms keeps even a fast 24 dB fader
# throw under ~1 dB per step; the cap bounds filter-graph size on long rides.
RAMP_STEP_MS = 120
MAX_RAMP_STEPS = 64


def subdivide_ramp(start_ms: int, end_ms: int, start_value: float, end_value: float) -> list[tuple[int, int, float]]:
    if end_ms <= start_ms:
        return []
    if start_value == end_value:
        return [(start_ms, end_ms, float(start_value))]
    duration_ms = end_ms - start_ms
    steps = max(1, min(MAX_RAMP_STEPS, -(-duration_ms // RAMP_STEP_MS)))
    windows: list[tuple[int, int, float]] = []
    for index in range(steps):
        step_start = start_ms + (duration_ms * index) // steps
        step_end = start_ms + (duration_ms * (index + 1)) // steps
        if step_end <= step_start:
            continue
        ratio = (index + 0.5) / steps
        windows.append((step_start, step_end, float(start_value + (end_value - start_value) * ratio)))
    return windows


def ramp_windows(points: list[AutomationPoint], *, offset_ms: int, clamp_end_ms: int | None = None) -> list[tuple[int, int, float]]:
    """Render automation point pairs as (start, end, value) windows in local time.

    Linear-curve segments are subdivided so the ramp is actually audible as
    motion; any other curve value holds the left point (step). This replaced a
    collapse-to-first-value behavior that rendered every knob ride as a locked
    value followed by a cliff.
    """
    ordered = sorted(points, key=lambda point: point.at_ms)
    windows: list[tuple[int, int, float]] = []
    for left, right in zip(ordered, ordered[1:]):
        start_ms = left.at_ms - offset_ms
        end_ms = right.at_ms - offset_ms
        if clamp_end_ms is not None:
            end_ms = min(end_ms, clamp_end_ms)
        start_ms = max(0, start_ms)
        end_ms = max(0, end_ms)
        if end_ms <= start_ms:
            continue
        if str(left.curve or "linear") == "linear":
            start_value = interpolate_automation_value(left, right, offset_ms + start_ms)
            end_value = interpolate_automation_value(left, right, offset_ms + end_ms)
            windows.extend(subdivide_ramp(start_ms, end_ms, start_value, end_value))
        else:
            windows.append((start_ms, end_ms, float(left.value)))
    return windows


def window_from_automation(automation: Automation) -> list[tuple[int, int, float]]:
    return ramp_windows(automation.points, offset_ms=0)


def interpolate_automation_value(left: AutomationPoint, right: AutomationPoint, at_ms: int) -> float:
    left_value = float(left.value)
    right_value = float(right.value)
    if right.at_ms <= left.at_ms:
        return right_value
    ratio = max(0.0, min(1.0, (at_ms - left.at_ms) / (right.at_ms - left.at_ms)))
    return left_value + ((right_value - left_value) * ratio)


def deck_automation_windows(session: MixSession, clip: Clip, param: str) -> list[tuple[int, int, float]]:
    if clip.end_ms is None:
        return []
    windows: list[tuple[int, int, float]] = []
    for automation in session.deck_automations:
        if automation.target != clip.deck or automation.param != param:
            continue
        windows.extend(
            ramp_windows(automation.points, offset_ms=clip.start_ms, clamp_end_ms=clip.end_ms - clip.start_ms)
        )
    return sorted(windows, key=lambda item: item[0])


def event_deck_automation_windows(session: MixSession, deck: str, start_ms: int, end_ms: int | None, param: str) -> list[tuple[int, int, float]]:
    if end_ms is None:
        return []
    windows: list[tuple[int, int, float]] = []
    for automation in session.deck_automations:
        if automation.target != deck or automation.param != param:
            continue
        windows.extend(ramp_windows(automation.points, offset_ms=start_ms, clamp_end_ms=end_ms - start_ms))
    return sorted(windows, key=lambda item: item[0])


def clip_automation_windows(session: MixSession, clip: Clip, param: str) -> list[tuple[int, int, float]]:
    windows: list[tuple[int, int, float]] = []
    automations = [*clip.automations, *(automation for automation in session.automations if automation.target == clip.id)]
    for automation in automations:
        if automation.param != param:
            continue
        windows.extend(ramp_windows(automation.points, offset_ms=clip.start_ms))
    windows.extend(deck_automation_windows(session, clip, param))
    return sorted(windows, key=lambda item: item[0])


def group_automation_windows(session: MixSession, group: StemGroup, param: str) -> list[tuple[int, int, float]]:
    windows: list[tuple[int, int, float]] = []
    automations = [*group.automations, *(automation for automation in session.automations if automation.target == group.id)]
    for automation in automations:
        if automation.param != param:
            continue
        windows.extend(ramp_windows(automation.points, offset_ms=group.start_ms))
    windows.extend(event_deck_automation_windows(session, group.deck, group.start_ms, group.end_ms, param))
    return sorted(windows, key=lambda item: item[0])


def stem_automation_windows(session: MixSession, group: StemGroup, stem_name: str, stem: StemState, param: str) -> list[tuple[int, int, float]]:
    target = f"stem-group:{group.id}:{stem_name}"
    points: list[Any] = []
    for automation in [*stem.automations, *(item for item in session.automations if item.target == target)]:
        if automation.param != param:
            continue
        points.extend(automation.points)
    points = sorted(points, key=lambda point: point.at_ms)
    windows: list[tuple[int, int, float]] = []
    if not points:
        return windows
    group_end_ms = group.end_ms if group.end_ms is not None else points[-1].at_ms
    if param in {"mute", "solo"}:
        # Toggles are steps: each point's state holds until the next point.
        for index, point in enumerate(points):
            next_at_ms = points[index + 1].at_ms if index + 1 < len(points) else group_end_ms
            start_ms = max(0, point.at_ms - group.start_ms)
            end_ms = max(0, next_at_ms - group.start_ms)
            if end_ms <= start_ms:
                continue
            windows.append((start_ms, end_ms, 1.0 if bool(point.value) else 0.0))
        return sorted(windows, key=lambda item: item[0])
    windows.extend(ramp_windows(points, offset_ms=group.start_ms))
    # Hold the final value through the end of the group like a released knob.
    last_start_ms = max(0, points[-1].at_ms - group.start_ms)
    last_end_ms = max(0, group_end_ms - group.start_ms)
    if last_end_ms > last_start_ms:
        windows.append((last_start_ms, last_end_ms, float(points[-1].value)))
    return sorted(windows, key=lambda item: item[0])


def crossfader_gain(position: float, side: str) -> float:
    position = max(-1.0, min(1.0, position))
    side = side.upper()
    if side == "A":
        return 1.0 if position <= 0 else max(0.0, 1.0 - position)
    if side == "B":
        return 1.0 if position >= 0 else max(0.0, 1.0 + position)
    return 1.0


def split_crossfader_segment(start_ms: int, end_ms: int, start_position: float, end_position: float) -> list[tuple[int, int, float, float]]:
    if end_ms <= start_ms:
        return []
    if start_position == end_position or (start_position <= 0 <= end_position) is False and (end_position <= 0 <= start_position) is False:
        return [(start_ms, end_ms, start_position, end_position)]
    crossing = start_ms + int(round((0 - start_position) / (end_position - start_position) * (end_ms - start_ms)))
    if crossing <= start_ms or crossing >= end_ms:
        return [(start_ms, end_ms, start_position, end_position)]
    return [(start_ms, crossing, start_position, 0.0), (crossing, end_ms, 0.0, end_position)]


def interpolate_crossfader_position(segment: tuple[int, int, float, float, int], at_ms: int) -> float:
    start_ms, end_ms, start_position, end_position, _order = segment
    if end_ms <= start_ms:
        return end_position
    ratio = max(0.0, min(1.0, (at_ms - start_ms) / (end_ms - start_ms)))
    return start_position + ((end_position - start_position) * ratio)


def crossfader_position_segments(session: MixSession) -> list[tuple[int, int, float, float]]:
    source_segments: list[tuple[int, int, float, float, int]] = []
    for order, automation in enumerate(session.automations):
        if automation.target != "crossfader" or automation.param != "position" or len(automation.points) < 2:
            continue
        points = sorted(automation.points, key=lambda point: point.at_ms)
        for left, right in zip(points, points[1:]):
            if right.at_ms <= left.at_ms:
                continue
            source_segments.append((left.at_ms, right.at_ms, float(left.value), float(right.value), order))
    if not source_segments:
        return []
    breakpoints = sorted({point for segment in source_segments for point in (segment[0], segment[1])})
    resolved: list[tuple[int, int, float, float]] = []
    for start_ms, end_ms in zip(breakpoints, breakpoints[1:]):
        active = [
            segment
            for segment in source_segments
            if segment[0] <= start_ms and end_ms <= segment[1]
        ]
        if not active:
            continue
        segment = max(active, key=lambda item: item[4])
        resolved.append(
            (
                start_ms,
                end_ms,
                interpolate_crossfader_position(segment, start_ms),
                interpolate_crossfader_position(segment, end_ms),
            )
        )
    return resolved


def clip_crossfader_windows(session: MixSession, clip: Clip) -> list[tuple[int, int, float, float]]:
    side = session.fader_routing.get(clip.deck, "THRU").upper()
    if side == "THRU":
        return []
    windows: list[tuple[int, int, float, float]] = []
    for absolute_start_ms, absolute_end_ms, start_position, end_position in crossfader_position_segments(session):
        overlap_start_ms = max(absolute_start_ms, clip.start_ms)
        overlap_end_ms = absolute_end_ms if clip.end_ms is None else min(absolute_end_ms, clip.end_ms)
        if overlap_end_ms <= overlap_start_ms:
            continue
        absolute_segment = (absolute_start_ms, absolute_end_ms, start_position, end_position, 0)
        overlap_start_position = interpolate_crossfader_position(absolute_segment, overlap_start_ms)
        overlap_end_position = interpolate_crossfader_position(absolute_segment, overlap_end_ms)
        start_ms = max(0, overlap_start_ms - clip.start_ms)
        end_ms = max(0, overlap_end_ms - clip.start_ms)
        for split_start, split_end, split_left, split_right in split_crossfader_segment(
            start_ms,
            end_ms,
            overlap_start_position,
            overlap_end_position,
        ):
            windows.append(
                (
                    split_start,
                    split_end,
                    crossfader_gain(split_left, side),
                    crossfader_gain(split_right, side),
                )
            )
    return sorted(windows, key=lambda item: item[0])


def group_crossfader_windows(session: MixSession, group: StemGroup) -> list[tuple[int, int, float, float]]:
    side = session.fader_routing.get(group.deck, "THRU").upper()
    if side == "THRU":
        return []
    windows: list[tuple[int, int, float, float]] = []
    for absolute_start_ms, absolute_end_ms, start_position, end_position in crossfader_position_segments(session):
        overlap_start_ms = max(absolute_start_ms, group.start_ms)
        overlap_end_ms = absolute_end_ms if group.end_ms is None else min(absolute_end_ms, group.end_ms)
        if overlap_end_ms <= overlap_start_ms:
            continue
        absolute_segment = (absolute_start_ms, absolute_end_ms, start_position, end_position, 0)
        overlap_start_position = interpolate_crossfader_position(absolute_segment, overlap_start_ms)
        overlap_end_position = interpolate_crossfader_position(absolute_segment, overlap_end_ms)
        start_ms = max(0, overlap_start_ms - group.start_ms)
        end_ms = max(0, overlap_end_ms - group.start_ms)
        for split_start, split_end, split_left, split_right in split_crossfader_segment(start_ms, end_ms, overlap_start_position, overlap_end_position):
            windows.append((split_start, split_end, crossfader_gain(split_left, side), crossfader_gain(split_right, side)))
    return sorted(windows, key=lambda item: item[0])


def clip_effect_filters(session: MixSession, clip: Clip) -> str:
    filters: list[str] = []
    for effect in session.effects:
        if effect.type != "vinyl_brake" or clip not in effect_target_clips(session, effect):
            continue
        start_ms = max(0, effect.start_ms - clip.start_ms)
        end_ms = max(start_ms, min((clip.end_ms or effect.end_ms) - clip.start_ms, effect.end_ms - clip.start_ms))
        if end_ms > start_ms:
            filters.append(f"volume=enable='between(t,{seconds(start_ms)},{seconds(end_ms)})':volume=0.000000")
    for start_ms, end_ms, lowpass_hz in clip_automation_windows(session, clip, "lowpass_hz"):
        filters.append(f"lowpass=enable='between(t,{seconds(start_ms)},{seconds(end_ms)})':f={lowpass_hz:.3f}")
    for start_ms, end_ms, highpass_hz in clip_automation_windows(session, clip, "highpass_hz"):
        filters.append(f"highpass=enable='between(t,{seconds(start_ms)},{seconds(end_ms)})':f={highpass_hz:.3f}")
    for start_ms, end_ms, eq_low_db in clip_automation_windows(session, clip, "eq_low_db"):
        filters.append(f"bass=enable='between(t,{seconds(start_ms)},{seconds(end_ms)})':g={eq_low_db:.3f}:f=120:w=0.7")
    for start_ms, end_ms, eq_mid_db in clip_automation_windows(session, clip, "eq_mid_db"):
        filters.append(
            f"equalizer=enable='between(t,{seconds(start_ms)},{seconds(end_ms)})':f=1000:t=q:w=1.0:g={eq_mid_db:.3f}"
        )
    for start_ms, end_ms, eq_high_db in clip_automation_windows(session, clip, "eq_high_db"):
        filters.append(f"treble=enable='between(t,{seconds(start_ms)},{seconds(end_ms)})':g={eq_high_db:.3f}:f=6500:w=0.7")
    for start_ms, end_ms, gain_db in clip_automation_windows(session, clip, "gain_db"):
        filters.append(
            f"volume=enable='between(t,{seconds(start_ms)},{seconds(end_ms)})':volume={gain_multiplier(gain_db):.6f}"
        )
    for start_ms, end_ms, start_gain, end_gain in clip_crossfader_windows(session, clip):
        if abs(start_gain - end_gain) < 0.000001:
            volume_expr = f"{start_gain:.6f}"
            eval_mode = ""
        else:
            duration = max(1, end_ms - start_ms)
            slope = (end_gain - start_gain) / (duration / 1000)
            volume_expr = f"'{start_gain:.6f}+({slope:.9f})*(t-{seconds(start_ms)})'"
            eval_mode = ":eval=frame"
        filters.append(
            f"volume=enable='between(t,{seconds(start_ms)},{seconds(end_ms)})':volume={volume_expr}{eval_mode}"
        )
    return ",".join(filters)


def effect_target_clips(session: MixSession, effect: EffectEvent) -> list[Clip]:
    if effect.target.startswith("deck:"):
        deck = effect.target.split(":", 1)[1]
        return [clip for clip in session.clips if clip.deck == deck and clip.start_ms < effect.end_ms and effect.start_ms < (clip.end_ms or clip.start_ms)]
    if effect.target in {"master", "all"}:
        return [clip for clip in session.clips if clip.start_ms < effect.end_ms and effect.start_ms < (clip.end_ms or clip.start_ms)]
    return [clip for clip in session.clips if clip.id == effect.target]


def manifest_stem_paths(group: StemGroup) -> dict[str, str]:
    if not group.manifest_path:
        return {}
    manifest_path = Path(group.manifest_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    stems = payload.get("stems") or {}
    result: dict[str, str] = {}
    for stem_name, stem_payload in stems.items():
        if not isinstance(stem_payload, dict) or not stem_payload.get("path"):
            continue
        path = Path(str(stem_payload["path"]))
        result[str(stem_name)] = str(path if path.is_absolute() else manifest_path.parent / path)
    return result


def stem_group_inputs(group: StemGroup) -> list[tuple[str, StemState, str]]:
    manifest_paths = manifest_stem_paths(group)
    active_items = [
        (name, stem)
        for name, stem in group.stems.items()
        if (stem.enabled and not stem.mute) or bool(stem.automations)
    ]
    solo_items = [(name, stem) for name, stem in active_items if stem.solo]
    selected = solo_items or active_items
    inputs: list[tuple[str, StemState, str]] = []
    for stem_name, stem in selected:
        path = stem.path or manifest_paths.get(stem_name)
        if path:
            inputs.append((stem_name, stem, path))
    return inputs


def stem_static_filters(stem: StemState) -> list[str]:
    filters: list[str] = []
    if stem.lowpass_hz is not None:
        filters.append(f"lowpass=f={stem.lowpass_hz:.3f}")
    if stem.highpass_hz is not None:
        filters.append(f"highpass=f={stem.highpass_hz:.3f}")
    if stem.eq_low_db:
        filters.append(f"bass=g={stem.eq_low_db:.3f}:f=120:w=0.7")
    if stem.eq_mid_db:
        filters.append(f"equalizer=f=1000:t=q:w=1.0:g={stem.eq_mid_db:.3f}")
    if stem.eq_high_db:
        filters.append(f"treble=g={stem.eq_high_db:.3f}:f=6500:w=0.7")
    return filters


def stem_dynamic_filters(session: MixSession, group: StemGroup, stem_name: str, stem: StemState) -> list[str]:
    filters: list[str] = []
    for start_ms, end_ms, lowpass_hz in stem_automation_windows(session, group, stem_name, stem, "lowpass_hz"):
        filters.append(f"lowpass=enable='between(t,{seconds(start_ms)},{seconds(end_ms)})':f={lowpass_hz:.3f}")
    for start_ms, end_ms, highpass_hz in stem_automation_windows(session, group, stem_name, stem, "highpass_hz"):
        filters.append(f"highpass=enable='between(t,{seconds(start_ms)},{seconds(end_ms)})':f={highpass_hz:.3f}")
    for start_ms, end_ms, eq_low_db in stem_automation_windows(session, group, stem_name, stem, "eq_low_db"):
        filters.append(f"bass=enable='between(t,{seconds(start_ms)},{seconds(end_ms)})':g={eq_low_db:.3f}:f=120:w=0.7")
    for start_ms, end_ms, eq_mid_db in stem_automation_windows(session, group, stem_name, stem, "eq_mid_db"):
        filters.append(f"equalizer=enable='between(t,{seconds(start_ms)},{seconds(end_ms)})':f=1000:t=q:w=1.0:g={eq_mid_db:.3f}")
    for start_ms, end_ms, eq_high_db in stem_automation_windows(session, group, stem_name, stem, "eq_high_db"):
        filters.append(f"treble=enable='between(t,{seconds(start_ms)},{seconds(end_ms)})':g={eq_high_db:.3f}:f=6500:w=0.7")
    for start_ms, end_ms, gain_db in stem_automation_windows(session, group, stem_name, stem, "gain_db"):
        filters.append(f"volume=enable='between(t,{seconds(start_ms)},{seconds(end_ms)})':volume={gain_multiplier(gain_db):.6f}")
    for start_ms, end_ms, muted in stem_automation_windows(session, group, stem_name, stem, "mute"):
        if muted:
            filters.append(f"volume=enable='between(t,{seconds(start_ms)},{seconds(end_ms)})':volume=0.000000")
    return filters


def convolution_reverb_filter() -> str:
    """afir configured as a plain unity-gain convolution.

    The reverb's musical interface lives on EffectEvent (room_size, damping,
    pre-delay via delay_ms, feedback as reverberance, wet, gain_db) and in the
    synthesized impulse response; this helper exists to hide afir's quirks in
    exactly one place:

    - afir outputs convolved (wet) signal only. Its `dry` option is the INPUT
      gain into the convolution, not a dry-mix control: dry=0 silences the
      effect entirely. Unity in/out is dry=1:wet=1.
    - gtype=none: the synthesized IR is already unit-energy normalized; afir's
      default peak normalization (gtype=peak) crushes it ~30 dB into
      inaudibility.

    The wet/dry musical balance is applied by volume filters after this.
    """
    return "afir=dry=1:wet=1:gtype=none"


def reverb_ir_parameters(effect: EffectEvent) -> dict[str, float]:
    room = max(0.1, min(1.0, effect.room_size))
    damping = max(0.0, min(1.0, effect.damping))
    pre_delay_s = max(0.02, min(0.1, effect.delay_ms / 1000.0))
    reverberance = max(0.0, min(1.0, effect.feedback))
    rt60_s = max(1.0, min(8.0, 1.35 + room * 1.55 + reverberance * 1.05))
    damping_hz = max(1500.0, min(24000.0, 24000.0 * ((1500.0 / 24000.0) ** damping)))
    return {"pre_delay_s": pre_delay_s, "rt60_s": rt60_s, "damping_hz": damping_hz}


def write_reverb_ir(effect: EffectEvent, path: Path, sample_rate: int, channels: int) -> None:
    """Synthesize a convolution impulse response from the effect parameters.

    Exponentially decaying decorrelated noise with a one-pole lowpass whose
    cutoff follows the damping control. Deterministically seeded so identical
    effect settings always render identical reverb; no external plugins.
    """
    import random
    import struct
    import wave

    params = reverb_ir_parameters(effect)
    pre_delay_samples = int(params["pre_delay_s"] * sample_rate)
    # Render the IR long enough for a -60 dB tail but keep convolution bounded.
    ir_seconds = min(4.0, params["rt60_s"])
    body_samples = int(ir_seconds * sample_rate)
    decay_per_sample = 10 ** (-3.0 / (params["rt60_s"] * sample_rate))
    alpha = 1.0 - math.exp(-2.0 * math.pi * params["damping_hz"] / sample_rate)
    rng = random.Random(f"{params['pre_delay_s']}:{params['rt60_s']}:{params['damping_hz']}:{channels}")
    channel_samples: list[list[float]] = []
    for _channel in range(channels):
        state = 0.0
        envelope = 1.0
        samples = [0.0] * pre_delay_samples
        for _ in range(body_samples):
            state += alpha * (rng.uniform(-1.0, 1.0) - state)
            samples.append(state * envelope)
            envelope *= decay_per_sample
        channel_samples.append(samples)
    # Normalize to unit energy so convolution is roughly level-preserving and
    # the effect's wet control keeps its meaning.
    energy = math.sqrt(sum(value * value for samples in channel_samples for value in samples) / channels)
    scale = (1.0 / energy) if energy > 0 else 1.0
    frame_count = pre_delay_samples + body_samples
    frames = bytearray()
    for index in range(frame_count):
        for samples in channel_samples:
            value = max(-1.0, min(1.0, samples[index] * scale))
            frames += struct.pack("<h", int(value * 32767))
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(channels)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(bytes(frames))


def prepare_reverb_irs(session: MixSession, ir_dir: Path, sample_rate: int, channels: int) -> dict[str, Path]:
    irs: dict[str, Path] = {}
    for effect in session.effects:
        if effect.type != "reverb":
            continue
        path = ir_dir / f"reverb-ir-{effect.id}.wav"
        write_reverb_ir(effect, path, sample_rate, channels)
        irs[effect.id] = path
    return irs


def vinyl_brake_stream_filter(effect: EffectEvent, clip: Clip, input_index: int, label: str, sample_rate: int, channels: int) -> str:
    relative_start_ms = max(0, effect.start_ms - clip.start_ms)
    source_start_ms = clip.trim_start_ms + int(round(relative_start_ms * tempo_factor(clip)))
    total_ms = max(1, effect.duration_ms)
    slices = max(18, min(72, total_ms // 15))
    slice_ms = total_ms / slices
    source_cursor_ms = float(source_start_ms)
    parts: list[str] = []
    labels: list[str] = []
    for index in range(slices):
        progress = (index + 0.5) / slices
        speed = max(0.045, (1.0 - progress) ** 1.9)
        out_ms = slice_ms if index < slices - 1 else total_ms - (slice_ms * (slices - 1))
        source_ms = max(2.0, out_ms * speed * tempo_factor(clip))
        part_label = f"{label}part{index}"
        volume = max(0.0, (1.0 - progress) ** 0.65)
        lowpass_hz = max(900.0, 18000.0 * ((900.0 / 18000.0) ** progress))
        fade_ms = min(3.0, max(0.5, out_ms / 5))
        fade_seconds = fade_ms / 1000
        fade_out_start = max(0.0, (out_ms / 1000) - fade_seconds)
        parts.append(
            f"[{input_index}:a]"
            f"atrim=start={seconds(int(round(source_cursor_ms)))}:duration={source_ms / 1000:.3f},"
            "asetpts=PTS-STARTPTS,"
            f"asetrate={max(1, int(round(sample_rate * speed)))},"
            f"aresample={sample_rate},"
            f"atrim=duration={out_ms / 1000:.3f},"
            f"lowpass=f={lowpass_hz:.3f},"
            f"afade=t=in:st=0:d={fade_seconds:.4f},"
            f"afade=t=out:st={fade_out_start:.4f}:d={fade_seconds:.4f},"
            f"volume={volume:.6f},"
            f"aformat=sample_rates={sample_rate}:channel_layouts={'stereo' if channels == 2 else 'mono'}"
            f"[{part_label}]"
        )
        labels.append(f"[{part_label}]")
        source_cursor_ms += source_ms
    parts.append(
        f"{''.join(labels)}concat=n={slices}:v=0:a=1,"
        f"volume={effect.wet:.6f},"
        f"volume={gain_multiplier(effect.gain_db):.6f},"
        f"adelay={effect.start_ms}:all=1,"
        f"aformat=sample_rates={sample_rate}:channel_layouts={'stereo' if channels == 2 else 'mono'}[{label}]"
    )
    return ";".join(parts)


def echo_stream_filter(effect: EffectEvent, clip: Clip, input_index: int, label: str, sample_rate: int, channels: int) -> str:
    relative_start_ms = max(0, effect.start_ms - clip.start_ms)
    source_start_ms = clip.trim_start_ms + int(round(relative_start_ms * tempo_factor(clip)))
    source_duration_ms = max(1, int(round(effect.duration_ms * tempo_factor(clip))))
    total_duration_ms = effect.duration_ms + effect.tail_ms
    delay_ms = max(1, effect.delay_ms)
    tap_count = max(1, min(10, (max(delay_ms + 1, total_duration_ms) - 1) // delay_ms))
    tap_sources = [f"echo{label}{index}src" for index in range(tap_count)]
    tap_outputs = [f"echo{label}{index}" for index in range(tap_count)]
    retime = ",".join(time_pitch_filters(clip, sample_rate))
    retime = f"{retime}," if retime else ""
    parts = [
        f"[{input_index}:a]atrim=start={seconds(source_start_ms)}:duration={seconds(source_duration_ms)},"
        "asetpts=PTS-STARTPTS,"
        f"{retime}"
        f"aformat=sample_rates={sample_rate}:channel_layouts={'stereo' if channels == 2 else 'mono'},"
        f"asplit={tap_count}{''.join(f'[{source}]' for source in tap_sources)}"
    ]
    for index, (source, output) in enumerate(zip(tap_sources, tap_outputs), start=1):
        tap_delay_ms = delay_ms * index
        tap_gain = effect.wet * (effect.feedback ** (index - 1)) * gain_multiplier(effect.gain_db)
        # Bound each tap BEFORE adelay: atrim placed after adelay discards the
        # inserted delay silence, which used to land every echo tap at t=0
        # under the dry signal instead of at its delay time.
        tap_budget_ms = max(1, total_duration_ms - tap_delay_ms)
        parts.append(
            f"[{source}]volume={tap_gain:.6f},"
            f"atrim=duration={seconds(tap_budget_ms)},"
            f"adelay={effect.start_ms + tap_delay_ms}:all=1,"
            f"aformat=sample_rates={sample_rate}:channel_layouts={'stereo' if channels == 2 else 'mono'}[{output}]"
        )
    post_filters = []
    if effect.lowpass_hz is not None:
        post_filters.append(f"lowpass=f={effect.lowpass_hz:.3f}")
    parts.append(
        f"{''.join(f'[{output}]' for output in tap_outputs)}"
        f"amix=inputs={tap_count}:duration=longest:normalize=0{',' + ','.join(post_filters) if post_filters else ''}[{label}]"
    )
    return ";".join(parts)


def effect_stream_filter(
    effect: EffectEvent,
    clip: Clip,
    input_index: int,
    label: str,
    sample_rate: int,
    channels: int,
    reverb_ir_indices: dict[str, int] | None = None,
) -> str:
    if effect.type == "vinyl_brake":
        return vinyl_brake_stream_filter(effect, clip, input_index, label, sample_rate, channels)
    if effect.type == "echo":
        return echo_stream_filter(effect, clip, input_index, label, sample_rate, channels)
    relative_start_ms = max(0, effect.start_ms - clip.start_ms)
    source_start_ms = clip.trim_start_ms + int(round(relative_start_ms * tempo_factor(clip)))
    source_duration_ms = max(1, int(round(effect.duration_ms * tempo_factor(clip))))
    total_duration_ms = effect.duration_ms + effect.tail_ms
    filters = [
        f"[{input_index}:a]atrim=start={seconds(source_start_ms)}:duration={seconds(source_duration_ms)}",
        "asetpts=PTS-STARTPTS",
    ]
    retime = time_pitch_filters(clip, sample_rate)
    filters.extend(retime)
    if effect.tail_ms:
        filters.append(f"apad=pad_dur={seconds(effect.tail_ms)}")
    if effect.type == "reverb":
        ir_index = (reverb_ir_indices or {}).get(effect.id)
        if ir_index is None:
            raise ValueError(f"reverb effect {effect.id} has no prepared impulse response input")
        filters.append(f"atrim=duration={seconds(total_duration_ms)}")
        filters.append(f"aformat=sample_rates={sample_rate}:channel_layouts={'stereo' if channels == 2 else 'mono'}[{label}dry]")
        segment = ",".join(filter(None, filters))
        post = [
            f"atrim=duration={seconds(total_duration_ms)}",
            f"lowpass=f={effect.lowpass_hz:.3f}" if effect.lowpass_hz is not None else "",
            f"volume={effect.wet:.6f}",
            f"volume={gain_multiplier(effect.gain_db):.6f}",
            f"adelay={effect.start_ms}:all=1",
            f"aformat=sample_rates={sample_rate}:channel_layouts={'stereo' if channels == 2 else 'mono'}[{label}]",
        ]
        convolve = f"[{label}dry][{ir_index}:a]{convolution_reverb_filter()}," + ",".join(filter(None, post))
        return ";".join([segment, convolve])
    filters.append(f"atrim=duration={seconds(total_duration_ms)}")
    if effect.lowpass_hz is not None:
        filters.append(f"lowpass=f={effect.lowpass_hz:.3f}")
    filters.extend(
        [
            f"volume={gain_multiplier(effect.gain_db):.6f}",
            f"adelay={effect.start_ms}:all=1",
            f"aformat=sample_rates={sample_rate}:channel_layouts={'stereo' if channels == 2 else 'mono'}[{label}]",
        ]
    )
    return ",".join(filter(None, filters))


def build_filter_complex(
    session: MixSession,
    lean_in_audio: dict[str, Path],
    sample_rate: int,
    channels: int,
    output_duration_ms: int | None = None,
    clip_input_indices: dict[int, int] | None = None,
    stem_group_input_indices: dict[tuple[int, str], int] | None = None,
    first_lean_input_index: int | None = None,
    reverb_ir_indices: dict[str, int] | None = None,
) -> str:
    active_lean_in_ids = set(lean_in_audio)
    session = replace(session, mic_lean_ins=[lean_in for lean_in in session.mic_lean_ins if lean_in.id in active_lean_in_ids])
    filters: list[str] = []
    music_labels: list[str] = []
    for index, clip in enumerate(session.clips):
        input_index = clip_input_indices[index] if clip_input_indices is not None else index
        source_duration = source_duration_ms(clip)
        duration = f":duration={seconds(source_duration)}" if source_duration is not None else ""
        label = f"music{index}"
        volume = gain_multiplier(clip.trim_db) * gain_multiplier(clip.gain_db)
        fade_in_ms = clip.fade_in_ms
        fade_out_ms = clip.fade_out_ms
        fade_filters = ""
        if fade_in_ms:
            fade_filters += f"afade=t=in:st=0:d={seconds(fade_in_ms)},"
        if fade_out_ms and clip.duration_ms is not None:
            fade_start_ms = max(0, clip.duration_ms - fade_out_ms)
            fade_filters += f"afade=t=out:st={seconds(fade_start_ms)}:d={seconds(fade_out_ms)},"
        retime_filters = ",".join(time_pitch_filters(clip, sample_rate))
        retime_filters = f"{retime_filters}," if retime_filters else ""
        reverse_filter = "areverse," if clip.reverse else ""
        effect_filters = clip_effect_filters(session, clip)
        effect_filters = f"{effect_filters}," if effect_filters else ""
        filters.append(
            f"[{input_index}:a]"
            f"atrim=start={seconds(clip.trim_start_ms)}{duration},"
            "asetpts=PTS-STARTPTS,"
            f"{reverse_filter}"
            f"{retime_filters}"
            f"{fade_filters}"
            f"{effect_filters}"
            f"volume={volume:.6f},"
            f"adelay={clip.start_ms}:all=1,"
            f"aformat=sample_rates={sample_rate}:channel_layouts={'stereo' if channels == 2 else 'mono'}"
            f"[{label}]"
        )
        music_labels.append(f"[{label}]")

    for group_index, group in enumerate(session.stem_groups):
        stem_labels: list[str] = []
        for stem_name, stem, _path in stem_group_inputs(group):
            if stem_group_input_indices is None:
                input_index = len(session.clips) + len(stem_labels)
            else:
                input_index = stem_group_input_indices[(group_index, stem_name)]
            source_duration = source_duration_ms(group)
            duration = f":duration={seconds(source_duration)}" if source_duration is not None else ""
            label = f"stem{group_index}{stem_name}"
            volume = gain_multiplier(group.gain_db) * gain_multiplier(stem.gain_db)
            fade_filters = ""
            if group.fade_in_ms:
                fade_filters += f"afade=t=in:st=0:d={seconds(group.fade_in_ms)},"
            if group.fade_out_ms and group.duration_ms is not None:
                fade_start_ms = max(0, group.duration_ms - group.fade_out_ms)
                fade_filters += f"afade=t=out:st={seconds(fade_start_ms)}:d={seconds(group.fade_out_ms)},"
            retime_filters = ",".join(time_pitch_filters(group, sample_rate))
            retime_filters = f"{retime_filters}," if retime_filters else ""
            reverse_filter = "areverse," if group.reverse else ""
            stem_filters = [*stem_static_filters(stem), *stem_dynamic_filters(session, group, stem_name, stem)]
            stem_filter_text = ",".join(stem_filters)
            stem_filter_text = f"{stem_filter_text}," if stem_filter_text else ""
            filters.append(
                f"[{input_index}:a]"
                f"atrim=start={seconds(group.trim_start_ms)}{duration},"
                "asetpts=PTS-STARTPTS,"
                f"{reverse_filter}"
                f"{retime_filters}"
                f"{fade_filters}"
                f"{stem_filter_text}"
                f"volume={volume:.6f},"
                f"aformat=sample_rates={sample_rate}:channel_layouts={'stereo' if channels == 2 else 'mono'}"
                f"[{label}]"
            )
            stem_labels.append(f"[{label}]")
        if not stem_labels:
            continue
        group_label = f"stemgroup{group_index}"
        if len(stem_labels) == 1:
            filters.append(f"{stem_labels[0]}anull[{group_label}sum]")
            current_group = f"{group_label}sum"
        else:
            filters.append(f"{''.join(stem_labels)}amix=inputs={len(stem_labels)}:duration=longest:normalize=0[{group_label}sum]")
            current_group = f"{group_label}sum"
        for start_ms, end_ms, gain_db in group_automation_windows(session, group, "gain_db"):
            out = f"{group_label}gain{start_ms}"
            filters.append(f"[{current_group}]volume=enable='between(t,{seconds(start_ms)},{seconds(end_ms)})':volume={gain_multiplier(gain_db):.6f}[{out}]")
            current_group = out
        for start_ms, end_ms, lowpass_hz in group_automation_windows(session, group, "lowpass_hz"):
            out = f"{group_label}lp{start_ms}"
            filters.append(f"[{current_group}]lowpass=enable='between(t,{seconds(start_ms)},{seconds(end_ms)})':f={lowpass_hz:.3f}[{out}]")
            current_group = out
        for start_ms, end_ms, highpass_hz in group_automation_windows(session, group, "highpass_hz"):
            out = f"{group_label}hp{start_ms}"
            filters.append(f"[{current_group}]highpass=enable='between(t,{seconds(start_ms)},{seconds(end_ms)})':f={highpass_hz:.3f}[{out}]")
            current_group = out
        for start_ms, end_ms, start_gain, end_gain in group_crossfader_windows(session, group):
            out = f"{group_label}xf{start_ms}"
            if abs(start_gain - end_gain) < 0.000001:
                volume_expr = f"{start_gain:.6f}"
                eval_mode = ""
            else:
                duration_ms = max(1, end_ms - start_ms)
                slope = (end_gain - start_gain) / (duration_ms / 1000)
                volume_expr = f"'{start_gain:.6f}+({slope:.9f})*(t-{seconds(start_ms)})'"
                eval_mode = ":eval=frame"
            filters.append(f"[{current_group}]volume=enable='between(t,{seconds(start_ms)},{seconds(end_ms)})':volume={volume_expr}{eval_mode}[{out}]")
            current_group = out
        out_label = f"musicstemgroup{group_index}"
        filters.append(
            f"[{current_group}]adelay={group.start_ms}:all=1,"
            f"aformat=sample_rates={sample_rate}:channel_layouts={'stereo' if channels == 2 else 'mono'}"
            f"[{out_label}]"
        )
        music_labels.append(f"[{out_label}]")

    effect_index = 0
    for effect in session.effects:
        for clip in effect_target_clips(session, effect):
            clip_index = session.clips.index(clip)
            input_index = clip_input_indices[clip_index] if clip_input_indices is not None else clip_index
            label = f"effect{effect_index}"
            effect_index += 1
            filters.append(
                effect_stream_filter(effect, clip, input_index, label, sample_rate, channels, reverb_ir_indices=reverb_ir_indices)
            )
            music_labels.append(f"[{label}]")

    next_input = first_lean_input_index if first_lean_input_index is not None else len(session.clips)
    if music_labels:
        filters.append(f"{''.join(music_labels)}amix=inputs={len(music_labels)}:duration=longest:normalize=0[musicmix]")
        current_music = "musicmix"
    else:
        filters.append(
            f"anullsrc=r={sample_rate}:cl={'stereo' if channels == 2 else 'mono'}:d={seconds(session_duration_ms(session))}[musicmix]"
        )
        current_music = "musicmix"

    for idx, (start_ms, end_ms, duck_volume) in enumerate(collect_master_automation(session, "duck_volume")):
        out = f"duck{idx}"
        filters.append(
            f"[{current_music}]volume=enable='between(t,{seconds(start_ms)},{seconds(end_ms)})':volume={duck_volume:.6f}[{out}]"
        )
        current_music = out

    for idx, (start_ms, end_ms, lowpass_hz) in enumerate(collect_master_automation(session, "lowpass_hz")):
        out = f"lowpass{idx}"
        filters.append(
            f"[{current_music}]lowpass=enable='between(t,{seconds(start_ms)},{seconds(end_ms)})':f={lowpass_hz:.3f}[{out}]"
        )
        current_music = out

    mix_labels = [f"[{current_music}]"]
    for index, lean_in in enumerate(session.mic_lean_ins):
        audio_path = lean_in_audio.get(lean_in.id)
        if audio_path is None:
            continue
        input_index = next_input
        next_input += 1
        label = f"lean{index}"
        volume = max(0.0, float(lean_in.volume))
        filters.append(
            f"[{input_index}:a]"
            "asetpts=PTS-STARTPTS,"
            f"volume={volume:.6f},"
            f"adelay={lean_in.start_ms}:all=1,"
            f"aformat=sample_rates={sample_rate}:channel_layouts={'stereo' if channels == 2 else 'mono'}"
            f"[{label}]"
        )
        mix_labels.append(f"[{label}]")

    output_filters = "alimiter=limit=0.98"
    if output_duration_ms is not None:
        output_filters = f"aresample=async=1:first_pts=0,atrim=duration={seconds(output_duration_ms)}," + output_filters
    filters.append(f"{''.join(mix_labels)}amix=inputs={len(mix_labels)}:duration=longest:normalize=0,{output_filters}[out]")
    return ";".join(filters)


def materialize_clip_stem_mixes(
    session: MixSession,
    db_path: Path,
    temp_dir: Path,
    sample_rate: int,
    channels: int,
) -> MixSession:
    """Replace clips that request play_stems with a premix of their ready stems.

    Clips are rendered from one source input, so stem selection has to be
    resolved before the filter graph is built. A clip that asks for stems that
    are not ready is a hard error: silently rendering the full track would put
    unplanned vocals/bass under the mix and make the stem metadata a lie.
    """
    clips = list(session.clips)
    changed = False
    premix_cache: dict[tuple[str, tuple[str, ...]], str] = {}
    for index, clip in enumerate(clips):
        stems = tuple(sorted(set(clip.play_stems or ())))
        if not stems:
            continue
        cache_key = (clip.path, stems)
        premixed = premix_cache.get(cache_key)
        if premixed is None:
            artifacts = ready_stem_artifacts(db_path, clip.path)
            if artifacts is None:
                raise ValueError(
                    f"clip {clip.id} requests stems {list(stems)} but no ready stem artifacts exist for "
                    f"{clip.path}; run scripts/slime_audio_stems.py split first or load it as a load_track action"
                )
            stem_paths = [artifacts["stems"][name] for name in stems]
            premix_path = temp_dir / f"clip-stem-premix-{index:03d}.wav"
            command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
            for path in stem_paths:
                command.extend(["-i", path])
            command.extend(
                [
                    "-filter_complex",
                    f"amix=inputs={len(stem_paths)}:duration=longest:normalize=0",
                    "-ac",
                    str(channels),
                    "-ar",
                    str(sample_rate),
                    str(premix_path),
                ]
            )
            subprocess.run(command, check=True)
            premixed = str(premix_path)
            premix_cache[cache_key] = premixed
        clips[index] = replace(clip, path=premixed, play_stems=None)
        changed = True
    if not changed:
        return session
    return replace(session, clips=clips)


def ffmpeg_command(
    session: MixSession,
    lean_in_audio: dict[str, Path],
    output: Path,
    sample_rate: int,
    channels: int,
    output_duration_ms: int | None = None,
    output_format: str = "auto",
    mp3_bitrate: str = "192k",
    reverb_irs: dict[str, Path] | None = None,
) -> list[str]:
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    input_paths: list[str] = []
    clip_input_indices: dict[int, int] = {}
    stem_group_input_indices: dict[tuple[int, str], int] = {}
    reverb_ir_indices: dict[str, int] = {}
    for clip_index, clip in enumerate(session.clips):
        try:
            input_index = input_paths.index(clip.path)
        except ValueError:
            input_index = len(input_paths)
            input_paths.append(clip.path)
            command.extend(["-i", clip.path])
        clip_input_indices[clip_index] = input_index
    for group_index, group in enumerate(session.stem_groups):
        for stem_name, _stem, path in stem_group_inputs(group):
            # Deck-clock cue/jump/loop actions compile into adjacent stem-group
            # segments that often read the same stem file repeatedly. Allocate a
            # fresh ffmpeg input for each rendered stem segment; reusing one
            # input label can cause later loop/jump segments to disappear.
            input_index = len(input_paths)
            input_paths.append(path)
            command.extend(["-i", path])
            stem_group_input_indices[(group_index, stem_name)] = input_index
    for effect_id, ir_path in (reverb_irs or {}).items():
        reverb_ir_indices[effect_id] = len(input_paths)
        input_paths.append(str(ir_path))
        command.extend(["-i", str(ir_path)])
    for lean_in in session.mic_lean_ins:
        audio_path = lean_in_audio.get(lean_in.id)
        if audio_path is not None:
            command.extend(["-i", str(audio_path)])
    command.extend(
        [
            "-filter_complex",
            build_filter_complex(
                session,
                lean_in_audio,
                sample_rate,
                channels,
                output_duration_ms,
                clip_input_indices,
                stem_group_input_indices,
                len(input_paths),
                reverb_ir_indices=reverb_ir_indices,
            ),
            "-map",
            "[out]",
            *output_codec_args(output, output_format, sample_rate, channels, mp3_bitrate),
            str(output),
        ]
    )
    return command


def spill_filter_complex_to_script(command: list[str], script_path: Path, *, min_length: int = 100_000) -> list[str]:
    if "-filter_complex" not in command:
        return command
    index = command.index("-filter_complex")
    filter_complex = command[index + 1]
    if len(filter_complex) < min_length:
        return command
    script_path.write_text(filter_complex, encoding="utf-8")
    return [*command[:index], "-filter_complex_script", str(script_path), *command[index + 2:]]


def resolved_output_format(output: Path, output_format: str) -> str:
    if output_format != "auto":
        return output_format
    suffix = output.suffix.lower()
    if suffix == ".mp3":
        return "mp3"
    if suffix == ".flac":
        return "flac"
    return "wav"


def output_codec_args(output: Path, output_format: str, sample_rate: int, channels: int, mp3_bitrate: str) -> list[str]:
    resolved = resolved_output_format(output, output_format)
    common = ["-ac", str(channels), "-ar", str(sample_rate)]
    if resolved == "wav":
        return ["-acodec", "pcm_s16le", *common]
    if resolved == "flac":
        return ["-acodec", "flac", *common]
    if resolved == "mp3":
        return ["-acodec", "libmp3lame", "-b:a", mp3_bitrate, *common]
    raise ValueError(f"unsupported output format: {output_format}")


def verify_rendered_audio(output: Path) -> None:
    report = rendered_audio_report(output)
    if report["duration_seconds"] <= 0:
        raise ValueError(f"rendered mix has no positive duration: {output}")
    if report["silent"]:
        raise ValueError(f"rendered mix is silent: {output}")


def rendered_audio_report(output: Path) -> dict[str, float | int | bool | None]:
    probe = subprocess.run(
        [
            "ffprobe",
            "-hide_banner",
            "-loglevel",
            "error",
            "-show_entries",
            "format=duration,bit_rate",
            "-of",
            "json",
            str(output),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    format_payload = json.loads(probe.stdout or "{}").get("format", {})
    duration = float(format_payload.get("duration") or 0)
    bit_rate = int(float(format_payload.get("bit_rate") or 0)) if format_payload.get("bit_rate") else None
    volume = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-i",
            str(output),
            "-af",
            "volumedetect",
            "-f",
            "null",
            "-",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    text = f"{volume.stdout}\n{volume.stderr}"
    mean_match = re.search(r"mean_volume:\s+(-?inf|-?\d+(?:\.\d+)?) dB", text)
    max_match = re.search(r"max_volume:\s+(-?inf|-?\d+(?:\.\d+)?) dB", text)
    mean_volume = None if mean_match is None or mean_match.group(1) == "-inf" else float(mean_match.group(1))
    max_volume = None if max_match is None or max_match.group(1) == "-inf" else float(max_match.group(1))
    return {
        "duration_seconds": duration,
        "bit_rate": bit_rate,
        "mean_volume_db": mean_volume,
        "max_volume_db": max_volume,
        "silent": mean_volume is None or max_volume is None,
        "clipping_risk": max_volume is not None and max_volume >= -0.1,
    }


def payload_clip_start_end(clip: dict) -> tuple[int, int | None]:
    start_ms = parse_ms(clip.get("start_ms", clip.get("start", 0)), f"clip {clip.get('id', '<unknown>')} start")
    duration = clip.get("duration_ms", clip.get("duration"))
    if duration is None:
        return start_ms, None
    return start_ms, start_ms + parse_ms(duration, f"clip {clip.get('id', '<unknown>')} duration")


def is_balance_bed_clip(clip: dict) -> bool:
    role = str(clip.get("planner_role") or clip.get("role") or "").strip().lower()
    if role in BED_ROLE_NAMES:
        return True
    if clip.get("bed_under"):
        return True
    clip_id = str(clip.get("id") or "").lower()
    return "-bed-" in clip_id or clip_id.startswith("bed-") or clip_id.endswith("-bed")


def automation_value_span(payload: dict, clip: dict, params: set[str]) -> dict[str, dict[str, float | int] | None]:
    clip_id = str(clip.get("id") or "")
    deck = str(clip.get("deck") or "")
    spans: dict[str, list[float]] = {param: [] for param in params}
    for automation in [*clip.get("automations", []), *payload.get("automations", []), *payload.get("deck_automations", [])]:
        if automation.get("param") not in params:
            continue
        target = str(automation.get("target") or "")
        source_clip_id = str(automation.get("source_clip_id") or "")
        if target not in {clip_id, deck} and source_clip_id != clip_id:
            continue
        for point in automation.get("points", []):
            try:
                spans[str(automation["param"])].append(float(point["value"]))
            except (KeyError, TypeError, ValueError):
                continue
    return {
        param: (
            {
                "min": min(values),
                "max": max(values),
                "range": max(values) - min(values),
                "points": len(values),
            }
            if values
            else None
        )
        for param, values in spans.items()
    }


def balance_bed_candidates(payload: dict, from_ms: int, duration_ms: int | None) -> list[dict]:
    window_end_ms = from_ms + duration_ms if duration_ms is not None else None
    candidates: list[dict] = []
    for clip in payload.get("clips", []):
        if not isinstance(clip, dict) or not is_balance_bed_clip(clip):
            continue
        start_ms, end_ms = payload_clip_start_end(clip)
        if end_ms is None:
            continue
        if end_ms <= from_ms:
            continue
        if window_end_ms is not None and start_ms >= window_end_ms:
            continue
        audit_start_ms = max(start_ms, from_ms)
        audit_end_ms = min(end_ms, window_end_ms) if window_end_ms is not None else end_ms
        if audit_end_ms <= audit_start_ms:
            continue
        candidates.append(
            {
                "id": clip.get("id"),
                "path": clip.get("path"),
                "deck": clip.get("deck"),
                "planner_role": clip.get("planner_role"),
                "start_ms": start_ms,
                "end_ms": end_ms,
                "audit_start_ms": audit_start_ms,
                "audit_duration_ms": audit_end_ms - audit_start_ms,
                "gain_db": float(clip.get("gain_db", 0.0)),
                "filter_spans": automation_value_span(payload, clip, {"gain_db", "lowpass_hz", "highpass_hz", "eq_low_db", "eq_mid_db", "eq_high_db"}),
            }
        )
    return candidates


def session_with_only_clip_ids(session: MixSession, clip_ids: set[str]) -> MixSession:
    return replace(
        session,
        clips=[clip for clip in session.clips if clip.id in clip_ids],
        stem_groups=[],
        mic_lean_ins=[],
        effects=[effect for effect in session.effects if effect.target in clip_ids or effect.target.startswith("deck:")],
    )


def render_report_for_session(
    session: MixSession,
    output: Path,
    temp_dir: Path,
    sample_rate: int,
    channels: int,
    output_duration_ms: int | None,
    output_format: str,
    mp3_bitrate: str,
) -> dict[str, float | int | bool | None]:
    reverb_irs = prepare_reverb_irs(session, temp_dir, sample_rate, channels)
    command = ffmpeg_command(session, {}, output, sample_rate, channels, output_duration_ms, output_format, mp3_bitrate, reverb_irs=reverb_irs)
    command = spill_filter_complex_to_script(command, temp_dir / f"{output.stem}-filter-complex.ffmpeg")
    subprocess.run(command, check=True)
    verify_rendered_audio(output)
    return rendered_audio_report(output)


def rendered_balance_audit(
    payload: dict,
    source_session: MixSession,
    temp_dir: Path,
    *,
    from_ms: int,
    duration_ms: int | None,
    sample_rate: int,
    channels: int,
    output_format: str,
    mp3_bitrate: str,
    min_bed_vs_full_db: float,
    min_filter_sweep_hz: float,
) -> dict:
    findings: list[dict] = []
    bed_reports: list[dict] = []
    candidates = balance_bed_candidates(payload, from_ms, duration_ms)
    for index, candidate in enumerate(candidates, start=1):
        audit_start_ms = int(candidate["audit_start_ms"])
        audit_duration_ms = int(candidate["audit_duration_ms"])
        full_session = shift_session_window(source_session, audit_start_ms, audit_duration_ms)
        bed_session = session_with_only_clip_ids(full_session, {str(candidate["id"])})
        full_report = render_report_for_session(
            full_session,
            temp_dir / f"balance-full-{index}.wav",
            temp_dir,
            sample_rate,
            channels,
            audit_duration_ms,
            output_format="wav",
            mp3_bitrate=mp3_bitrate,
        )
        bed_report = render_report_for_session(
            bed_session,
            temp_dir / f"balance-bed-{index}.wav",
            temp_dir,
            sample_rate,
            channels,
            audit_duration_ms,
            output_format="wav",
            mp3_bitrate=mp3_bitrate,
        )
        full_mean = full_report.get("mean_volume_db")
        bed_mean = bed_report.get("mean_volume_db")
        bed_vs_full_db = None if full_mean is None or bed_mean is None else float(bed_mean) - float(full_mean)
        lowpass_span = candidate["filter_spans"].get("lowpass_hz") if isinstance(candidate.get("filter_spans"), dict) else None
        highpass_span = candidate["filter_spans"].get("highpass_hz") if isinstance(candidate.get("filter_spans"), dict) else None
        filter_range_hz = max(
            float(lowpass_span.get("range", 0.0)) if lowpass_span else 0.0,
            float(highpass_span.get("range", 0.0)) if highpass_span else 0.0,
        )
        bed_entry = {
            **candidate,
            "full_audio": full_report,
            "bed_audio": bed_report,
            "bed_vs_full_db": bed_vs_full_db,
            "filter_range_hz": filter_range_hz,
        }
        if bed_vs_full_db is None:
            findings.append({"kind": "unmeasurable_bed_balance", "id": candidate["id"]})
        elif bed_vs_full_db < min_bed_vs_full_db:
            findings.append(
                {
                    "kind": "buried_rhythm_bed",
                    "id": candidate["id"],
                    "bed_vs_full_db": bed_vs_full_db,
                    "threshold_db": min_bed_vs_full_db,
                }
            )
        if filter_range_hz < min_filter_sweep_hz:
            findings.append(
                {
                    "kind": "static_or_token_filter",
                    "id": candidate["id"],
                    "filter_range_hz": filter_range_hz,
                    "threshold_hz": min_filter_sweep_hz,
                }
            )
        bed_reports.append(bed_entry)
    return {
        "accepted": not findings,
        "from_ms": from_ms,
        "duration_ms": duration_ms,
        "bed_count": len(bed_reports),
        "beds": bed_reports,
        "findings": findings,
        "thresholds": {
            "min_bed_vs_full_db": min_bed_vs_full_db,
            "min_filter_sweep_hz": min_filter_sweep_hz,
        },
    }


def clip_start_end_payload(clip: dict) -> tuple[int, int | None]:
    start_ms = parse_ms(clip.get("start_ms", clip.get("start", 0)), "clip start")
    duration = clip.get("duration_ms", clip.get("duration"))
    duration_ms = parse_ms(duration, "clip duration") if duration is not None else None
    return start_ms, start_ms + duration_ms if duration_ms is not None else None


def routine_window(payload: dict, routine_id: str, pad_ms: int) -> tuple[int, int]:
    starts: list[int] = []
    ends: list[int] = []
    for clip in payload.get("clips", []):
        if clip.get("routine_id") != routine_id:
            continue
        start_ms, end_ms = clip_start_end_payload(clip)
        starts.append(start_ms)
        if end_ms is not None:
            ends.append(end_ms)
    for automation in payload.get("automations", []):
        if automation.get("routine_id") != routine_id:
            continue
        points = automation.get("points") or []
        point_times = [
            parse_ms(point.get("at_ms", point.get("at", 0)), "automation point")
            for point in points
            if isinstance(point, dict)
        ]
        starts.extend(point_times[:1])
        ends.extend(point_times[-1:])
    for effect in payload.get("effects", []):
        if effect.get("routine_id") != routine_id:
            continue
        start_ms = parse_ms(effect.get("start_ms", effect.get("start", 0)), "effect start")
        duration_ms = parse_ms(effect.get("duration_ms", effect.get("duration", 0)), "effect duration")
        tail_ms = parse_ms(effect.get("tail_ms", effect.get("tail", 0)), "effect tail")
        starts.append(start_ms)
        ends.append(start_ms + duration_ms + tail_ms)
    if not starts or not ends:
        raise ValueError(f"routine id not found or has no timed events: {routine_id}")
    return max(0, min(starts) - pad_ms), max(ends) + pad_ms


def routine_taste_report(payload: dict, routine_id: str, start_ms: int, end_ms: int) -> dict:
    routine_clips = [clip for clip in payload.get("clips", []) if clip.get("routine_id") == routine_id]
    active_clips = []
    for clip in payload.get("clips", []):
        clip_start, clip_end = clip_start_end_payload(clip)
        if clip_end is not None and clip_start < end_ms and start_ms < clip_end:
            active_clips.append(clip)
    unrelated = [
        clip
        for clip in active_clips
        if clip.get("routine_id") not in {routine_id, None} and clip.get("source_clip_id") not in {item.get("id") for item in routine_clips}
    ]
    routine_recipes = sorted({str(clip.get("routine_recipe")) for clip in routine_clips if clip.get("routine_recipe")})
    routine_effects = [effect for effect in payload.get("effects", []) if effect.get("routine_id") == routine_id]
    errors: list[str] = []
    warnings: list[str] = []
    if not routine_clips:
        errors.append("routine has no clips")
    if len(active_clips) > 3:
        warnings.append("more than three clips are active during the routine window")
    if unrelated:
        errors.append("unrelated routine clips overlap the audition window")
    if any(clip.get("pitch_shift_semitones") not in {None, 0} for clip in active_clips):
        warnings.append("audition includes rendered pitch correction; verify key fit by ear")
    if any(abs(float(clip.get("tempo_shift_pct") or 0.0)) > 4.0 for clip in active_clips):
        errors.append("tempo shift exceeds conservative routine audition limit")
    return {
        "routine_id": routine_id,
        "routine_recipes": routine_recipes,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "active_clip_ids": [clip.get("id") for clip in active_clips],
        "checks": {
            "key_fit": "unknown_without_analysis_metadata",
            "bpm_fit": "ok" if not any(abs(float(clip.get("tempo_shift_pct") or 0.0)) > 4.0 for clip in active_clips) else "fail",
            "vocal_on_vocal_risk": "unknown_without_stems",
            "dense_drums_risk": "unknown_without_stems",
            "effect_tails": "detected" if any(parse_ms(effect.get("tail_ms", effect.get("tail", 0)), "effect tail") > 0 for effect in routine_effects) else "not_detected",
        },
        "warnings": warnings,
        "errors": errors,
        "accepted": not errors,
    }


def prepare_lean_in_audio(
    session: MixSession,
    temp_dir: Path,
    kokoro_url: str,
    default_voice: str,
    timeout: int,
    sample_rate: int,
    channels: int,
    skip_tts: bool,
) -> dict[str, Path]:
    audio: dict[str, Path] = {}
    for lean_in in session.mic_lean_ins:
        wav = temp_dir / f"{lean_in.id}.wav"
        if skip_tts:
            subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    f"anullsrc=r={sample_rate}:cl={'stereo' if channels == 2 else 'mono'}",
                    "-t",
                    "0.1",
                    str(wav),
                ],
                check=True,
            )
        else:
            try:
                raw = temp_dir / f"{lean_in.id}-raw.wav"
                synthesize_kokoro(kokoro_url, lean_in.voice or default_voice, lean_in.text, raw, timeout)
                normalize_tts(raw, wav, sample_rate, channels)
                report = rendered_audio_report(wav)
                if report["silent"]:
                    raise ValueError(f"silent lean-in audio for {lean_in.id}")
            except Exception as error:
                raise ValueError(f"failed lean-in audio for {lean_in.id}: {error}") from error
        audio[lean_in.id] = wav
    return audio


def main() -> int:
    parser = argparse.ArgumentParser(description="Render a planned SlimeAudio mix session to one Snapcast-ready audio file.")
    parser.add_argument("session", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--kokoro-url", default=DEFAULT_KOKORO_URL)
    parser.add_argument("--voice", default="am_eric")
    parser.add_argument("--tts-timeout-seconds", type=int, default=90)
    parser.add_argument("--sample-rate", type=int, default=48_000)
    parser.add_argument("--channels", type=int, default=2)
    parser.add_argument("--format", choices=["auto", "wav", "mp3", "flac"], default="auto")
    parser.add_argument("--mp3-bitrate", default="192k")
    parser.add_argument(
        "--from",
        dest="from_time",
        default="0",
        help="Render a live-edit window starting at this mix timestamp/playhead.",
    )
    parser.add_argument("--duration", help="Render only this much timeline after --from.")
    parser.add_argument("--routine-id", help="Render an audition window around a planned routine id.")
    parser.add_argument("--routine-pad-ms", type=int, default=5_000)
    parser.add_argument("--report-output", type=Path, help="Write a machine-readable audition/render verification report.")
    parser.add_argument("--force-routine-risk", action="store_true", help="Render even if routine taste checks report errors.")
    parser.add_argument("--audit-balance", action="store_true", help="Render solo bed windows and compare their loudness against the full overlap.")
    parser.add_argument("--fail-balance-audit", action="store_true", help="Exit non-zero when --audit-balance finds buried beds or token filters.")
    parser.add_argument("--min-bed-vs-full-db", type=float, default=-16.0, help="Minimum acceptable rendered bed mean loudness relative to the full overlap.")
    parser.add_argument("--min-filter-sweep-hz", type=float, default=300.0, help="Minimum low/high-pass movement before a bed filter is treated as token/static.")
    parser.add_argument("--db", type=Path, default=DEFAULT_LIBRARY_DB, help="Music library DB used to resolve ready stem artifacts for clip play_stems.")
    parser.add_argument("--skip-tts", action="store_true", help="Use tiny silent lean-in placeholders; useful for command validation.")
    parser.add_argument("--verify", action=argparse.BooleanOptionalAction, default=True, help="Probe rendered duration and reject silent output.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    payload = load_payload(args.session)
    routine_report = None
    if args.routine_id:
        routine_start_ms, routine_end_ms = routine_window(payload, args.routine_id, args.routine_pad_ms)
        args.from_time = str(routine_start_ms)
        duration_ms = routine_end_ms - routine_start_ms
        routine_report = routine_taste_report(payload, args.routine_id, routine_start_ms, routine_end_ms)
        if not routine_report["accepted"] and not args.force_routine_risk:
            if args.report_output:
                args.report_output.write_text(json.dumps({"routine": routine_report}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            raise ValueError(f"routine audition rejected: {', '.join(routine_report['errors'])}")
    else:
        duration_ms = parse_ms(args.duration, "render duration") if args.duration else None
    render_start_ms = parse_ms(args.from_time, "render start")
    session = shift_session_window(load_session(args.session), render_start_ms, duration_ms)
    with tempfile.TemporaryDirectory(prefix="slime-session-mixdown-") as temp:
        if not args.dry_run:
            session = materialize_clip_stem_mixes(session, args.db, Path(temp), args.sample_rate, args.channels)
        lean_audio = prepare_lean_in_audio(
            session,
            Path(temp),
            args.kokoro_url,
            args.voice,
            args.tts_timeout_seconds,
            args.sample_rate,
            args.channels,
            args.skip_tts or args.dry_run,
        )
        command = ffmpeg_command(
            session,
            lean_audio,
            args.output,
            args.sample_rate,
            args.channels,
            duration_ms,
            args.format,
            args.mp3_bitrate,
            reverb_irs=prepare_reverb_irs(session, Path(temp), args.sample_rate, args.channels),
        )
        if args.dry_run:
            report = {
                "command": command,
                "filter_complex": command[command.index("-filter_complex") + 1],
                "render": {"from_ms": render_start_ms, "duration_ms": duration_ms},
            }
            if routine_report is not None:
                report["routine"] = routine_report
            if args.report_output:
                args.report_output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            print(json.dumps(report, indent=2))
            return 0
        command = spill_filter_complex_to_script(command, Path(temp) / "filter-complex.ffmpeg")
        subprocess.run(command, check=True)
        audio_report = None
        balance_report = None
        if args.verify:
            verify_rendered_audio(args.output)
            audio_report = rendered_audio_report(args.output)
        if args.audit_balance:
            audit_session = materialize_clip_stem_mixes(
                load_session(args.session), args.db, Path(temp), args.sample_rate, args.channels
            )
            balance_report = rendered_balance_audit(
                payload,
                audit_session,
                Path(temp),
                from_ms=render_start_ms,
                duration_ms=duration_ms,
                sample_rate=args.sample_rate,
                channels=args.channels,
                output_format=args.format,
                mp3_bitrate=args.mp3_bitrate,
                min_bed_vs_full_db=args.min_bed_vs_full_db,
                min_filter_sweep_hz=args.min_filter_sweep_hz,
            )
        if args.report_output:
            args.report_output.write_text(
                json.dumps(
                    {
                        "output": str(args.output),
                        "render": {"from_ms": render_start_ms, "duration_ms": duration_ms},
                        "routine": routine_report,
                        "audio": audio_report,
                        "balance": balance_report,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
        if args.audit_balance and args.fail_balance_audit and balance_report is not None and not balance_report["accepted"]:
            raise ValueError(
                "rendered balance audit failed: "
                + ", ".join(f"{finding['kind']}:{finding.get('id')}" for finding in balance_report["findings"])
            )
    print(f"rendered {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
