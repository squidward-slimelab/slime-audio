import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from slime_audio_dj import (
    TrackAnalysis,
    camelot,
    estimate_bpm,
    key_match,
    relative_tonic,
    semitone_distance,
    transition_plan,
)


def track(path: str, bpm: float, tonic: int, mode: str, energy: float = 0.2) -> TrackAnalysis:
    return TrackAnalysis(
        path=path,
        duration_s=180.0,
        sample_rate=44100,
        channels=2,
        bpm=bpm,
        beat_offset_ms=0,
        key=f"{tonic} {mode}",
        tonic=tonic,
        mode=mode,
        camelot=camelot(tonic, mode),
        energy=energy,
        loudness_db=-12.0,
        confidence={"bpm": 0.9, "key": 0.9},
    )


class SlimeAudioDjTests(unittest.TestCase):
    def test_camelot_relative_major_minor_pair(self):
        self.assertEqual(camelot(9, "minor"), "8A")
        self.assertEqual(camelot(0, "major"), "8B")
        self.assertEqual(relative_tonic(9, "minor"), 0)
        self.assertEqual(relative_tonic(0, "major"), 9)

    def test_minor_to_major_rotation_scores_as_key_match_without_pitch_shift(self):
        source = track("a-minor.wav", 124, 9, "minor")
        target = track("c-major.wav", 124, 0, "major")

        score, pitch_shift, relation, notes = key_match(source, target, max_pitch_shift=2)

        self.assertGreater(score, 0.9)
        self.assertEqual(pitch_shift, 0)
        self.assertEqual(relation, "relative major/minor rotation")
        self.assertTrue(any("mode-rotation" in note for note in notes))

    def test_pitch_shift_prefers_small_same_mode_moves(self):
        source = track("c-minor.wav", 128, 0, "minor")
        target = track("d-minor.wav", 128, 2, "minor")

        score, pitch_shift, relation, _notes = key_match(source, target, max_pitch_shift=2)

        self.assertGreater(score, 0.75)
        self.assertEqual(pitch_shift, -2)
        self.assertEqual(relation, "pitch-shift same mode")

    def test_transition_plan_combines_tempo_key_and_energy(self):
        source = track("a-minor.wav", 124, 9, "minor", energy=0.3)
        target = track("c-major.wav", 126, 0, "major", energy=0.32)

        plan = transition_plan(source, target)

        self.assertGreater(plan.score, 0.85)
        self.assertEqual(plan.pitch_shift_semitones, 0)
        self.assertEqual(plan.key_relation, "relative major/minor rotation")
        self.assertAlmostEqual(plan.target_tempo_shift_pct, 1.61, places=2)

    def test_semitone_distance_uses_shortest_signed_path(self):
        self.assertEqual(semitone_distance(11, 1), 2)
        self.assertEqual(semitone_distance(1, 11), -2)

    def test_estimate_bpm_locks_simple_pulse_train(self):
        envelope = [0.0] * 240
        # 120 BPM at 46 ms frames is roughly every 11 frames.
        for index in range(0, len(envelope), 11):
            envelope[index] = 1000.0

        bpm, _offset_ms, confidence = estimate_bpm(envelope)

        self.assertIsNotNone(bpm)
        self.assertAlmostEqual(bpm, 118.58, places=1)
        self.assertGreaterEqual(confidence, 0.0)


if __name__ == "__main__":
    unittest.main()
