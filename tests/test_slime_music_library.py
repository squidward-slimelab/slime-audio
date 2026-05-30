import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from slime_music_library import (
    Source,
    command_backfill_tunebat_local,
    command_set_lyrics,
    command_set_tunebat,
    connect,
    duplicate_key,
    normalize,
    preferred_path_for_file,
    scan,
)


class SlimeMusicLibraryTests(unittest.TestCase):
    def test_normalize_removes_noise(self):
        self.assertEqual(normalize("Song Title (Remastered) [Explicit]"), "song title")

    def test_duplicate_key_combines_same_album_artist_title_across_formats(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            left = root / "Artist" / "Album" / "01 - Song.mp3"
            right = root / "Artist" / "Album" / "Song.flac"
            left.parent.mkdir(parents=True, exist_ok=True)
            right.parent.mkdir(parents=True, exist_ok=True)
            left.write_bytes(b"a" * 100)
            right.write_bytes(b"b" * 200)

            left_key = duplicate_key(left, root, 100)[0]
            right_key = duplicate_key(right, root, 200)[0]

            self.assertEqual(left_key, right_key)

    def test_preferred_files_routes_to_strongest_server_with_duplicate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            db = temp / "library.sqlite3"
            patrick = temp / "patrick" / "Music"
            spatula = temp / "spatula" / "Music"
            patrick_track = patrick / "Artist" / "Album" / "01 - Song.flac"
            spatula_track = spatula / "Artist" / "Album" / "01 - Song.flac"
            patrick_track.parent.mkdir(parents=True)
            spatula_track.parent.mkdir(parents=True)
            patrick_track.write_bytes(b"a" * 100)
            spatula_track.write_bytes(b"b" * 100)

            conn = connect(db)
            totals = scan(
                conn,
                [
                    Source("spatula", "krusty-krab", spatula, 50),
                    Source("patrick", "rockhouse", patrick, 100),
                ],
                prune=True,
            )

            self.assertEqual(totals["files"], 2)
            preferred = conn.execute("SELECT server, path FROM preferred_files").fetchone()
            self.assertEqual(preferred["server"], "patrick")
            self.assertEqual(preferred["path"], str(patrick_track))
            self.assertEqual(preferred_path_for_file(conn, spatula_track), patrick_track)
            track = conn.execute("SELECT copies, server_count, preferred_server, preferred_path, locations FROM tracks").fetchone()
            self.assertEqual(track["copies"], 2)
            self.assertEqual(track["server_count"], 2)
            self.assertEqual(track["preferred_server"], "patrick")
            self.assertEqual(track["preferred_path"], str(patrick_track))
            self.assertIn(str(spatula_track), track["locations"])

    def test_tracks_view_includes_lyrics_and_tunebat_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            root = temp / "music"
            track_path = root / "Artist" / "Album" / "01 - Song.flac"
            track_path.parent.mkdir(parents=True)
            track_path.write_bytes(b"a" * 100)
            conn = connect(temp / "library.sqlite3")
            scan(conn, [Source("patrick", "rockhouse", root, 100)], prune=True)
            duplicate_key = conn.execute("SELECT duplicate_key FROM tracks").fetchone()["duplicate_key"]

            command_set_lyrics(conn, duplicate_key, "hello slime lab", "test", "https://example.test/lyrics", emit=False)
            command_set_tunebat(
                conn,
                duplicate_key,
                "https://tunebat.com/Info/song/abc",
                "Song",
                "Artist",
                "C# major",
                "major",
                "3B",
                126.0,
                95,
                0.71,
                0.62,
                0.53,
                {"bpm": 126, "key": "C# major"},
                emit=False,
            )

            row = conn.execute(
                """
                SELECT has_lyrics, lyrics_source, tunebat_key, tunebat_mode, tunebat_camelot, tunebat_bpm, tunebat_url
                FROM tracks
                """
            ).fetchone()
            self.assertEqual(row["has_lyrics"], 1)
            self.assertEqual(row["lyrics_source"], "test")
            self.assertEqual(row["tunebat_key"], "C# major")
            self.assertEqual(row["tunebat_mode"], "major")
            self.assertEqual(row["tunebat_camelot"], "3B")
            self.assertEqual(row["tunebat_bpm"], 126.0)
            self.assertEqual(row["tunebat_url"], "https://tunebat.com/Info/song/abc")

    def test_backfill_tunebat_local_stores_missing_analysis(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            root = temp / "music"
            track_path = root / "Artist" / "Album" / "01 - Song.flac"
            analyzer = temp / "fake-analyzer.js"
            track_path.parent.mkdir(parents=True)
            track_path.write_bytes(b"a" * 100)
            analyzer.write_text(
                """
                console.log(JSON.stringify({
                  analyzer_url: "https://tunebat.com/Analyzer",
                  filename: "01 - Song.flac",
                  key: "A minor",
                  mode: "minor",
                  camelot: "8A",
                  bpm: 120,
                  energy: 0.5
                }));
                """,
                encoding="utf-8",
            )
            conn = connect(temp / "library.sqlite3")
            scan(conn, [Source("patrick", "rockhouse", root, 100)], prune=True)

            command_backfill_tunebat_local(conn, analyzer, limit=1, max_seconds=30, force=False, emit=False)

            row = conn.execute("SELECT tunebat_key, tunebat_camelot, tunebat_bpm FROM tracks").fetchone()
            self.assertEqual(row["tunebat_key"], "A minor")
            self.assertEqual(row["tunebat_camelot"], "8A")
            self.assertEqual(row["tunebat_bpm"], 120.0)

    def test_scan_prunes_removed_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            root = temp / "music"
            track = root / "Artist" / "Album" / "Song.mp3"
            track.parent.mkdir(parents=True)
            track.write_bytes(b"a" * 100)
            conn = connect(temp / "library.sqlite3")
            source = Source("patrick", "rockhouse", root, 100)

            scan(conn, [source], prune=True)
            track.unlink()
            scan(conn, [source], prune=True)

            count = conn.execute("SELECT COUNT(*) AS count FROM files").fetchone()["count"]
            self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
