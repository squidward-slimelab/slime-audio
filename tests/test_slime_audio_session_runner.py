import json
import sys
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import slime_audio_session_runner as runner


class SlimeAudioSessionRunnerTests(unittest.TestCase):
    def test_window_selects_overlapping_clips(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1", "deck-2"],
                        "clips": [
                            {"id": "a", "deck": "deck-1", "path": "/music/a.flac", "start": 0, "duration": 20_000},
                            {"id": "b", "deck": "deck-2", "path": "/music/b.flac", "start": 15_000, "duration": 20_000},
                            {"id": "c", "deck": "deck-1", "path": "/music/c.flac", "start": 40_000, "duration": 10_000},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            session = runner.load_session(session_path)

        self.assertEqual([clip.id for clip in runner.clips_in_window(session, 10_000, 30_000)], ["a", "b"])

    def test_dry_run_renders_session_window_from_state_playhead(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            session_path = temp / "session.json"
            state_path = temp / "state.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "clips": [
                            {"id": "a", "deck": "deck-1", "path": "/music/a.flac", "start": 0, "duration": 20_000},
                            {"id": "b", "deck": "deck-1", "path": "/music/b.flac", "start": 25_000, "duration": 20_000},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            state_path.write_text(json.dumps({"playhead_ms": 25_000}), encoding="utf-8")
            args = runner.parse_args_from(
                [
                    "--session",
                    str(session_path),
                    "--state",
                    str(state_path),
                    "--window-ms",
                    "10000",
                    "--target",
                    "all",
                    "--dry-run",
                ]
            )

            with patch.object(runner, "render_window", wraps=runner.render_window) as render:
                with redirect_stdout(StringIO()):
                    self.assertEqual(runner.run_session(args), 0)

            state = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertEqual(state["timeline_mode"], "native-session-runner")
        self.assertEqual(state["window_start_ms"], 25_000)
        self.assertEqual(state["window_end_ms"], 35_000)
        self.assertEqual([clip["id"] for clip in state["current_clips"]], ["b"])
        render.assert_called_once()
        self.assertEqual(render.call_args.args[1], 25_000)
        self.assertEqual(render.call_args.args[2], 10_000)

    def test_stream_command_targets_snapcast_without_legacy_delay(self):
        args = runner.parse_args_from(["--target", "all", "--mode", "snapcast", "--dry-run"])

        command = runner.stream_command(args, Path("/tmp/window.wav"))

        self.assertIn("--mode", command)
        self.assertEqual(command[command.index("--mode") + 1], "snapcast")
        self.assertEqual(command[command.index("--delay-ms") + 1], "0")

    def test_prepare_window_uses_configured_temp_dir(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            temp_root = temp / "runner-temp"
            session_path = temp / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "clips": [
                            {"id": "a", "deck": "deck-1", "path": "/music/a.flac", "start": 0, "duration": 20_000},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            args = runner.parse_args_from(
                [
                    "--session",
                    str(session_path),
                    "--temp-dir",
                    str(temp_root),
                    "--dry-run",
                ]
            )
            session = runner.load_session(session_path)

            prepared = runner.prepare_window(args, session, 0, 10_000)
            try:
                self.assertTrue(prepared.output.is_relative_to(temp_root))
            finally:
                prepared.cleanup()

    def test_persistent_snapcast_reuses_fifo_handle_between_windows(self):
        args = Namespace(
            snapcast_fifo=Mock(),
            channels=2,
            sample_rate=48_000,
        )
        args.snapcast_fifo.open.return_value = "fifo-handle"
        snapcast = runner.PersistentSnapcast(args)
        snapcast.fifo_handle = args.snapcast_fifo.open("wb")

        with patch.object(runner, "require_ffmpeg", return_value="ffmpeg"):
            with patch.object(runner.subprocess, "Popen") as popen:
                popen.return_value = Mock()
                first = snapcast.start_window(Path("/tmp/a.wav"))
                second = snapcast.start_window(Path("/tmp/b.wav"))

        args.snapcast_fifo.open.assert_called_once_with("wb")
        self.assertEqual(popen.call_count, 2)
        self.assertIsNone(first.handle)
        self.assertIsNone(second.handle)
        self.assertEqual(popen.call_args_list[0].kwargs["stdout"], "fifo-handle")
        self.assertEqual(popen.call_args_list[1].kwargs["stdout"], "fifo-handle")


if __name__ == "__main__":
    unittest.main()
