import os
import json
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import slime_audio_playlist_runner as runner


def process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class SlimeAudioPlaylistRunnerTests(unittest.TestCase):
    def tearDown(self):
        runner.stop_active_stream()

    def test_stop_active_stream_kills_stream_process_group_children(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            child_pid_file = temp / "child.pid"
            blocker = temp / "blocker.py"
            blocker.write_text(
                textwrap.dedent(
                    f"""
                    import subprocess
                    import sys
                    import time
                    from pathlib import Path

                    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
                    Path({str(child_pid_file)!r}).write_text(str(child.pid), encoding="utf-8")
                    try:
                        time.sleep(60)
                    finally:
                        child.terminate()
                    """
                ),
                encoding="utf-8",
            )

            result: dict[str, int] = {}
            thread = threading.Thread(
                target=lambda: result.setdefault("returncode", runner.run_stream([sys.executable, str(blocker)])),
                daemon=True,
            )
            thread.start()

            child_pid = self.wait_for_child_pid(child_pid_file)
            self.assertTrue(process_exists(child_pid))

            runner.stop_active_stream()
            thread.join(timeout=10)

            self.assertFalse(thread.is_alive())
            self.assertNotEqual(result.get("returncode"), 0)
            self.assertProcessGone(child_pid)

    def test_load_or_create_state_preserves_future_swaps_and_appends_new_playlist_tracks(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            runner.write_state(
                state_path,
                {
                    "completed": ["a.flac"],
                    "current": "b.flac",
                    "index": 1,
                    "order": ["a.flac", "b.flac", "custom-future.flac"],
                    "shuffle": False,
                },
            )

            state = runner.load_or_create_state(state_path, ["a.flac", "b.flac", "c.flac"], shuffle=False)

        self.assertEqual(state["order"], ["a.flac", "b.flac", "custom-future.flac", "c.flac"])
        self.assertEqual(state["current"], "b.flac")

    def test_queue_edits_only_touch_future_tracks(self):
        state = {
            "completed": ["a.flac"],
            "current": "b.flac",
            "index": 1,
            "order": ["a.flac", "b.flac", "c.flac"],
            "shuffle": False,
        }

        with self.assertRaisesRegex(ValueError, "current or completed"):
            runner.edit_remove(state, ["b.flac"])

        swapped = runner.edit_swap(state, "c.flac", "d.flac")
        self.assertEqual(swapped["order"], ["a.flac", "b.flac", "d.flac"])

    def test_queue_edit_cli_records_history(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            state_path = temp / "state.json"
            history_path = temp / "history.jsonl"
            runner.write_state(
                state_path,
                {
                    "completed": [],
                    "current": "a.flac",
                    "index": 0,
                    "order": ["a.flac", "b.flac"],
                    "shuffle": False,
                },
            )
            original_argv = sys.argv[:]
            try:
                sys.argv = [
                    "slime_audio_playlist_runner.py",
                    "queue-swap",
                    "--state",
                    str(state_path),
                    "--history-log",
                    str(history_path),
                    "b.flac",
                    "c.flac",
                ]
                with redirect_stdout(StringIO()):
                    self.assertEqual(runner.main(), 0)
            finally:
                sys.argv = original_argv

            state = json.loads(state_path.read_text(encoding="utf-8"))
            history = [json.loads(line) for line in history_path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(state["order"], ["a.flac", "c.flac"])
        self.assertEqual(history[-1]["event"], "queue_edited")
        self.assertEqual(history[-1]["action"], "swap")

    def wait_for_child_pid(self, path: Path) -> int:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if path.exists():
                return int(path.read_text(encoding="utf-8"))
            time.sleep(0.05)
        self.fail("child process did not start")

    def assertProcessGone(self, pid: int) -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if not process_exists(pid):
                return
            time.sleep(0.05)
        self.fail(f"process still exists: {pid}")


if __name__ == "__main__":
    unittest.main()
