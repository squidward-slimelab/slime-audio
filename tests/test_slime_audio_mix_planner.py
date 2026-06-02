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


def write_analysis_cache(path: Path, tracks: list[str], *, bpm: float = 120.0) -> None:
    path.write_text(
        json.dumps(
            {
                f"cache-{index}": {
                    "path": track,
                    "duration_s": 180.0,
                    "sample_rate": 48000,
                    "channels": 2,
                    "bpm": bpm,
                    "beat_offset_ms": 0,
                    "confidence": {"bpm": 0.9, "key": 0.0},
                    "beatgrid": {
                        "bpm": bpm,
                        "beat_offset_ms": 0,
                        "phrase_beats": 32,
                        "phrase_ms": round((60_000 / bpm) * 32),
                    },
                    "structure": [],
                }
                for index, track in enumerate(tracks)
            }
        ),
        encoding="utf-8",
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
                "/music/after.flac": analysis("/music/after.flac", tonic=0),
            },
            lock_before_ms=130_000,
            double_every=1,
        )

        clips = {clip["id"]: clip for clip in planned["clips"]}
        self.assertLess(clips["after"]["start_ms"], clips["next"]["start_ms"] + clips["next"]["duration_ms"])
        self.assertGreater(clips["after"]["fade_in_ms"], 0)
        self.assertIn("double-after", clips)
        self.assertEqual(clips["double-after"]["planner_role"], "drop-double")
        self.assertEqual(clips["double-after"]["trim_start_ms"], 64_000)
        self.assertEqual(
            clips["double-after"]["start_ms"] + clips["double-after"]["duration_ms"],
            clips["after"]["start_ms"],
        )
        self.assertTrue(any(move.kind == "blend" for move in moves))
        self.assertTrue(any(move.kind == "double" for move in moves))
        self.assertTrue(any(item.get("planner_role") == "mix-planner" for item in planned["automations"]))

    def test_incompatible_tracks_do_not_overlap_or_double(self):
        payload = {
            "version": 1,
            "decks": ["deck-3", "deck-1", "deck-2", "deck-4"],
            "clips": [
                {"id": "current", "deck": "deck-3", "path": "/music/current.flac", "start_ms": 0, "duration_ms": 120_000},
                {"id": "bad", "deck": "deck-1", "path": "/music/bad.flac", "start_ms": 140_000, "duration_ms": 120_000},
            ],
            "mic_lean_ins": [],
            "automations": [],
        }

        planned, moves = plan_future_mix(
            payload,
            {
                "/music/current.flac": analysis("/music/current.flac", bpm=120, tonic=0, mode="major"),
                "/music/bad.flac": analysis("/music/bad.flac", bpm=132, tonic=6, mode="minor"),
            },
            lock_before_ms=0,
            double_every=1,
        )

        clips = {clip["id"]: clip for clip in planned["clips"]}
        self.assertEqual(clips["bad"]["start_ms"], clips["current"]["start_ms"] + clips["current"]["duration_ms"])
        self.assertEqual(clips["bad"]["fade_in_ms"], 0)
        self.assertFalse(any(clip.get("planner_role") == "drop-double" for clip in planned["clips"]))
        self.assertIn("cut", moves[-1].reason)

    def test_small_rendered_tempo_and_pitch_shift_can_enable_overlay(self):
        payload = {
            "version": 1,
            "decks": ["deck-3", "deck-1"],
            "clips": [
                {"id": "current", "deck": "deck-3", "path": "/music/current.flac", "start_ms": 0, "duration_ms": 120_000},
                {"id": "next", "deck": "deck-1", "path": "/music/next.flac", "start_ms": 140_000, "duration_ms": 120_000},
            ],
            "mic_lean_ins": [],
            "automations": [],
        }

        planned, moves = plan_future_mix(
            payload,
            {
                "/music/current.flac": analysis("/music/current.flac", bpm=120, tonic=0, mode="major"),
                "/music/next.flac": analysis("/music/next.flac", bpm=121, tonic=2, mode="major"),
            },
            lock_before_ms=0,
            double_every=10,
        )

        clips = {clip["id"]: clip for clip in planned["clips"]}
        self.assertLess(clips["next"]["start_ms"], clips["current"]["start_ms"] + clips["current"]["duration_ms"])
        self.assertNotEqual(clips["next"]["pitch_shift_semitones"], 0)
        self.assertNotEqual(clips["next"]["tempo_shift_pct"], 0)
        self.assertIn("overlap", moves[-1].reason)

        no_pitch, no_pitch_moves = plan_future_mix(
            payload,
            {
                "/music/current.flac": analysis("/music/current.flac", bpm=120, tonic=0, mode="major"),
                "/music/next.flac": analysis("/music/next.flac", bpm=121, tonic=2, mode="major"),
            },
            lock_before_ms=0,
            max_pitch_shift_semitones=0,
        )
        no_pitch_clips = {clip["id"]: clip for clip in no_pitch["clips"]}
        self.assertEqual(no_pitch_clips["next"]["start_ms"], no_pitch_clips["current"]["start_ms"] + no_pitch_clips["current"]["duration_ms"])
        self.assertIn("cut", no_pitch_moves[-1].reason)

    def test_future_mix_planner_can_add_real_routines_from_cache(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = Path(temp_dir) / "dj-cache.json"
            tracks = ["/music/current.flac", "/music/next.flac", "/music/after.flac"]
            write_analysis_cache(cache, tracks)
            payload = {
                "version": 1,
                "decks": ["deck-3", "deck-1", "deck-2", "deck-4"],
                "clips": [
                    {"id": "current", "deck": "deck-3", "path": tracks[0], "start_ms": 0, "duration_ms": 120_000},
                    {"id": "next", "deck": "deck-1", "path": tracks[1], "start_ms": 140_000, "duration_ms": 120_000},
                    {"id": "after", "deck": "deck-2", "path": tracks[2], "start_ms": 280_000, "duration_ms": 120_000},
                ],
                "mic_lean_ins": [],
                "automations": [],
            }

            planned, moves = plan_future_mix(
                payload,
                {track: analysis(track) for track in tracks},
                lock_before_ms=130_000,
                double_every=10,
                routine_every=1,
                routine_cache_path=cache,
            )

        self.assertTrue(any(move.kind == "routine" for move in moves))
        self.assertTrue(any(clip.get("routine_recipe") == "echo-stabs" for clip in planned["clips"]))
        self.assertTrue(any(clip.get("routine_recipe") == "loop-roll" for clip in planned["clips"]))
        self.assertTrue(any(effect.get("routine_recipe") == "echo-stabs" for effect in planned.get("effects", [])))
        self.assertTrue(any(slip.get("routine_recipe") == "loop-roll" for slip in planned.get("slip_events", [])))


if __name__ == "__main__":
    unittest.main()
