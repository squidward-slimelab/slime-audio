"""Streaming/underrun performance tests for the snapcast playback path.

Scope: everything this host controls about client audio continuity — the FIFO
feed cadence and the window handoff gap. Whatever arrives at the snapserver
FIFO is what clients hear one buffer later, so a handoff gap under the client
buffer and a paced (non-burst) feed are the no-underrun contract this side of
the network. The Windows tray receivers themselves are exercised by the
receiver-health workflow, not unit tests.

Companion budget: test_slime_audio_session_runner_performance asserts window
renders beat the prerender lead, so the next window's file exists before the
current one ends.
"""

import math
import os
import select
import shutil
import struct
import subprocess
import sys
import threading
import time
import unittest
import wave
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

REPO_ROOT = Path(__file__).resolve().parents[1]
STREAM_SCRIPT = REPO_ROOT / "scripts" / "slime_audio_stream.py"


def write_tone(path: Path, *, duration_s: float, sample_rate: int = 48_000) -> None:
    frames = bytearray()
    for index in range(int(sample_rate * duration_s)):
        value = 0.3 * math.sin(2 * math.pi * 220.0 * index / sample_rate)
        packed = struct.pack("<h", int(value * 32767))
        frames += packed + packed
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(2)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(bytes(frames))


class FifoTap:
    """Drain a FIFO like snapserver would, recording arrival times and EOFs."""

    def __init__(self, fifo_path: Path):
        self.fifo_path = fifo_path
        self.data_events: list[tuple[float, int]] = []
        self.eof_times: list[float] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        fd = os.open(self.fifo_path, os.O_RDONLY | os.O_NONBLOCK)
        try:
            while not self._stop.is_set():
                ready, _, _ = select.select([fd], [], [], 0.05)
                if not ready:
                    continue
                data = os.read(fd, 65536)
                now = time.monotonic()
                if data:
                    self.data_events.append((now, len(data)))
                else:
                    self.eof_times.append(now)
                    time.sleep(0.05)
        finally:
            os.close(fd)

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)

    def __enter__(self) -> "FifoTap":
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is required for streaming performance tests")
class SlimeAudioStreamingPerformanceTests(unittest.TestCase):
    def test_window_handoff_keeps_feeding_within_the_client_buffer(self):
        """Two back-to-back continuation windows must hand off without the FIFO
        going quiet for longer than the snapcast client buffer, without EOF
        (the runner's FIFO hold contract), and with realtime-paced arrival
        rather than a burst-and-silence pattern."""
        max_gap_s = float(os.environ.get("SLIME_AUDIO_MAX_HANDOFF_GAP_S", "0.5"))
        max_first_byte_s = float(os.environ.get("SLIME_AUDIO_MAX_STREAM_STARTUP_S", "1.5"))
        window_s = 2.0
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            fifo = temp / "snapfifo"
            os.mkfifo(fifo)
            window_wav = temp / "window.wav"
            write_tone(window_wav, duration_s=window_s)

            def stream_command() -> list[str]:
                return [
                    sys.executable,
                    str(STREAM_SCRIPT),
                    str(window_wav),
                    "--target",
                    "all",
                    "--mode",
                    "snapcast",
                    "--continuation",
                    "--no-active-pointer",
                    "--snapcast-fifo",
                    str(fifo),
                    "--snapcast-buffer-ms",
                    "1000",
                ]

            with FifoTap(fifo) as tap:
                time.sleep(0.2)
                # The runner holds a write handle for the whole session so
                # per-window writer exits never EOF the snapserver.
                hold_fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
                try:
                    first_spawn = time.monotonic()
                    first = subprocess.run(stream_command(), capture_output=True, text=True)
                    second = subprocess.run(stream_command(), capture_output=True, text=True)
                    time.sleep(0.2)
                finally:
                    # Fully stop observing before releasing the hold: the EOF
                    # at session teardown is legitimate; EOF during a handoff
                    # is the bug this test exists to catch.
                    tap.stop()
                    os.close(hold_fd)

            self.assertEqual(first.returncode, 0, first.stderr[-500:])
            self.assertEqual(second.returncode, 0, second.stderr[-500:])
            self.assertGreater(len(tap.data_events), 10)

            first_byte_latency = tap.data_events[0][0] - first_spawn
            gaps = [
                later[0] - earlier[0]
                for earlier, later in zip(tap.data_events, tap.data_events[1:])
            ]
            arrival_span = tap.data_events[-1][0] - tap.data_events[0][0]
            audio_s = 2 * window_s

        # A nonblocking FIFO reports EOF until the first writer attaches, so
        # only EOFs after audio starts flowing indicate a broken hold.
        first_data_ts = tap.data_events[0][0]
        mid_stream_eofs = [ts for ts in tap.eof_times if ts > first_data_ts]
        self.assertEqual(mid_stream_eofs, [], "FIFO saw EOF during a window handoff; snapserver would drop the stream")
        self.assertLess(
            first_byte_latency,
            max_first_byte_s,
            f"stream startup too slow: first byte after {first_byte_latency:.2f}s",
        )
        self.assertLess(
            max(gaps),
            max_gap_s,
            f"window handoff starved the FIFO for {max(gaps):.3f}s; snapcast clients "
            f"underrun once the gap exceeds their buffer",
        )
        # ffmpeg feeds with -re (realtime pacing). A burst-then-silence feed
        # would break the runner's window timing and can overrun the FIFO.
        self.assertGreater(
            arrival_span,
            audio_s * 0.5,
            f"{audio_s:.1f}s of audio arrived in {arrival_span:.2f}s; the feed is bursting instead of pacing",
        )


if __name__ == "__main__":
    unittest.main()
