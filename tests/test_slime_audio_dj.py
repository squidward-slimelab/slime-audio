import sys
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from slime_audio_dj import (
    CuePoint,
    StructureWindow,
    TrackAnalysis,
    analysis_db_path,
    analyze_with_cache,
    beat_grid,
    cache_key,
    camelot,
    cue_points_for_analysis,
    detect_structure_windows,
    drop_candidates_for_analysis,
    estimate_bpm,
    key_match,
    relative_tonic,
    select_cue,
    semitone_distance,
    session_tension_windows,
    suggested_lean_in_windows,
    transition_plan,
)
from slime_audio_session import parse_session
from slime_music_library import Source, command_set_tunebat, connect, scan


def track(path: str, bpm: float, tonic: int, mode: str, energy: float = 0.2) -> TrackAnalysis:
    return TrackAnalysis(
        path=path,
        duration_s=180.0,
        sample_rate=44100,
        channels=2,
        bpm=bpm,
        beat_offset_ms=0,
        key=f"{tonic} {mode}",
        tonic=tonic,
        mode=mode,
        camelot=camelot(tonic, mode),
        energy=energy,
        loudness_db=-12.0,
        confidence={"bpm": 0.9, "key": 0.9},
        structure=[
            StructureWindow("intro", 0, 16_000, 0.55, "opening phrase region"),
            StructureWindow("build", 48_000, 64_000, 0.74, "energy rising into a likely transition"),
            StructureWindow("drop", 64_000, 80_000, 0.91, "energy crosses high threshold"),
        ],
    )


