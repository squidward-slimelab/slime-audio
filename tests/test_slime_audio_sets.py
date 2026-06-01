import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import slime_audio_sets as sets


def write_session(path: Path, clip_id: str = "a") -> None:
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "decks": ["deck-1", "deck-2"],
                "clips": [
                    {"id": clip_id, "deck": "deck-1", "path": f"/music/{clip_id}.flac", "start": 0, "duration": 30_000},
                ],
                "mic_lean_ins": [],
                "automations": [],
            }
        ),
        encoding="utf-8",
    )


class SlimeAudioSetTests(unittest.TestCase):
    def test_archive_activate_and_save_loaded_preserve_archive_until_save(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            sets_dir = temp / "sets"
            source = temp / "source.json"
            active = temp / "mix-session.json"
            state = temp / "state.json"
            pointer = temp / "active-set.json"
            write_session(source, "archived")

            metadata = sets.archive_set(session=source, sets_dir=sets_dir, title="Test Set", slug="test-set")
            archive_session = Path(metadata["session_path"])
            archived_before = json.loads(archive_session.read_text(encoding="utf-8"))
            activated = sets.activate_set(
                sets_dir=sets_dir,
                slug="test-set",
                active_session=active,
                active_state=state,
                active_pointer=pointer,
                reset_state=True,
            )
            active_payload = json.loads(active.read_text(encoding="utf-8"))
            active_payload["clips"][0]["id"] = "edited"
            active.write_text(json.dumps(active_payload), encoding="utf-8")

            self.assertEqual(json.loads(archive_session.read_text(encoding="utf-8")), archived_before)
            saved = sets.save_loaded_set(sets_dir=sets_dir, active_pointer=pointer, active_session=active)
            saved_clip_id = json.loads(Path(saved["session_path"]).read_text(encoding="utf-8"))["clips"][0]["id"]

            self.assertEqual(activated["active"]["slug"], "test-set")
            self.assertEqual(saved_clip_id, "edited")
            self.assertEqual(saved["duration_ms"], 30_000)

    def test_new_and_fork_create_named_sessions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            sets_dir = temp / "sets"
            active = temp / "mix-session.json"
            state = temp / "state.json"
            pointer = temp / "active-set.json"

            created = sets.new_set(
                sets_dir=sets_dir,
                title="Blank Set",
                slug="blank",
                active_session=active,
                active_state=state,
                active_pointer=pointer,
            )
            forked = sets.fork_set(sets_dir=sets_dir, source_slug="blank", title="Forked Set", slug="forked")
            listed = sets.list_sets(sets_dir)

        self.assertEqual(created["set"]["slug"], "blank")
        self.assertEqual(forked["forked_from"], "blank")
        self.assertEqual({item["slug"] for item in listed}, {"blank", "forked"})

    def test_render_dry_run_uses_mixdown_and_cleanup_prunes_old_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            sets_dir = temp / "sets"
            render_dir = temp / "renders"
            source = temp / "source.json"
            write_session(source)
            sets.archive_set(session=source, sets_dir=sets_dir, title="Render Set", slug="render-set")
            old = render_dir / "old.mp3"
            render_dir.mkdir()
            old.write_bytes(b"x" * 1024)
            old_time = 1
            os.utime(old, (old_time, old_time))
            with patch.object(sets.subprocess, "run") as run, patch.object(sets.time, "time", return_value=old_time + 48 * 3600):
                result = sets.render_set(
                    sets_dir=sets_dir,
                    slug="render-set",
                    session=None,
                    output=None,
                    render_dir=render_dir,
                    output_format="mp3",
                    mp3_bitrate="128k",
                    from_time="0",
                    duration="00:10.000",
                    skip_tts=True,
                    dry_run=False,
                    keep=0,
                    max_age_hours=12,
                    max_total_mb=1,
                )

        self.assertTrue(any("slime_audio_session_mixdown.py" in item for item in result["command"]))
        run.assert_called_once()
        self.assertIn(str(old), result["deleted"])
        self.assertFalse(old.exists())


if __name__ == "__main__":
    unittest.main()
