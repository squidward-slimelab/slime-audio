#!/usr/bin/env python3
"""Stem-aware section detection — plot intros, breakdowns, builds, and drops on the grid.

The mono-RMS structure detector can only see overall loudness, so it mislabels
a busy verse as a "drop" and misses a drums-out breakdown that stays loud on the
bass. This does the deadmau5 move: it reads the already-split stems, computes a
per-band RMS envelope for each, combines them into an energy profile, and reads
the SECTIONS off the band interplay — a breakdown is where the DRUMS drop out, a
drop is where drums+bass slam back in, a build is the rise into it. Every
boundary snaps to the bar/phrase grid. The result overwrites track_dj_structure,
so every downstream cue and every choreography decision inherits it.

    slime_audio_sections.py detect "<track path>" [--json]
    slime_audio_sections.py backfill [--limit N]
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from slime_audio_session import DEFAULT_LIBRARY_DB, ready_stem_artifacts  # noqa: E402

BANDS = ("drums", "bass", "other", "vocals")
# Drums and bass carry the "is the groove playing" signal; melodics and vocals
# ride on top. This weighting is the overall-energy curve the sections read.
ENERGY_WEIGHTS = {"drums": 0.42, "bass": 0.30, "other": 0.18, "vocals": 0.10}
HOP_MS = 250


def band_rms_db(stem_path: str, hop_ms: int = HOP_MS) -> tuple[list[float], int]:
    """Per-frame RMS (dBFS) of one stem, decoded to mono through ffmpeg."""
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", stem_path, "-ac", "1", "-ar", "22050", "-f", "s16le", "-"],
        capture_output=True,
    )
    if proc.returncode != 0 or not proc.stdout:
        return [], 22050
    import array
    import math

    samples = array.array("h")
    samples.frombytes(proc.stdout[: len(proc.stdout) - (len(proc.stdout) % 2)])
    sr = 22050
    hop = max(1, int(sr * hop_ms / 1000))
    out: list[float] = []
    for i in range(0, len(samples), hop):
        frame = samples[i : i + hop]
        if not frame:
            break
        acc = sum(s * s for s in frame) / len(frame)
        rms = math.sqrt(acc) / 32768.0
        out.append(20.0 * math.log10(rms) if rms > 1e-6 else -120.0)
    return out, sr


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, int(q * (len(ordered) - 1))))]


def normalize(env: list[float]) -> list[float]:
    """Map a band's dB envelope to 0..1 against its own active range."""
    if not env:
        return env
    floor = percentile(env, 0.05)
    ceil = percentile(env, 0.95)
    span = max(1e-3, ceil - floor)
    return [max(0.0, min(1.0, (v - floor) / span)) for v in env]


def snap_to_grid(ms: int, beat_offset_ms: int, step_ms: float) -> int:
    if step_ms <= 0:
        return ms
    k = round((ms - beat_offset_ms) / step_ms)
    return max(0, int(round(beat_offset_ms + k * step_ms)))


