import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import slime_audio_web as web


class SlimeAudioWebTests(unittest.TestCase):
    def test_session_dashboard_includes_now_and_timeline(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1", "deck-2", "deck-3", "deck-4"],
                        "clips": [
                            {"id": "a", "deck": "deck-1", "path": "/music/Artist/Album/a.flac", "start": 0, "duration": 30_000},
                            {"id": "b", "deck": "deck-2", "path": "/music/Artist/Album/b.flac", "start": 30_000, "duration": 40_000},
                            {"id": "c", "deck": "deck-3", "path": "/music/Artist/Album/c.flac", "start": 70_000, "duration": 50_000},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            state_path.write_text(
                json.dumps(
                    {
                        "current": "/music/Artist/Album/b.flac",
                        "resolved_current": "/music/Artist/Album/b.flac",
                        "started_at": "2026-05-30T12:00:00-0400",
                        "playhead_ms": 70_000,
                        "window_started_at": "2026-05-30T12:00:00-0400",
                        "window_start_ms": 30_000,
                        "window_end_ms": 70_000,
                        "updated_at": "2026-05-30T12:00:50-0400",
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(web, "time") as fake_time:
                fake_time.time.return_value = web.parse_timestamp("2026-05-30T12:01:00-0400")
                data = web.load_dashboard_state(state_path, session_path)

        self.assertEqual(data["now"]["track"]["title"], "b")
        self.assertEqual(data["now"]["elapsed_ms"], 40000)
        self.assertEqual(data["session"]["source"], "mix-session")
        self.assertEqual(data["session"]["timeline_mode"], "native")
        self.assertEqual(data["session"]["raw"]["decks"], ["deck-1", "deck-2", "deck-3", "deck-4"])
        self.assertEqual([event["start_ms"] for event in data["session"]["events"]], [0, 30_000, 70_000])
        self.assertEqual(data["session"]["events"][1]["duration_ms"], 40_000)
        self.assertEqual(data["dashboard"]["schema_version"], 1)
        self.assertEqual(data["dashboard"]["transport"]["status"], "playing")
        self.assertEqual(data["dashboard"]["transport"]["playhead_ms"], 70_000)
        self.assertEqual(data["dashboard"]["now"]["id"], "c")
        self.assertEqual([lane["id"] for lane in data["dashboard"]["lanes"][:4]], ["deck-3", "deck-1", "deck-2", "deck-4"])
        self.assertEqual(data["dashboard"]["session"]["counts"]["song"], 3)
        self.assertEqual([event["status"] for event in data["dashboard"]["events"] if event["kind"] == "song"], ["done", "done", "current"])

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

    def test_dashboard_view_model_separates_stale_missing_and_future_events(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1", "deck-2", "deck-3", "deck-4"],
                        "clips": [
                            {"id": "a", "deck": "deck-1", "path": "/music/A/B/a.flac", "start": 0, "duration": 30_000},
                            {"id": "b", "deck": "deck-2", "path": "/music/A/B/b.flac", "start": 40_000, "duration": 30_000},
                        ],
                        "mic_lean_ins": [{"id": "drop", "start": 45_000, "text": "talk here"}],
                        "automations": [{"target": "b", "param": "gain_db", "points": [{"at": 42_000, "value": -6}, {"at": 46_000, "value": 0}]}],
                    }
                ),
                encoding="utf-8",
            )
            state_path.write_text(
                json.dumps(
                    {
                        "current": None,
                        "playhead_ms": 45_000,
                        "window_started_at": "2026-05-30T12:00:00-0400",
                        "window_start_ms": 40_000,
                        "window_end_ms": 70_000,
                        "updated_at": "2026-05-30T12:00:00-0400",
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(web, "time") as fake_time:
                fake_time.time.return_value = web.parse_timestamp("2026-05-30T12:01:00-0400")
                data = web.load_dashboard_state(state_path, session_path)

        dashboard = data["dashboard"]
        self.assertEqual(dashboard["transport"]["status"], "stale")
        self.assertTrue(dashboard["transport"]["stale"])
        self.assertEqual(dashboard["now"]["id"], "b")
        self.assertEqual([event["id"] for event in dashboard["upcoming"]], [])
        self.assertEqual(dashboard["commentary"][0]["id"], "drop")
        self.assertEqual(dashboard["automation"][0]["param"], "gain_db")
        self.assertEqual(dashboard["health"]["runner_state"], "stale")

    def test_choose_state_path_prefers_explicit_path(self):
        explicit = Path("/tmp/example-state.json")

        self.assertEqual(web.choose_state_path(explicit), explicit)

    def test_choose_session_path_returns_none_for_missing_explicit_path(self):
        self.assertIsNone(web.choose_session_path(Path("/tmp/missing-session.json")))


if __name__ == "__main__":
    unittest.main()
