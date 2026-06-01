import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from slime_audio_session import load_session, parse_ms, playhead_ms_from_state, session_summary
from slime_audio_session import main as session_main
from slime_audio_session_mixdown import shift_session_window


def run_cli(argv: list[str]) -> int:
    original_argv = sys.argv[:]
    try:
        sys.argv = argv
        with redirect_stdout(StringIO()):
            return session_main()
    finally:
        sys.argv = original_argv


def write_analysis_cache(path: Path, track: str, *, bpm: float, beat_offset_ms: int = 0, confidence: float = 0.9) -> None:
    path.write_text(
        json.dumps(
            {
                "cache-key": {
                    "path": track,
                    "duration_s": 120.0,
                    "sample_rate": 48000,
                    "channels": 2,
                    "bpm": bpm,
                    "beat_offset_ms": beat_offset_ms,
                    "key": None,
                    "tonic": None,
                    "mode": None,
                    "camelot": None,
                    "energy": 0.5,
                    "loudness_db": -12.0,
                    "confidence": {"bpm": confidence, "key": 0.0},
                    "beatgrid": {
                        "bpm": bpm,
                        "beat_offset_ms": beat_offset_ms,
                        "phrase_beats": 32,
                        "phrase_ms": round((60_000 / bpm) * 32),
                    },
                    "structure": [],
                }
            }
        ),
        encoding="utf-8",
    )


