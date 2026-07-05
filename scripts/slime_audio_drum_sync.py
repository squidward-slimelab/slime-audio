#!/usr/bin/env python3
"""Measure whether two records' drums actually line up in a rendered junction.

The beatgrid-based alignment in the mix planner snaps an incoming record onto
the outgoing record's DETECTED grid. That is only as true as the grid: a
misdetected downbeat, or a human drummer who does not hold a constant BPM,
leaves the drums audibly flamming even when the grid math reports a perfect
fit. This tool ignores the grid entirely. For each junction it renders each
record's DRUMS ALONE through the real mixdown filter chain (so it hears
exactly what the room hears, warp and all), then cross-correlates their onset
envelopes at the entry, middle, and exit of the overlap. The peak-correlation
lag is the real drum offset in milliseconds; comparing entry vs exit exposes
drift that a single-point check would miss.

Usage:
    slime_audio_drum_sync.py SESSION.json [--flag-offset-ms 25] [--json]
"""
from __future__ import annotations

import argparse
import array
import copy
import json
import math
import subprocess
import sys
import tempfile
import wave
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from slime_audio_session import (  # noqa: E402
    DEFAULT_LIBRARY_DB,
    action_type,
    apply_master_key,
    apply_master_tempo,
    compile_actions_payload,
    ready_stem_artifacts,
)

MIXDOWN = REPO_ROOT / "scripts" / "slime_audio_session_mixdown.py"
HOP_MS = 4.0
MAX_LAG_MS = 130.0
PROBE_MS = 6_000


def load_leads(payload: dict[str, Any]) -> list[dict[str, Any]]:
    leads = [
        action
        for action in payload.get("actions", []) or []
        if action_type(action) == "load_track" and str(action.get("planner_role") or "") == "lead"
    ]
    return sorted(leads, key=lambda a: int(a.get("at_ms") or 0))


def base_masters(payload: dict[str, Any]) -> dict[str, Any]:
    keep = {}
    for key in ("version", "master_bpm", "master_bpm_automation", "master_key", "master_key_automation", "max_tempo_stretch_pct", "max_key_shift_semitones", "decks"):
        if payload.get(key) is not None:
            keep[key] = copy.deepcopy(payload[key])
    keep.setdefault("version", 1)
    keep.setdefault("decks", ["deck-1", "deck-2", "deck-3", "deck-4"])
    return keep


