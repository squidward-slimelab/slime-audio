import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import slime_audio_web as web


class SlimeAudioWebTests(unittest.TestCase):
    def test_playlist_dashboard_includes_now_and_timeline(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "completed": ["/music/Artist/Album/a.flac"],
                        "current": "/music/Artist/Album/b.flac",
                        "index": 1,
                        "order": ["/music/Artist/Album/a.flac", "/music/Artist/Album/b.flac", "/music/Artist/Album/c.flac"],
                        "resolved_current": "/music/Artist/Album/b.flac",
                        "started_at": "2026-05-30T12:00:00-0400",
                        "next_transition": {"next": "/music/Artist/Album/c.flac", "key_relation": "relative", "pitch_shift_semitones": 0, "target_tempo_shift_pct": 1.2},
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(web, "time") as fake_time:
                fake_time.time.return_value = web.parse_timestamp("2026-05-30T12:01:00-0400")
                data = web.load_dashboard_state(state_path, None)

        self.assertEqual(data["now"]["track"]["title"], "b")
        self.assertEqual(data["now"]["elapsed_ms"], 60000)
        self.assertEqual(data["session"]["raw"]["source"], "playlist-runner-state")
        self.assertEqual(data["session"]["raw"]["decks"], ["deck-3", "deck-1", "deck-2", "deck-4"])
        self.assertEqual([event["status"] for event in data["session"]["events"]], ["done", "current", "planned"])
        self.assertIsNone(data["session"]["events"][1]["duration_ms"])

    def test_session_events_include_clips_vocals_and_automation(self):
        payload = {
            "version": 1,
            "decks": ["deck-1", "deck-2"],
            "clips": [
                {
                    "id": "intro",
                    "deck": "deck-1",
                    "path": "/music/Artist/Album/a.flac",
                    "start": "00:00.000",
                    "duration": "00:30.000",
                }
            ],
            "mic_lean_ins": [
                {
                    "id": "drop",
                    "start": "00:10.000",
                    "text": "hello",
                    "ducking": {"target": "master", "param": "duck_volume", "points": [{"at": "00:09.750", "value": 0.4}, {"at": "00:12.000", "value": 1.0}]},
                }
            ],
            "automations": [
                {"target": "intro", "param": "gain_db", "points": [{"at": 0, "value": -12}, {"at": 4000, "value": 0}]}
            ],
        }

        events = web.session_events(payload)

        self.assertEqual([event["kind"] for event in events], ["song", "automation", "automation", "vocal"])
        self.assertEqual(events[0]["title"], "a")
        self.assertEqual(events[-1]["text"], "hello")

    def test_choose_state_path_prefers_explicit_path(self):
        explicit = Path("/tmp/example-state.json")

        self.assertEqual(web.choose_state_path(explicit), explicit)


if __name__ == "__main__":
    unittest.main()
