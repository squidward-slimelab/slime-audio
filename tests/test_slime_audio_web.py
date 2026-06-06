import json
import sys
import tempfile
import unittest
from array import array
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import slime_audio_web as web
import slime_audio_sets as sets


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
                fake_time.time.return_value = web.parse_timestamp("2026-05-30T12:01:01-0400")
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
        self.assertEqual([lane["id"] for lane in data["dashboard"]["lanes"][:5]], ["deck-3", "deck-1", "deck-5", "deck-2", "deck-4"])
        self.assertEqual(data["dashboard"]["lanes"][2]["label"], "MIC")
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
        self.assertEqual(events[-1]["deck"], "deck-5")

    def test_dashboard_places_vocals_on_dedicated_vocal_lane(self):
        payload = {
            "version": 1,
            "decks": ["deck-1", "deck-2", "deck-3", "deck-4"],
            "clips": [],
            "mic_lean_ins": [{"id": "drop", "start": "00:10.000", "text": "hello"}],
        }

        events = [web.normalize_event(event, None) for event in web.session_events(payload)]
        vocal = next(event for event in events if event["kind"] == "vocal")
        lanes = web.lane_rows(events)

        self.assertEqual(vocal["lane"], "deck-5")
        self.assertEqual(vocal["display_meta"], "mic lean-in | vocal channel")
        self.assertIn("deck-5", [lane["id"] for lane in lanes])

    def test_dashboard_shows_crossfader_routing_and_motion(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1", "deck-2", "deck-3", "deck-4"],
                        "fader_routing": {
                            "deck_assignments": {
                                "deck-1": "A",
                                "deck-2": "B",
                                "deck-3": "A",
                                "deck-4": "B",
                            }
                        },
                        "clips": [
                            {"id": "a", "deck": "deck-1", "path": "/music/A/B/a.flac", "start": 0, "duration": 30_000},
                            {"id": "b", "deck": "deck-2", "path": "/music/A/B/b.flac", "start": 0, "duration": 30_000},
                        ],
                        "automations": [
                            {
                                "target": "crossfader",
                                "param": "position",
                                "points": [{"at": 0, "value": -1}, {"at": 10_000, "value": 1}],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            state_path.write_text(json.dumps({"playhead_ms": 1_000}), encoding="utf-8")
            data = web.load_dashboard_state(state_path, session_path)

        fader_lane = next(lane for lane in data["dashboard"]["lanes"] if lane["id"] == "fader")
        self.assertEqual(data["dashboard"]["session"]["fader_routing"]["deck_assignments"]["deck-1"], "A")
        self.assertEqual(fader_lane["events"][0]["target"], "crossfader")
        self.assertEqual(fader_lane["events"][0]["display_meta"], "crossfader motion")

    def test_dashboard_can_view_archived_set_without_playback_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            sets_dir = temp / "sets"
            active_set = temp / "active-set.json"
            session_path = temp / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "clips": [
                            {"id": "archive-a", "deck": "deck-1", "path": "/music/A/B/archive.flac", "start": 0, "duration": 30_000},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            sets.archive_set(session=session_path, sets_dir=sets_dir, title="Archive A", slug="archive-a")
            active_set.write_text(json.dumps({"slug": "live", "title": "Live Set"}), encoding="utf-8")

            with patch.object(web, "DEFAULT_SETS_DIR", sets_dir), patch.object(web, "DEFAULT_ACTIVE_SET", active_set):
                data = web.load_archived_dashboard_state("archive-a")

        self.assertEqual(data["viewed_set"]["title"], "Archive A")
        self.assertEqual(data["dashboard"]["viewed_set"]["slug"], "archive-a")
        self.assertEqual(data["dashboard"]["active_set"]["slug"], "live")
        self.assertEqual(data["dashboard"]["events"][0]["id"], "archive-a")
        self.assertEqual(data["dashboard"]["events"][0]["status"], "unknown")

    def test_dashboard_labels_instant_double_clips(self):
        event = web.normalize_event(
            web.session_clip_event(
                {
                    "id": "double-a",
                    "deck": "deck-2",
                    "path": "/music/Artist/Album/a.flac",
                    "start": "00:08.000",
                    "duration": "00:08.000",
                    "planner_role": "instant-double",
                    "source_clip_id": "a",
                    "routine_id": "routine-a",
                    "routine_recipe": "stabs",
                    "source_technique": "instant-doubles",
                }
            ),
            None,
        )

        self.assertEqual(event["planner_role"], "instant-double")
        self.assertEqual(event["display_meta"], "stabs routine of a")

    def test_dashboard_places_attached_effect_tracks_under_source_deck(self):
        payload = {
            "version": 1,
            "decks": ["deck-1", "deck-2"],
            "clips": [
                {"id": "lead", "deck": "deck-2", "path": "/music/A/B/a.flac", "start": 0, "duration": 20_000},
                {
                    "id": "lead-brake",
                    "kind": "effect-track",
                    "deck": "deck-1",
                    "attached_deck": "deck-2",
                    "effect_parent_clip_id": "lead",
                    "path": "/music/A/B/a.flac",
                    "start": 8_000,
                    "duration": 500,
                    "routine_recipe": "brake-drop",
                },
            ],
        }

        events = [web.normalize_event(event, None) for event in web.session_events(payload)]
        effect_track = next(event for event in events if event["kind"] == "effect-track")
        lanes = web.lane_rows(events)

        self.assertEqual(effect_track["lane"], "deck-2-fx")
        self.assertEqual(effect_track["display_title"], "brake-drop")
        self.assertEqual(effect_track["display_meta"], "attached to lead | brake-drop")
        self.assertIn("deck-2-fx", [lane["id"] for lane in lanes])

    def test_dashboard_shows_effect_events_on_effects_lane(self):
        payload = {
            "version": 1,
            "decks": ["deck-1"],
            "clips": [{"id": "lead", "deck": "deck-1", "path": "/music/A/B/a.flac", "start": 0, "duration": 20_000}],
            "effects": [
                {
                    "id": "lead-echo",
                    "type": "echo",
                    "target": "lead",
                    "start": 8_000,
                    "duration": 2_000,
                    "tail_ms": 3_000,
                    "routine_id": "routine-a",
                    "routine_recipe": "echo-stabs",
                }
            ],
        }

        events = [web.normalize_event(event, None) for event in web.session_events(payload)]
        effect = next(event for event in events if event["kind"] == "effect")
        lanes = web.lane_rows(events)

        self.assertEqual(effect["lane"], "effects")
        self.assertEqual(effect["display_title"], "echo")
        self.assertEqual(effect["display_meta"], "lead | tail 3.0s | echo-stabs")
        self.assertIn("effects", [lane["id"] for lane in lanes])

    def test_waveform_payload_handles_missing_files(self):
        payload = web.waveform_payload(Path("/tmp/slime-audio-missing-waveform-file.flac"))

        self.assertFalse(payload["available"])
        self.assertEqual(payload["peaks"], [])
        self.assertIn("not found", payload["error"])

    def test_band_envelopes_return_rgb_frequency_bands(self):
        samples = array("h", [0, 8000, -8000, 12000, -12000, 4000, -4000, 0] * 20)

        bands = web.band_envelopes(samples, rate=12_000, bins=8)

        self.assertEqual(set(bands), {"low", "mid", "high"})
        self.assertEqual({len(values) for values in bands.values()}, {8})
        self.assertGreater(max(bands["low"]), 0)
        self.assertGreater(max(bands["mid"]), 0)
        self.assertGreater(max(bands["high"]), 0)

    def test_dashboard_shows_slip_events_on_effects_lane(self):
        payload = {
            "version": 1,
            "decks": ["deck-1", "deck-2"],
            "clips": [
                {"id": "lead", "deck": "deck-1", "path": "/music/A/B/a.flac", "start": 0, "duration": 20_000},
                {"id": "lead-brake", "deck": "deck-2", "path": "/music/A/B/a.flac", "start": 8_000, "duration": 500},
            ],
            "slip_events": [
                {
                    "id": "lead-slip",
                    "source_clip_id": "lead",
                    "target_clip_id": "lead-brake",
                    "start": 8_000,
                    "duration": 500,
                    "source_start_ms": 8_000,
                    "source_resume_ms": 8_500,
                    "routine_recipe": "brake-drop",
                }
            ],
        }

        events = [web.normalize_event(event, None) for event in web.session_events(payload)]
        slip = next(event for event in events if event["kind"] == "slip")

        self.assertEqual(slip["lane"], "effects")
        self.assertEqual(slip["display_title"], "slip/flux")
        self.assertEqual(slip["display_meta"], "lead-brake over lead | brake-drop")

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
                fake_time.time.return_value = web.parse_timestamp("2026-05-30T12:01:01-0400")
                data = web.load_dashboard_state(state_path, session_path)

        dashboard = data["dashboard"]
        self.assertEqual(dashboard["transport"]["status"], "stale")
        self.assertTrue(dashboard["transport"]["stale"])
        self.assertEqual(dashboard["now"]["id"], "b")
        self.assertEqual([event["id"] for event in dashboard["upcoming"]], [])
        self.assertEqual(dashboard["commentary"][0]["id"], "drop")
        self.assertEqual(dashboard["automation"][0]["param"], "gain_db")
        self.assertEqual(dashboard["health"]["runner_state"], "stale")

    def test_dashboard_places_deck_automation_on_deck_lane(self):
        payload = {
            "version": 1,
            "decks": ["deck-1", "deck-2"],
            "clips": [{"id": "lead", "deck": "deck-2", "path": "/music/A/B/a.flac", "start": 0, "duration": 20_000}],
            "deck_automations": [
                {"target": "deck-2", "param": "gain_db", "points": [{"at": 0, "value": -9}, {"at": 20_000, "value": -8}]}
            ],
        }

        events = [web.normalize_event(event, None) for event in web.session_events(payload)]
        automation = next(event for event in events if event["kind"] == "automation")

        self.assertEqual(automation["lane"], "deck-2")
        self.assertEqual(automation["target"], "deck-2")
        self.assertEqual(automation["display_meta"], "deck-2 | gain_db | -9 -> -8")

    def test_native_runner_window_is_not_stale_between_state_writes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "clips": [
                            {"id": "a", "deck": "deck-1", "path": "/music/A/B/a.flac", "start": 0, "duration": 240_000},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            state_path.write_text(
                json.dumps(
                    {
                        "current": "/music/A/B/a.flac",
                        "started_at": "2026-05-30T12:00:00-0400",
                        "window_started_at": "2026-05-30T12:00:00-0400",
                        "window_start_ms": 0,
                        "window_end_ms": 180_000,
                        "updated_at": "2026-05-30T12:00:00-0400",
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(web, "time") as fake_time:
                fake_time.time.return_value = web.parse_timestamp("2026-05-30T12:01:00-0400")
                data = web.load_dashboard_state(state_path, session_path)

        self.assertEqual(data["dashboard"]["transport"]["status"], "playing")
        self.assertFalse(data["dashboard"]["transport"]["stale"])
        self.assertEqual(data["dashboard"]["health"]["runner_state"], "ok")

    def test_choose_state_path_prefers_explicit_path(self):
        explicit = Path("/tmp/example-state.json")

        self.assertEqual(web.choose_state_path(explicit), explicit)

    def test_choose_paths_prefer_active_pointer(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            state_path = temp / "fresh-state.json"
            session_path = temp / "fresh-session.json"
            active_pointer = temp / "active-set.json"
            default_state = temp / "mix-session-state.json"
            default_session = temp / "mix-session.json"
            state_path.write_text("{}", encoding="utf-8")
            session_path.write_text("{}", encoding="utf-8")
            default_state.write_text("{}", encoding="utf-8")
            default_session.write_text("{}", encoding="utf-8")
            active_pointer.write_text(
                json.dumps(
                    {
                        "active_state_path": str(state_path),
                        "active_session_path": str(session_path),
                    }
                ),
                encoding="utf-8",
            )

            with patch.object(web, "DEFAULT_ACTIVE_SET", active_pointer):
                with patch.object(web, "DEFAULT_STATE", default_state):
                    with patch.object(web, "DEFAULT_SESSION", default_session):
                        self.assertEqual(web.choose_state_path(None), state_path)
                        self.assertEqual(web.choose_session_path(None), session_path)

    def test_choose_session_path_returns_none_for_missing_explicit_path(self):
        self.assertIsNone(web.choose_session_path(Path("/tmp/missing-session.json")))


if __name__ == "__main__":
    unittest.main()
