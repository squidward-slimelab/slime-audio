import json
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from slime_audio_dj import BeatGrid, StructureWindow, TrackAnalysis
from slime_audio_mix_planner import plan_future_mix


def analysis(path: str, *, bpm: float = 120.0, tonic: int = 0, mode: str = "major") -> TrackAnalysis:
    return TrackAnalysis(
        path=path,
        duration_s=180.0,
        sample_rate=44_100,
        channels=2,
        bpm=bpm,
        beat_offset_ms=0,
        key=f"{tonic} {mode}",
        tonic=tonic,
        mode=mode,
        camelot="8B",
        energy=0.25,
        loudness_db=-12.0,
        confidence={"bpm": 0.8, "key": 0.7},
        beatgrid=BeatGrid(bpm=bpm, beat_offset_ms=0, phrase_beats=32, phrase_ms=16_000),
        structure=[
            StructureWindow("intro", 0, 16_000, 0.5, "opening phrase"),
            StructureWindow("build", 8_000, 16_000, 0.95, "early rise"),
            StructureWindow("drop", 64_000, 80_000, 0.9, "release"),
        ],
    )


class SlimeAudioMixPlannerTests(unittest.TestCase):
    def test_future_mix_planner_adds_blends_doubles_and_automation(self):
        payload = {
            "version": 1,
            "decks": ["deck-3", "deck-1", "deck-2", "deck-4"],
            "clips": [
                {"id": "current", "deck": "deck-3", "path": "/music/current.flac", "start_ms": 0, "duration_ms": 120_000},
                {"id": "next", "deck": "deck-1", "path": "/music/next.flac", "start_ms": 140_000, "duration_ms": 120_000},
                {"id": "after", "deck": "deck-2", "path": "/music/after.flac", "start_ms": 280_000, "duration_ms": 120_000},
            ],
            "mic_lean_ins": [],
            "automations": [],
        }

        planned, moves = plan_future_mix(
            payload,
            {
                "/music/current.flac": analysis("/music/current.flac"),
                "/music/next.flac": analysis("/music/next.flac", tonic=0),
                "/music/after.flac": analysis("/music/after.flac", tonic=7),
            },
            lock_before_ms=130_000,
            double_every=1,
        )

        clips = {clip["id"]: clip for clip in planned["clips"]}
        self.assertLess(clips["after"]["start_ms"], clips["next"]["start_ms"] + clips["next"]["duration_ms"])
        self.assertGreater(clips["after"]["fade_in_ms"], 0)
        self.assertIn("double-next", clips)
        self.assertEqual(clips["double-next"]["planner_role"], "drop-double")
        self.assertEqual(clips["double-next"]["trim_start_ms"], 64_000)
        self.assertTrue(any(move.kind == "blend" for move in moves))
        self.assertTrue(any(move.kind == "double" for move in moves))
        self.assertTrue(any(item.get("planner_role") == "mix-planner" for item in planned["automations"]))


if __name__ == "__main__":
    unittest.main()
