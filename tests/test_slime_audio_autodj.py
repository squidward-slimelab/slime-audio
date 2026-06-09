import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from slime_audio_autodj import SelectedTrack, session_payload, validate_no_vanilla_leads


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
        track = SelectedTrack(
            path="/music/lead.flac",
            artist="Artist",
            title="Long Lead",
            album="Album",
            score=1.0,
            duration_ms=240_000,
            last_played_at=None,
            plays_seen=0,
            reasons=[],
        )
        args = SimpleNamespace(
            max_tracks=1,
            default_track_ms=240_000,
            max_lead_clip_ms=90_000,
            fade_in_ms=2_500,
            fade_out_ms=5_000,
            base_overlap_ms=8_000,
            title="test",
            intent="test",
            min_tracks=1,
        )

        payload = session_payload([track], args)

        self.assertEqual(payload["clips"][0]["duration_ms"], 90_000)
        self.assertEqual(payload["notes"]["max_lead_clip_ms"], 90_000)

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
