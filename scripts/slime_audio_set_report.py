#!/usr/bin/env python3
"""Objective rubric signals for a mix session (skills/slime-audio-dj/RUBRIC.md).

Reads a session JSON and prints the measurable half of the rubric: blend/cut
ratio, tempo identity, transform usage, stem/bed presence, automation motion,
mic count, and (with --history) time from generation to first audio. The
numbers locate problems; only ears grade.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path


def load_session(path: Path) -> dict:
    with path.open() as handle:
        return json.load(handle)


def lead_clips(session: dict) -> list[dict]:
    return [c for c in session.get("clips", []) if not c.get("play_stems")]


def transition_stats(session: dict) -> dict:
    plans = session.get("transition_plans", [])
    decisions = Counter(str(p.get("decision")) for p in plans)
    blends = decisions.get("blend", 0)
    cuts = decisions.get("cut", 0)
    overlaps = [int(p.get("overlap_ms") or 0) for p in plans if p.get("decision") == "blend"]
    total = blends + cuts
    return {
        "transitions": total,
        "blends": blends,
        "cuts": cuts,
        "blend_ratio": round(blends / total, 3) if total else None,
        "mean_overlap_ms": int(sum(overlaps) / len(overlaps)) if overlaps else 0,
    }


def source_bpm_lookup(db_path: Path, paths: list[str]) -> dict[str, float]:
    if not db_path.exists():
        return {}
    lookup: dict[str, float] = {}
    with sqlite3.connect(db_path) as conn:
        for chunk_start in range(0, len(paths), 500):
            chunk = paths[chunk_start : chunk_start + 500]
            marks = ",".join("?" for _ in chunk)
            rows = conn.execute(
                f"SELECT preferred_path, tunebat_bpm FROM tracks WHERE preferred_path IN ({marks}) AND tunebat_bpm IS NOT NULL",
                chunk,
            )
            lookup.update({str(path): float(bpm) for path, bpm in rows})
    return lookup


def tempo_identity(clips: list[dict], bpm_by_path: dict[str, float]) -> dict:
    rendered = []
    for clip in clips:
        bpm = bpm_by_path.get(str(clip.get("path")))
        if bpm:
            rendered.append(bpm * (1.0 + float(clip.get("tempo_shift_pct") or 0.0) / 100.0))
    if not rendered:
        return {"analyzed_leads": 0, "modal_bpm": None, "lock_coverage": None}
    modal = Counter(round(bpm) for bpm in rendered).most_common(1)[0][0]
    within = sum(1 for bpm in rendered if abs(bpm - modal) <= 1.0)
    return {
        "analyzed_leads": len(rendered),
        "modal_bpm": modal,
        "lock_coverage": round(within / len(rendered), 3),
    }


def transform_stats(clips: list[dict]) -> dict:
    neutral = sum(
        1
        for clip in clips
        if not float(clip.get("tempo_shift_pct") or 0.0) and not int(clip.get("pitch_shift_semitones") or 0)
    )
    return {"leads": len(clips), "neutral_leads": neutral, "reshaped_leads": len(clips) - neutral}


def layer_stats(session: dict) -> dict:
    stem_clips = [c for c in session.get("clips", []) if c.get("play_stems")]
    actions = session.get("actions", [])
    stem_actions = [a for a in actions if a.get("play_stems")]
    action_kinds = Counter(str(a.get("action")) for a in actions)
    return {
        "stem_layers": len(stem_clips) + len(stem_actions),
        "actions": dict(action_kinds),
    }


def motion_stats(session: dict) -> dict:
    ramps = session.get("deck_automations", []) + session.get("automations", [])
    moving = 0
    params = Counter()
    for ramp in ramps:
        points = ramp.get("points") or []
        values = {p.get("value") for p in points}
        params[str(ramp.get("param"))] += 1
        if len(values) > 1:
            moving += 1
    return {"automation_ramps": len(ramps), "moving_ramps": moving, "params": dict(params)}


def history_timing(history_path: Path, slug: str) -> dict:
    generated = first_audio = None
    with history_path.open() as handle:
        for line in handle:
            if slug not in line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            name = event.get("event")
            stamp = event.get("timestamp")
            if name == "autodj_material_selected" and generated is None:
                generated = stamp
            if name == "session_window_started" and first_audio is None:
                first_audio = stamp
    return {"material_selected": generated, "first_audio": first_audio}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", type=Path)
    parser.add_argument("--db", type=Path, default=Path("runtime/slime-music-library.sqlite3"))
    parser.add_argument("--history", type=Path, default=None)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()

    session = load_session(args.session)
    leads = lead_clips(session)
    duration_ms = max((int(c.get("start_ms") or 0) + int(c.get("duration_ms") or 0) for c in session.get("clips", [])), default=0)
    report = {
        "title": session.get("title"),
        "duration_min": round(duration_ms / 60000.0, 1),
        "transitions": transition_stats(session),
        "tempo_identity": tempo_identity(leads, source_bpm_lookup(args.db, [str(c.get("path")) for c in leads])),
        "transforms": transform_stats(leads),
        "layers": layer_stats(session),
        "motion": motion_stats(session),
        "mic_lean_ins": len(session.get("mic_lean_ins", [])),
    }
    if args.history and args.history.exists():
        report["timing"] = history_timing(args.history, args.session.stem)

    if args.as_json:
        json.dump(report, sys.stdout, indent=2)
        print()
        return 0

    t = report["transitions"]
    ti = report["tempo_identity"]
    tf = report["transforms"]
    print(f"{report['title']} — {report['duration_min']} min, {tf['leads']} leads")
    ratio = "n/a" if t["blend_ratio"] is None else f"{t['blend_ratio']:.0%}"
    print(f"handoffs     : {t['blends']} blends / {t['cuts']} cuts ({ratio} blended, mean overlap {t['mean_overlap_ms'] // 1000}s)")
    if ti["modal_bpm"] is not None:
        print(f"tempo        : modal {ti['modal_bpm']} BPM, {ti['lock_coverage']:.0%} of {ti['analyzed_leads']} analyzed leads within ±1")
    else:
        print("tempo        : no analyzed leads found in library db")
    print(f"reshaping    : {tf['reshaped_leads']}/{tf['leads']} leads carry tempo/pitch transforms")
    print(f"layers       : {report['layers']['stem_layers']} stem layers; actions {report['layers']['actions'] or 'none'}")
    m = report["motion"]
    print(f"motion       : {m['moving_ramps']}/{m['automation_ramps']} ramps actually move ({m['params'] or 'none'})")
    print(f"mic          : {report['mic_lean_ins']} lean-ins")
    if "timing" in report:
        print(f"timing       : material selected {report['timing']['material_selected']} → first audio {report['timing']['first_audio']}")
    print("(numbers locate; only ears grade — see skills/slime-audio-dj/RUBRIC.md)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
