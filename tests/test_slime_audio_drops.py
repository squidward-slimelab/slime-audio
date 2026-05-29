import unittest

from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from slime_audio_drops import Drop, ProgressClock, due_delay_ms, parse_bool, parse_offset_ms, parse_target, status_matches_drop


class SlimeAudioDropTests(unittest.TestCase):
    def test_parse_offset_accepts_clock_strings(self):
        self.assertEqual(parse_offset_ms("1:02.500"), 62500)
        self.assertEqual(parse_offset_ms("1:00:02"), 3602000)

    def test_parse_target_defaults_port(self):
        self.assertEqual(parse_target("SPATULA").port, 47777)
        self.assertEqual(parse_target("SPATULA:48888").port, 48888)

    def test_parse_bool_accepts_common_strings(self):
        self.assertTrue(parse_bool("true"))
        self.assertFalse(parse_bool("false"))
        self.assertTrue(parse_bool(None, True))

    def test_status_matches_track_uri(self):
        status = {"item": {"uri": "spotify:track:abc", "id": "abc", "name": "Song"}}

        self.assertTrue(status_matches_drop(status, Drop("one", "hello", 1000, track_uri="spotify:track:abc")))
        self.assertFalse(status_matches_drop(status, Drop("two", "hello", 1000, track_uri="spotify:track:def")))

    def test_paused_status_is_not_due(self):
        status = {"is_playing": False, "progress_ms": 900, "item": {"uri": "spotify:track:abc"}}
        drop = Drop("one", "hello", 1000, track_uri="spotify:track:abc")

        self.assertIsNone(due_delay_ms(status, drop, lead_ms=3000, late_tolerance_ms=1200, delay_pad_ms=250))

    def test_due_delay_includes_remaining_time_and_pad(self):
        status = {"is_playing": True, "progress_ms": 900, "item": {"uri": "spotify:track:abc"}}
        drop = Drop("one", "hello", 1000, track_uri="spotify:track:abc")

        self.assertEqual(due_delay_ms(status, drop, lead_ms=3000, late_tolerance_ms=1200, delay_pad_ms=250), 350)

    def test_late_drop_is_skipped(self):
        status = {"is_playing": True, "progress_ms": 5000, "item": {"uri": "spotify:track:abc"}}
        drop = Drop("one", "hello", 1000, track_uri="spotify:track:abc")

        self.assertIsNone(due_delay_ms(status, drop, lead_ms=3000, late_tolerance_ms=1200, delay_pad_ms=250))

    def test_progress_clock_estimates_when_spotify_progress_is_stale(self):
        clock = ProgressClock()
        status = {"is_playing": True, "progress_ms": 0, "item": {"uri": "spotify:track:abc"}}

        clock.update(status)
        clock._base_time -= 2

        updated = clock.update(status)
        self.assertGreaterEqual(updated["progress_ms"], 1900)
        self.assertFalse(updated["progress_known"])

    def test_progress_clock_trusts_observed_track_change_as_start(self):
        clock = ProgressClock()
        first = {"is_playing": True, "progress_ms": 0, "item": {"uri": "spotify:track:abc"}}
        second = {"is_playing": True, "progress_ms": 0, "item": {"uri": "spotify:track:def"}}

        clock.update(first)

        self.assertTrue(clock.update(second)["progress_known"])

    def test_progress_clock_freezes_while_paused(self):
        clock = ProgressClock()
        playing = {"is_playing": True, "progress_ms": 0, "item": {"uri": "spotify:track:abc"}}
        paused = {"is_playing": False, "progress_ms": 0, "item": {"uri": "spotify:track:abc"}}

        clock.update(playing)
        clock._base_time -= 2
        frozen = clock.update(paused)["progress_ms"]
        clock._base_time -= 2

        self.assertEqual(clock.update(paused)["progress_ms"], frozen)


if __name__ == "__main__":
    unittest.main()