def detect_sections(path: str, db_path: Path = DEFAULT_LIBRARY_DB) -> list[dict[str, Any]]:
    import sqlite3

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT bpm, beat_offset_ms, phrase_ms, duration_s FROM track_dj_analysis WHERE path = ? AND bpm IS NOT NULL",
        (path,),
    ).fetchone()
    conn.close()
    if not row or not row[0]:
        return []
    bpm, beat_offset_ms, phrase_ms, duration_s = float(row[0]), int(row[1] or 0), int(row[2] or 0), float(row[3] or 0)
    bar_ms = (60_000.0 / bpm) * 4
    if not phrase_ms:
        phrase_ms = int(round(bar_ms * 4))
    art = ready_stem_artifacts(db_path, path)
    if not art:
        return []

    # Per-band envelopes, resampled to bar resolution and normalized.
    per_band_bars: dict[str, list[float]] = {}
    n_bars = max(1, int(round((duration_s * 1000) / bar_ms)))
    for band in BANDS:
        stem_path = art["stems"].get(band)
        if not stem_path:
            per_band_bars[band] = [0.0] * n_bars
            continue
        env, _ = band_rms_db(stem_path)
        env = normalize(env)
        bars: list[float] = []
        for bar in range(n_bars):
            lo = int(bar * bar_ms / HOP_MS)
            hi = int((bar + 1) * bar_ms / HOP_MS)
            window = env[lo:hi]
            bars.append(sum(window) / len(window) if window else 0.0)
        per_band_bars[band] = bars

    energy = [
        sum(ENERGY_WEIGHTS[b] * per_band_bars[b][i] for b in BANDS)
        for i in range(n_bars)
    ]
    drums = per_band_bars["drums"]

    # A "break" is a SIGNIFICANT dip below the track's own groove level, not
    # merely below average: a funk track with three levels (quiet intro,
    # steady verse, climax vamp) must not read every verse as a breakdown just
    # because the vamp is louder. The reference is the groove the track spends
    # most of its "on" time at (upper-median energy); a break is a few dB
    # under it, or the drums genuinely thinning out. This also handles EDM,
    # where the breakdown dips far below the groove and the drop returns to it.
    groove = max(0.15, percentile(energy, 0.60))
    e_lo, e_hi = percentile(energy, 0.05), percentile(energy, 0.92)
    break_thr = min(groove * 0.72, e_lo + 0.55 * max(0.05, e_hi - e_lo))
    drums_groove = max(0.15, percentile(drums, 0.60))
    drums_break = drums_groove * 0.55
    low_thr = break_thr  # kept for breakdown-depth confidence below

    # Per-bar regime: BREAK (a real dip below the groove, or drums thinned) vs
    # FULL (the groove is driving).
    def is_break(i: int) -> bool:
        return energy[i] < break_thr or drums[i] < drums_break
    def is_full(i: int) -> bool:
        return not is_break(i)

    regime = ["break" if is_break(i) else "full" for i in range(n_bars)]
    # Smooth out blips shorter than a bar-pair: a single dropped bar inside a
    # groove is not a section boundary, and one stray loud bar in a breakdown
    # is not a drop. Flip runs shorter than 2 bars to their neighbours so the
    # sections are real regions, not a stutter of contradictory labels.
    def smooth(seq: list[str]) -> list[str]:
        out = list(seq)
        i = 0
        while i < len(out):
            j = i
            while j < len(out) and out[j] == out[i]:
                j += 1
            if j - i < 2 and i > 0 and j < len(out):
                out[i:j] = [out[i - 1]] * (j - i)
            i = j
        return out
    regime = smooth(smooth(regime))

    # Contiguous runs of the same regime.
    runs: list[tuple[str, int, int]] = []
    start = 0
    for i in range(1, n_bars + 1):
        if i == n_bars or regime[i] != regime[start]:
            runs.append((regime[start], start, i))
            start = i

    sections: list[dict[str, Any]] = []

    def add(kind: str, start_bar: int, end_bar: int, confidence: float, reason: str) -> None:
        s = snap_to_grid(int(round(start_bar * bar_ms)), beat_offset_ms, phrase_ms)
        e = snap_to_grid(int(round(end_bar * bar_ms)), beat_offset_ms, phrase_ms)
        if e <= s:
            e = s + int(round(phrase_ms))
        sections.append({"kind": kind, "start_ms": s, "end_ms": e, "confidence": round(min(1.0, max(0.05, confidence)), 3), "reason": reason})

    first_full = next((r for r in runs if r[0] == "full" and r[2] - r[1] >= 2), None)
    last_full = next((r for r in reversed(runs) if r[0] == "full" and r[2] - r[1] >= 2), None)

    # Intro: up to the first sustained groove.
    if first_full and first_full[1] > 0:
        add("intro", 0, first_full[1], 0.55, "stem-band: sparse lead-in before the first sustained groove")
    # Outro: the trailing region after the last sustained groove (already below
    # the groove by construction) if it is long enough to be a real tail.
    if last_full and n_bars - last_full[2] >= 2:
        add("outro", last_full[2], n_bars, 0.5, "stem-band: energy winds down after the last groove")

    prev_full_end = first_full[1] if first_full else 0
    for kind, s, e in runs:
        length = e - s
        if kind == "break" and length >= 2 and (not first_full or s >= first_full[1]):
            # A drums-out / low-energy stretch = breakdown.
            depth = low_thr - (sum(energy[s:e]) / length)
            add("breakdown", s, e, 0.6 + max(0.0, depth), "stem-band: drums drop out / sustained low-energy section")
        if kind == "full" and length >= 2 and s > 0 and regime[s - 1] != "full":
            # Groove slams back in after a break/build = a drop.
            jump = energy[s] - energy[s - 1]
            add("drop", s, min(e, s + 2), 0.62 + max(0.0, jump), "stem-band: drums+bass slam in — drop entry")
            # The bar or two of rising energy before it is the build.
            if s - 1 >= 0 and regime[s - 1] != "full":
                b0 = s
                while b0 - 1 >= prev_full_end and energy[b0 - 1] > low_thr and not is_full(b0 - 1):
                    b0 -= 1
                if s - b0 >= 1:
                    add("build", b0, s, 0.55 + max(0.0, energy[s - 1] - energy[b0]), "stem-band: energy rising into the drop")
            prev_full_end = e

    # One label per phrase boundary, and no contradictions: a phrase that is
    # both a drop entry and (nominally) a build/breakdown keeps only the drop.
    priority = {"drop": 0, "breakdown": 1, "build": 2, "intro": 3, "outro": 3}
    by_start: dict[int, dict[str, Any]] = {}
    for sec in sorted(sections, key=lambda x: (x["start_ms"], priority.get(x["kind"], 9), -x["confidence"])):
        key = sec["start_ms"]
        if key not in by_start:
            by_start[key] = sec
    return sorted(by_start.values(), key=lambda x: (x["start_ms"], priority.get(x["kind"], 9)))


