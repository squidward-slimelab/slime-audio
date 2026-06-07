import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import slime_music_library_service as service


class SlimeMusicLibraryServiceTests(unittest.TestCase):
    def test_live_playback_skips_expensive_backfills(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            state_path = temp / "active-state.json"
            active_pointer = temp / "active-set.json"
            state_path.write_text(
                json.dumps(
                    {
                        "current": "/music/a.flac",
                        "runner_updated_at": "2026-05-30T12:00:00-0400",
                        "window_started_at": "2026-05-30T12:00:00-0400",
                    }
                ),
                encoding="utf-8",
            )
            active_pointer.write_text(json.dumps({"active_state_path": str(state_path)}), encoding="utf-8")
            args = service.parse_args(
                [
                    "--no-scan",
                    "--db",
                    str(temp / "library.sqlite3"),
                    "--active-pointer",
                    str(active_pointer),
                    "--tunebat-backfill-limit",
                    "12",
                    "--dj-analysis-backfill-limit",
                    "6",
                ]
            )

            with patch.object(service.time, "time", return_value=service.parse_timestamp("2026-05-30T12:01:00-0400")):
                with patch.object(service, "run_json") as run_json:
                    result = service.run_once(args)

        run_json.assert_not_called()
        self.assertTrue(result["live_playback"]["active"])
        self.assertEqual(result["tunebat_backfill"]["reason"], "live playback active")
        self.assertEqual(result["dj_analysis_backfill"]["reason"], "live playback active")

    def test_stale_playback_state_allows_backfill(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            state_path = temp / "active-state.json"
            active_pointer = temp / "active-set.json"
            state_path.write_text(
                json.dumps(
                    {
                        "current": "/music/a.flac",
                        "runner_updated_at": "2026-05-30T12:00:00-0400",
                    }
                ),
                encoding="utf-8",
            )
            active_pointer.write_text(json.dumps({"active_state_path": str(state_path)}), encoding="utf-8")
            args = service.parse_args(
                [
                    "--no-scan",
                    "--db",
                    str(temp / "library.sqlite3"),
                    "--active-pointer",
                    str(active_pointer),
                    "--tunebat-backfill-limit",
                    "1",
                    "--dj-analysis-backfill-limit",
                    "0",
                    "--live-grace-seconds",
                    "300",
                ]
            )

            with patch.object(service.time, "time", return_value=service.parse_timestamp("2026-05-30T12:10:01-0400")):
                with patch.object(service, "run_json", return_value={"ok": True}) as run_json:
                    result = service.run_once(args)

        run_json.assert_called_once()
        self.assertFalse(result["live_playback"]["active"])
        self.assertEqual(result["tunebat_backfill"], {"ok": True})


if __name__ == "__main__":
    unittest.main()
