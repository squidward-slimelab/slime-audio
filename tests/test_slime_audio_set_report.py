"""Rubric signal extraction from session JSON (slime_audio_set_report).

Scope: the objective half of skills/slime-audio-dj/RUBRIC.md — blend/cut
ratio, tempo identity against library BPMs, transform/layer/motion counts.
These numbers only locate problems; the grading judgment stays with ears.
"""

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import slime_audio_set_report as report


def synthetic_session() -> dict:
    return {
        "title": "Test Set",
        "clips": [
            {"id": "lead-001", "path": "/music/a.flac", "start_ms": 0, "duration_ms": 240000, "tempo_shift_pct": -10.0, "pitch_shift_semitones": 0},
            {"id": "lead-002", "path": "/music/b.flac", "start_ms": 230000, "duration_ms": 240000, "tempo_shift_pct": 12.5, "pitch_shift_semitones": 1},
            {"id": "lead-003", "path": "/music/c.flac", "start_ms": 470000, "duration_ms": 240000, "tempo_shift_pct": 0.0, "pitch_shift_semitones": 0},
            {"id": "bed-001", "path": "/music/d.flac", "start_ms": 100000, "duration_ms": 60000, "play_stems": ["drums", "bass"], "tempo_shift_pct": 0.0, "pitch_shift_semitones": 0},
        ],
        "transition_plans": [
            {"decision": "blend", "overlap_ms": 10000},
            {"decision": "cut", "overlap_ms": 0},
        ],
        "deck_automations": [
            {"param": "lowpass_hz", "points": [{"at_ms": 0, "value": 22050}, {"at_ms": 5000, "value": 2200}]},
            {"param": "eq_low_db", "points": [{"at_ms": 0, "value": 0}, {"at_ms": 5000, "value": 0}]},
        ],
        "actions": [],
        "automations": [],
        "mic_lean_ins": [],
    }


class SetReportTest(unittest.TestCase):
    def setUp(self):
        self.session = synthetic_session()
        self.leads = report.lead_clips(self.session)

    def test_stem_clips_are_not_leads(self):
        self.assertEqual([c["id"] for c in self.leads], ["lead-001", "lead-002", "lead-003"])

    def test_transition_stats(self):
        stats = report.transition_stats(self.session)
        self.assertEqual(stats["blends"], 1)
        self.assertEqual(stats["cuts"], 1)
        self.assertEqual(stats["blend_ratio"], 0.5)
        self.assertEqual(stats["mean_overlap_ms"], 10000)

    def test_tempo_identity_uses_rendered_bpm(self):
        # a: 100 * 0.90 = 90, b: 80 * 1.125 = 90, c: 120 neutral = 120 → modal 90, 2/3 locked
        bpm = {"/music/a.flac": 100.0, "/music/b.flac": 80.0, "/music/c.flac": 120.0}
        identity = report.tempo_identity(self.leads, bpm)
        self.assertEqual(identity["modal_bpm"], 90)
        self.assertEqual(identity["analyzed_leads"], 3)
        self.assertAlmostEqual(identity["lock_coverage"], 2 / 3, places=3)

    def test_tempo_identity_counts_octave_renders_as_locked(self):
        # Against a 90 master: 90 straight, 180 double-time, 45 half-time all lock.
        clips = [
            {"id": "l1", "path": "/m/a", "source_bpm": 90.0, "tempo_shift_pct": 0.0},
            {"id": "l2", "path": "/m/b", "source_bpm": 178.0, "tempo_shift_pct": (180.0 / 178.0 - 1.0) * 100.0},
            {"id": "l3", "path": "/m/c", "source_bpm": 130.0, "tempo_shift_pct": 0.0},
        ]
        identity = report.tempo_identity(clips, {}, master_bpm=90.0)
        self.assertEqual(identity["analyzed_leads"], 3)
        self.assertAlmostEqual(identity["lock_coverage"], 2 / 3, places=3)

    def test_transform_and_layer_and_motion_stats(self):
        transforms = report.transform_stats(self.leads)
        self.assertEqual(transforms["reshaped_leads"], 2)
        self.assertEqual(transforms["neutral_leads"], 1)
        layers = report.layer_stats(self.session)
        self.assertEqual(layers["stem_layers"], 1)
        motion = report.motion_stats(self.session)
        self.assertEqual(motion["automation_ramps"], 2)
        self.assertEqual(motion["moving_ramps"], 1)

    def test_source_bpm_lookup_reads_preferred_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "library.sqlite3"
            with sqlite3.connect(db_path) as conn:
                conn.execute("CREATE TABLE tracks (preferred_path TEXT, tunebat_bpm REAL)")
                conn.execute("INSERT INTO tracks VALUES ('/music/a.flac', 100.0)")
                conn.execute("INSERT INTO tracks VALUES ('/music/unanalyzed.flac', NULL)")
            lookup = report.source_bpm_lookup(db_path, ["/music/a.flac", "/music/unanalyzed.flac", "/music/missing.flac"])
            self.assertEqual(lookup, {"/music/a.flac": 100.0})


if __name__ == "__main__":
    unittest.main()
