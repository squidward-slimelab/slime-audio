import os
import sys
import tempfile
import textwrap
import threading
import time
import unittest
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
