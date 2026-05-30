import json
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from slime_audio_candidates import candidate_rows, load_constraints, set_constraints
from slime_music_library import Source, command_set_tunebat, connect, scan


class SlimeAudioCandidateTests(unittest.TestCase):
    def test_candidates_filter_recent_history_and_excluded_artists(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            root = temp / "music"
            played = root / "Played Artist" / "Album" / "01 - Played.flac"
            banned = root / "Banned Artist" / "Album" / "01 - Banned.flac"
            keeper = root / "Good Artist" / "Album" / "01 - Keeper.flac"
            for index, path in enumerate([played, banned, keeper], start=1):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(bytes([index]) * 100)

            conn = connect(temp / "library.sqlite3")
            scan(conn, [Source("patrick", "rockhouse", root, 100)], prune=True)
            for row in conn.execute("SELECT duplicate_key, title_guess FROM tracks").fetchall():
                energy = 0.8 if row["title_guess"] == "Keeper" else 0.2
                command_set_tunebat(
                    conn,
                    row["duplicate_key"],
                    "",
                    row["title_guess"],
                    "",
                    "",
                    "",
                    "",
                    124.0,
                    None,
                    energy,
                    None,
                    None,
                    None,
                    emit=False,
                )
            history = temp / "history.jsonl"
            history.write_text(
                json.dumps({"event": "track_started", "resolved_track": str(played)}) + "\n",
                encoding="utf-8",
            )
            constraints_path = temp / "constraints.json"
            constraints = set_constraints(
                constraints_path,
                vibe="",
                direction="",
                energy_target=0.75,
                exclude_artist=["Banned Artist"],
                exclude_term=[],
                clear_excludes=False,
                notes=None,
                reason="test",
            )

            candidates = candidate_rows(conn, constraints, history_path=history, recent_limit=10, limit=10)

        self.assertEqual([candidate["title_guess"] for candidate in candidates], ["Keeper"])
        self.assertIn("energy 0.80 vs target 0.75", candidates[0]["reasons"])

    def test_constraints_persist_change_reasons(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "constraints.json"

            constraints = set_constraints(
                path,
                vibe="daytime",
                direction="brighter",
                energy_target=0.6,
                exclude_artist=["Artist"],
                exclude_term=["dubstep"],
                clear_excludes=False,
                notes="keep it moving",
                reason="operator steering",
            )
            loaded = load_constraints(path)

        self.assertEqual(constraints.vibe, "daytime")
        self.assertEqual(loaded.direction, "brighter")
        self.assertEqual(loaded.exclude_artists, ["Artist"])
        self.assertEqual(loaded.exclude_terms, ["dubstep"])
        self.assertEqual(loaded.changes[-1]["reason"], "operator steering")

    def test_candidates_skip_untagged_root_files_by_default(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            root = temp / "music"
            sound_effect = root / "airhorn.wav"
            music = root / "Artist" / "Album" / "01 - Song.flac"
            sound_effect.parent.mkdir(parents=True, exist_ok=True)
            music.parent.mkdir(parents=True, exist_ok=True)
            sound_effect.write_bytes(b"a" * 100)
            music.write_bytes(b"b" * 100)
            conn = connect(temp / "library.sqlite3")
            scan(conn, [Source("patrick", "rockhouse", root, 100)], prune=True)

            candidates = candidate_rows(
                conn,
                load_constraints(temp / "missing-constraints.json"),
                history_path=temp / "missing-history.jsonl",
                recent_limit=10,
                limit=10,
            )

        self.assertEqual([candidate["title_guess"] for candidate in candidates], ["Song"])


if __name__ == "__main__":
    unittest.main()
