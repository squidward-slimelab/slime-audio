import json
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from slime_audio_session import load_session
from slime_audio_session_mixdown import build_filter_complex, ffmpeg_command, session_duration_ms, shift_session_window


class SlimeAudioSessionMixdownTests(unittest.TestCase):
    def test_mixdown_filter_includes_lean_in_duck_and_lowpass(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1", "deck-2"],
                        "clips": [
                            {
                                "id": "bed",
                                "deck": "deck-1",
                                "path": "/music/bed.flac",
                                "start": "00:00.000",
                                "duration": "00:30.000",
                                "gain_db": -3,
                            }
                        ],
                        "mic_lean_ins": [
                            {
                                "id": "lean",
                                "start": "00:10.000",
                                "text": "quick note",
                                "volume": 1.8,
                                "ducking": {
                                    "target": "master",
                                    "param": "duck_volume",
                                    "points": [{"at": "00:09.750", "value": 0.45}, {"at": "00:13.000", "value": 1.0}],
                                },
                                "lowpass": {
                                    "target": "master",
                                    "param": "lowpass_hz",
                                    "points": [{"at": "00:09.750", "value": 1400}, {"at": "00:13.000", "value": 22050}],
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            session = load_session(session_path)
            filters = build_filter_complex(session, {"lean": Path("/tmp/lean.wav")}, 48_000, 2)

        self.assertIn("adelay=10000:all=1", filters)
        self.assertIn("volume=1.800000,adelay=10000:all=1", filters)
        self.assertIn("volume=enable='between(t,9.750,13.000)':volume=0.450000", filters)
        self.assertIn("lowpass=enable='between(t,9.750,13.000)':f=1400.000", filters)
        self.assertIn("amix=inputs=2", filters)

    def test_ffmpeg_command_maps_session_inputs_and_output(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "clips": [{"id": "bed", "deck": "deck-1", "path": "/music/bed.flac", "start": 0, "duration": 1000}],
                        "mic_lean_ins": [{"id": "lean", "start": 500, "text": "hi"}],
                    }
                ),
                encoding="utf-8",
            )
            session = load_session(session_path)
            command = ffmpeg_command(session, {"lean": Path("/tmp/lean.wav")}, Path("/tmp/out.wav"), 48_000, 2)

        self.assertEqual(command[0], "ffmpeg")
        self.assertIn("/music/bed.flac", command)
        self.assertIn("/tmp/lean.wav", command)
        self.assertEqual(command[-1], "/tmp/out.wav")

    def test_mixdown_filter_renders_tempo_and_pitch_shift_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "clips": [
                            {
                                "id": "shifted",
                                "deck": "deck-1",
                                "path": "/music/shifted.flac",
                                "start": 0,
                                "duration": 10_000,
                                "tempo_shift_pct": 3.0,
                                "pitch_shift_semitones": 1,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            filters = build_filter_complex(load_session(session_path), {}, 48_000, 2)

        self.assertIn("asetrate=50854", filters)
        self.assertIn("aresample=48000", filters)
        self.assertIn("atempo=0.943874", filters)
        self.assertIn("atempo=1.030000", filters)
        self.assertIn("atrim=start=0.000:duration=10.300", filters)

    def test_session_duration_includes_lean_in_effect_window(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "clips": [],
                        "mic_lean_ins": [
                            {
                                "id": "lean",
                                "start": "00:10.000",
                                "text": "hi",
                                "lowpass": {
                                    "target": "master",
                                    "param": "lowpass_hz",
                                    "points": [{"at": "00:09.750", "value": 1400}, {"at": "00:15.000", "value": 22050}],
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            session = load_session(session_path)

        self.assertEqual(session_duration_ms(session), 15_000)

    def test_overlapping_clips_only_fade_when_explicitly_planned(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1", "deck-2"],
                        "clips": [
                            {"id": "a", "deck": "deck-1", "path": "/music/a.flac", "start": 0, "duration": 30_000},
                            {
                                "id": "b",
                                "deck": "deck-2",
                                "path": "/music/b.flac",
                                "start": 24_000,
                                "duration": 30_000,
                                "fade_in_ms": 4_000,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            filters = build_filter_complex(load_session(session_path), {}, 48_000, 2)

        self.assertNotIn("afade=t=out:st=24.000:d=6.000", filters)
        self.assertIn("afade=t=in:st=0:d=4.000", filters)

    def test_shift_session_window_trims_current_clip_and_shifts_future_events(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1", "deck-2"],
                        "clips": [
                            {
                                "id": "current",
                                "deck": "deck-1",
                                "path": "/music/current.flac",
                                "start": 5_000,
                                "trim_start": 1_000,
                                "duration": 20_000,
                            },
                            {
                                "id": "future",
                                "deck": "deck-2",
                                "path": "/music/future.flac",
                                "start": 30_000,
                                "duration": 10_000,
                            },
                        ],
                        "mic_lean_ins": [{"id": "lean", "start": 32_000, "text": "incoming"}],
                    }
                ),
                encoding="utf-8",
            )
            shifted = shift_session_window(load_session(session_path), 12_000)
            filters = build_filter_complex(shifted, {"lean": Path("/tmp/lean.wav")}, 48_000, 2)

        self.assertEqual(shifted.clips[0].id, "current")
        self.assertEqual(shifted.clips[0].start_ms, 0)
        self.assertEqual(shifted.clips[0].trim_start_ms, 8_000)
        self.assertEqual(shifted.clips[0].duration_ms, 13_000)
        self.assertEqual(shifted.clips[1].start_ms, 18_000)
        self.assertEqual(shifted.mic_lean_ins[0].start_ms, 20_000)
        self.assertIn("atrim=start=8.000:duration=13.000", filters)
        self.assertIn("adelay=0:all=1", filters)
        self.assertIn("adelay=18000:all=1", filters)

    def test_shift_session_window_converts_timeline_overlap_to_source_trim_with_tempo_shift(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "clips": [
                            {
                                "id": "fast",
                                "deck": "deck-1",
                                "path": "/music/fast.flac",
                                "start": 0,
                                "trim_start": 1_000,
                                "duration": 20_000,
                                "tempo_shift_pct": 5,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            shifted = shift_session_window(load_session(session_path), 4_000)
            filters = build_filter_complex(shifted, {}, 48_000, 2)

        self.assertEqual(shifted.clips[0].trim_start_ms, 5_200)
        self.assertEqual(shifted.clips[0].duration_ms, 16_000)
        self.assertIn("atrim=start=5.200:duration=16.800", filters)
        self.assertIn("atempo=1.050000", filters)

    def test_shift_session_window_limits_duration(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "clips": [
                            {
                                "id": "bed",
                                "deck": "deck-1",
                                "path": "/music/bed.flac",
                                "start": 5_000,
                                "trim_start": 1_000,
                                "duration": 20_000,
                            },
                            {
                                "id": "too-late",
                                "deck": "deck-1",
                                "path": "/music/late.flac",
                                "start": 35_000,
                                "duration": 10_000,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            shifted = shift_session_window(load_session(session_path), 10_000, 8_000)
            filters = build_filter_complex(shifted, {}, 48_000, 2, 8_000)

        self.assertEqual([clip.id for clip in shifted.clips], ["bed"])
        self.assertEqual(shifted.clips[0].trim_start_ms, 6_000)
        self.assertEqual(shifted.clips[0].duration_ms, 8_000)
        self.assertIn("atrim=duration=8.000,alimiter", filters)


if __name__ == "__main__":
    unittest.main()
