import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import slime_audio_commentary_planner as planner
from slime_audio_session import load_session


class SlimeAudioCommentaryPlannerTests(unittest.TestCase):
    def test_planner_adds_spaced_future_lean_ins_and_logs_context(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            session_path = temp / "session.json"
            state_path = temp / "state.json"
            log_path = temp / "commentary.jsonl"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1", "deck-2"],
                        "clips": [
                            {"id": "a", "deck": "deck-1", "path": "/music/Artist/Album/a.flac", "start": 0, "duration": 120_000},
                            {"id": "b", "deck": "deck-2", "path": "/music/Artist/Album/b.flac", "start": 180_000, "duration": 120_000},
                            {"id": "c", "deck": "deck-1", "path": "/music/Artist/Album/c.flac", "start": 420_000, "duration": 120_000},
                            {"id": "d", "deck": "deck-2", "path": "/music/Artist/Album/d.flac", "start": 720_000, "duration": 120_000},
                        ],
                        "mic_lean_ins": [{"id": "existing", "start": 180_000, "text": "already talked"}],
                    }
                ),
                encoding="utf-8",
            )
            state_path.write_text(json.dumps({"playhead_ms": 30_000}), encoding="utf-8")
            original_argv = sys.argv[:]
            try:
                sys.argv = [
                    "slime_audio_commentary_planner.py",
                    "--session",
                    str(session_path),
                    "--state",
                    str(state_path),
                    "--log",
                    str(log_path),
                    "--count",
                    "2",
                    "--lead-ms",
                    "30",
                    "--min-spacing-ms",
                    "200000",
                    "--id-prefix",
                    "test-comment",
                ]
                with redirect_stdout(StringIO()):
                    self.assertEqual(planner.main(), 0)
            finally:
                sys.argv = original_argv

            session = load_session(session_path)
            log = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]

        planned = [lean for lean in session.mic_lean_ins if lean.id.startswith("test-comment")]
        self.assertEqual(len(planned), 2)
        self.assertTrue(all(lean.start_ms > 30_000 for lean in planned))
        self.assertGreaterEqual(abs(planned[1].start_ms - planned[0].start_ms), 200_000)
        self.assertEqual([effect.param for effect in planned[0].effects], ["duck_volume", "lowpass_hz"])
        self.assertEqual(log[-1]["event"], "commentary_planned")
        self.assertIn(log[-1]["kind"], {"track", "transition"})
        self.assertTrue(log[-1]["track"] or log[-1]["next_track"])

    def test_dry_run_does_not_mutate_session(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            session_path = temp / "session.json"
            state_path = temp / "state.json"
            log_path = temp / "commentary.jsonl"
            session_payload = {
                "version": 1,
                "decks": ["deck-1"],
                "clips": [{"id": "a", "deck": "deck-1", "path": "/music/a.flac", "start": 0, "duration": 120_000}],
                "mic_lean_ins": [],
            }
            session_path.write_text(json.dumps(session_payload), encoding="utf-8")
            state_path.write_text(json.dumps({"playhead_ms": 0}), encoding="utf-8")

            with redirect_stdout(StringIO()):
                self.assertEqual(
                    self.run_cli(
                        [
                            "slime_audio_commentary_planner.py",
                            "--session",
                            str(session_path),
                            "--state",
                            str(state_path),
                            "--log",
                            str(log_path),
                            "--dry-run",
                        ]
                    ),
                    0,
                )

            saved = json.loads(session_path.read_text(encoding="utf-8"))
            log = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(saved, session_payload)
        self.assertTrue(log[-1]["dry_run"])

    def test_tension_plan_candidates_are_preferred(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            session_path = temp / "session.json"
            state_path = temp / "state.json"
            log_path = temp / "commentary.jsonl"
            tension_path = temp / "tension.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "clips": [{"id": "a", "deck": "deck-1", "path": "/music/a.flac", "start": 0, "duration": 180_000}],
                        "mic_lean_ins": [],
                    }
                ),
                encoding="utf-8",
            )
            state_path.write_text(json.dumps({"playhead_ms": 0}), encoding="utf-8")
            tension_path.write_text(
                json.dumps(
                    [
                        {
                            "start_ms": 95_000,
                            "kind": "pre-drop",
                            "clip_id": "a",
                            "track": "/music/a.flac",
                            "reason": "a.flac: speak just before the detected drop",
                            "talking_points": ["detected release point in a; speak briefly before it and clear the drop"],
                        }
                    ]
                ),
                encoding="utf-8",
            )

            with redirect_stdout(StringIO()):
                self.assertEqual(
                    self.run_cli(
                        [
                            "slime_audio_commentary_planner.py",
                            "--session",
                            str(session_path),
                            "--state",
                            str(state_path),
                            "--log",
                            str(log_path),
                            "--tension-plan",
                            str(tension_path),
                            "--count",
                            "1",
                            "--lead-ms",
                            "30",
                        ]
                    ),
                    0,
                )

            log = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(log[-1]["kind"], "pre-drop")
        self.assertEqual(log[-1]["start_ms"], 95_000)
        self.assertIn("detected release point", log[-1]["text"])

    def run_cli(self, argv: list[str]) -> int:
        original_argv = sys.argv[:]
        try:
            sys.argv = argv
            return planner.main()
        finally:
            sys.argv = original_argv


if __name__ == "__main__":
    unittest.main()
