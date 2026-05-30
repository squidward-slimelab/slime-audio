import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import slime_audio_lean_ins
from slime_audio_session import load_session


class SlimeAudioLeanInTests(unittest.TestCase):
    def test_lean_in_planner_writes_duck_and_lowpass_session_event(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            session_path = temp / "session.json"
            log_path = temp / "lean-ins.jsonl"
            original_argv = sys.argv[:]
            try:
                sys.argv = [
                    "slime_audio_lean_ins.py",
                    "--session",
                    str(session_path),
                    "--create",
                    "--id-prefix",
                    "test-lean",
                    "--start",
                    "00:12.000",
                    "--text",
                    "quick note",
                    "--duck-volume",
                    "0.4",
                    "--lowpass-hz",
                    "1200",
                    "--duck-ms",
                    "2000",
                    "--log",
                    str(log_path),
                ]
                with redirect_stdout(StringIO()):
                    self.assertEqual(slime_audio_lean_ins.main(), 0)
            finally:
                sys.argv = original_argv

            session = load_session(session_path)
            log = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(len(session.mic_lean_ins), 1)
        lean_in = session.mic_lean_ins[0]
        self.assertEqual(lean_in.start_ms, 12_000)
        self.assertEqual([effect.param for effect in lean_in.effects], ["duck_volume", "lowpass_hz"])
        self.assertEqual(lean_in.effects[0].points[0].value, 0.4)
        self.assertEqual(lean_in.effects[1].points[0].value, 1200)
        self.assertEqual(log[-1]["event"], "lean_in_planned")

    def test_script_source_does_not_use_packet_audio_transport(self):
        source = Path(slime_audio_lean_ins.__file__).read_text(encoding="utf-8")

        self.assertNotIn("socket", source)
        self.assertNotIn("SLA1", source)
        self.assertNotIn("sendto", source)


if __name__ == "__main__":
    unittest.main()
