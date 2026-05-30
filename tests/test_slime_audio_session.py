import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from slime_audio_session import load_session, parse_ms, session_summary
from slime_audio_session import main as session_main


def run_cli(argv: list[str]) -> int:
    original_argv = sys.argv[:]
    try:
        sys.argv = argv
        with redirect_stdout(StringIO()):
            return session_main()
    finally:
        sys.argv = original_argv


class SlimeAudioSessionTests(unittest.TestCase):
    def test_parse_ms_accepts_clock_strings(self):
        self.assertEqual(parse_ms("01:02.500", "time"), 62_500)
        self.assertEqual(parse_ms("1:02:03", "time"), 3_723_000)

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


if __name__ == "__main__":
    unittest.main()
