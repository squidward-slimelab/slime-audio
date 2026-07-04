import json
import math
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from slime_audio_session import load_session
from slime_audio_session_mixdown import (
    balance_bed_candidates,
    build_filter_complex,
    crossfader_gain,
    ffmpeg_command,
    prepare_lean_in_audio,
    routine_taste_report,
    routine_window,
    session_with_only_clip_ids,
    session_duration_ms,
    shift_session_window,
    spill_filter_complex_to_script,
    stem_automation_windows,
    stem_group_inputs,
)


class SlimeAudioSessionMixdownTests(unittest.TestCase):
    def write_beep_track(self, path: Path, *, frequency_hz: float = 440.0, duration_s: float = 5.0, interval_s: float = 0.5) -> None:
        sample_rate = 48_000
        beep_s = 0.08
        frame_count = int(duration_s * sample_rate)
        interval_frames = int(interval_s * sample_rate)
        beep_frames = int(beep_s * sample_rate)
        samples = []
        for index in range(frame_count):
            in_beep = (index % interval_frames) < beep_frames
            value = 0.0
            if in_beep:
                value = 0.55 * math.sin(2 * math.pi * frequency_hz * (index / sample_rate))
            samples.append(struct.pack("<h", int(max(-1.0, min(1.0, value)) * 32767)))
        with wave.open(str(path), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(sample_rate)
            audio.writeframes(b"".join(samples))

    def write_beatgrid_track(self, path: Path, *, bpm: float = 120.0, bars: int = 12) -> None:
        sample_rate = 48_000
        beat_s = 60.0 / bpm
        beep_s = 0.05
        beat_count = bars * 4
        frame_count = int((beat_count * beat_s + 0.25) * sample_rate)
        samples = []
        for index in range(frame_count):
            elapsed_s = index / sample_rate
            beat_index = int(elapsed_s / beat_s)
            beat_pos_s = elapsed_s - (beat_index * beat_s)
            value = 0.0
            if beat_index < beat_count and beat_pos_s < beep_s:
                downbeat = beat_index % 4 == 0
                frequency_hz = 880.0 if downbeat else 440.0
                amplitude = 0.70 if downbeat else 0.45
                value = amplitude * math.sin(2 * math.pi * frequency_hz * elapsed_s)
            samples.append(struct.pack("<h", int(max(-1.0, min(1.0, value)) * 32767)))
        with wave.open(str(path), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(sample_rate)
            audio.writeframes(b"".join(samples))

    def write_stem_beat_track(self, path: Path, *, frequency_hz: float, beat_offset: int, bpm: float = 120.0, bars: int = 8) -> None:
        sample_rate = 48_000
        beat_s = 60.0 / bpm
        beep_s = 0.05
        beat_count = bars * 4
        frame_count = int((beat_count * beat_s + 0.25) * sample_rate)
        samples = []
        for index in range(frame_count):
            elapsed_s = index / sample_rate
            beat_index = int(elapsed_s / beat_s)
            beat_pos_s = elapsed_s - (beat_index * beat_s)
            value = 0.0
            if beat_index < beat_count and beat_index % 4 == beat_offset and beat_pos_s < beep_s:
                value = 0.55 * math.sin(2 * math.pi * frequency_hz * elapsed_s)
            samples.append(struct.pack("<h", int(max(-1.0, min(1.0, value)) * 32767)))
        with wave.open(str(path), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(sample_rate)
            audio.writeframes(b"".join(samples))

    def read_wav_samples(self, path: Path) -> tuple[int, list[float]]:
        with wave.open(str(path), "rb") as audio:
            channels = audio.getnchannels()
            sample_rate = audio.getframerate()
            frames = audio.readframes(audio.getnframes())
        values = struct.unpack("<" + "h" * (len(frames) // 2), frames)
        if channels > 1:
            values = values[::channels]
        return sample_rate, [value / 32768.0 for value in values]

    def test_inactive_stem_group_does_not_fall_back_to_full_track(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            session_path = temp / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "actions": [
                            {
                                "type": "load_track",
                                "id": "muted-preload",
                                "deck": "deck-1",
                                "source_path": "/music/full.flac",
                                "at_ms": 0,
                                "duration_ms": 10_000,
                                "play_stems": [],
                                "stems": {
                                    "vocals": {"path": "/stems/vocals.wav"},
                                    "drums": {"path": "/stems/drums.wav"},
                                    "bass": {"path": "/stems/bass.wav"},
                                    "other": {"path": "/stems/other.wav"},
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            session = load_session(session_path)

        self.assertEqual(stem_group_inputs(session.stem_groups[0]), [])

    def test_stem_toggle_points_render_as_persistent_mute_windows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            session_path = temp / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "actions": [
                            {
                                "type": "load_track",
                                "id": "bass-preload",
                                "deck": "deck-1",
                                "source_path": "/music/full.flac",
                                "at_ms": 0,
                                "duration_ms": 10_000,
                                "play_stems": ["bass"],
                                "stems": {
                                    "vocals": {"path": "/stems/vocals.wav"},
                                    "drums": {"path": "/stems/drums.wav"},
                                    "bass": {"path": "/stems/bass.wav"},
                                    "other": {"path": "/stems/other.wav"},
                                },
                            },
                            {"type": "stem_toggle", "id": "bass-muted", "target": "bass-preload", "stem": "bass", "at_ms": 0, "enabled": False},
                            {"type": "stem_toggle", "id": "bass-live", "target": "bass-preload", "stem": "bass", "at_ms": 2_000, "enabled": True},
                            {"type": "stem_toggle", "id": "bass-out", "target": "bass-preload", "stem": "bass", "at_ms": 8_000, "enabled": False},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            session = load_session(session_path)

        # Toggles segment the deck clock: silent preload, bass punched in for
        # 2s-8s, silent tail — the same audible result the old persistent mute
        # windows produced, expressed as segments.
        segments = [
            (group.start_ms, group.duration_ms, [name for name, stem in sorted(group.stems.items()) if stem.enabled])
            for group in sorted(session.stem_groups, key=lambda g: g.start_ms)
        ]
        self.assertEqual(segments, [(0, 2_000, []), (2_000, 6_000, ["bass"]), (8_000, 2_000, [])])
        live = next(group for group in session.stem_groups if group.start_ms == 2_000)
        self.assertEqual([(name, path) for name, _stem, path in stem_group_inputs(live)], [("bass", "/stems/bass.wav")])

    def beep_centers_seconds(self, samples: list[float], sample_rate: int) -> list[float]:
        frame_size = sample_rate // 100
        rms = []
        for offset in range(0, len(samples), frame_size):
            chunk = samples[offset : offset + frame_size]
            if not chunk:
                continue
            rms.append(math.sqrt(sum(value * value for value in chunk) / len(chunk)))
        threshold = max(rms, default=0.0) * 0.35
        centers = []
        start = None
        for index, value in enumerate(rms + [0.0]):
            if value >= threshold and start is None:
                start = index
            elif value < threshold and start is not None:
                centers.append(((start + index - 1) / 2) * (frame_size / sample_rate))
                start = None
        return centers

    def dominant_frequency_hz(self, samples: list[float], sample_rate: int, center_s: float) -> float:
        # Goertzel scan over a tight window. The old zero-crossing count was
        # diluted by silence inside its window and misread correct renders by
        # up to a semitone in either direction.
        start = max(0, int((center_s - 0.012) * sample_rate))
        end = min(len(samples), int((center_s + 0.012) * sample_rate))
        window = samples[start:end]
        if not window:
            return 0.0

        def power(frequency_hz: float) -> float:
            omega = 2 * math.pi * frequency_hz / sample_rate
            coeff = 2 * math.cos(omega)
            q0 = q1 = q2 = 0.0
            for value in window:
                q0 = coeff * q1 - q2 + value
                q2 = q1
                q1 = q0
            return q1 * q1 + q2 * q2 - coeff * q1 * q2

        return max((x * 2.5 for x in range(40, 1200)), key=power)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is required for render-level DSP tests")
    def test_rendered_tempo_and_pitch_shifts_preserve_expected_beep_timing_and_frequency(self):
        cases = [
            ("tempo-only", 100.0, 0, 0.25, 440.0),
            ("pitch-only", 0.0, 12, 0.50, 880.0),
            ("tempo-and-pitch", 100.0, 12, 0.25, 880.0),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            source = temp / "beeps.wav"
            self.write_beep_track(source)
            for name, tempo_shift, pitch_shift, expected_interval, expected_frequency in cases:
                with self.subTest(name=name):
                    session_path = temp / f"{name}.json"
                    output = temp / f"{name}.wav"
                    session_path.write_text(
                        json.dumps(
                            {
                                "version": 1,
                                "decks": ["deck-1"],
                                "clips": [
                                    {
                                        "id": name,
                                        "deck": "deck-1",
                                        "path": str(source),
                                        "start": 0,
                                        "duration": 2000,
                                        "tempo_shift_pct": tempo_shift,
                                        "pitch_shift_semitones": pitch_shift,
                                    }
                                ],
                            }
                        ),
                        encoding="utf-8",
                    )
                    session = load_session(session_path)
                    subprocess.run(ffmpeg_command(session, {}, output, 48_000, 1), check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    sample_rate, samples = self.read_wav_samples(output)
                    centers = self.beep_centers_seconds(samples, sample_rate)

                    self.assertGreaterEqual(len(centers), 4)
                    intervals = [right - left for left, right in zip(centers, centers[1:4])]
                    mean_interval = sum(intervals) / len(intervals)
                    frequency = self.dominant_frequency_hz(samples, sample_rate, centers[1])

                    self.assertAlmostEqual(mean_interval, expected_interval, delta=0.035)
                    self.assertAlmostEqual(frequency, expected_frequency, delta=35.0)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is required for render-level beatgrid tests")
    def test_rendered_tempo_and_pitch_shifts_preserve_long_synthetic_beatgrid(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            source = temp / "beatgrid.wav"
            output = temp / "shifted-grid.wav"
            self.write_beatgrid_track(source, bpm=120.0, bars=12)
            session_path = temp / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "clips": [
                            {
                                "id": "synthetic-grid",
                                "deck": "deck-1",
                                "path": str(source),
                                "start": 0,
                                "duration": 19_200,
                                "tempo_shift_pct": 25.0,
                                "pitch_shift_semitones": 7,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            session = load_session(session_path)
            subprocess.run(ffmpeg_command(session, {}, output, 48_000, 1), check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            sample_rate, samples = self.read_wav_samples(output)
            centers = self.beep_centers_seconds(samples, sample_rate)

            self.assertGreaterEqual(len(centers), 44)
            beat_intervals = [right - left for left, right in zip(centers[4:40], centers[5:41])]
            expected_beat_s = 0.4
            mean_beat_s = sum(beat_intervals) / len(beat_intervals)
            max_drift_s = max(abs(interval - expected_beat_s) for interval in beat_intervals)
            downbeat_centers = centers[::4]
            downbeat_intervals = [right - left for left, right in zip(downbeat_centers[1:9], downbeat_centers[2:10])]
            mean_downbeat_s = sum(downbeat_intervals) / len(downbeat_intervals)
            downbeat_frequency = self.dominant_frequency_hz(samples, sample_rate, downbeat_centers[2])

            self.assertAlmostEqual(mean_beat_s, expected_beat_s, delta=0.012)
            self.assertLess(max_drift_s, 0.025)
            self.assertAlmostEqual(mean_downbeat_s, expected_beat_s * 4, delta=0.025)
            self.assertAlmostEqual(downbeat_frequency, 880.0 * (2 ** (7 / 12)), delta=55.0)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is required for render-level stem DSP tests")
    def test_rendered_stem_group_applies_tempo_and_pitch_shifts_to_every_attached_stem(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            stem_specs = {
                "vocals": (220.0, 0),
                "drums": (330.0, 1),
                "bass": (440.0, 2),
                "other": (550.0, 3),
            }
            stem_paths = {}
            for stem_name, (frequency_hz, beat_offset) in stem_specs.items():
                path = temp / f"{stem_name}.wav"
                self.write_stem_beat_track(path, frequency_hz=frequency_hz, beat_offset=beat_offset)
                stem_paths[stem_name] = path
            output = temp / "shifted-stems.wav"
            session_path = temp / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "stem_groups": [
                            {
                                "id": "synthetic-stems",
                                "deck": "deck-1",
                                "source_path": str(temp / "source.wav"),
                                "start": 0,
                                "duration": 12_800,
                                "tempo_shift_pct": 25.0,
                                "pitch_shift_semitones": 7,
                                "stems": {
                                    stem_name: {"path": str(path)}
                                    for stem_name, path in stem_paths.items()
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            session = load_session(session_path)
            subprocess.run(ffmpeg_command(session, {}, output, 48_000, 1), check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            sample_rate, samples = self.read_wav_samples(output)
            centers = self.beep_centers_seconds(samples, sample_rate)

            self.assertGreaterEqual(len(centers), 28)
            beat_intervals = [right - left for left, right in zip(centers[4:24], centers[5:25])]
            expected_beat_s = 0.4
            mean_beat_s = sum(beat_intervals) / len(beat_intervals)
            max_drift_s = max(abs(interval - expected_beat_s) for interval in beat_intervals)
            self.assertAlmostEqual(mean_beat_s, expected_beat_s, delta=0.012)
            self.assertLess(max_drift_s, 0.025)

            pitch_factor = 2 ** (7 / 12)
            for index, (stem_name, (frequency_hz, _beat_offset)) in enumerate(stem_specs.items()):
                with self.subTest(stem=stem_name):
                    measured = self.dominant_frequency_hz(samples, sample_rate, centers[4 + index])
                    self.assertAlmostEqual(measured, frequency_hz * pitch_factor, delta=35.0)

    def test_balance_audit_candidates_use_payload_planner_metadata(self):
        payload = {
            "clips": [
                {
                    "id": "lead",
                    "deck": "deck-1",
                    "path": "/music/lead.flac",
                    "start_ms": 0,
                    "duration_ms": 60_000,
                    "planner_role": "lead",
                },
                {
                    "id": "bed",
                    "deck": "deck-2",
                    "path": "/music/bed.flac",
                    "start_ms": 10_000,
                    "duration_ms": 30_000,
                    "planner_role": "rhythm-bed",
                    "gain_db": -8,
                },
            ],
            "deck_automations": [
                {
                    "target": "deck-2",
                    "param": "lowpass_hz",
                    "source_clip_id": "bed",
                    "points": [{"at_ms": 10_000, "value": 900}, {"at_ms": 30_000, "value": 2400}],
                },
                {
                    "target": "deck-2",
                    "param": "gain_db",
                    "source_clip_id": "bed",
                    "points": [{"at_ms": 10_000, "value": -8}, {"at_ms": 40_000, "value": -6}],
                },
            ],
        }

        candidates = balance_bed_candidates(payload, 0, 60_000)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["id"], "bed")
        self.assertEqual(candidates[0]["audit_start_ms"], 10_000)
        self.assertEqual(candidates[0]["audit_duration_ms"], 30_000)
        self.assertEqual(candidates[0]["filter_spans"]["lowpass_hz"]["range"], 1500)
        self.assertEqual(candidates[0]["filter_spans"]["gain_db"]["min"], -8)

    def test_session_with_only_clip_ids_keeps_requested_clip_and_drops_others(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1", "deck-2"],
                        "clips": [
                            {"id": "lead", "deck": "deck-1", "path": "/music/lead.flac", "start": 0, "duration": 10_000},
                            {"id": "bed", "deck": "deck-2", "path": "/music/bed.flac", "start": 0, "duration": 10_000},
                        ],
                        "effects": [
                            {"id": "lead-echo", "type": "echo", "target": "lead", "start_ms": 1_000, "duration_ms": 1_000},
                            {"id": "deck-effect", "type": "echo", "target": "deck:deck-2", "start_ms": 1_000, "duration_ms": 1_000},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            session = load_session(session_path)

        solo = session_with_only_clip_ids(session, {"bed"})

        self.assertEqual([clip.id for clip in solo.clips], ["bed"])
        self.assertEqual([effect.id for effect in solo.effects], ["deck-effect"])

    def test_mixdown_filter_combines_static_trim_and_fader_gain(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "clips": [
                            {
                                "id": "lead",
                                "deck": "deck-1",
                                "path": "/music/lead.flac",
                                "start": 0,
                                "duration": 10_000,
                                "trim_db": -3,
                                "gain_db": -6,
                            }
                        ],
                        "automations": [
                            {
                                "target": "lead",
                                "param": "gain_db",
                                "points": [{"at": 2_000, "value": -12}, {"at": 4_000, "value": -12}],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            filters = build_filter_complex(load_session(session_path), {}, 48_000, 2)

        self.assertIn("volume=enable='between(t,2.000,4.000)':volume=0.251189", filters)
        self.assertIn("volume=0.354813,adelay=0:all=1", filters)

    def test_mixdown_filter_renders_reverse_rate_shifted_scratch_clip(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "clips": [
                            {
                                "id": "scratch",
                                "deck": "deck-1",
                                "path": "/music/scratch.flac",
                                "start": 1_000,
                                "trim_start": 2_000,
                                "duration": 500,
                                "reverse": True,
                                "playback_rate": 1.5,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            filters = build_filter_complex(load_session(session_path), {}, 48_000, 2)

        # aresample before asetrate: rate/pitch math runs at the render rate.
        self.assertIn("atrim=start=2.000:duration=0.750,asetpts=PTS-STARTPTS,areverse,aresample=48000,asetrate=72000,aresample=48000", filters)
        self.assertIn("adelay=1000:all=1", filters)

    def test_long_filter_complex_can_spill_to_script_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            script = Path(temp_dir) / "filter.ffmpeg"
            command = ["ffmpeg", "-i", "a.flac", "-filter_complex", "a" * 12, "-map", "[out]", "out.mp3"]

            updated = spill_filter_complex_to_script(command, script, min_length=10)

            self.assertIn("-filter_complex_script", updated)
            self.assertNotIn("-filter_complex", updated)
            self.assertEqual(script.read_text(encoding="utf-8"), "a" * 12)

    def test_ffmpeg_command_deduplicates_repeated_clip_inputs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1", "deck-2"],
                        "clips": [
                            {"id": "a", "deck": "deck-1", "path": "/music/a.flac", "start": 0, "duration": 1_000},
                            {"id": "b", "deck": "deck-2", "path": "/music/a.flac", "start": 1_000, "duration": 1_000},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            session = load_session(session_path)

            command = ffmpeg_command(session, {}, Path("/tmp/out.mp3"), 48_000, 2)
            filters = command[command.index("-filter_complex") + 1]

        self.assertEqual(command.count("-i"), 1)
        self.assertIn("[0:a]atrim=start=0.000:duration=1.000", filters)
        self.assertIn("[0:a]atrim=start=0.000:duration=1.000", filters)

    def test_ffmpeg_command_expands_stem_group_inputs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "stem_groups": [
                            {
                                "id": "stem-hook",
                                "deck": "deck-1",
                                "source_path": "/music/source.flac",
                                "start": 2_000,
                                "trim_start": 1_000,
                                "duration": 4_000,
                                "gain_db": -3,
                                "stems": {
                                    "vocals": {"path": "/stems/vocals.wav", "gain_db": -6, "highpass_hz": 180},
                                    "bass": {"path": "/stems/bass.wav", "mute": True},
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            session = load_session(session_path)

            command = ffmpeg_command(session, {}, Path("/tmp/out.wav"), 48_000, 2)
            filters = command[command.index("-filter_complex") + 1]

        self.assertIn("/stems/vocals.wav", command)
        self.assertNotIn("/stems/bass.wav", command)
        self.assertIn("atrim=start=1.000:duration=4.000", filters)
        self.assertIn("highpass=f=180.000", filters)
        self.assertIn("adelay=2000:all=1", filters)

    def test_ffmpeg_command_does_not_deduplicate_repeated_stem_group_inputs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "stem_groups": [
                            {
                                "id": "loop-a",
                                "deck": "deck-1",
                                "source_path": "/music/source.flac",
                                "start": 0,
                                "trim_start": 1_000,
                                "duration": 4_000,
                                "stems": {"drums": {"path": "/stems/drums.wav"}},
                            },
                            {
                                "id": "loop-b",
                                "deck": "deck-1",
                                "source_path": "/music/source.flac",
                                "start": 4_000,
                                "trim_start": 1_000,
                                "duration": 4_000,
                                "stems": {"drums": {"path": "/stems/drums.wav"}},
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            session = load_session(session_path)

            command = ffmpeg_command(session, {}, Path("/tmp/out.wav"), 48_000, 2)
            filters = command[command.index("-filter_complex") + 1]

        self.assertEqual(command.count("-i"), 2)
        self.assertEqual(command.count("/stems/drums.wav"), 2)
        self.assertIn("[0:a]atrim=start=1.000:duration=4.000", filters)
        self.assertIn("[1:a]atrim=start=1.000:duration=4.000", filters)

    def test_stem_group_does_not_fall_back_to_source_when_stem_paths_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "stem_groups": [
                            {
                                "id": "missing-stem",
                                "deck": "deck-1",
                                "source_path": "/music/source.flac",
                                "start": 0,
                                "duration": 1_000,
                                "stem_set_id": "missing",
                                "stems": {"vocals": {"enabled": True}},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            session = load_session(session_path)

            command = ffmpeg_command(session, {}, Path("/tmp/out.wav"), 48_000, 2)

        self.assertNotIn("/music/source.flac", command)

    def test_mixdown_filter_includes_lean_in_duck_and_lowpass(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1", "deck-2"],
                        "clips": [
                            {
                                "id": "bed",
                                "deck": "deck-1",
                                "path": "/music/bed.flac",
                                "start": "00:00.000",
                                "duration": "00:30.000",
                                "gain_db": -3,
                            }
                        ],
                        "mic_lean_ins": [
                            {
                                "id": "lean",
                                "start": "00:10.000",
                                "text": "quick note",
                                "volume": 1.8,
                                "ducking": {
                                    "target": "master",
                                    "param": "duck_volume",
                                    "points": [{"at": "00:09.750", "value": 0.45}, {"at": "00:13.000", "value": 1.0}],
                                },
                                "lowpass": {
                                    "target": "master",
                                    "param": "lowpass_hz",
                                    "points": [{"at": "00:09.750", "value": 1400}, {"at": "00:13.000", "value": 22050}],
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            session = load_session(session_path)
            filters = build_filter_complex(session, {"lean": Path("/tmp/lean.wav")}, 48_000, 2)

        self.assertIn("adelay=10000:all=1", filters)
        self.assertIn("volume=1.800000,adelay=10000:all=1", filters)
        self.assertIn("volume=enable='between(t,9.750,13.000)':volume=0.450000", filters)
        self.assertIn("lowpass=enable='between(t,9.750,13.000)':f=1400.000", filters)
        self.assertIn("amix=inputs=2", filters)

    def test_mixdown_filter_omits_lean_in_duck_when_audio_is_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "clips": [
                            {
                                "id": "bed",
                                "deck": "deck-1",
                                "path": "/music/bed.flac",
                                "start": "00:00.000",
                                "duration": "00:30.000",
                            }
                        ],
                        "mic_lean_ins": [
                            {
                                "id": "lean",
                                "start": "00:10.000",
                                "text": "quick note",
                                "ducking": {
                                    "target": "master",
                                    "param": "duck_volume",
                                    "points": [{"at": "00:09.750", "value": 0.45}, {"at": "00:13.000", "value": 1.0}],
                                },
                                "lowpass": {
                                    "target": "master",
                                    "param": "lowpass_hz",
                                    "points": [{"at": "00:09.750", "value": 1400}, {"at": "00:13.000", "value": 22050}],
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            filters = build_filter_complex(load_session(session_path), {}, 48_000, 2)

        self.assertNotIn("volume=enable='between(t,9.750,13.000)':volume=0.450000", filters)
        self.assertNotIn("lowpass=enable='between(t,9.750,13.000)':f=1400.000", filters)
        self.assertIn("amix=inputs=1", filters)

    def test_prepare_lean_in_audio_fails_when_tts_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "clips": [],
                        "mic_lean_ins": [{"id": "lean", "start": "00:01.000", "text": "quick note"}],
                    }
                ),
                encoding="utf-8",
            )
            session = load_session(session_path)
            with self.assertRaisesRegex(ValueError, "failed lean-in audio for lean"):
                prepare_lean_in_audio(session, Path(temp_dir), "http://127.0.0.1:1", "af_heart", 1, 48_000, 2, False)

    def test_ffmpeg_command_maps_session_inputs_and_output(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "clips": [{"id": "bed", "deck": "deck-1", "path": "/music/bed.flac", "start": 0, "duration": 1000}],
                        "mic_lean_ins": [{"id": "lean", "start": 500, "text": "hi"}],
                    }
                ),
                encoding="utf-8",
            )
            session = load_session(session_path)
            command = ffmpeg_command(session, {"lean": Path("/tmp/lean.wav")}, Path("/tmp/out.wav"), 48_000, 2)

        self.assertEqual(command[0], "ffmpeg")
        self.assertIn("/music/bed.flac", command)
        self.assertIn("/tmp/lean.wav", command)
        self.assertEqual(command[-1], "/tmp/out.wav")
        self.assertIn("pcm_s16le", command)

    def test_ffmpeg_command_can_export_review_mp3(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "clips": [{"id": "bed", "deck": "deck-1", "path": "/music/bed.flac", "start": 0, "duration": 1000}],
                    }
                ),
                encoding="utf-8",
            )
            session = load_session(session_path)
            command = ffmpeg_command(session, {}, Path("/tmp/review.mp3"), 48_000, 2)

        self.assertIn("libmp3lame", command)
        self.assertIn("-b:a", command)
        self.assertIn("192k", command)
        self.assertEqual(command[-1], "/tmp/review.mp3")

    def test_mixdown_filter_renders_tempo_and_pitch_shift_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "clips": [
                            {
                                "id": "shifted",
                                "deck": "deck-1",
                                "path": "/music/shifted.flac",
                                "start": 0,
                                "duration": 10_000,
                                "tempo_shift_pct": 3.0,
                                "pitch_shift_semitones": 1,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            filters = build_filter_complex(load_session(session_path), {}, 48_000, 2)

        self.assertIn("asetrate=50854", filters)
        self.assertIn("aresample=48000", filters)
        self.assertIn("atempo=0.943874", filters)
        self.assertIn("atempo=1.030000", filters)
        self.assertIn("atrim=start=0.000:duration=10.300", filters)

    def test_mixdown_filter_renders_static_bed_carve_automation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1", "deck-2"],
                        "clips": [
                            {
                                "id": "bed",
                                "deck": "deck-1",
                                "path": "/music/bed.flac",
                                "start": "00:10.000",
                                "duration": "00:30.000",
                            },
                            {
                                "id": "lead",
                                "deck": "deck-2",
                                "path": "/music/lead.flac",
                                "start": "00:18.000",
                                "duration": "00:12.000",
                            },
                        ],
                        "automations": [
                            {
                                "target": "bed",
                                "param": "lowpass_hz",
                                "points": [{"at": "00:18.000", "value": 1600}, {"at": "00:30.000", "value": 1600}],
                            },
                            {
                                "target": "bed",
                                "param": "highpass_hz",
                                "points": [{"at": "00:18.000", "value": 120}, {"at": "00:30.000", "value": 120}],
                            },
                            {
                                "target": "bed",
                                "param": "gain_db",
                                "points": [{"at": "00:18.000", "value": -9}, {"at": "00:30.000", "value": -9}],
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            filters = build_filter_complex(load_session(session_path), {}, 48_000, 2)

        self.assertIn("lowpass=enable='between(t,8.000,20.000)':f=1600.000", filters)
        self.assertIn("highpass=enable='between(t,8.000,20.000)':f=120.000", filters)
        self.assertIn("volume=enable='between(t,8.000,20.000)':volume=0.354813", filters)

    def test_mixdown_applies_deck_automation_to_clip_window(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1", "deck-2"],
                        "clips": [
                            {
                                "id": "bed",
                                "deck": "deck-2",
                                "path": "/music/bed.flac",
                                "start": "00:10.000",
                                "duration": "00:30.000",
                            }
                        ],
                        "deck_automations": [
                            {
                                "target": "deck-2",
                                "param": "gain_db",
                                "points": [{"at": "00:08.000", "value": -9}, {"at": "00:38.000", "value": -6}],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            filters = build_filter_complex(load_session(session_path), {}, 48_000, 2)

        # The -9 -> -6 dB deck ride renders as an actual ramp (subdivided
        # steps rising across the clip window), not one value locked for the
        # whole window - the old assertion here pinned exactly that bug.
        windows = re.findall(r"volume=enable='between\(t,([0-9.]+),([0-9.]+)\)':volume=([0-9.]+)", filters)
        self.assertGreater(len(windows), 8)
        self.assertEqual(float(windows[0][0]), 0.0)
        self.assertEqual(float(windows[-1][1]), 28.0)
        values = [float(value) for _start, _end, value in windows]
        self.assertTrue(all(later > earlier for earlier, later in zip(values, values[1:])))
        # Clip starts 2s into the ride (-9 dB at 8s, -6 dB at 38s), and the
        # ride ends inside the clip window: first step ~-8.8 dB, last ~-6 dB.
        self.assertAlmostEqual(values[0], 10 ** (-8.8 / 20), delta=0.01)
        self.assertAlmostEqual(values[-1], 10 ** (-6.0 / 20), delta=0.01)

    def test_mixdown_filter_renders_echo_effect_with_tail(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "clips": [
                            {
                                "id": "lead",
                                "deck": "deck-1",
                                "path": "/music/lead.flac",
                                "start": 5_000,
                                "trim_start": 12_000,
                                "duration": 20_000,
                            }
                        ],
                        "effects": [
                            {
                                "id": "lead-echo",
                                "type": "echo",
                                "target": "lead",
                                "start": 9_000,
                                "duration": 2_000,
                                "tail_ms": 3_000,
                                "wet": 0.4,
                                "gain_db": -9,
                                "delay_ms": 375,
                                "feedback": 0.45,
                                "lowpass_hz": 4200,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            session = load_session(session_path)
            filters = build_filter_complex(session, {}, 48_000, 2)

        self.assertIn("atrim=start=16.000:duration=2.000", filters)
        self.assertIn("asplit=10", filters)
        self.assertIn("adelay=9375:all=1", filters)
        self.assertIn("adelay=12750:all=1", filters)
        self.assertIn("volume=0.141925", filters)
        self.assertNotIn("aecho=", filters)
        self.assertNotIn("afade=t=out:st=2.000:d=3.000", filters)
        # Tap tails are bounded BEFORE adelay: atrim after adelay discards the
        # inserted delay silence and lands every tap at t=0.
        self.assertIn("atrim=duration=4.625,adelay=9375:all=1", filters)
        self.assertIn("atrim=duration=1.250,adelay=12750:all=1", filters)
        for segment in filters.split(";"):
            if "adelay=" in segment and "echoeffect" in segment:
                delay_pos = segment.index("adelay=")
                self.assertNotIn("atrim=", segment[delay_pos:])
        self.assertIn("lowpass=f=4200.000", filters)
        self.assertEqual(session_duration_ms(session), 25_000)

    def test_mixdown_filter_renders_reverb_effect_with_tail(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "clips": [
                            {
                                "id": "lead",
                                "deck": "deck-1",
                                "path": "/music/lead.flac",
                                "start": 5_000,
                                "trim_start": 12_000,
                                "duration": 8_000,
                            }
                        ],
                        "effects": [
                            {
                                "id": "lead-reverb",
                                "type": "reverb",
                                "target": "lead",
                                "start": 9_000,
                                "duration": 2_000,
                                "tail_ms": 4_000,
                                "wet": 0.38,
                                "gain_db": -10,
                                "delay_ms": 80,
                                "feedback": 0.46,
                                "room_size": 0.72,
                                "damping": 0.55,
                                "lowpass_hz": 5200,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            session = load_session(session_path)
            filters = build_filter_complex(session, {}, 48_000, 2, reverb_ir_indices={"lead-reverb": 7})

        self.assertIn("atrim=start=16.000:duration=2.000", filters)
        self.assertIn("apad=pad_dur=4.000", filters)
        # The interface fact: the reverb convolves the source window with the
        # prepared impulse-response input. afir's quirky gain options are an
        # implementation detail owned by convolution_reverb_filter(), and the
        # rendered-tail test guards them behaviorally.
        self.assertIn("[7:a]afir", filters)
        self.assertIn("volume=0.380000", filters)
        self.assertNotIn("ladspa", filters)
        self.assertNotIn("afade=t=out:st=2.000:d=4.000", filters)
        self.assertIn("atrim=duration=6.000", filters)
        self.assertIn("lowpass=f=5200.000", filters)
        self.assertIn("adelay=9000:all=1", filters)
        self.assertEqual(session_duration_ms(session), 15_000)

    def test_mixdown_filter_renders_vinyl_brake_effect(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "clips": [
                            {
                                "id": "lead",
                                "deck": "deck-1",
                                "path": "/music/lead.flac",
                                "start": 5_000,
                                "trim_start": 12_000,
                                "duration": 8_000,
                            }
                        ],
                        "effects": [
                            {
                                "id": "lead-brake",
                                "type": "vinyl_brake",
                                "target": "lead",
                                "start": 9_000,
                                "duration": 1_000,
                                "wet": 1.0,
                                "gain_db": 0,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            session = load_session(session_path)
            filters = build_filter_complex(session, {}, 48_000, 2)

        self.assertIn("asetrate=47311", filters)
        self.assertIn("lowpass=f=920.659", filters)
        self.assertIn("afade=t=in:st=0:d=0.0030", filters)
        self.assertIn("afade=t=out:st=0.0122:d=0.0030", filters)
        self.assertIn("concat=n=66:v=0:a=1", filters)
        self.assertIn("adelay=9000:all=1", filters)
        self.assertIn("volume=enable='between(t,4.000,5.000)':volume=0.000000", filters)

    def test_mixdown_filter_applies_per_track_eq_automation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "clips": [
                            {
                                "id": "bed",
                                "deck": "deck-1",
                                "path": "/music/bed.flac",
                                "start": 5_000,
                                "duration": 20_000,
                            }
                        ],
                        "automations": [
                            {
                                "target": "bed",
                                "param": "eq_low_db",
                                "points": [{"at_ms": 8_000, "value": -5.5}, {"at_ms": 18_000, "value": -5.5}],
                            },
                            {
                                "target": "bed",
                                "param": "eq_mid_db",
                                "points": [{"at_ms": 8_000, "value": 2.0}, {"at_ms": 18_000, "value": 2.0}],
                            },
                            {
                                "target": "bed",
                                "param": "eq_high_db",
                                "points": [{"at_ms": 8_000, "value": -3.0}, {"at_ms": 18_000, "value": -3.0}],
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            session = load_session(session_path)
            filters = build_filter_complex(session, {}, 48_000, 2)

        self.assertIn("bass=enable='between(t,3.000,13.000)':g=-5.500:f=120:w=0.7", filters)
        self.assertIn("equalizer=enable='between(t,3.000,13.000)':f=1000:t=q:w=1.0:g=2.000", filters)
        self.assertIn("treble=enable='between(t,3.000,13.000)':g=-3.000:f=6500:w=0.7", filters)

    def test_crossfader_gain_maps_hard_sides_and_center(self):
        self.assertEqual(crossfader_gain(-1.0, "A"), 1.0)
        self.assertEqual(crossfader_gain(-1.0, "B"), 0.0)
        self.assertEqual(crossfader_gain(0.0, "A"), 1.0)
        self.assertEqual(crossfader_gain(0.0, "B"), 1.0)
        self.assertEqual(crossfader_gain(1.0, "A"), 0.0)
        self.assertEqual(crossfader_gain(1.0, "B"), 1.0)

    def test_mixdown_filter_applies_crossfader_routing_to_deck_gains(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1", "deck-2", "deck-3", "deck-4"],
                        "fader_routing": {
                            "deck_assignments": {
                                "deck-1": "A",
                                "deck-2": "B",
                                "deck-3": "A",
                                "deck-4": "B",
                            }
                        },
                        "clips": [
                            {"id": "left", "deck": "deck-1", "path": "/music/left.flac", "start": 0, "duration": 20_000},
                            {"id": "right", "deck": "deck-2", "path": "/music/right.flac", "start": 0, "duration": 20_000},
                        ],
                        "automations": [
                            {
                                "target": "crossfader",
                                "param": "position",
                                "points": [
                                    {"at_ms": 0, "value": -1},
                                    {"at_ms": 10_000, "value": -1},
                                    {"at_ms": 10_000, "value": 1},
                                    {"at_ms": 20_000, "value": 1},
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            filters = build_filter_complex(load_session(session_path), {}, 48_000, 2)

        self.assertIn("volume=enable='between(t,0.000,10.000)':volume=1.000000", filters)
        self.assertIn("volume=enable='between(t,10.000,20.000)':volume=0.000000", filters)
        self.assertIn("volume=enable='between(t,0.000,10.000)':volume=0.000000", filters)
        self.assertIn("volume=enable='between(t,10.000,20.000)':volume=1.000000", filters)

    def test_crossfader_routine_automation_overrides_broad_default_position(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1", "deck-2"],
                        "fader_routing": {"deck_assignments": {"deck-1": "A", "deck-2": "B"}},
                        "clips": [
                            {"id": "scratch", "deck": "deck-1", "path": "/music/a.flac", "start": 4_000, "duration": 500},
                            {"id": "source", "deck": "deck-2", "path": "/music/b.flac", "start": 0, "duration": 8_000},
                        ],
                        "automations": [
                            {
                                "target": "crossfader",
                                "param": "position",
                                "points": [{"at_ms": 0, "value": 1}, {"at_ms": 8_000, "value": 1}],
                            },
                            {
                                "target": "crossfader",
                                "param": "position",
                                "planner_role": "scratch-transform-cuts",
                                "points": [
                                    {"at_ms": 4_000, "value": 1},
                                    {"at_ms": 4_001, "value": -1},
                                    {"at_ms": 4_499, "value": -1},
                                    {"at_ms": 4_500, "value": 1},
                                ],
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            filters = build_filter_complex(load_session(session_path), {}, 48_000, 2)

        self.assertIn("volume=enable='between(t,0.001,0.499)':volume=1.000000", filters)
        self.assertNotIn("volume=enable='between(t,0.000,0.500)':volume=0.000000", filters)

    def test_mixdown_filter_renders_gradual_crossfader_ramps(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1", "deck-2"],
                        "fader_routing": {"deck_assignments": {"deck-1": "A", "deck-2": "B"}},
                        "clips": [
                            {"id": "left", "deck": "deck-1", "path": "/music/left.flac", "start": 0, "duration": 20_000},
                            {"id": "right", "deck": "deck-2", "path": "/music/right.flac", "start": 0, "duration": 20_000},
                        ],
                        "automations": [
                            {
                                "target": "crossfader",
                                "param": "position",
                                "points": [{"at_ms": 0, "value": -1}, {"at_ms": 20_000, "value": 1}],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            filters = build_filter_complex(load_session(session_path), {}, 48_000, 2)

        self.assertIn("volume=enable='between(t,10.000,20.000)':volume='1.000000+(-0.100000000)*(t-10.000)':eval=frame", filters)
        self.assertIn("volume=enable='between(t,0.000,10.000)':volume='0.000000+(0.100000000)*(t-0.000)':eval=frame", filters)

    def test_session_duration_includes_lean_in_effect_window(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "clips": [],
                        "mic_lean_ins": [
                            {
                                "id": "lean",
                                "start": "00:10.000",
                                "text": "hi",
                                "lowpass": {
                                    "target": "master",
                                    "param": "lowpass_hz",
                                    "points": [{"at": "00:09.750", "value": 1400}, {"at": "00:15.000", "value": 22050}],
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            session = load_session(session_path)

        self.assertEqual(session_duration_ms(session), 15_000)

    def test_overlapping_clips_only_fade_when_explicitly_planned(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1", "deck-2"],
                        "clips": [
                            {"id": "a", "deck": "deck-1", "path": "/music/a.flac", "start": 0, "duration": 30_000},
                            {
                                "id": "b",
                                "deck": "deck-2",
                                "path": "/music/b.flac",
                                "start": 24_000,
                                "duration": 30_000,
                                "fade_in_ms": 4_000,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            filters = build_filter_complex(load_session(session_path), {}, 48_000, 2)

        self.assertNotIn("afade=t=out:st=24.000:d=6.000", filters)
        self.assertIn("afade=t=in:st=0:d=4.000", filters)

    def test_shift_session_window_trims_current_clip_and_shifts_future_events(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1", "deck-2"],
                        "clips": [
                            {
                                "id": "current",
                                "deck": "deck-1",
                                "path": "/music/current.flac",
                                "start": 5_000,
                                "trim_start": 1_000,
                                "duration": 20_000,
                            },
                            {
                                "id": "future",
                                "deck": "deck-2",
                                "path": "/music/future.flac",
                                "start": 30_000,
                                "duration": 10_000,
                            },
                        ],
                        "mic_lean_ins": [{"id": "lean", "start": 32_000, "text": "incoming"}],
                    }
                ),
                encoding="utf-8",
            )
            shifted = shift_session_window(load_session(session_path), 12_000)
            filters = build_filter_complex(shifted, {"lean": Path("/tmp/lean.wav")}, 48_000, 2)

        self.assertEqual(shifted.clips[0].id, "current")
        self.assertEqual(shifted.clips[0].start_ms, 0)
        self.assertEqual(shifted.clips[0].trim_start_ms, 8_000)
        self.assertEqual(shifted.clips[0].duration_ms, 13_000)
        self.assertEqual(shifted.clips[1].start_ms, 18_000)
        self.assertEqual(shifted.mic_lean_ins[0].start_ms, 20_000)
        self.assertIn("atrim=start=8.000:duration=13.000", filters)
        self.assertIn("adelay=0:all=1", filters)
        self.assertIn("adelay=18000:all=1", filters)

    def test_shift_session_window_converts_timeline_overlap_to_source_trim_with_tempo_shift(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "clips": [
                            {
                                "id": "fast",
                                "deck": "deck-1",
                                "path": "/music/fast.flac",
                                "start": 0,
                                "trim_start": 1_000,
                                "duration": 20_000,
                                "tempo_shift_pct": 5,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            shifted = shift_session_window(load_session(session_path), 4_000)
            filters = build_filter_complex(shifted, {}, 48_000, 2)

        self.assertEqual(shifted.clips[0].trim_start_ms, 5_200)
        self.assertEqual(shifted.clips[0].duration_ms, 16_000)
        self.assertIn("atrim=start=5.200:duration=16.800", filters)
        self.assertIn("atempo=1.050000", filters)

    def test_shift_session_window_limits_duration(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "clips": [
                            {
                                "id": "bed",
                                "deck": "deck-1",
                                "path": "/music/bed.flac",
                                "start": 5_000,
                                "trim_start": 1_000,
                                "duration": 20_000,
                            },
                            {
                                "id": "too-late",
                                "deck": "deck-1",
                                "path": "/music/late.flac",
                                "start": 35_000,
                                "duration": 10_000,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            shifted = shift_session_window(load_session(session_path), 10_000, 8_000)
            filters = build_filter_complex(shifted, {}, 48_000, 2, 8_000)

        self.assertEqual([clip.id for clip in shifted.clips], ["bed"])
        self.assertEqual(shifted.clips[0].trim_start_ms, 6_000)
        self.assertEqual(shifted.clips[0].duration_ms, 8_000)
        self.assertIn("aresample=async=1:first_pts=0,atrim=duration=8.000,alimiter", filters)

    def test_routine_window_and_taste_report_accept_named_routine(self):
        payload = {
            "version": 1,
            "decks": ["deck-1", "deck-2"],
            "clips": [
                {"id": "source", "deck": "deck-1", "path": "/music/a.flac", "start": 0, "duration": 30_000},
                {
                    "id": "routine-double",
                    "deck": "deck-2",
                    "path": "/music/a.flac",
                    "start": 10_000,
                    "duration": 8_000,
                    "routine_id": "routine-a",
                    "routine_recipe": "stabs",
                    "source_clip_id": "source",
                },
            ],
            "automations": [
                {
                    "target": "routine-double",
                    "param": "gain_db",
                    "routine_id": "routine-a",
                    "points": [{"at_ms": 10_000, "value": -3}, {"at_ms": 18_000, "value": -96}],
                }
            ],
        }

        start_ms, end_ms = routine_window(payload, "routine-a", 5_000)
        report = routine_taste_report(payload, "routine-a", start_ms, end_ms)

        self.assertEqual((start_ms, end_ms), (5_000, 23_000))
        self.assertTrue(report["accepted"])
        self.assertEqual(report["routine_recipes"], ["stabs"])
        self.assertEqual(report["active_clip_ids"], ["source", "routine-double"])

    def test_routine_taste_report_rejects_unrelated_routine_overlap(self):
        payload = {
            "version": 1,
            "decks": ["deck-1", "deck-2", "deck-3"],
            "clips": [
                {"id": "source", "deck": "deck-1", "path": "/music/a.flac", "start": 0, "duration": 30_000},
                {
                    "id": "routine-double",
                    "deck": "deck-2",
                    "path": "/music/a.flac",
                    "start": 10_000,
                    "duration": 8_000,
                    "routine_id": "routine-a",
                    "routine_recipe": "stabs",
                    "source_clip_id": "source",
                },
                {
                    "id": "other-routine",
                    "deck": "deck-3",
                    "path": "/music/b.flac",
                    "start": 12_000,
                    "duration": 8_000,
                    "routine_id": "routine-b",
                    "routine_recipe": "stabs",
                },
            ],
        }

        report = routine_taste_report(payload, "routine-a", 5_000, 23_000)

        self.assertFalse(report["accepted"])
        self.assertIn("unrelated routine clips overlap the audition window", report["errors"])

    def _ready_stem_db(self, temp: Path, source: Path, *, stem_frequencies: dict[str, float]) -> Path:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        from slime_music_library import connect

        artifact_root = temp / "stems" / "set-a"
        artifact_root.mkdir(parents=True)
        (artifact_root / "manifest.json").write_text("{}", encoding="utf-8")
        for stem, frequency in stem_frequencies.items():
            self.write_beep_track(artifact_root / f"{stem}.wav", frequency_hz=frequency, duration_s=2.0)
        db_path = temp / "library.sqlite3"
        conn = connect(db_path)
        conn.execute(
            """
            INSERT INTO track_stem_sets(
                id, duplicate_key, source_path, source_size, source_mtime, model, profile, artifact_root,
                sample_rate, channels, duration_ms, status, error, created_at, updated_at
            )
            VALUES ('set-a', NULL, ?, 1, 1, 'htdemucs', '4stem', ?, 48000, 1, 2000, 'ready', NULL, 'now', 'now')
            """,
            (str(source), str(artifact_root)),
        )
        for stem in ("vocals", "drums", "bass", "other"):
            conn.execute(
                "INSERT INTO track_stems(stem_set_id, stem_name, path) VALUES ('set-a', ?, ?)",
                (stem, str(artifact_root / f"{stem}.wav")),
            )
        conn.commit()
        conn.close()
        return db_path

    def test_reverb_controls_behave_like_hardware(self):
        """The knobs mean what their labels say: bigger room decays longer,
        more damping is darker, pre-delay follows delay_ms within sane bounds."""
        from slime_audio_session import EffectEvent
        from slime_audio_session_mixdown import reverb_ir_parameters

        def params(**overrides):
            effect = EffectEvent(
                id="fx",
                type="reverb",
                target="lead",
                start_ms=0,
                duration_ms=1_000,
                tail_ms=2_000,
                **overrides,
            )
            return reverb_ir_parameters(effect)

        self.assertGreater(params(room_size=1.0)["rt60_s"], params(room_size=0.1)["rt60_s"])
        self.assertLess(params(damping=0.9)["damping_hz"], params(damping=0.1)["damping_hz"])
        self.assertGreater(params(feedback=0.9)["rt60_s"], params(feedback=0.1)["rt60_s"])
        self.assertEqual(params(delay_ms=60)["pre_delay_s"], 0.06)
        self.assertEqual(params(delay_ms=5)["pre_delay_s"], 0.02)
        self.assertEqual(params(delay_ms=500)["pre_delay_s"], 0.1)

    def test_reverb_room_size_audibly_lengthens_the_ir_tail(self):
        from slime_audio_session import EffectEvent
        from slime_audio_session_mixdown import write_reverb_ir

        def late_energy(room_size: float) -> float:
            effect = EffectEvent(
                id="fx", type="reverb", target="lead", start_ms=0, duration_ms=1_000, tail_ms=2_000, room_size=room_size
            )
            with tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / "ir.wav"
                write_reverb_ir(effect, path, 48_000, 1)
                rate, samples = self.read_wav_samples(path)
            late = samples[int(1.0 * rate) : int(1.5 * rate)]
            return math.sqrt(sum(value * value for value in late) / max(1, len(late)))

        self.assertGreater(late_energy(1.0), late_energy(0.1) * 2)

    def test_rendered_deck_gain_ramp_actually_glides(self):
        """A fader ride must render as motion. The old windowing held the ramp's
        first value for the whole window and then jumped - a locked knob."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            source = temp / "steady.wav"
            rate = self._write_event_track(source, events=[(0.0, 8.0, 440.0)])
            session_path = temp / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "clips": [{"id": "lead", "deck": "deck-1", "path": str(source), "start_ms": 0, "duration_ms": 8_000}],
                        "deck_automations": [
                            {
                                "target": "deck-1",
                                "param": "gain_db",
                                "points": [{"at_ms": 2_000, "value": -24}, {"at_ms": 6_000, "value": 0}],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            from slime_audio_session import load_session

            session = load_session(session_path)
            output = temp / "out.wav"
            subprocess.run(ffmpeg_command(session, {}, output, rate, 1), check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            _rate, samples = self.read_wav_samples(output)

            def level_db(t: float) -> float:
                value = self._segment_rms(samples, rate, t - 0.1, t + 0.1)
                return 20 * math.log10(max(value, 1e-9))

            baseline = level_db(1.0)
            quarter = level_db(3.0)
            middle = level_db(4.0)
            three_quarter = level_db(5.0)
            landed = level_db(6.5)

        self.assertAlmostEqual(quarter - baseline, -18.0, delta=2.0)
        self.assertAlmostEqual(middle - baseline, -12.0, delta=2.0)
        self.assertAlmostEqual(three_quarter - baseline, -6.0, delta=2.0)
        self.assertAlmostEqual(landed, baseline, delta=1.0)

    def test_reverb_ir_is_deterministic_and_energy_normalized(self):
        from slime_audio_session import EffectEvent
        from slime_audio_session_mixdown import write_reverb_ir

        effect = EffectEvent(
            id="fx", type="reverb", target="lead", start_ms=0, duration_ms=1_000, tail_ms=2_000, room_size=0.6, damping=0.4
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            write_reverb_ir(effect, temp / "a.wav", 48_000, 2)
            write_reverb_ir(effect, temp / "b.wav", 48_000, 2)
            self.assertEqual((temp / "a.wav").read_bytes(), (temp / "b.wav").read_bytes())
            sample_rate, samples = self.read_wav_samples(temp / "a.wav")
            energy = math.sqrt(sum(value * value for value in samples))
            self.assertGreater(energy, 0.3)
            self.assertLess(energy, 3.0)

    def _write_event_track(self, path: Path, *, events: list[tuple[float, float, float]], duration_s: float = 8.0) -> int:
        """Stereo track that is silent except for (start_s, end_s, frequency_hz) events."""
        sample_rate = 48_000
        frames = bytearray()
        for index in range(int(sample_rate * duration_s)):
            t = index / sample_rate
            value = 0.0
            for start_s, end_s, frequency_hz in events:
                if start_s <= t < end_s:
                    value = 0.6 * math.sin(2 * math.pi * frequency_hz * (t - start_s))
            packed = struct.pack("<h", int(max(-1.0, min(1.0, value)) * 32767))
            frames += packed + packed
        with wave.open(str(path), "wb") as audio:
            audio.setnchannels(2)
            audio.setsampwidth(2)
            audio.setframerate(sample_rate)
            audio.writeframes(bytes(frames))
        return sample_rate

    def _render_effect_session(self, temp: Path, source: Path, effect: dict, sample_rate: int) -> list[float]:
        from slime_audio_session import load_session
        from slime_audio_session_mixdown import prepare_reverb_irs

        session_path = temp / "session.json"
        session_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "decks": ["deck-1"],
                    "clips": [{"id": "lead", "deck": "deck-1", "path": str(source), "start_ms": 0, "duration_ms": 8_000}],
                    "effects": [effect],
                }
            ),
            encoding="utf-8",
        )
        session = load_session(session_path)
        output = temp / "out.wav"
        command = ffmpeg_command(
            session, {}, output, sample_rate, 2, reverb_irs=prepare_reverb_irs(session, temp, sample_rate, 2)
        )
        subprocess.run(spill_filter_complex_to_script(command, temp / "fc.txt"), check=True)
        _rate, samples = self.read_wav_samples(output)
        return samples

    @staticmethod
    def _segment_rms(samples: list[float], rate: int, t0: float, t1: float) -> float:
        segment = samples[int(t0 * rate) : int(t1 * rate)]
        return math.sqrt(sum(value * value for value in segment) / max(1, len(segment)))

    def test_rendered_echo_taps_land_at_delay_times(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            source = temp / "beep.wav"
            rate = self._write_event_track(source, events=[(2.0, 2.12, 880.0)])
            samples = self._render_effect_session(
                temp,
                source,
                {
                    "id": "fx",
                    "type": "echo",
                    "target": "lead",
                    "start_ms": 2_000,
                    "duration_ms": 300,
                    "tail_ms": 2_000,
                    "delay_ms": 400,
                    "feedback": 0.5,
                    "wet": 0.8,
                    "gain_db": 0,
                },
                rate,
            )
            dry = self._segment_rms(samples, rate, 2.0, 2.12)
            tap1 = self._segment_rms(samples, rate, 2.4, 2.52)
            tap2 = self._segment_rms(samples, rate, 2.8, 2.92)
            silence = self._segment_rms(samples, rate, 0.5, 1.5)

        self.assertGreater(dry, 0.25)
        self.assertGreater(tap1, 0.1, "first echo tap must be audible at start+delay")
        self.assertGreater(tap2, 0.04, "second echo tap must be audible at start+2*delay")
        self.assertAlmostEqual(tap2 / tap1, 0.5, delta=0.15)
        self.assertLess(silence, 0.01)

    def test_rendered_reverb_adds_decaying_tail(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            source = temp / "tone.wav"
            # Tone from 1.0-2.0s only: everything after 2.0s is pure effect tail.
            rate = self._write_event_track(source, events=[(1.0, 2.0, 440.0)])
            samples = self._render_effect_session(
                temp,
                source,
                {
                    "id": "fx",
                    "type": "reverb",
                    "target": "lead",
                    "start_ms": 1_000,
                    "duration_ms": 1_000,
                    "tail_ms": 3_000,
                    "wet": 0.8,
                    "gain_db": 0,
                    "room_size": 0.6,
                    "damping": 0.4,
                },
                rate,
            )
            early_tail = self._segment_rms(samples, rate, 2.1, 2.9)
            late_tail = self._segment_rms(samples, rate, 3.4, 4.2)
            after_tail = self._segment_rms(samples, rate, 6.0, 7.0)

        self.assertGreater(early_tail, 0.02, "reverb tail must be audible after the dry tone stops")
        self.assertGreater(early_tail, late_tail, "reverb tail must decay")
        self.assertLess(after_tail, 0.005)

    def test_materialize_clip_stem_mixes_substitutes_ready_stem_premix(self):
        from slime_audio_session_mixdown import materialize_clip_stem_mixes

        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            source = temp / "song.flac"
            source.write_bytes(b"fake")
            db_path = self._ready_stem_db(
                temp,
                source,
                stem_frequencies={"vocals": 440.0, "drums": 220.0, "bass": 110.0, "other": 660.0},
            )
            session_path = temp / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "clips": [
                            {
                                "id": "bed",
                                "deck": "deck-1",
                                "path": str(source),
                                "start_ms": 0,
                                "duration_ms": 1_000,
                                "play_stems": ["drums", "bass"],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            session = load_session(session_path)
            work = temp / "work"
            work.mkdir()

            materialized = materialize_clip_stem_mixes(session, db_path, work, 48_000, 1)

            clip = materialized.clips[0]
            self.assertNotEqual(clip.path, str(source))
            self.assertIsNone(clip.play_stems)
            premix = Path(clip.path)
            self.assertTrue(premix.exists())
            sample_rate, samples = self.read_wav_samples(premix)
            self.assertEqual(sample_rate, 48_000)
            self.assertGreater(max(abs(sample) for sample in samples), 0.05)

    def test_materialize_clip_stem_mixes_fails_without_ready_stems(self):
        from slime_audio_session_mixdown import materialize_clip_stem_mixes

        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            session_path = temp / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "clips": [
                            {
                                "id": "bed",
                                "deck": "deck-1",
                                "path": str(temp / "song.flac"),
                                "start_ms": 0,
                                "duration_ms": 1_000,
                                "play_stems": ["drums"],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            session = load_session(session_path)

            with self.assertRaises(ValueError) as raised:
                materialize_clip_stem_mixes(session, temp / "missing.sqlite3", temp, 48_000, 1)

        self.assertIn("no ready stem artifacts", str(raised.exception))

    def test_materialize_clip_stem_mixes_ignores_plain_clips(self):
        from slime_audio_session_mixdown import materialize_clip_stem_mixes

        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            session_path = temp / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "clips": [
                            {"id": "song", "deck": "deck-1", "path": str(temp / "song.flac"), "start_ms": 0, "duration_ms": 1_000}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            session = load_session(session_path)

            materialized = materialize_clip_stem_mixes(session, temp / "missing.sqlite3", temp, 48_000, 1)

        self.assertIs(materialized, session)


class WindowFadeTests(unittest.TestCase):
    """Authored fades belong to a clip's real ends, not to window cuts.

    A render-window boundary landing inside a blended clip must not re-apply
    the clip's fade_in at the window start or its fade_out at the window end —
    that rendered as a dip to silence over seconds at seemingly random moments
    (heard live across 2026-07-03's sets).
    """

    @staticmethod
    def session_with_faded_clip():
        payload = {
            "version": 1,
            "decks": ["deck-1"],
            "clips": [
                {
                    "id": "lead",
                    "deck": "deck-1",
                    "path": "/music/a.flac",
                    "start_ms": 0,
                    "duration_ms": 300_000,
                    "fade_in_ms": 8_000,
                    "fade_out_ms": 8_000,
                }
            ],
        }
        from slime_audio_session import parse_session

        return parse_session(payload)

    def test_mid_clip_window_strips_both_fades(self):
        window = shift_session_window(self.session_with_faded_clip(), 100_000, 100_000)
        clip = window.clips[0]
        self.assertEqual(clip.fade_in_ms, 0)
        self.assertEqual(clip.fade_out_ms, 0)

    def test_window_containing_clip_ends_keeps_fades(self):
        window = shift_session_window(self.session_with_faded_clip(), 0, 300_000)
        clip = window.clips[0]
        self.assertEqual(clip.fade_in_ms, 8_000)
        self.assertEqual(clip.fade_out_ms, 8_000)

    def test_head_window_keeps_fade_in_strips_fade_out(self):
        window = shift_session_window(self.session_with_faded_clip(), 0, 100_000)
        clip = window.clips[0]
        self.assertEqual(clip.fade_in_ms, 8_000)
        self.assertEqual(clip.fade_out_ms, 0)

    def test_tail_window_strips_fade_in_keeps_fade_out(self):
        window = shift_session_window(self.session_with_faded_clip(), 200_000, 100_000)
        clip = window.clips[0]
        self.assertEqual(clip.fade_in_ms, 0)
        self.assertEqual(clip.fade_out_ms, 8_000)


class TimePitchFilterTests(unittest.TestCase):
    """Pitch/rate math must run at the render sample rate, not the source's.

    asetrate relabels whatever rate the stream actually has: a 44.1k source
    against 48k math renders sharp and short (a +1 st clip landed ~+2.5 st and
    ~8% short live on 2026-07-03, skipping the playhead forward every window).
    Normalizing with aresample first makes the ratios exact.
    """

    @staticmethod
    def clip(**overrides):
        from slime_audio_session import Clip

        values = {"id": "lead", "deck": "deck-2", "path": "/music/a.flac", "start_ms": 0}
        values.update(overrides)
        return Clip(**values)

    def test_pitch_shift_normalizes_to_render_rate_first(self):
        from slime_audio_session_mixdown import time_pitch_filters

        filters = time_pitch_filters(self.clip(pitch_shift_semitones=1), 48_000)
        self.assertEqual(filters[0], "aresample=48000")
        self.assertTrue(filters[1].startswith("asetrate="))

    def test_playback_rate_also_normalized_and_neutral_clip_untouched(self):
        from slime_audio_session_mixdown import time_pitch_filters

        filters = time_pitch_filters(self.clip(playback_rate=1.25), 48_000)
        self.assertEqual(filters[0], "aresample=48000")
        self.assertEqual(time_pitch_filters(self.clip(), 48_000), [])
        # Pure tempo shifts use atempo (time-domain) and need no normalization.
        tempo_only = time_pitch_filters(self.clip(tempo_shift_pct=5.0), 48_000)
        self.assertTrue(all(f.startswith("atempo=") for f in tempo_only))


if __name__ == "__main__":
    unittest.main()
