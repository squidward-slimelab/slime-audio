import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import wave
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import slime_audio_session_runner as runner


def write_silence(path: Path, *, duration_s: float = 24.0) -> None:
    sample_rate = 48_000
    frame_count = int(duration_s * sample_rate)
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(b"\0\0" * frame_count)


def stem_group(group_id: str, deck: str, start_ms: int, duration_ms: int, stems: dict[str, Path], **extra: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": group_id,
        "deck": deck,
        "source_path": str(stems["source"]),
        "start_ms": start_ms,
        "trim_start_ms": 0,
        "duration_ms": duration_ms,
        "stems": {
            name: {"path": str(path)}
            for name, path in stems.items()
            if name != "source"
        },
    }
    payload.update(extra)
    return payload


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is required for render performance tests")
class SlimeAudioSessionRunnerPerformanceTests(unittest.TestCase):
    def test_stem_window_render_is_comfortably_faster_than_realtime(self):
        min_speedup = float(os.environ.get("SLIME_AUDIO_RENDER_MIN_SPEEDUP", "4.5"))
        window_ms = 90_000
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            shared_stems: dict[str, Path] = {"source": temp / "source.wav"}
            for stem_name in ("vocals", "drums", "bass", "other"):
                path = temp / f"{stem_name}.wav"
                write_silence(path, duration_s=180.0)
                shared_stems[stem_name] = path
            stem_sets = [shared_stems for _ in range(5)]
            groups = [
                stem_group("lead-1", "deck-1", 0, 90_000, stem_sets[0]),
                stem_group("lead-2", "deck-2", 0, 90_000, stem_sets[1], tempo_shift_pct=1.5, pitch_shift_semitones=1),
                stem_group(
                    "bed-1",
                    "deck-3",
                    4_000,
                    68_000,
                    stem_sets[2],
                    play_stems=["drums", "bass", "other"],
                    gain_db=-8.0,
                    tempo_shift_pct=3.0,
                    pitch_shift_semitones=2,
                ),
                stem_group(
                    "bed-2",
                    "deck-4",
                    12_000,
                    68_000,
                    stem_sets[3],
                    play_stems=["drums", "bass", "other"],
                    gain_db=-9.0,
                    tempo_shift_pct=-2.0,
                    pitch_shift_semitones=-1,
                ),
                stem_group(
                    "bed-3",
                    "deck-5",
                    20_000,
                    60_000,
                    stem_sets[4],
                    play_stems=["drums", "bass", "other"],
                    gain_db=-10.0,
                    tempo_shift_pct=4.0,
                    pitch_shift_semitones=3,
                ),
            ]

            session_path = temp / "session.json"
            output = temp / "window.wav"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1", "deck-2", "deck-3", "deck-4", "deck-5"],
                        "stem_groups": groups,
                        "deck_automations": [
                            {
                                "target": "deck-3",
                                "param": "lowpass_hz",
                                "source_clip_id": "bed-1",
                                "points": [
                                    {"at_ms": 4_000, "value": 900},
                                    {"at_ms": 40_000, "value": 3600},
                                    {"at_ms": 72_000, "value": 1200},
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            args = runner.parse_args_from(
                [
                    "--session",
                    str(session_path),
                    "--state",
                    str(temp / "state.json"),
                    "--target",
                    "all",
                    "--skip-tts",
                    "--sample-rate",
                    "48000",
                    "--channels",
                    "2",
                ]
            )

            started = time.perf_counter()
            runner.render_window(args, 0, window_ms, output)
            elapsed = time.perf_counter() - started
            output_size = output.stat().st_size

            realtime_seconds = window_ms / 1000
            speedup = realtime_seconds / elapsed
        self.assertGreater(output_size, 44)
        self.assertGreaterEqual(
            speedup,
            min_speedup,
            f"stem window render too slow: {elapsed:.2f}s for {realtime_seconds:.1f}s "
            f"window ({speedup:.2f}x realtime, required {min_speedup:.2f}x)",
        )
