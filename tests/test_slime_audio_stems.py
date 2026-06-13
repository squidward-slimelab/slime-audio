import json
import math
import struct
import sys
import tempfile
import unittest
import wave
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import slime_audio_stems as stems
from slime_music_library import connect


def write_tone(path: Path, *, frequency: float, duration_ms: int = 1000, sample_rate: int = 8000, amplitude: float = 0.25) -> None:
    frames = int(sample_rate * duration_ms / 1000)
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        payload = bytearray()
        for index in range(frames):
            value = int(32767 * amplitude * math.sin(2 * math.pi * frequency * index / sample_rate))
            payload.extend(struct.pack("<h", value))
        audio.writeframes(bytes(payload))


def write_constant(path: Path, *, values: list[float], sample_rate: int = 1000) -> None:
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        payload = bytearray()
        for value in values:
            payload.extend(struct.pack("<h", int(32767 * value)))
        audio.writeframes(bytes(payload))


class SlimeAudioStemsTests(unittest.TestCase):
    def test_split_ingests_seeded_stems_and_writes_manifest_db_windows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            db_path = temp / "library.sqlite3"
            source = temp / "source.wav"
            stem_dir = temp / "demucs"
            stem_root = temp / "stems"
            stem_dir.mkdir()
            write_tone(source, frequency=220, duration_ms=2000)
            write_tone(stem_dir / "vocals.wav", frequency=440, duration_ms=2000)
            write_tone(stem_dir / "drums.wav", frequency=880, duration_ms=2000)
            write_tone(stem_dir / "bass.wav", frequency=110, duration_ms=2000)
            write_tone(stem_dir / "other.wav", frequency=330, duration_ms=2000)

            with redirect_stdout(StringIO()) as stdout:
                result = stems.main(
                    [
                        "--db",
                        str(db_path),
                        "--stem-root",
                        str(stem_root),
                        "split",
                        str(source),
                        "--source-stems-dir",
                        str(stem_dir),
                    ]
                )

            payload = json.loads(stdout.getvalue())
            conn = connect(db_path)
            stem_set = conn.execute("SELECT * FROM track_stem_sets WHERE id = ?", (payload["id"],)).fetchone()
            stem_rows = conn.execute("SELECT * FROM track_stems WHERE stem_set_id = ?", (payload["id"],)).fetchall()
            window_count = conn.execute("SELECT COUNT(*) AS count FROM track_stem_windows WHERE stem_set_id = ?", (payload["id"],)).fetchone()["count"]
            conn.close()
            manifest_exists = Path(payload["manifest"]).exists()

        self.assertEqual(result, 0)
        self.assertEqual(stem_set["status"], "ready")
        self.assertEqual(len(stem_rows), 4)
        self.assertGreater(window_count, 0)
        self.assertTrue(manifest_exists)

    def test_verify_reports_missing_artifacts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            db_path = temp / "library.sqlite3"
            artifact_root = temp / "missing"
            conn = connect(db_path)
            conn.execute(
                """
                INSERT INTO track_stem_sets(
                    id, source_path, source_size, source_mtime, model, profile, artifact_root,
                    status, created_at, updated_at
                )
                VALUES ('abc', '/music/a.wav', 1, 1, 'htdemucs', '4stem', ?, 'ready', 'now', 'now')
                """,
                (str(artifact_root),),
            )
            conn.commit()
            conn.close()

            with redirect_stdout(StringIO()) as stdout:
                result = stems.main(["--db", str(db_path), "verify", "abc"])

            payload = json.loads(stdout.getvalue())

        self.assertEqual(result, 1)
        self.assertIn("manifest missing", payload["errors"])

    def test_run_demucs_uses_remote_host_when_configured(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            output = temp / "remote-output" / "htdemucs" / "source"
            output.mkdir(parents=True)
            for stem_name in stems.CANONICAL_STEMS:
                (output / f"{stem_name}.wav").write_bytes(b"stem")

            calls = []

            def fake_remote_shell(host: str, command: str, capture: bool = False) -> str:
                calls.append((host, command, capture))
                if capture:
                    return "/tmp/slime-audio-demucs.remote"
                return ""

            with patch.object(stems, "remote_shell", side_effect=fake_remote_shell):
                with patch.object(stems, "remote_path_readable", return_value=True):
                    with patch.object(stems, "remote_rsync_from"):
                        with patch.object(stems.subprocess, "run"):
                            result = stems.run_demucs(
                                Path("/mnt/rockhouse/Music/source.flac"),
                                temp,
                                demucs_bin="demucs",
                                model="htdemucs",
                                jobs=1,
                                demucs_host="squidward@patrick",
                            )

        self.assertEqual(result, output)
        self.assertTrue(any(call[0] == "squidward@patrick" and "demucs" in call[1] for call in calls))

    def test_remote_path_readable_quotes_shell_sensitive_paths(self):
        calls = []

        def fake_run(command, check=False):
            calls.append(command)
            return type("Result", (), {"returncode": 0})()

        with patch.object(stems.subprocess, "run", side_effect=fake_run):
            readable = stems.remote_path_readable("squidward@robokrabs", Path("/mnt/Music/Album (Disc 1)/Track Name.flac"))

        self.assertTrue(readable)
        self.assertEqual(calls[0][0:2], ["ssh", "squidward@robokrabs"])
        self.assertIn("'/mnt/Music/Album (Disc 1)/Track Name.flac'", calls[0][2])

    def test_run_demucs_falls_back_to_second_remote_host(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            output = temp / "remote-output" / "htdemucs" / "source"
            output.mkdir(parents=True)
            for stem_name in stems.CANONICAL_STEMS:
                (output / f"{stem_name}.wav").write_bytes(b"stem")

            calls = []

            def fake_run_remote_demucs(source_path, temp_dir, *, host, demucs_bin, model, jobs, remote_workdir=None):
                calls.append(host)
                if host == "squidward@patrick":
                    raise RuntimeError("ssh failed")
                return output

            with patch.object(stems, "run_remote_demucs", side_effect=fake_run_remote_demucs):
                result = stems.run_demucs(
                    Path("/mnt/rockhouse/Music/source.flac"),
                    temp,
                    demucs_bin="demucs",
                    model="htdemucs",
                    jobs=1,
                    demucs_host="squidward@patrick,squidward@robokrabs",
                )

        self.assertEqual(result, output)
        self.assertEqual(calls, ["squidward@patrick", "squidward@robokrabs"])

    def test_run_demucs_reports_all_remote_failures(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)

            def fake_run_remote_demucs(source_path, temp_dir, *, host, demucs_bin, model, jobs, remote_workdir=None):
                raise RuntimeError(f"{host} unavailable")

            with patch.object(stems, "run_remote_demucs", side_effect=fake_run_remote_demucs):
                with self.assertRaisesRegex(RuntimeError, "remote Demucs failed on all hosts"):
                    stems.run_demucs(
                        Path("/mnt/rockhouse/Music/source.flac"),
                        temp,
                        demucs_bin="demucs",
                        model="htdemucs",
                        jobs=1,
                        demucs_host="squidward@patrick,squidward@robokrabs",
                    )

    def test_local_demucs_flag_disables_default_remote_host(self):
        args = stems.parse_args(["split", "/tmp/source.wav", "--local-demucs"])

        self.assertIsNone(args.demucs_host)

    def test_default_demucs_hosts_include_gpu_fallback(self):
        args = stems.parse_args(["split", "/tmp/source.wav"])

        self.assertEqual(stems.parse_demucs_hosts(args.demucs_host), ["squidward@patrick", "squidward@robokrabs"])

    def test_measure_stem_slice_combines_selected_stems_for_exact_window(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            write_constant(temp / "drums.wav", values=[0.0] * 500 + [0.25] * 500)
            write_constant(temp / "bass.wav", values=[0.0] * 500 + [0.25] * 500)
            write_constant(temp / "vocals.wav", values=[0.5] * 1000)

            result = stems.measure_stem_slice(
                {
                    "drums": temp / "drums.wav",
                    "bass": temp / "bass.wav",
                    "vocals": temp / "vocals.wav",
                },
                ["drums", "bass"],
                start_ms=500,
                duration_ms=500,
            )

        self.assertEqual(result["actual_duration_ms"], 500)
        expected = (int(32767 * 0.25) / 32768.0) * 2
        self.assertAlmostEqual(result["loudness_db"], 20 * math.log10(expected), places=3)
        self.assertAlmostEqual(result["peak_db"], 20 * math.log10(expected), places=3)

    def test_measure_slice_command_resolves_ready_stem_set_and_prints_json(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            db_path = temp / "library.sqlite3"
            artifact_root = temp / "stems" / "abc"
            artifact_root.mkdir(parents=True)
            for stem_name, value in {"vocals": 0.5, "drums": 0.25, "bass": 0.125, "other": 0.0}.items():
                write_constant(artifact_root / f"{stem_name}.wav", values=[value] * 1000)
            conn = connect(db_path)
            conn.execute(
                """
                INSERT INTO track_stem_sets(
                    id, source_path, source_size, source_mtime, model, profile, artifact_root,
                    status, created_at, updated_at
                )
                VALUES ('abc', '/music/a.wav', 1, 1, 'htdemucs', '4stem', ?, 'ready', 'now', 'now')
                """,
                (str(artifact_root),),
            )
            for stem_name in stems.CANONICAL_STEMS:
                conn.execute(
                    """
                    INSERT INTO track_stems(stem_set_id, stem_name, path, loudness_db, peak_db)
                    VALUES ('abc', ?, ?, NULL, NULL)
                    """,
                    (stem_name, str(artifact_root / f"{stem_name}.wav")),
                )
            conn.commit()
            conn.close()

            with redirect_stdout(StringIO()) as stdout:
                result = stems.main(
                    [
                        "--db",
                        str(db_path),
                        "measure-slice",
                        "abc",
                        "--stems",
                        "drums,bass",
                        "--start-ms",
                        "0",
                        "--duration-ms",
                        "1000",
                    ]
                )
            payload = json.loads(stdout.getvalue())

        self.assertEqual(result, 0)
        self.assertEqual(payload["stem_set_id"], "abc")
        self.assertEqual(payload["stems"], ["drums", "bass"])
        expected = (int(32767 * 0.25) + int(32767 * 0.125)) / 32768.0
        self.assertAlmostEqual(payload["loudness_db"], 20 * math.log10(expected), places=3)


if __name__ == "__main__":
    unittest.main()
