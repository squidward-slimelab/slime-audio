import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from slime_audio_autodj import SelectedTrack, filter_defensible_source_tracks, session_payload, validate_no_vanilla_leads
from slime_audio_dj import BeatGrid, StructureWindow, TrackAnalysis


def autodj_args(**overrides):
    values = {
        "max_tracks": 1,
        "default_track_ms": 240_000,
        "max_lead_clip_ms": 90_000,
        "max_fast_lead_clip_ms": 64_000,
        "min_section_clip_ms": 32_000,
        "min_section_confidence": 0.45,
        "require_section_analysis": False,
        "fade_in_ms": 2_500,
        "fade_out_ms": 5_000,
        "base_overlap_ms": 8_000,
        "title": "test",
        "intent": "test",
        "min_tracks": 1,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def selected_track(path="/music/lead.flac"):
    return SelectedTrack(
        path=path,
        artist="Artist",
        title="Long Lead",
        album="Album",
        score=1.0,
        duration_ms=240_000,
        last_played_at=None,
        plays_seen=0,
        reasons=[],
    )


def analysis(path="/music/lead.flac"):
    return TrackAnalysis(
        path=path,
        duration_s=240.0,
        sample_rate=44_100,
        channels=2,
        bpm=120.0,
        beat_offset_ms=0,
        key=None,
        tonic=None,
        mode=None,
        camelot=None,
        energy=0.5,
        loudness_db=-12.0,
        confidence={"bpm": 0.9},
        beatgrid=BeatGrid(bpm=120.0, beat_offset_ms=0, phrase_beats=32, phrase_ms=16_000),
        structure=[
            StructureWindow("intro", 0, 32_000, 0.5, "opening"),
            StructureWindow("drop", 64_000, 128_000, 0.9, "release"),
        ],
    )


class SlimeAudioAutodjTests(unittest.TestCase):
    def test_vanilla_lead_guard_rejects_untouched_long_lead(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-2"],
                        "clips": [
                            {
                                "id": "lead",
                                "deck": "deck-2",
                                "path": "/music/lead.flac",
                                "start_ms": 0,
                                "duration_ms": 240_000,
                                "planner_role": "lead",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(SystemExit):
                validate_no_vanilla_leads(
                    session_path,
                    SimpleNamespace(min_vanilla_check_ms=90_000, max_vanilla_lead_ms=90_000),
                )

    def test_session_payload_caps_leads_to_short_sections(self):
        track = selected_track()
        args = autodj_args()

        payload = session_payload([track], args)

        self.assertEqual(payload["clips"][0]["duration_ms"], 90_000)
        self.assertEqual(payload["notes"]["max_lead_clip_ms"], 90_000)

    def test_session_payload_prefers_detected_structure_window(self):
        track = selected_track()
        args = autodj_args(require_section_analysis=True)

        payload = session_payload([track], args, {track.path: analysis()})

        self.assertEqual(payload["clips"][0]["trim_start_ms"], 64_000)
        self.assertEqual(payload["clips"][0]["duration_ms"], 64_000)
        self.assertEqual(payload["clips"][0]["source_window_reason"], "structure:drop")

    def test_session_payload_requires_structure_when_configured(self):
        track = selected_track()
        args = autodj_args(require_section_analysis=True)

        with self.assertRaises(SystemExit):
            session_payload([track], args)

    def test_session_payload_rejects_phrase_only_analysis_when_structure_required(self):
        track = selected_track()
        args = autodj_args(require_section_analysis=True)
        phrase_only = replace(analysis(), structure=[])

        with self.assertRaises(SystemExit):
            session_payload([track], args, {track.path: phrase_only})

    def test_filter_defensible_source_tracks_drops_unstructured_tracks(self):
        good = selected_track("/music/good.flac")
        bad = selected_track("/music/bad.flac")
        args = autodj_args(require_section_analysis=True)

        accepted, rejected = filter_defensible_source_tracks([bad, good], {good.path: analysis(good.path)}, args)

        self.assertEqual([track.path for track in accepted], [good.path])
        self.assertEqual(rejected[0]["path"], bad.path)

    def test_legacy_filter_rides_do_not_satisfy_vanilla_guard(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-2"],
                        "clips": [
                            {
                                "id": "lead",
                                "deck": "deck-2",
                                "path": "/music/lead.flac",
                                "start_ms": 0,
                                "duration_ms": 240_000,
                                "planner_role": "lead",
                            }
                        ],
                        "deck_automations": [
                            {
                                "target": "deck-2",
                                "param": "highpass_hz",
                                "source_clip_id": "lead",
                                "planner_role": "autodj-lead-filter-ride",
                                "points": [
                                    {"at_ms": 69_000, "value": 30},
                                    {"at_ms": 75_000, "value": 220},
                                    {"at_ms": 85_000, "value": 30},
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(SystemExit):
                validate_no_vanilla_leads(
                    session_path,
                    SimpleNamespace(min_vanilla_check_ms=90_000, max_vanilla_lead_ms=90_000),
                )


if __name__ == "__main__":
    unittest.main()