class SlimeAudioDjTests(unittest.TestCase):
    def test_camelot_relative_major_minor_pair(self):
        self.assertEqual(camelot(9, "minor"), "8A")
        self.assertEqual(camelot(0, "major"), "8B")
        self.assertEqual(relative_tonic(9, "minor"), 0)
        self.assertEqual(relative_tonic(0, "major"), 9)

    def test_minor_to_major_rotation_scores_as_key_match_without_pitch_shift(self):
        source = track("a-minor.wav", 124, 9, "minor")
        target = track("c-major.wav", 124, 0, "major")

        score, pitch_shift, relation, notes = key_match(source, target, max_pitch_shift=2)

        self.assertGreater(score, 0.9)
        self.assertEqual(pitch_shift, 0)
        self.assertEqual(relation, "relative major/minor rotation")
        self.assertTrue(any("mode-rotation" in note for note in notes))

    def test_pitch_shift_prefers_small_same_mode_moves(self):
        source = track("c-minor.wav", 128, 0, "minor")
        target = track("d-minor.wav", 128, 2, "minor")

        score, pitch_shift, relation, _notes = key_match(source, target, max_pitch_shift=2)

        self.assertGreater(score, 0.75)
        self.assertEqual(pitch_shift, -2)
        self.assertEqual(relation, "pitch-shift same mode")

    def test_transition_plan_combines_tempo_key_and_energy(self):
        source = track("a-minor.wav", 124, 9, "minor", energy=0.3)
        target = track("c-major.wav", 126, 0, "major", energy=0.32)

        plan = transition_plan(source, target)

        self.assertGreater(plan.score, 0.85)
        self.assertEqual(plan.pitch_shift_semitones, 0)
        self.assertEqual(plan.key_relation, "relative major/minor rotation")
        self.assertAlmostEqual(plan.target_tempo_shift_pct, 1.61, places=2)

    def test_semitone_distance_uses_shortest_signed_path(self):
        self.assertEqual(semitone_distance(11, 1), 2)
        self.assertEqual(semitone_distance(1, 11), -2)

    def test_estimate_bpm_locks_simple_pulse_train(self):
        envelope = [0.0] * 240
        # 120 BPM at 46 ms frames is roughly every 11 frames.
        for index in range(0, len(envelope), 11):
            envelope[index] = 1000.0

        bpm, _offset_ms, confidence = estimate_bpm(envelope)

        self.assertIsNotNone(bpm)
        self.assertAlmostEqual(bpm, 118.58, places=1)
        self.assertGreaterEqual(confidence, 0.0)

    def test_beat_grid_calculates_phrase_length(self):
        grid = beat_grid(120.0, 250)

        self.assertEqual(grid.beat_offset_ms, 250)
        self.assertEqual(grid.phrase_beats, 32)
        self.assertEqual(grid.phrase_ms, 16000)

    def test_detect_structure_finds_drop_after_energy_rise(self):
        # 46 ms frames, roughly 60 seconds. Start low, rise, then hit a high-energy section.
        envelope = ([100.0] * 300) + ([250.0 + index * 6 for index in range(120)]) + ([1200.0] * 900)

        windows = detect_structure_windows(envelope, 120.0, 0, duration_s=len(envelope) * 0.046)
        kinds = [window.kind for window in windows]
        suggestions = suggested_lean_in_windows(windows)

        self.assertIn("build", kinds)
        self.assertIn("drop", kinds)
        self.assertTrue(any(item["kind"] == "pre-drop" for item in suggestions))

    def test_session_tension_windows_maps_track_structure_to_mix_time(self):
        session = parse_session(
            {
                "version": 1,
                "decks": ["deck-1", "deck-2"],
                "clips": [
                    {"id": "a", "deck": "deck-1", "path": "/music/a.flac", "start": 120_000, "duration": 120_000},
                    {"id": "b", "deck": "deck-2", "path": "/music/b.flac", "start": 240_000, "duration": 120_000},
                ],
                "mic_lean_ins": [],
            }
        )
        analyses = {
            "/music/a.flac": track("/music/a.flac", 124, 9, "minor", energy=0.3),
            "/music/b.flac": track("/music/b.flac", 126, 0, "major", energy=0.31),
        }

        windows = session_tension_windows(session, analyses)
        pre_drop = next(window for window in windows if window.kind == "pre-drop" and window.clip_id == "a")
        transition = next(window for window in windows if window.kind == "transition")

        self.assertEqual(pre_drop.start_ms, 182_500)
        self.assertIn("speak briefly", " ".join(pre_drop.talking_points))
        self.assertEqual(transition.start_ms, 232_000)
        self.assertEqual(transition.next_clip_id, "b")
        self.assertIn("key relation", transition.reason)

    def test_analyze_with_cache_overrides_raw_cache_with_library_tunebat_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            root = temp / "Music"
            track_path = root / "Artist" / "Album" / "01 - Song.mp3"
            track_path.parent.mkdir(parents=True)
            track_path.write_bytes(b"not real audio but good enough for cache-key")
            conn = connect(temp / "library.sqlite3")
            scan(conn, [Source("patrick", "rockhouse", root, 100)], prune=True)
            duplicate_key = conn.execute("SELECT duplicate_key FROM tracks").fetchone()["duplicate_key"]
            command_set_tunebat(
                conn,
                duplicate_key,
                "https://tunebat.com/Analyzer",
                "Song",
                "Artist",
                "C# major",
                "major",
                "3B",
                126.0,
                None,
                0.75,
                None,
                None,
                {"source": "test"},
                emit=False,
            )
            cache_path = temp / "dj-cache.json"
            raw = track(str(track_path), 81.52, 6, "minor", energy=0.2)
            cache_path.write_text(json.dumps({cache_key(track_path): raw.__dict__}, default=lambda value: value.__dict__), encoding="utf-8")

            analysis = analyze_with_cache([track_path], cache_path, "ffmpeg", 44_100, temp / "library.sqlite3", temp / "missing-analyzer.js")[0]
            cached = json.loads(cache_path.read_text(encoding="utf-8"))[cache_key(track_path)]

        self.assertEqual(analysis.bpm, 126.0)
        self.assertEqual(analysis.camelot, "3B")
        self.assertEqual(analysis.key, "C# major")
        self.assertEqual(analysis.mode, "major")
        self.assertEqual(analysis.confidence["bpm"], 1.0)
        self.assertEqual(cached["bpm"], 126.0)
        self.assertEqual(cached["camelot"], "3B")

    def test_analyze_with_cache_persists_structure_in_music_db_and_reuses_it(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            track_path = temp / "track.wav"
            track_path.write_bytes(b"fake audio identity")
            db_path = temp / "library.sqlite3"
            cache_path = temp / "dj-cache.json"
            computed = track(str(track_path), 120, 0, "major", energy=0.4)

            with patch("slime_audio_dj.analyze_track", return_value=computed) as analyze_mock:
                first = analyze_with_cache([track_path], cache_path, "ffmpeg", 44_100, db_path, temp / "missing-analyzer.js")[0]
                second = analyze_with_cache([track_path], cache_path, "ffmpeg", 44_100, db_path, temp / "missing-analyzer.js")[0]

            self.assertEqual(analyze_mock.call_count, 1)
            self.assertEqual(first.structure, second.structure)
            conn = connect(db_path)
            identity_path = analysis_db_path(track_path)
            analysis_row = conn.execute("SELECT bpm, phrase_beats FROM track_dj_analysis WHERE path = ?", (identity_path,)).fetchone()
            structure_rows = conn.execute("SELECT kind FROM track_dj_structure WHERE path = ? ORDER BY start_ms", (identity_path,)).fetchall()
            drop_rows = conn.execute("SELECT kind FROM track_dj_drop_candidates WHERE path = ? ORDER BY start_ms", (identity_path,)).fetchall()
            cue_rows = conn.execute("SELECT kind, quantized FROM track_dj_cues WHERE path = ? ORDER BY at_ms", (identity_path,)).fetchall()
            conn.close()

        self.assertEqual(analysis_row["bpm"], 120.0)
        self.assertEqual(analysis_row["phrase_beats"], 32)
        self.assertIn("drop", [row["kind"] for row in structure_rows])
        self.assertIn("pre_drop", [row["kind"] for row in drop_rows])
        self.assertIn("hook", [row["kind"] for row in cue_rows])
        self.assertTrue(any(row["kind"] == "drop" and row["quantized"] == 1 for row in cue_rows))

    def test_analyze_with_cache_invalidates_db_analysis_when_file_identity_changes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            track_path = temp / "track.wav"
            track_path.write_bytes(b"first identity")
            db_path = temp / "library.sqlite3"
            cache_path = temp / "dj-cache.json"
            first = track(str(track_path), 120, 0, "major", energy=0.4)
            updated = track(str(track_path), 128, 2, "minor", energy=0.6)

            with patch("slime_audio_dj.analyze_track", return_value=first) as analyze_mock:
                analyze_with_cache([track_path], cache_path, "ffmpeg", 44_100, db_path, temp / "missing-analyzer.js")
            track_path.write_bytes(b"second identity with different size")
            with patch("slime_audio_dj.analyze_track", return_value=updated) as analyze_mock:
                analysis = analyze_with_cache([track_path], cache_path, "ffmpeg", 44_100, db_path, temp / "missing-analyzer.js")[0]

            self.assertEqual(analyze_mock.call_count, 1)
            self.assertEqual(analysis.bpm, 128)

    def test_drop_candidates_are_derived_from_stored_structure(self):
        analysis = track("/music/a.wav", 124, 9, "minor")

        candidates = drop_candidates_for_analysis(analysis)

        self.assertTrue(any(candidate["kind"] == "drop" and candidate["start_ms"] == 64_000 for candidate in candidates))
        self.assertTrue(any(candidate["kind"] == "pre_drop" and candidate["end_ms"] == 64_000 for candidate in candidates))

    def test_cue_points_are_quantized_and_selectable_by_kind(self):
        analysis = TrackAnalysis(
            path="/music/a.wav",
            duration_s=180.0,
            sample_rate=44100,
            channels=2,
            bpm=120.0,
            beat_offset_ms=0,
            key=None,
            tonic=None,
            mode=None,
            camelot=None,
            energy=0.5,
            loudness_db=-12.0,
            confidence={"bpm": 0.9, "key": 0.0},
            beatgrid=beat_grid(120.0, 0),
            structure=[StructureWindow("drop", 63_700, 80_000, 0.91, "release")],
        )

        cues = cue_points_for_analysis(analysis)
        selected = select_cue(analysis, {"hook", "drop"})

        self.assertIn(CuePoint("drop", "drop", 64_000, 80_000, 0.91, "detected_structure", True, "release"), cues)
        self.assertIsNotNone(selected)
        self.assertEqual(selected.kind, "drop")
        self.assertEqual(selected.at_ms, 64_000)


if __name__ == "__main__":
    unittest.main()