def store_sections(path: str, sections: list[dict[str, Any]], db_path: Path = DEFAULT_LIBRARY_DB) -> None:
    import sqlite3

    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute("DELETE FROM track_dj_structure WHERE path = ?", (path,))
        conn.executemany(
            "INSERT INTO track_dj_structure(path, kind, start_ms, end_ms, confidence, reason) VALUES (?, ?, ?, ?, ?, ?)",
            [(path, s["kind"], s["start_ms"], s["end_ms"], s["confidence"], s["reason"]) for s in sections],
        )
    conn.close()


def main() -> int:
    import json

    parser = argparse.ArgumentParser(description="Stem-aware section/cue detection on the grid.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("detect")
    d.add_argument("path")
    d.add_argument("--json", action="store_true")
    d.add_argument("--no-store", action="store_true")
    d.add_argument("--db", type=Path, default=DEFAULT_LIBRARY_DB)
    b = sub.add_parser("backfill")
    b.add_argument("--limit", type=int, default=0)
    b.add_argument("--db", type=Path, default=DEFAULT_LIBRARY_DB)
    args = parser.parse_args()

    if args.cmd == "detect":
        sections = detect_sections(args.path, args.db)
        if not args.no_store and sections:
            store_sections(args.path, sections, args.db)
        if args.json:
            print(json.dumps(sections, indent=2))
        else:
            print(f"{Path(args.path).name}: {len(sections)} sections")
            for s in sorted(sections, key=lambda x: x["start_ms"]):
                print(f"  {s['start_ms']/1000:6.1f}s–{s['end_ms']/1000:6.1f}s  {s['kind']:<10} conf={s['confidence']:.2f}")
        return 0

    if args.cmd == "backfill":
        import sqlite3

        conn = sqlite3.connect(args.db)
        rows = conn.execute(
            "SELECT DISTINCT source_path FROM track_stem_sets WHERE status='ready' ORDER BY updated_at DESC"
        ).fetchall()
        conn.close()
        paths = [r[0] for r in rows]
        if args.limit:
            paths = paths[: args.limit]
        ok = 0
        for p in paths:
            try:
                sections = detect_sections(p, args.db)
                if sections:
                    store_sections(p, sections, args.db)
                    ok += 1
            except Exception as exc:  # noqa: BLE001
                print(f"  fail {Path(p).name}: {exc}", file=sys.stderr)
        print(json.dumps({"tracks": len(paths), "detected": ok}))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
