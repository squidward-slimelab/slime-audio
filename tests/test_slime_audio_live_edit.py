import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from slime_audio_live_edit import main as live_edit_main
from slime_audio_session import load_session


def run_cli(argv: list[str]) -> int:
    original_argv = sys.argv[:]
    try:
        sys.argv = argv
        with redirect_stdout(StringIO()):
            return live_edit_main()
    finally:
        sys.argv = original_argv


class SlimeAudioLiveEditTests(unittest.TestCase):
    def test_live_edit_defaults_to_state_lock_and_writes_history(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            session_path = temp / "mix-session.json"
            state_path = temp / "mix-session-state.json"
            history_path = temp / "play-history.jsonl"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "clips": [
                            {"id": "current", "deck": "deck-1", "path": "/music/a.flac", "start": 0, "duration": 20_000}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            state_path.write_text(json.dumps({"playhead_ms": 10_000}), encoding="utf-8")

            self.assertEqual(
                run_cli(
                    [
                        "slime_audio_live_edit.py",
                        "add-action",
                        "--session",
                        str(session_path),
                        "--state",
                        str(state_path),
                        "--history-log",
                        str(history_path),
                        "--actor",
                        "test-dj",
                        "--reason",
                        "unit test",
                        "--action-json",
                        json.dumps(
                            {
                                "type": "load_track",
                                "id": "future",
                                "deck": "deck-1",
                                "source_path": "/music/b.flac",
                                "at_ms": 30_000,
                                "duration_ms": 12_000,
                                "stems": {
                                    "vocals": "/stems/b/vocals.flac",
                                    "drums": "/stems/b/drums.flac",
                                    "bass": "/stems/b/bass.flac",
                                    "other": "/stems/b/other.flac",
                                },
                            }
                        ),
                    ]
                ),
                0,
            )
            session = load_session(session_path)
            history = [json.loads(line) for line in history_path.read_text(encoding="utf-8").splitlines()]

        # An untouched load renders the original file: a plain clip segment.
        self.assertEqual([clip.id for clip in session.clips], ["current", "future"])
        self.assertEqual(session.stem_groups, [])
        self.assertEqual(history[-1]["type"], "live_edit_applied")
        self.assertEqual(history[-1]["command"], "add-action")
        self.assertEqual(history[-1]["actor"], "test-dj")
        self.assertEqual(history[-1]["reason"], "unit test")
        self.assertEqual(history[-1]["live_edit_lock_ms"], 10_000)
        self.assertEqual(history[-1]["affected"][0]["id"], "future")

    def test_live_edit_rejects_past_edit_before_history_write(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            session_path = temp / "mix-session.json"
            state_path = temp / "mix-session-state.json"
            history_path = temp / "play-history.jsonl"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "clips": [
                            {"id": "past", "deck": "deck-1", "path": "/music/a.flac", "start": 0, "duration": 20_000}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            state_path.write_text(json.dumps({"playhead_ms": 10_000}), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "live edit lock"):
                run_cli(
                    [
                        "slime_audio_live_edit.py",
                        "move",
                        "--session",
                        str(session_path),
                        "--state",
                        str(state_path),
                        "--history-log",
                        str(history_path),
                        "--id",
                        "past",
                        "--start",
                        "00:02.000",
                    ]
                )

        self.assertFalse(history_path.exists())


if __name__ == "__main__":
    unittest.main()
