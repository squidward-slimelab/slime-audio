import json
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from slime_audio_candidates import candidate_rows, load_constraints, recent_play_index, set_constraints
from slime_music_library import Source, command_set_tunebat, connect, scan


class SlimeAudioCandidateTests(unittest.TestCase):
    def test_candidates_penalize_recent_history_and_filter_excluded_artists(self):
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
                json.dumps(
                    {
                        "event": "track_started",
                        "resolved_track": str(played),
                        "timestamp": "2026-06-09T05:00:00-0400",
                    }
                )
                + "\n",
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

        self.assertEqual([candidate["title_guess"] for candidate in candidates], ["Keeper", "Played"])
        self.assertIn("energy 0.80 vs target 0.75", candidates[0]["reasons"])
        self.assertEqual(candidates[1]["plays_seen"], 1)
        self.assertTrue(any(reason.startswith("last played ") for reason in candidates[1]["reasons"]))

    def test_session_window_history_counts_as_recent_play_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            root = temp / "music"
            track = root / "Session Artist" / "Album" / "01 - Session Track.flac"
            track.parent.mkdir(parents=True, exist_ok=True)
            track.write_bytes(b"a" * 100)

            conn = connect(temp / "library.sqlite3")
            scan(conn, [Source("patrick", "rockhouse", root, 100)], prune=True)

            session = temp / "session.json"
            session.write_text(json.dumps({"clips": [{"id": "clip-a", "path": str(track)}]}), encoding="utf-8")
            history = temp / "history.jsonl"
            history.write_text(
                json.dumps(
                    {
                        "event": "session_window_started",
                        "session": str(session),
                        "clips": ["clip-a"],
                        "timestamp": "2026-06-09T05:00:00-0400",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            recency = recent_play_index(conn, history, 10)
            candidates = candidate_rows(
                conn,
                load_constraints(temp / "missing-constraints.json"),
                history_path=history,
                recent_limit=10,
                limit=10,
            )

        self.assertEqual(len(recency), 1)
        self.assertEqual(candidates[0]["title_guess"], "Session Track")
        self.assertEqual(candidates[0]["plays_seen"], 1)
        self.assertIsNotNone(candidates[0]["last_played_at"])

    def test_autodj_selection_history_counts_as_recent_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            root = temp / "music"
            track = root / "Selected Artist" / "Album" / "01 - Selected Track.flac"
            track.parent.mkdir(parents=True, exist_ok=True)
            track.write_bytes(b"a" * 100)

            conn = connect(temp / "library.sqlite3")
            scan(conn, [Source("patrick", "rockhouse", root, 100)], prune=True)

            history = temp / "history.jsonl"
            history.write_text(
                json.dumps(
                    {
                        "event": "autodj_material_selected",
                        "dry_run": True,
                        "paths": [str(track)],
                        "timestamp": "2026-06-09T05:00:00-0400",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            recency = recent_play_index(conn, history, 10)
            candidates = candidate_rows(
                conn,
                load_constraints(temp / "missing-constraints.json"),
                history_path=history,
                recent_limit=10,
                limit=10,
            )

        self.assertEqual(len(recency), 1)
        self.assertEqual(candidates[0]["title_guess"], "Selected Track")
        self.assertEqual(candidates[0]["plays_seen"], 1)
        self.assertIsNotNone(candidates[0]["last_played_at"])

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

    def test_candidates_skip_duplicate_folders(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            root = temp / "music"
            keep = root / "Artist" / "Album" / "01 - Keep.flac"
            duplicate = root / "Artist" / "duplicates" / "01 - Duplicate.flac"
            duplicated = root / "Artist" / "duplicated" / "01 - Duplicated.flac"
            duplicate_singular = root / "Artist" / "duplicate" / "01 - Duplicate Singular.flac"
            for path in [keep, duplicate, duplicated, duplicate_singular]:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"a" * 100)
            conn = connect(temp / "library.sqlite3")
            scan(conn, [Source("patrick", "rockhouse", root, 100)], prune=True)

            candidates = candidate_rows(
                conn,
                load_constraints(temp / "missing-constraints.json"),
                history_path=temp / "missing-history.jsonl",
                recent_limit=10,
                limit=10,
            )

        self.assertEqual([candidate["title_guess"] for candidate in candidates], ["Keep"])


if __name__ == "__main__":
    unittest.main()
