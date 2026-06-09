import json
import signal
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
    def setUp(self):
        self._default_pause_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._default_pause_dir.cleanup)
        pause_path = Path(self._default_pause_dir.name) / "missing-dj-watchdog.paused"
        patcher = patch.object(runner, "DEFAULT_DJ_PAUSE_FILE", pause_path)
        patcher.start()
        self.addCleanup(patcher.stop)

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
        self.assertEqual(state["runner_status"], "running")
        self.assertIsInstance(state["runner_pid"], int)
        self.assertIn("runner_started_at", state)
        self.assertIn("runner_updated_at", state)

    def test_pause_file_blocks_runner_before_active_pointer_or_state_write(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            session_path = temp / "session.json"
            state_path = temp / "state.json"
            active_pointer = temp / "active-set.json"
            history_path = temp / "history.jsonl"
            pause_file = temp / "dj-watchdog.paused"
            session_path.write_text('{"version": 1, "decks": [], "clips": []}', encoding="utf-8")
            pause_file.write_text("paused for structure work", encoding="utf-8")
            args = runner.parse_args_from(
                [
                    "--session",
                    str(session_path),
                    "--state",
                    str(state_path),
                    "--active-pointer",
                    str(active_pointer),
                    "--history-log",
                    str(history_path),
                    "--pause-file",
                    str(pause_file),
                    "--target",
                    "all",
                ]
            )

            with redirect_stdout(StringIO()) as stdout:
                self.assertEqual(runner.run_session(args), 0)

            history = [json.loads(line) for line in history_path.read_text(encoding="utf-8").splitlines()]

        self.assertFalse(active_pointer.exists())
        self.assertFalse(state_path.exists())
        self.assertEqual(history[-1]["event"], "playback_start_blocked")
        self.assertEqual(history[-1]["component"], "session_runner")
        self.assertEqual(json.loads(stdout.getvalue())["status"], "paused")

    def test_stream_command_targets_snapcast_without_legacy_delay(self):
        args = runner.parse_args_from(["--target", "all", "--mode", "snapcast", "--dry-run"])

        command = runner.stream_command(args, Path("/tmp/window.wav"))

        self.assertIn("--mode", command)
        self.assertEqual(command[command.index("--mode") + 1], "snapcast")
        self.assertEqual(command[command.index("--delay-ms") + 1], "0")

    def test_no_persistent_snapcast_uses_window_stream_command(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            session_path = temp / "session.json"
            state_path = temp / "state.json"
            active_pointer = temp / "active-set.json"
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
                    "--state",
                    str(state_path),
                    "--active-pointer",
                    str(active_pointer),
                    "--target",
                    "SPONGEBOT",
                    "--mode",
                    "snapcast",
                    "--no-persistent-snapcast",
                    "--window-ms",
                    "10000",
                ]
            )

            with patch.object(runner, "render_window", return_value=["render"]):
                with patch.object(runner, "start_stream") as start_stream:
                    process = Mock()
                    process.poll.side_effect = [0]
                    process.wait.return_value = 0
                    start_stream.return_value = process
                    with patch.object(runner, "append_history"):
                        with patch.object(runner, "session_duration_ms", return_value=10_000):
                            with patch.object(runner, "playhead_ms_from_state", side_effect=[0, 10_000]):
                                self.assertEqual(runner.run_session(args), 0)
            pointer = json.loads(active_pointer.read_text(encoding="utf-8"))

        start_stream.assert_called_once()
        command = start_stream.call_args.args[0]
        self.assertIn("slime_audio_stream.py", command[1])
        self.assertIn("--mode", command)
        self.assertEqual(command[command.index("--mode") + 1], "snapcast")
        self.assertEqual(Path(pointer["active_session_path"]), session_path.resolve())
        self.assertEqual(Path(pointer["active_state_path"]), state_path.resolve())
        self.assertEqual(pointer["slug"], "session")

    def test_live_window_state_is_written_after_stream_starts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            session_path = temp / "session.json"
            state_path = temp / "state.json"
            active_pointer = temp / "active-set.json"
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
                    "--state",
                    str(state_path),
                    "--active-pointer",
                    str(active_pointer),
                    "--target",
                    "SPONGEBOT",
                    "--mode",
                    "snapcast",
                    "--no-persistent-snapcast",
                    "--window-ms",
                    "10000",
                ]
            )
            process = Mock()
            process.poll.side_effect = [0]
            order = []
            original_write_window_state = runner.write_window_state

            def start_window_stream(*_args):
                order.append("stream")
                return runner.RunningWindow(process), ["stream"]

            def write_window_state(*write_args, **write_kwargs):
                order.append("state")
                return original_write_window_state(*write_args, **write_kwargs)

            with patch.object(runner, "render_window", return_value=["render"]):
                with patch.object(runner, "start_window_stream", side_effect=start_window_stream):
                    with patch.object(runner, "wait_window_stream", return_value=0):
                        with patch.object(runner, "append_history"):
                            with patch.object(runner, "playhead_ms_from_state", side_effect=[0, 20_000]):
                                with patch.object(runner, "write_window_state", side_effect=write_window_state):
                                    self.assertEqual(runner.run_session(args), 0)

        self.assertEqual(order[:2], ["stream", "state"])

    def test_dry_run_does_not_update_active_pointer(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            session_path = temp / "session.json"
            state_path = temp / "state.json"
            active_pointer = temp / "active-set.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "clips": [{"id": "a", "deck": "deck-1", "path": "/music/a.flac", "start": 0, "duration": 20_000}],
                    }
                ),
                encoding="utf-8",
            )
            state_path.write_text(json.dumps({"playhead_ms": 0}), encoding="utf-8")
            args = runner.parse_args_from(
                [
                    "--session",
                    str(session_path),
                    "--state",
                    str(state_path),
                    "--active-pointer",
                    str(active_pointer),
                    "--target",
                    "all",
                    "--dry-run",
                ]
            )

            with redirect_stdout(StringIO()):
                self.assertEqual(runner.run_session(args), 0)

        self.assertFalse(active_pointer.exists())

    def test_record_runner_exit_writes_state_and_history(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            state_path = temp / "state.json"
            history_path = temp / "history.jsonl"
            session_path = temp / "session.json"
            args = runner.parse_args_from(
                [
                    "--session",
                    str(session_path),
                    "--state",
                    str(state_path),
                    "--history-log",
                    str(history_path),
                    "--target",
                    "all",
                ]
            )

            runner.record_runner_exit(args, status="fatal", reason="RuntimeError: boom", traceback="trace")

            state = json.loads(state_path.read_text(encoding="utf-8"))
            history = [json.loads(line) for line in history_path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(state["runner_status"], "fatal")
        self.assertEqual(state["runner_exit_reason"], "RuntimeError: boom")
        self.assertEqual(state["traceback"], "trace")
        self.assertEqual(history[-1]["event"], "session_runner_exit")
        self.assertEqual(history[-1]["status"], "fatal")
        self.assertEqual(history[-1]["reason"], "RuntimeError: boom")

    def test_running_status_clears_stale_runner_exit_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            state_path = temp / "state.json"
            session_path = temp / "session.json"
            state_path.write_text(
                json.dumps(
                    {
                        "runner_status": "stopped",
                        "runner_exit_at": "2026-06-09T10:00:00-0400",
                        "runner_exit_reason": "signal:SIGTERM",
                    }
                ),
                encoding="utf-8",
            )
            args = runner.parse_args_from(["--session", str(session_path), "--state", str(state_path), "--target", "all"])

            runner.write_runner_status(args, "running")

            state = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertEqual(state["runner_status"], "running")
        self.assertNotIn("runner_exit_at", state)
        self.assertNotIn("runner_exit_reason", state)

    def test_signal_handler_records_stopped_exit_before_stopping_stream(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            args = runner.parse_args_from(
                [
                    "--session",
                    str(temp / "session.json"),
                    "--state",
                    str(temp / "state.json"),
                    "--history-log",
                    str(temp / "history.jsonl"),
                    "--target",
                    "all",
                ]
            )

            with patch.object(runner, "stop_active_stream") as stop_active_stream:
                runner.install_signal_handlers(args)
                handler = signal.getsignal(signal.SIGTERM)
                with self.assertRaises(SystemExit) as raised:
                    handler(signal.SIGTERM, None)

            state = json.loads((temp / "state.json").read_text(encoding="utf-8"))

        self.assertEqual(raised.exception.code, 128 + signal.SIGTERM)
        stop_active_stream.assert_called_once()
        self.assertEqual(state["runner_status"], "stopped")
        self.assertEqual(state["runner_exit_reason"], "signal:SIGTERM")

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

    def test_persistent_snapcast_keeps_fifo_alive_between_window_writers(self):
        args = Namespace(
            snapcast_fifo=Mock(),
            channels=2,
            sample_rate=48_000,
        )
        first_handle = Mock()
        second_handle = Mock()
        args.snapcast_fifo.open.side_effect = [first_handle, second_handle]
        snapcast = runner.PersistentSnapcast(args)
        snapcast.fifo_keepalive_fd = 10

        with patch.object(runner, "require_ffmpeg", return_value="ffmpeg"):
            with patch.object(runner.subprocess, "Popen") as popen:
                popen.return_value = Mock()
                first = snapcast.start_window(Path("/tmp/a.wav"))
                second = snapcast.start_window(Path("/tmp/b.wav"))

        self.assertEqual(args.snapcast_fifo.open.call_count, 2)
        self.assertEqual(popen.call_count, 2)
        self.assertIs(first.handle, first_handle)
        self.assertIs(second.handle, second_handle)
        self.assertEqual(popen.call_args_list[0].kwargs["stdout"], first_handle)
        self.assertEqual(popen.call_args_list[1].kwargs["stdout"], second_handle)

    def test_default_snapcast_fifo_is_process_scoped(self):
        args = runner.parse_args_from(["--target", "all", "--dry-run"])

        self.assertEqual(args.snapcast_fifo.name, f"slime-audio-snapfifo-{runner.os.getpid()}")

    def test_fifo_keepalive_waits_for_snapserver_reader(self):
        path = Path("/tmp/slime-test-fifo")
        not_ready = OSError()
        not_ready.errno = runner.errno.ENXIO

        with patch.object(runner.os, "open", side_effect=[not_ready, 11, 12]) as open_fifo:
            with patch.object(runner.os, "close") as close:
                with patch.object(runner.time, "sleep") as sleep:
                    with patch.object(runner.time, "monotonic", side_effect=[0.0, 0.1]):
                        self.assertEqual(runner.open_fifo_writer_when_reader_ready(path), 12)

        self.assertEqual(open_fifo.call_count, 3)
        close.assert_called_once_with(11)
        sleep.assert_called_once_with(0.05)

    def test_fifo_keepalive_closes_bootstrap_on_timeout(self):
        path = Path("/tmp/slime-test-fifo")
        not_ready = OSError()
        not_ready.errno = runner.errno.ENXIO

        with patch.object(runner.os, "open", side_effect=[not_ready, 11, not_ready]) as open_fifo:
            with patch.object(runner.os, "close") as close:
                with patch.object(runner.time, "sleep"):
                    with patch.object(runner.time, "monotonic", side_effect=[0.0, 0.1, 10.0]):
                        with self.assertRaises(OSError):
                            runner.open_fifo_writer_when_reader_ready(path)

        self.assertEqual(open_fifo.call_count, 3)
        close.assert_called_once_with(11)


if __name__ == "__main__":
    unittest.main()