class SlimeAudioSessionTests(unittest.TestCase):
    def test_parse_ms_accepts_clock_strings(self):
        self.assertEqual(parse_ms("01:02.500", "time"), 62_500)
        self.assertEqual(parse_ms("1:02:03", "time"), 3_723_000)

    def test_playhead_from_playlist_state_uses_duration_cache(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            (temp / "timeline-duration-cache.json").write_text(
                json.dumps({"/music/a.flac": 10_000, "/music/b.flac": 20_000}),
                encoding="utf-8",
            )
            state = temp / "state.json"
            state.write_text(
                json.dumps(
                    {
                        "started_at": "2026-05-30T19:00:00-0400",
                        "index": 2,
                        "order": ["/music/a.flac", "/music/b.flac", "/music/c.flac"],
                    }
                ),
                encoding="utf-8",
            )

            state_payload = json.loads(state.read_text(encoding="utf-8"))
            state_payload["started_at"] = "1970-01-01T00:00:00+0000"
            state.write_text(json.dumps(state_payload), encoding="utf-8")
            playhead = playhead_ms_from_state(state, now=5)

        self.assertEqual(playhead, 35_000)

    def test_session_allows_non_contiguous_overlapping_clips_on_different_decks(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "session.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1", "deck-2"],
                        "clips": [
                            {
                                "id": "a",
                                "deck": "deck-1",
                                "path": "/music/a.flac",
                                "start": "00:00.000",
                                "trim_start": "00:32.000",
                                "duration": "00:30.000",
                            },
                            {
                                "id": "b",
                                "deck": "deck-2",
                                "path": "/music/b.flac",
                                "start": "00:10.000",
                                "trim_start": "01:12.000",
                                "duration": "00:16.000",
                            },
                            {
                                "id": "c",
                                "deck": "deck-1",
                                "path": "/music/c.flac",
                                "start": "01:20.000",
                                "duration": "00:20.000",
                            },
                        ],
                        "mic_lean_ins": [
                            {
                                "id": "mic-1",
                                "start": "00:08.000",
                                "text": "quick note",
                                "ducking": {
                                    "target": "master",
                                    "param": "duck_volume",
                                    "points": [
                                        {"at": "00:07.900", "value": 0.5},
                                        {"at": "00:10.000", "value": 1.0},
                                    ],
                                },
                                "lowpass": {
                                    "target": "master",
                                    "param": "lowpass_hz",
                                    "points": [
                                        {"at": "00:07.900", "value": 1400},
                                        {"at": "00:10.000", "value": 22050},
                                    ],
                                },
                            }
                        ],
                        "automations": [
                            {
                                "target": "b",
                                "param": "gain_db",
                                "points": [
                                    {"at": "00:10.000", "value": -18.0},
                                    {"at": "00:14.000", "value": -4.0},
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            session = load_session(path)
            summary = session_summary(session)

        self.assertEqual(summary["clip_count"], 3)
        self.assertEqual(summary["mic_lean_in_count"], 1)
        self.assertEqual(summary["automation_count"], 3)
        self.assertEqual(summary["clips_by_deck"]["deck-1"][1]["id"], "c")

    def test_session_rejects_same_deck_overlap_when_durations_are_known(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "session.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "clips": [
                            {"id": "a", "deck": "deck-1", "path": "/music/a.flac", "start": 0, "duration": 20_000},
                            {"id": "b", "deck": "deck-1", "path": "/music/b.flac", "start": 10_000, "duration": 20_000},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "overlap"):
                load_session(path)

    def test_session_rejects_unplanned_manual_param(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "session.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "clips": [
                            {"id": "a", "deck": "deck-1", "path": "/music/a.flac", "start": 0, "duration": 20_000}
                        ],
                        "automations": [
                            {"target": "a", "param": "panic_button", "points": [{"at": 0, "value": 1}]}
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "not an automatable param"):
                load_session(path)

    def test_cli_edits_session_plan(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "session.json"
            self.assertEqual(
                run_cli(
                    [
                    "slime_audio_session.py",
                    "add-clip",
                    str(path),
                    "--create",
                    "--id",
                    "intro",
                    "--deck",
                    "deck-1",
                    "--path",
                    "/music/intro.flac",
                    "--start",
                    "00:00.000",
                    "--trim-start",
                    "00:32.000",
                    "--duration",
                    "00:16.000",
                    ]
                ),
                0,
            )
            self.assertEqual(
                run_cli(
                    [
                    "slime_audio_session.py",
                    "add-mic",
                    str(path),
                    "--id",
                    "mic-1",
                    "--start",
                    "00:08.000",
                    "--text",
                    "incoming",
                    "--duck-volume",
                    "0.5",
                    "--lowpass-hz",
                    "1200",
                    ]
                ),
                0,
            )
            self.assertEqual(
                run_cli(
                    [
                    "slime_audio_session.py",
                    "move",
                    str(path),
                    "--id",
                    "intro",
                    "--start",
                    "00:04.000",
                    ]
                ),
                0,
            )
            self.assertEqual(
                run_cli(
                    [
                    "slime_audio_session.py",
                    "automate",
                    str(path),
                    "--target",
                    "intro",
                    "--param",
                    "gain_db",
                    "--points-json",
                    '[{"at":"00:04.000","value":-18},{"at":"00:08.000","value":-3}]',
                    ]
                ),
                0,
            )

            session = load_session(path)
            summary = session_summary(session)

        self.assertEqual(summary["clip_count"], 1)
        self.assertEqual(summary["mic_lean_in_count"], 1)
        self.assertEqual(summary["automation_count"], 3)
        self.assertEqual(summary["clips_by_deck"]["deck-1"][0]["start_ms"], 4_000)
        lean_in = session.mic_lean_ins[0]
        self.assertEqual([effect.param for effect in lean_in.effects], ["duck_volume", "lowpass_hz"])

    def test_cli_imports_playlist_as_timestamped_timeline(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            playlist = temp / "playlist.txt"
            session_path = temp / "session.json"
            playlist.write_text("/music/one.flac\n/music/two.flac\n/music/three.flac\n", encoding="utf-8")

            self.assertEqual(
                run_cli(
                    [
                        "slime_audio_session.py",
                        "import-playlist",
                        str(session_path),
                        "--playlist",
                        str(playlist),
                        "--start",
                        "00:05.000",
                        "--decks",
                        "deck-1,deck-2",
                        "--default-duration",
                        "00:10.000",
                        "--overlap-ms",
                        "2000",
                        "--no-probe",
                    ]
                ),
                0,
            )

            session = load_session(session_path)

        self.assertEqual([clip.start_ms for clip in session.clips], [5_000, 13_000, 21_000])
        self.assertEqual([clip.deck for clip in session.clips], ["deck-1", "deck-2", "deck-1"])
        self.assertEqual([clip.duration_ms for clip in session.clips], [10_000, 10_000, 10_000])

    def test_cli_remove_deletes_targeted_automation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "session.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "clips": [
                            {"id": "a", "deck": "deck-1", "path": "/music/a.flac", "start": 0, "duration": 20_000}
                        ],
                        "automations": [
                            {"target": "a", "param": "gain_db", "points": [{"at": 0, "value": -6}]}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(run_cli(["slime_audio_session.py", "remove", str(path), "--id", "a"]), 0)

            payload = json.loads(path.read_text(encoding="utf-8"))
            session = load_session(path)

        self.assertEqual(payload["clips"], [])
        self.assertEqual(payload["automations"], [])
        self.assertEqual(session.clips, [])

    def test_live_edit_lock_rejects_past_edits_without_force(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "session.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "clips": [
                            {"id": "past", "deck": "deck-1", "path": "/music/a.flac", "start": 0, "duration": 20_000},
                            {"id": "future", "deck": "deck-1", "path": "/music/b.flac", "start": 30_000, "duration": 20_000},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "live edit lock"):
                run_cli(
                    [
                        "slime_audio_session.py",
                        "move",
                        str(path),
                        "--id",
                        "past",
                        "--start",
                        "00:12.000",
                        "--lock-before",
                        "00:10.000",
                    ]
                )
            self.assertEqual(
                run_cli(
                    [
                        "slime_audio_session.py",
                        "move",
                        str(path),
                        "--id",
                        "future",
                        "--start",
                        "00:35.000",
                        "--lock-before",
                        "00:10.000",
                    ]
                ),
                0,
            )

            session = load_session(path)

        self.assertEqual(session.clips[1].start_ms, 35_000)

    def test_cli_beat_jump_offsets_trim_by_half_beat_from_cached_grid(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            path = temp / "session.json"
            cache = temp / "dj-cache.json"
            track = "/music/a.flac"
            write_analysis_cache(cache, track, bpm=120, beat_offset_ms=0)
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "clips": [
                            {
                                "id": "double",
                                "deck": "deck-1",
                                "path": track,
                                "start": 10_000,
                                "trim_start": 1_000,
                                "duration": 20_000,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                run_cli(
                    [
                        "slime_audio_session.py",
                        "beat-jump",
                        str(path),
                        "--id",
                        "double",
                        "--beats",
                        "1/2",
                        "--cache",
                        str(cache),
                    ]
                ),
                0,
            )
            session = load_session(path)
            shifted = shift_session_window(session, 10_500)

        self.assertEqual(session.clips[0].trim_start_ms, 1_250)
        self.assertEqual(shifted.clips[0].trim_start_ms, 1_750)

    def test_cli_beat_jump_offsets_start_by_half_beat_from_cached_grid(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            path = temp / "session.json"
            cache = temp / "dj-cache.json"
            track = "/music/b.flac"
            write_analysis_cache(cache, track, bpm=128, beat_offset_ms=0)
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "clips": [
                            {"id": "offset", "deck": "deck-1", "path": track, "start": 8_000, "trim_start": 0, "duration": 20_000}
                        ],
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                run_cli(
                    [
                        "slime_audio_session.py",
                        "beat-jump",
                        str(path),
                        "--id",
                        "offset",
                        "--beats",
                        "1/2",
                        "--field",
                        "start",
                        "--cache",
                        str(cache),
                    ]
                ),
                0,
            )
            session = load_session(path)

        self.assertEqual(session.clips[0].start_ms, 8_234)

    def test_cli_beat_jump_rejects_low_confidence_grid_without_force(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            path = temp / "session.json"
            cache = temp / "dj-cache.json"
            track = "/music/loose.flac"
            write_analysis_cache(cache, track, bpm=120, confidence=0.1)
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "clips": [
                            {"id": "loose", "deck": "deck-1", "path": track, "start": 10_000, "trim_start": 1_000, "duration": 20_000}
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "confidence too low"):
                run_cli(
                    [
                        "slime_audio_session.py",
                        "beat-jump",
                        str(path),
                        "--id",
                        "loose",
                        "--beats",
                        "1",
                        "--cache",
                        str(cache),
                    ]
                )

    def test_cli_mashup_bed_adds_filter_and_gain_automation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "session.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1", "deck-2"],
                        "clips": [
                            {"id": "bed", "deck": "deck-1", "path": "/music/bed.flac", "start": 0, "duration": 60_000},
                            {"id": "lead", "deck": "deck-2", "path": "/music/lead.flac", "start": 16_000, "duration": 32_000},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                run_cli(
                    [
                        "slime_audio_session.py",
                        "mashup-bed",
                        str(path),
                        "--bed-id",
                        "bed",
                        "--start",
                        "00:16.000",
                        "--end",
                        "00:48.000",
                        "--gain-db",
                        "-10",
                        "--lowpass-hz",
                        "1800",
                        "--highpass-hz",
                        "100",
                    ]
                ),
                0,
            )
            session = load_session(path)

        self.assertEqual([automation.param for automation in session.automations], ["gain_db", "lowpass_hz", "highpass_hz"])
        self.assertEqual([automation.target for automation in session.automations], ["bed", "bed", "bed"])
        self.assertEqual(session.automations[0].points[0].at_ms, 16_000)
        self.assertEqual(session.automations[0].points[-1].at_ms, 48_000)


if __name__ == "__main__":
    unittest.main()
