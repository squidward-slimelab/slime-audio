import json
import sys
import tempfile
import unittest
import wave
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from slime_audio_analysis_preflight import build_report, session_paths
from slime_audio_dj import BeatGrid, CuePoint, StructureWindow, TrackAnalysis, store_analysis_in_db


def write_silent_wav(path: Path, duration_ms: int, *, sample_rate: int = 8000) -> None:
    frame_count = max(1, int(sample_rate * duration_ms / 1000))
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(b"\x00\x00" * frame_count)


def ready_analysis(path: Path) -> TrackAnalysis:
    return TrackAnalysis(
        path=str(path),
        duration_s=1.0,
        sample_rate=8000,
        channels=1,
        bpm=120.0,
        beat_offset_ms=0,
        key="C major",
        tonic=0,
        mode="major",
        camelot="8B",
        energy=0.5,
        loudness_db=-12.0,
        confidence={"bpm": 0.9, "key": 0.8},
        beatgrid=BeatGrid(bpm=120.0, beat_offset_ms=0, phrase_beats=32, phrase_ms=16_000),
        structure=[StructureWindow("intro", 0, 1000, 0.8, "test")],
        cues=[CuePoint("clean_intro", "clean intro", 0, 1000, 0.8, "test", True, "test")],
    )


class SlimeAudioAnalysisPreflightTests(unittest.TestCase):
    def test_build_report_marks_ready_and_missing_tracks(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            db_path = temp_path / "library.sqlite3"
            ready = temp_path / "ready.wav"
            missing = temp_path / "missing.wav"
            write_silent_wav(ready, 1000)
            write_silent_wav(missing, 1000)
            store_analysis_in_db(db_path, ready, ready_analysis(ready))

            report = build_report([ready, missing], db_path=db_path)

            self.assertEqual(report["track_count"], 2)
            self.assertEqual(report["ready_count"], 1)
            self.assertEqual(report["problem_counts"]["missing_analysis"], 1)
            self.assertFalse(report["tracks"][1]["ready"])

    def test_session_paths_can_select_future_window(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            past = temp_path / "past.wav"
            future = temp_path / "future.wav"
            write_silent_wav(past, 1000)
            write_silent_wav(future, 1000)
            session_path = temp_path / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "clips": [
                            {"id": "past", "deck": "deck-1", "path": str(past), "start_ms": 0, "duration_ms": 1000},
                            {"id": "future", "deck": "deck-1", "path": str(future), "start_ms": 10_000, "duration_ms": 1000},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(session_paths(session_path, from_ms=5_000), [future])


if __name__ == "__main__":
    unittest.main()