def render_drums(payload: dict[str, Any], lead: dict[str, Any], start_ms: int, duration_ms: int, sample_rate: int) -> Path | None:
    source_path = str(lead.get("source_path") or lead.get("path") or "")
    if ready_stem_artifacts(DEFAULT_LIBRARY_DB, source_path) is None:
        return None
    session = base_masters(payload)
    drums_load = copy.deepcopy(lead)
    drums_load["play_stems"] = ["drums"]
    drums_load.pop("stems", None)
    drums_load["deck"] = "deck-1"
    session["actions"] = [drums_load]
    tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(session, tmp)
    tmp.close()
    out = Path(tempfile.mktemp(suffix=".wav"))
    cmd = [
        sys.executable,
        str(MIXDOWN),
        tmp.name,
        "--output",
        str(out),
        "--from",
        str(int(start_ms)),
        "--duration",
        str(int(duration_ms)),
        "--sample-rate",
        str(sample_rate),
        "--channels",
        "1",
        "--skip-tts",
        "--no-verify",
        "--db",
        str(DEFAULT_LIBRARY_DB),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    Path(tmp.name).unlink(missing_ok=True)
    if result.returncode != 0 or not out.exists():
        return None
    return out


def onset_envelope(wav_path: Path) -> tuple[list[float], float]:
    with wave.open(str(wav_path), "rb") as wf:
        sr = wf.getframerate()
        n = wf.getnframes()
        width = wf.getsampwidth()
        channels = wf.getnchannels()
        raw = wf.readframes(n)
    if width != 2:
        return [], sr
    samples = array.array("h")
    samples.frombytes(raw)
    if channels > 1:
        samples = array.array("h", samples[::channels])
    hop = max(1, int(sr * HOP_MS / 1000.0))
    energy: list[float] = []
    for i in range(0, len(samples) - hop, hop):
        acc = 0.0
        for s in samples[i : i + hop]:
            acc += s * s
        energy.append(math.sqrt(acc / hop))
    # Smooth, then half-wave-rectified first difference = onset strength.
    onsets: list[float] = [0.0]
    for i in range(1, len(energy)):
        onsets.append(max(0.0, energy[i] - energy[i - 1]))
    return onsets, sr


def normalized(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec] if norm > 0 else vec


def best_lag_ms(a: list[float], b: list[float]) -> tuple[float, float]:
    """Cross-correlate onset envelopes; return (lag_ms, correlation).

    Positive lag = record b's drums land LATE relative to a."""
    if not a or not b:
        return 0.0, 0.0
    a, b = normalized(a), normalized(b)
    max_lag = int(MAX_LAG_MS / HOP_MS)
    best_score, best_lag = -1.0, 0
    n = min(len(a), len(b))
    for lag in range(-max_lag, max_lag + 1):
        score = 0.0
        for i in range(n):
            j = i + lag
            if 0 <= j < n:
                score += a[i] * b[j]
        if score > best_score:
            best_score, best_lag = score, lag
    return best_lag * HOP_MS, best_score


def audit_junction(payload: dict[str, Any], outgoing: dict[str, Any], incoming: dict[str, Any], sample_rate: int) -> dict[str, Any] | None:
    out_start = int(outgoing.get("at_ms") or 0)
    out_end = out_start + int(outgoing.get("duration_ms") or 0)
    in_start = int(incoming.get("at_ms") or 0)
    in_end = in_start + int(incoming.get("duration_ms") or 0)
    overlap_start = max(out_start, in_start)
    overlap_end = min(out_end, in_end)
    if overlap_end - overlap_start < 8_000:
        return None
    render_from = overlap_start
    render_len = min(overlap_end - overlap_start, 96_000)
    out_wav = render_drums(payload, outgoing, render_from, render_len, sample_rate)
    in_wav = render_drums(payload, incoming, render_from, render_len, sample_rate)
    try:
        if out_wav is None or in_wav is None:
            return {
                "incoming": str(incoming.get("id")),
                "outgoing": str(outgoing.get("id")),
                "skipped": "one side has no ready drum stems",
            }
        out_env, _ = onset_envelope(out_wav)
        in_env, _ = onset_envelope(in_wav)
    finally:
        for w in (out_wav, in_wav):
            if w is not None:
                w.unlink(missing_ok=True)
    if not out_env or not in_env:
        return None
    probe_frames = int(PROBE_MS / HOP_MS)
    total = min(len(out_env), len(in_env))
    probes = {
        "entry": (0, probe_frames),
        "middle": (max(0, total // 2 - probe_frames // 2), min(total, total // 2 + probe_frames // 2)),
        "exit": (max(0, total - probe_frames), total),
    }
    measured: dict[str, dict[str, float]] = {}
    for name, (lo, hi) in probes.items():
        if hi - lo < probe_frames // 2:
            continue
        lag_ms, corr = best_lag_ms(out_env[lo:hi], in_env[lo:hi])
        measured[name] = {"offset_ms": round(lag_ms, 1), "correlation": round(corr, 3)}
    if not measured:
        return None
    offsets = [m["offset_ms"] for m in measured.values()]
    max_abs = max(abs(o) for o in offsets)
    drift = (measured.get("exit", {}).get("offset_ms", offsets[-1])) - (measured.get("entry", {}).get("offset_ms", offsets[0]))
    return {
        "incoming": str(incoming.get("id")),
        "outgoing": str(outgoing.get("id")),
        "overlap_ms": overlap_end - overlap_start,
        "probes": measured,
        "max_abs_offset_ms": round(max_abs, 1),
        "drift_ms": round(drift, 1),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify drum sync at every rendered junction by ear-equivalent measurement.")
    parser.add_argument("session", type=Path)
    parser.add_argument("--flag-offset-ms", type=float, default=25.0, help="Offsets at or beyond this (ms) are audible flams.")
    parser.add_argument("--flag-drift-ms", type=float, default=30.0, help="Entry->exit drift at or beyond this (ms) means the tempos are not truly locked.")
    parser.add_argument("--sample-rate", type=int, default=22_050)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    payload = json.loads(args.session.read_text(encoding="utf-8"))
    # Compile + apply masters so rendered warp/pitch match what plays.
    apply_master_key(apply_master_tempo(compile_actions_payload(copy.deepcopy(payload))))
    leads = load_leads(payload)
    results: list[dict[str, Any]] = []
    for outgoing, incoming in zip(leads, leads[1:]):
        audit = audit_junction(payload, outgoing, incoming, args.sample_rate)
        if audit is not None:
            results.append(audit)

    flagged = [
        r
        for r in results
        if not r.get("skipped")
        and (r.get("max_abs_offset_ms", 0) >= args.flag_offset_ms or abs(r.get("drift_ms", 0)) >= args.flag_drift_ms)
    ]
    summary = {
        "session": str(args.session),
        "junctions_measured": len([r for r in results if not r.get("skipped")]),
        "flagged": len(flagged),
        "results": results,
    }
    if args.json:
        print(json.dumps(summary, indent=2))
        return 1 if flagged else 0

    print(f"drum sync — {args.session.name}")
    for r in results:
        if r.get("skipped"):
            print(f"  {r['incoming']:<44} SKIP ({r['skipped']})")
            continue
        probe_str = "  ".join(f"{name}:{p['offset_ms']:+.0f}ms" for name, p in r["probes"].items())
        verdict = "FLAM" if r["max_abs_offset_ms"] >= args.flag_offset_ms else ("DRIFT" if abs(r["drift_ms"]) >= args.flag_drift_ms else "ok")
        print(f"  {r['incoming']:<44} {verdict:>5}  max|off|={r['max_abs_offset_ms']:>4.0f}ms drift={r['drift_ms']:+.0f}ms   {probe_str}")
    print(f"\n{summary['junctions_measured']} junctions measured, {summary['flagged']} flagged (>= {args.flag_offset_ms:.0f}ms offset or {args.flag_drift_ms:.0f}ms drift)")
    print("(offset = incoming drums late(+)/early(-) vs outgoing; measured from rendered audio, grid-independent)")
    return 1 if flagged else 0


if __name__ == "__main__":
    sys.exit(main())
