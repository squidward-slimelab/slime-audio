import json
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from slime_audio_dj import BeatGrid, CuePoint, StructureWindow, TrackAnalysis
from slime_audio_mix_planner import plan_future_mix


def analysis(
    path: str,
    *,
    bpm: float = 120.0,
    tonic: int = 0,
    mode: str = "major",
    duration_s: float = 180.0,
    drop_ms: int = 64_000,
    explicit_drop_cue: bool = False,
) -> TrackAnalysis:
    return TrackAnalysis(
        path=path,
        duration_s=duration_s,
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
            StructureWindow("drop", drop_ms, drop_ms + 16_000, 0.9, "release"),
        ],
        cues=[
            CuePoint("drop", "drop", drop_ms, drop_ms + 16_000, 0.9, "test", True, "explicit test drop"),
            CuePoint("hook", "hook", drop_ms, drop_ms + 16_000, 0.9, "test", True, "explicit test hook"),
        ]
        if explicit_drop_cue
        else None,
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
    def test_planner_syncs_placeholder_duration_to_analyzed_audio(self):
        payload = {
            "version": 1,
            "decks": ["deck-3", "deck-1"],
            "clips": [
                {"id": "current", "deck": "deck-3", "path": "/music/current.flac", "start_ms": 0, "duration_ms": 120_000},
                {"id": "short", "deck": "deck-1", "path": "/music/short.flac", "start_ms": 140_000, "duration_ms": 240_000},
                {"id": "long", "deck": "deck-3", "path": "/music/long.flac", "start_ms": 380_000, "duration_ms": 240_000},
            ],
            "mic_lean_ins": [],
            "automations": [],
        }

        planned, _moves = plan_future_mix(
            payload,
            {
                "/music/current.flac": analysis("/music/current.flac"),
                "/music/short.flac": analysis("/music/short.flac", duration_s=173.8),
                "/music/long.flac": analysis("/music/long.flac", duration_s=276.898),
            },
            lock_before_ms=0,
            double_every=0,
        )

        clips = {clip["id"]: clip for clip in planned["clips"]}
        self.assertEqual(clips["short"]["duration_ms"], 173_800)
        self.assertEqual(clips["short"]["fade_out_ms"], 0)
        self.assertEqual(clips["long"]["duration_ms"], 276_898)
        self.assertEqual(clips["long"]["fade_out_ms"], 0)

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
        self.assertLessEqual(clips["after"]["fade_in_ms"], 8_000)
        self.assertEqual(clips["after"]["fade_out_ms"], 0)
        self.assertIn("double-after", clips)
        self.assertEqual(clips["double-after"]["planner_role"], "drop-double")
        self.assertEqual(clips["double-after"]["trim_start_ms"], 64_000)
        self.assertEqual(
            clips["double-after"]["start_ms"] + clips["double-after"]["duration_ms"],
            clips["after"]["start_ms"],
        )
        self.assertTrue(any(move.kind == "blend" for move in moves))
        self.assertTrue(any(move.kind == "double" for move in moves))
        self.assertFalse(
            any(
                item.get("planner_role") == "mix-planner"
                and item.get("target") == "master"
                and item.get("param") == "duck_volume"
                for item in planned.get("automations", [])
            )
        )
        deck_automations = planned["deck_automations"]
        self.assertTrue(any(item["param"] == "lowpass_hz" and item["related_clip_id"] == "after" for item in deck_automations))
        self.assertTrue(any(item["param"] == "highpass_hz" and item["related_clip_id"] == "next" for item in deck_automations))
        self.assertTrue(any(item["param"] == "eq_low_db" for item in deck_automations))
        self.assertTrue(all(item.get("planner_role") in {"mix-planner-filter-carve", "mix-planner-eq-carve"} for item in deck_automations))
        transition_plans = {item["to_clip_id"]: item for item in planned["transition_plans"]}
        self.assertEqual(transition_plans["next"]["planner_role"], "mix-planner-transition-plan")
        self.assertEqual(transition_plans["next"]["from_clip_id"], "current")
        self.assertEqual(transition_plans["next"]["decision"], "blend")
        self.assertGreater(transition_plans["next"]["overlap_ms"], 0)
        self.assertIn("score", transition_plans["next"])
        self.assertEqual(transition_plans["after"]["from_clip_id"], "next")
        self.assertEqual(transition_plans["after"]["pitch_shift_semitones"], clips["after"]["pitch_shift_semitones"])

    def test_future_mix_planner_cuts_when_analysis_is_missing(self):
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
                # "after" has no analysis at all: the pair decision must fall
                # back to an explicit cut, never a blind overlap.
                "/music/current.flac": analysis("/music/current.flac"),
                "/music/next.flac": analysis("/music/next.flac", tonic=0),
            },
            lock_before_ms=130_000,
            double_every=1,
        )

        clips = {clip["id"]: clip for clip in planned["clips"]}
        self.assertEqual(clips["after"]["start_ms"], clips["next"]["start_ms"] + clips["next"]["duration_ms"])
        self.assertEqual(clips["after"]["fade_in_ms"], 0)
        self.assertNotIn("double-after", clips)
        transition_plans = {item["to_clip_id"]: item for item in planned["transition_plans"]}
        self.assertEqual(transition_plans["after"]["decision"], "cut")
        self.assertEqual(transition_plans["after"]["overlap_ms"], 0)
        self.assertTrue(any(move.kind == "cut" for move in moves))

    def test_future_mix_planner_does_not_preview_incoming_tracks_by_default(self):
        payload = {
            "version": 1,
            "decks": ["deck-3", "deck-1", "deck-2", "deck-4"],
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
                "/music/current.flac": analysis("/music/current.flac"),
                "/music/next.flac": analysis("/music/next.flac"),
            },
            lock_before_ms=0,
        )

        self.assertTrue(any(move.kind == "blend" for move in moves))
        self.assertFalse(any(clip.get("planner_role") == "drop-double" for clip in planned["clips"]))

    def test_transition_bass_restore_anchors_to_incoming_drop_cue(self):
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

        planned, _moves = plan_future_mix(
            payload,
            {
                "/music/current.flac": analysis("/music/current.flac"),
                "/music/next.flac": analysis("/music/next.flac", drop_ms=36_000, explicit_drop_cue=True),
            },
            lock_before_ms=0,
        )

        clips = {clip["id"]: clip for clip in planned["clips"]}
        drop_at = clips["next"]["start_ms"] + 36_000
        incoming_low = next(
            automation
            for automation in planned["deck_automations"]
            if automation["target"] == clips["next"]["deck"]
            and automation["param"] == "eq_low_db"
            and automation["related_clip_id"] == "current"
        )
        incoming_highpass = next(
            automation
            for automation in planned["deck_automations"]
            if automation["target"] == clips["next"]["deck"]
            and automation["param"] == "highpass_hz"
            and automation["related_clip_id"] == "current"
        )
        self.assertEqual(incoming_low["points"][-1]["at_ms"], drop_at)
        self.assertEqual(incoming_highpass["points"][-1]["at_ms"], drop_at)

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
        self.assertEqual(planned.get("deck_automations", []), [])
        self.assertIn("cut", moves[-1].reason)
        transition_plan = planned["transition_plans"][-1]
        self.assertEqual(transition_plan["from_clip_id"], "current")
        self.assertEqual(transition_plan["to_clip_id"], "bad")
        self.assertEqual(transition_plan["decision"], "cut")
        self.assertEqual(transition_plan["overlap_ms"], 0)
        self.assertIn("below overlay threshold", transition_plan["reason"])

    def test_planner_can_rewrite_only_a_bounded_future_block(self):
        payload = {
            "version": 1,
            "decks": ["deck-3", "deck-1", "deck-2", "deck-4"],
            "clips": [
                {"id": "current", "deck": "deck-3", "path": "/music/current.flac", "start_ms": 0, "duration_ms": 120_000},
                {"id": "inside", "deck": "deck-1", "path": "/music/inside.flac", "start_ms": 140_000, "duration_ms": 120_000},
                {"id": "outside", "deck": "deck-2", "path": "/music/outside.flac", "start_ms": 320_000, "duration_ms": 120_000},
            ],
            "transition_plans": [
                {"id": "old-inside", "planner_role": "mix-planner-transition-plan", "to_clip_id": "inside", "start_ms": 140_000},
                {"id": "old-outside", "planner_role": "mix-planner-transition-plan", "to_clip_id": "outside", "start_ms": 320_000},
            ],
            "mic_lean_ins": [],
            "automations": [],
        }

        planned, moves = plan_future_mix(
            payload,
            {
                "/music/current.flac": analysis("/music/current.flac"),
                "/music/inside.flac": analysis("/music/inside.flac"),
            },
            lock_before_ms=130_000,
            plan_until_ms=260_000,
            double_every=10,
        )

        clips = {clip["id"]: clip for clip in planned["clips"]}
        self.assertLess(clips["inside"]["start_ms"], 140_000)
        self.assertEqual(clips["outside"]["start_ms"], 320_000)
        self.assertTrue(any(move.clip_id == "inside" for move in moves))
        self.assertFalse(any(move.clip_id == "outside" for move in moves))
        transition_plans = {item["id"]: item for item in planned["transition_plans"]}
        self.assertNotIn("old-inside", transition_plans)
        self.assertIn("old-outside", transition_plans)
        self.assertTrue(any(item.get("to_clip_id") == "inside" for item in planned["transition_plans"]))

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

if __name__ == "__main__":
    unittest.main()
