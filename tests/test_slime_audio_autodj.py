import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from slime_audio_autodj import add_lead_filter_rides, validate_no_vanilla_leads


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

    def test_lead_activity_rides_satisfy_vanilla_guard(self):
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
                        "deck_automations": [],
                    }
                ),
                encoding="utf-8",
            )
            args = SimpleNamespace(
                min_vanilla_check_ms=90_000,
                max_vanilla_lead_ms=90_000,
                lead_activity_interval_ms=75_000,
                lead_activity_highpass_hz=220.0,
            )

            activity = add_lead_filter_rides(session_path, args)
            guard = validate_no_vanilla_leads(session_path, args)

        self.assertEqual(activity["added"], 3)
        self.assertEqual(guard["checked"], 1)


if __name__ == "__main__":
    unittest.main()
