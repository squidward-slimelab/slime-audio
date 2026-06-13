import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from slime_audio_beat_jumps import refine_loop_anchor_from_envelope, rewrite_payload, snap_duration_to_grid, snap_ms_to_grid
from slime_audio_dj import beat_grid


class SlimeAudioBeatJumpTests(unittest.TestCase):
    def test_snap_ms_to_grid_uses_nearest_beat(self):
        grid = beat_grid(120.0, 250)

        self.assertEqual(snap_ms_to_grid(1240, grid), 1250)
        self.assertEqual(snap_ms_to_grid(1510, grid), 1750)

    def test_snap_duration_to_grid_uses_nearest_beat_count(self):
        grid = beat_grid(120.0, 0)

        self.assertEqual(snap_duration_to_grid(1900, grid), 2000)

    def test_rewrite_snaps_jump_and_loop_actions_to_session_grid(self):
        payload = {
            "bpm": 120,
            "actions": [
                {"type": "load_track", "id": "lead", "deck": "deck-1", "source_path": "/music/a.flac", "at_ms": 0},
                {"type": "loop_start", "id": "loop", "target": "lead", "at_ms": 1240, "position_ms": 3200, "length_ms": 1900, "exit_ms": 3900},
                {"type": "jump_to_cue", "id": "jump", "target": "lead", "cue_id": "drop", "at_ms": 5510},
            ],
        }

        rewritten, report = rewrite_payload(payload)

        actions = {action["id"]: action for action in rewritten["actions"]}
        self.assertEqual(actions["loop"]["at_ms"], 1000)
        self.assertEqual(actions["loop"]["exit_ms"], 4000)
        self.assertEqual(actions["jump"]["at_ms"], 5500)
        self.assertGreaterEqual(len(report), 3)

    def test_refine_loop_anchor_prefers_matching_transient_boundaries(self):
        envelope = [10.0] * 900
        onsets = [0.0] * 900
        onsets[540] = 10_000.0
        onsets[670] = 9_500.0
        onsets[510] = 5_000.0
        onsets[640] = 4_500.0

        refined = refine_loop_anchor_from_envelope(
            envelope,
            onsets,
            current_ms=5100,
            length_ms=1300,
            frame_ms=10,
            search_ms=500,
        )

        self.assertEqual(refined, 5300)


if __name__ == "__main__":
    unittest.main()
