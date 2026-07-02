import json
import sys
import tempfile
import unittest
import wave
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from slime_audio_session import AUDACITY_REVERB_PRESETS, audit_hidden_volume_sag, audit_session_durations, load_session, parse_ms, playhead_ms_from_state, prepare_load_track_action_stems, session_summary
from slime_audio_session import main as session_main
from slime_audio_session_mixdown import shift_session_window
from slime_music_library import connect


def run_cli(argv: list[str]) -> int:
    original_argv = sys.argv[:]
    try:
        sys.argv = argv
        with redirect_stdout(StringIO()):
            return session_main()
    finally:
        sys.argv = original_argv


def write_silent_wav(path: Path, duration_ms: int, *, sample_rate: int = 8000) -> None:
    frame_count = max(1, int(sample_rate * duration_ms / 1000))
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(b"\x00\x00" * frame_count)


def write_analysis_cache(path: Path, track: str, *, bpm: float, beat_offset_ms: int = 0, confidence: float = 0.9) -> None:
    path.write_text(
        json.dumps(
            {
                "cache-key": {
                    "path": track,
                    "duration_s": 120.0,
                    "sample_rate": 48000,
                    "channels": 2,
                    "bpm": bpm,
                    "beat_offset_ms": beat_offset_ms,
                    "key": None,
                    "tonic": None,
                    "mode": None,
                    "camelot": None,
                    "energy": 0.5,
                    "loudness_db": -12.0,
                    "confidence": {"bpm": confidence, "key": 0.0},
                    "beatgrid": {
                        "bpm": bpm,
                        "beat_offset_ms": beat_offset_ms,
                        "phrase_beats": 32,
                        "phrase_ms": round((60_000 / bpm) * 32),
                    },
                    "structure": [],
                }
            }
        ),
        encoding="utf-8",
    )


def write_cue_db(db_path: Path, track: Path, *, kind: str, at_ms: int, confidence: float = 0.9) -> None:
    conn = connect(db_path)
    stat = track.stat()
    identity_path = str(track.resolve())
    conn.execute(
        """
        INSERT INTO track_dj_analysis(
            path, file_size, file_mtime_ns, duration_s, sample_rate, channels,
            bpm, beat_offset_ms, energy, loudness_db, bpm_confidence, key_confidence,
            phrase_beats, phrase_ms, updated_at
        )
        VALUES (?, ?, ?, 120.0, 44100, 2, 120.0, 0, 0.5, -12.0, 0.9, 0.0, 32, 16000, 1)
        """,
        (identity_path, stat.st_size, stat.st_mtime_ns),
    )
    conn.execute(
        """
        INSERT INTO track_dj_cues(path, kind, label, at_ms, end_ms, confidence, source, quantized, reason, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, 'test', 1, 'test cue', 1)
        """,
        (identity_path, kind, kind, at_ms, at_ms + 8000, confidence),
    )
    conn.commit()
    conn.close()


class SlimeAudioSessionTests(unittest.TestCase):
    def test_load_session_rejects_artifact_source_paths(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1", "deck-2"],
                        "clips": [
                            {
                                "id": "bad-clip",
                                "deck": "deck-1",
                                "path": "/music/Artist/Album/separated/htdemucs/Track/vocals.flac",
                                "start_ms": 0,
                                "duration_ms": 1000,
                            }
                        ],
                        "stem_groups": [
                            {
                                "id": "bad-group",
                                "deck": "deck-2",
                                "source_path": "/music/Artist/Album/isolated/Track_Vocal.wav",
                                "start_ms": 0,
                                "duration_ms": 1000,
                                "stems": {
                                    "vocals": {
                                        "path": "/music/Artist/Album/separated/htdemucs/Track/vocals.flac"
                                    }
                                },
                            }
                        ],
                    }
                )
            )

            with self.assertRaisesRegex(ValueError, "artifact/duplicate source path"):
                load_session(session_path)

    def test_load_session_accepts_stem_group_with_stem_automation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "stem_groups": [
                            {
                                "id": "hook",
                                "deck": "deck-1",
                                "source_path": "/music/source.flac",
                                "start": "00:08.000",
                                "trim_start": "00:32.000",
                                "duration": "00:16.000",
                                "stems": {
                                    "vocals": {
                                        "enabled": True,
                                        "path": "/stems/vocals.wav",
                                        "gain_db": -3,
                                        "automations": [
                                            {
                                                "param": "highpass_hz",
                                                "points": [
                                                    {"at": "00:08.000", "value": 180},
                                                    {"at": "00:24.000", "value": 220},
                                                ],
                                            }
                                        ],
                                    },
                                    "drums": {"enabled": False},
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            session = load_session(session_path)
            summary = session_summary(session)

        self.assertEqual(len(session.stem_groups), 1)
        self.assertEqual(session.stem_groups[0].stems["vocals"].gain_db, -3)
        self.assertEqual(summary["stem_group_count"], 1)

    def test_parse_ms_accepts_clock_strings(self):
        self.assertEqual(parse_ms("01:02.500", "time"), 62_500)
        self.assertEqual(parse_ms("1:02:03", "time"), 3_723_000)

    def test_audit_session_durations_reports_placeholder_mismatch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            track = temp_path / "short.wav"
            write_silent_wav(track, 1_000)
            session_path = temp_path / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "clips": [
                            {
                                "id": "placeholder",
                                "deck": "deck-1",
                                "path": str(track),
                                "start_ms": 0,
                                "duration_ms": 240_000,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            report = audit_session_durations(load_session(session_path), threshold_ms=500)

            self.assertEqual(report["checked"], 1)
            self.assertEqual(report["mismatch_count"], 1)
            self.assertEqual(report["mismatches"][0]["kind"], "scheduled_too_long")
            self.assertNotEqual(run_cli(["slime_audio_session.py", "audit-durations", str(session_path), "--threshold-ms", "500"]), 0)

    def test_audit_session_durations_passes_real_duration(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            track = temp_path / "one-second.wav"
            write_silent_wav(track, 1_000)
            session_path = temp_path / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "clips": [
                            {
                                "id": "real-duration",
                                "deck": "deck-1",
                                "path": str(track),
                                "start_ms": 0,
                                "duration_ms": 1_000,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(run_cli(["slime_audio_session.py", "audit-durations", str(session_path), "--threshold-ms", "500"]), 0)

    def test_audit_session_durations_can_ignore_past_clips(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            track = temp_path / "short.wav"
            write_silent_wav(track, 1_000)
            session_path = temp_path / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "clips": [
                            {
                                "id": "past-placeholder",
                                "deck": "deck-1",
                                "path": str(track),
                                "start_ms": 0,
                                "duration_ms": 240_000,
                            },
                            {
                                "id": "future-real",
                                "deck": "deck-1",
                                "path": str(track),
                                "start_ms": 300_000,
                                "duration_ms": 1_000,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                run_cli(
                    [
                        "slime_audio_session.py",
                        "audit-durations",
                        str(session_path),
                        "--threshold-ms",
                        "500",
                        "--from-ms",
                        "250000",
                    ]
                ),
                0,
            )

    def test_audit_volume_reports_hidden_sag(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1", "deck-2"],
                        "clips": [
                            {
                                "id": "saggy",
                                "deck": "deck-1",
                                "path": "/music/a.flac",
                                "start_ms": 10_000,
                                "duration_ms": 60_000,
                                "fade_out_ms": 8_000,
                            }
                        ],
                        "deck_automations": [
                            {
                                "target": "deck-1",
                                "param": "gain_db",
                                "points": [{"at_ms": 20_000, "value": 0}, {"at_ms": 24_000, "value": -18}],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            report = audit_hidden_volume_sag(load_session(session_path), from_ms=0)

            self.assertEqual(report["finding_count"], 2)
            self.assertNotEqual(run_cli(["slime_audio_session.py", "audit-volume", str(session_path), "--from-ms", "0"]), 0)

    def test_audit_volume_can_ignore_past_sag(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "clips": [
                            {
                                "id": "past-sag",
                                "deck": "deck-1",
                                "path": "/music/a.flac",
                                "start_ms": 0,
                                "duration_ms": 10_000,
                                "fade_out_ms": 8_000,
                            },
                            {
                                "id": "future-clean",
                                "deck": "deck-1",
                                "path": "/music/b.flac",
                                "start_ms": 30_000,
                                "duration_ms": 10_000,
                                "fade_out_ms": 0,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(run_cli(["slime_audio_session.py", "audit-volume", str(session_path), "--from-ms", "20000"]), 0)

    def test_actions_compile_load_track_stem_toggle_and_knob_lerp(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "actions": [
                            {
                                "type": "load_track",
                                "id": "lead-load",
                                "deck": "deck-1",
                                "source_path": "/music/lead.flac",
                                "at": "00:08.000",
                                "trim_start": "00:16.000",
                                "duration": "00:32.000",
                                "tempo_shift_pct": 2.5,
                                "pitch_shift_semitones": -1,
                                "play_stems": ["drums", "bass", "other"],
                                "stems": {
                                    "vocals": {"path": "/stems/lead/vocals.flac"},
                                    "drums": {"path": "/stems/lead/drums.flac"},
                                    "bass": {"path": "/stems/lead/bass.flac"},
                                    "other": {"path": "/stems/lead/other.flac"},
                                },
                            },
                            {"type": "stem_toggle", "id": "vocal-in", "target": "lead-load", "stem": "vocals", "at": "00:24.000", "enabled": True},
                            {"type": "knob_lerp", "id": "filter-open", "target": "deck-1", "param": "lowpass_hz", "at": "00:08.000", "duration": "00:08.000", "from": 800, "to": 1800},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            session = load_session(session_path)
            group = session.stem_groups[0]

        self.assertEqual(group.id, "lead-load")
        self.assertEqual(group.start_ms, 8_000)
        self.assertEqual(group.trim_start_ms, 16_000)
        self.assertEqual(group.duration_ms, 32_000)
        self.assertEqual(group.tempo_shift_pct, 2.5)
        self.assertEqual(group.pitch_shift_semitones, -1)
        self.assertFalse(group.stems["vocals"].enabled)
        self.assertEqual(group.stems["vocals"].automations[0].param, "mute")
        self.assertEqual(group.stems["vocals"].automations[0].points[0].value, False)
        self.assertEqual(session.deck_automations[0].target, "deck-1")
        self.assertEqual(session.deck_automations[0].param, "lowpass_hz")

    def test_actions_compile_cue_jump_with_deck_clock_segments(self):
        stems = {
            "vocals": {"path": "/stems/lead/vocals.flac"},
            "drums": {"path": "/stems/lead/drums.flac"},
            "bass": {"path": "/stems/lead/bass.flac"},
            "other": {"path": "/stems/lead/other.flac"},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "actions": [
                            {"type": "load_track", "id": "lead-load", "deck": "deck-1", "source_path": "/music/lead.flac", "at_ms": 0, "trim_start_ms": 10_000, "duration_ms": 60_000, "stems": stems},
                            {"type": "set_cue", "id": "drop", "target": "lead-load", "position_ms": 42_000, "at_ms": 1_000},
                            {"type": "jump_to_cue", "target": "lead-load", "cue_id": "drop", "at_ms": 16_000},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            session = load_session(session_path)

        self.assertEqual([group.id for group in session.stem_groups], ["lead-load", "lead-load-segment-02"])
        self.assertEqual([(group.start_ms, group.trim_start_ms, group.duration_ms) for group in session.stem_groups], [(0, 10_000, 16_000), (16_000, 42_000, 28_000)])

    def test_actions_compile_load_track_duration_caps_later_deck_close(self):
        stems = {
            "vocals": {"path": "/stems/lead/vocals.flac"},
            "drums": {"path": "/stems/lead/drums.flac"},
            "bass": {"path": "/stems/lead/bass.flac"},
            "other": {"path": "/stems/lead/other.flac"},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "actions": [
                            {
                                "type": "load_track",
                                "id": "short-vocal",
                                "deck": "deck-1",
                                "source_path": "/music/vocal.flac",
                                "at_ms": 10_000,
                                "duration_ms": 12_000,
                                "stems": stems,
                            },
                            {
                                "type": "load_track",
                                "id": "next-vocal",
                                "deck": "deck-1",
                                "source_path": "/music/next.flac",
                                "at_ms": 60_000,
                                "duration_ms": 8_000,
                                "stems": stems,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            session = load_session(session_path)

        self.assertEqual(
            [(group.id, group.start_ms, group.duration_ms) for group in session.stem_groups],
            [("short-vocal", 10_000, 12_000), ("next-vocal", 60_000, 8_000)],
        )

    def test_actions_compile_pause_and_play_transport_segments(self):
        stems = {
            "vocals": {"path": "/stems/lead/vocals.flac"},
            "drums": {"path": "/stems/lead/drums.flac"},
            "bass": {"path": "/stems/lead/bass.flac"},
            "other": {"path": "/stems/lead/other.flac"},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "actions": [
                            {
                                "type": "load_track",
                                "id": "lead-load",
                                "deck": "deck-1",
                                "source_path": "/music/lead.flac",
                                "at_ms": 0,
                                "trim_start_ms": 10_000,
                                "duration_ms": 60_000,
                                "tempo_shift_pct": 25,
                                "stems": stems,
                            },
                            {"type": "pause", "id": "pause-lead", "target": "deck-1", "at_ms": 8_000},
                            {"type": "play", "id": "resume-lead", "target": "deck-1", "at_ms": 12_000},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            session = load_session(session_path)

        self.assertEqual([group.id for group in session.stem_groups], ["lead-load", "lead-load-segment-02"])
        self.assertEqual([(group.start_ms, group.trim_start_ms, group.duration_ms) for group in session.stem_groups], [(0, 10_000, 8_000), (12_000, 20_000, 50_000)])

    def test_actions_compile_cue_and_seek_transport_segments(self):
        stems = {
            "vocals": {"path": "/stems/lead/vocals.flac"},
            "drums": {"path": "/stems/lead/drums.flac"},
            "bass": {"path": "/stems/lead/bass.flac"},
            "other": {"path": "/stems/lead/other.flac"},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "actions": [
                            {"type": "load_track", "id": "lead-load", "deck": "deck-1", "source_path": "/music/lead.flac", "at_ms": 0, "trim_start_ms": 0, "duration_ms": 80_000, "stems": stems},
                            {"type": "set_cue", "id": "drop", "target": "lead-load", "position_ms": 32_000, "at_ms": 1_000},
                            {"type": "cue", "id": "cue-drop", "target": "lead-load", "cue_id": "drop", "at_ms": 8_000},
                            {"type": "play", "id": "play-drop", "target": "deck-1", "at_ms": 12_000},
                            {"type": "seek", "id": "seek-hook", "target": "deck-1", "position_ms": 48_000, "at_ms": 20_000},
                            {"type": "cue_seek", "id": "park-outro", "target": "deck-1", "position_ms": 64_000, "at_ms": 28_000},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            session = load_session(session_path)

        self.assertEqual([group.id for group in session.stem_groups], ["lead-load", "lead-load-segment-02", "lead-load-segment-03"])
        self.assertEqual([(group.start_ms, group.trim_start_ms, group.duration_ms) for group in session.stem_groups], [(0, 0, 8_000), (12_000, 32_000, 8_000), (20_000, 48_000, 8_000)])

    def test_actions_compile_loop_segments_and_resume_deck_clock(self):
        stems = {
            "vocals": {"path": "/stems/lead/vocals.flac"},
            "drums": {"path": "/stems/lead/drums.flac"},
            "bass": {"path": "/stems/lead/bass.flac"},
            "other": {"path": "/stems/lead/other.flac"},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "actions": [
                            {"type": "load_track", "id": "lead-load", "deck": "deck-1", "source_path": "/music/lead.flac", "at_ms": 0, "trim_start_ms": 0, "duration_ms": 40_000, "stems": stems},
                            {"type": "stem_toggle", "target": "lead-load", "stem": "vocals", "at_ms": 4_000, "enabled": False},
                            {"type": "loop_start", "target": "lead-load", "at_ms": 8_000, "position_ms": 24_000, "length_ms": 4_000, "exit_ms": 16_000},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            session = load_session(session_path)

        self.assertEqual([group.id for group in session.stem_groups], ["lead-load", "lead-load-loop-01", "lead-load-loop-02", "lead-load-segment-02"])
        self.assertEqual([(group.start_ms, group.trim_start_ms, group.duration_ms) for group in session.stem_groups], [(0, 0, 8_000), (8_000, 24_000, 4_000), (12_000, 24_000, 4_000), (16_000, 28_000, 12_000)])
        self.assertTrue(all(group.stems["vocals"].automations for group in session.stem_groups))

    def test_session_covers_cues_beat_jumps_play_pause_and_loops_together(self):
        stems = {
            "vocals": {"path": "/stems/lead/vocals.flac"},
            "drums": {"path": "/stems/lead/drums.flac"},
            "bass": {"path": "/stems/lead/bass.flac"},
            "other": {"path": "/stems/lead/other.flac"},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            session_path = temp / "session.json"
            cache = temp / "dj-cache.json"
            jumped_track = "/music/jumped.flac"
            write_analysis_cache(cache, jumped_track, bpm=120, beat_offset_ms=0)
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1", "deck-2"],
                        "clips": [
                            {
                                "id": "jumped-clip",
                                "deck": "deck-2",
                                "path": jumped_track,
                                "start_ms": 40_000,
                                "trim_start_ms": 1_000,
                                "duration_ms": 8_000,
                            }
                        ],
                        "actions": [
                            {
                                "type": "load_track",
                                "id": "lead-load",
                                "deck": "deck-1",
                                "source_path": "/music/lead.flac",
                                "at_ms": 0,
                                "trim_start_ms": 0,
                                "duration_ms": 48_000,
                                "stems": stems,
                            },
                            {"type": "set_cue", "id": "hook", "target": "lead-load", "position_ms": 16_000, "at_ms": 1_000},
                            {"type": "cue", "id": "park-hook", "target": "lead-load", "cue_id": "hook", "at_ms": 4_000},
                            {"type": "play", "id": "play-hook", "target": "deck-1", "at_ms": 6_000},
                            {"type": "pause", "id": "pause-hook", "target": "deck-1", "at_ms": 10_000},
                            {"type": "play", "id": "resume-hook", "target": "deck-1", "at_ms": 12_000},
                            {"type": "loop_start", "id": "loop-hook", "target": "lead-load", "at_ms": 16_000, "position_ms": 24_000, "length_ms": 4_000, "exit_ms": 24_000},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                run_cli(
                    [
                        "slime_audio_session.py",
                        "beat-jump",
                        str(session_path),
                        "--id",
                        "jumped-clip",
                        "--beats",
                        "1",
                        "--cache",
                        str(cache),
                    ]
                ),
                0,
            )
            session = load_session(session_path)

        self.assertEqual(session.clips[0].trim_start_ms, 1_500)
        self.assertEqual(
            [(group.start_ms, group.trim_start_ms, group.duration_ms) for group in session.stem_groups],
            [
                (0, 0, 4_000),
                (6_000, 16_000, 4_000),
                (12_000, 20_000, 4_000),
                (16_000, 24_000, 4_000),
                (20_000, 24_000, 4_000),
                (24_000, 28_000, 20_000),
            ],
        )

    def test_tempo_shifted_loop_length_is_source_clock_not_deck_clock(self):
        stems = {
            "vocals": {"path": "/stems/lead/vocals.flac"},
            "drums": {"path": "/stems/lead/drums.flac"},
            "bass": {"path": "/stems/lead/bass.flac"},
            "other": {"path": "/stems/lead/other.flac"},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "actions": [
                            {
                                "type": "load_track",
                                "id": "lead-load",
                                "deck": "deck-1",
                                "source_path": "/music/lead.flac",
                                "at_ms": 0,
                                "trim_start_ms": 0,
                                "duration_ms": 40_000,
                                "tempo_shift_pct": 25,
                                "stems": stems,
                            },
                            {
                                "type": "loop_start",
                                "target": "lead-load",
                                "at_ms": 8_000,
                                "position_ms": 24_000,
                                "length_ms": 4_000,
                                "exit_ms": 16_000,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            session = load_session(session_path)

        self.assertEqual(
            [(group.start_ms, group.trim_start_ms, group.duration_ms) for group in session.stem_groups],
            [(0, 0, 8_000), (8_000, 24_000, 3_200), (11_200, 24_000, 3_200), (14_400, 24_000, 1_600), (16_000, 28_000, 12_000)],
        )

    def test_add_action_hydrates_ready_stems_from_db(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            db_path = temp / "library.sqlite3"
            manifest_path = temp / "stems" / "set-a" / "manifest.json"
            manifest_path.parent.mkdir(parents=True)
            manifest_path.write_text("{}", encoding="utf-8")
            source = temp / "lead.flac"
            source.write_bytes(b"fake")
            conn = connect(db_path)
            conn.execute(
                """
                INSERT INTO track_stem_sets(
                    id, duplicate_key, source_path, source_size, source_mtime, model, profile, artifact_root,
                    sample_rate, channels, duration_ms, status, error, created_at, updated_at
                )
                VALUES (?, NULL, ?, ?, ?, 'htdemucs', '4stem', ?, 44100, 2, 1000, 'ready', NULL, 'now', 'now')
                """,
                ("set-a", str(source.resolve()), source.stat().st_size, source.stat().st_mtime, str(manifest_path.parent)),
            )
            for stem in ("vocals", "drums", "bass", "other"):
                conn.execute(
                    "INSERT INTO track_stems(stem_set_id, stem_name, path) VALUES ('set-a', ?, ?)",
                    (stem, str(manifest_path.parent / f"{stem}.wav")),
                )
            conn.commit()
            session_path = temp / "session.json"
            action = {
                "type": "load_track",
                "id": "lead-load",
                "deck": "deck-1",
                "source_path": str(source),
                "at_ms": 0,
                "duration_ms": 1000,
            }

            self.assertEqual(
                run_cli(["slime_audio_session.py", "add-action", str(session_path), "--create", "--db", str(db_path), "--action-json", json.dumps(action)]),
                0,
            )
            payload = json.loads(session_path.read_text(encoding="utf-8"))

        stored = payload["actions"][0]
        self.assertEqual(stored["stem_set_id"], "set-a")
        self.assertEqual(stored["manifest_path"], str(manifest_path))
        self.assertEqual(set(stored["stems"]), {"vocals", "drums", "bass", "other"})
        self.assertTrue(all(stored["stems"][stem]["path"].endswith(f"{stem}.wav") for stem in stored["stems"]))

    def test_prepare_load_track_runs_split_when_ready_stems_missing(self):
        action = {"type": "load_track", "id": "lead-load", "deck": "deck-1", "source_path": "/music/lead.flac"}
        artifacts = {
            "stem_set_id": "set-b",
            "manifest_path": "/stems/set-b/manifest.json",
            "stems": {
                "vocals": "/stems/set-b/vocals.wav",
                "drums": "/stems/set-b/drums.wav",
                "bass": "/stems/set-b/bass.wav",
                "other": "/stems/set-b/other.wav",
            },
        }
        with patch("slime_audio_session.ready_stem_artifacts", side_effect=[None, artifacts]) as ready, patch(
            "slime_audio_session.subprocess.run",
            return_value=SimpleNamespace(returncode=0, stdout='{"status":"ready"}', stderr=""),
        ) as run:
            prepared = prepare_load_track_action_stems(action, db_path=Path("/tmp/library.sqlite3"))

        self.assertEqual(ready.call_count, 2)
        self.assertIn("split", run.call_args.args[0])
        self.assertEqual(prepared["stem_set_id"], "set-b")
        self.assertEqual(prepared["stems"]["vocals"]["path"], "/stems/set-b/vocals.wav")

    def test_playhead_from_playlist_state_uses_duration_cache(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            (temp / "timeline-duration-cache.json").write_text(
                json.dumps({"/music/a.flac": 10_000, "/music/b.flac": 20_000}),
                encoding="utf-8",
            )
            state = temp / "state.json"
            state.write_text(
                json.dumps(
                    {
                        "started_at": "2026-05-30T19:00:00-0400",
                        "index": 2,
                        "order": ["/music/a.flac", "/music/b.flac", "/music/c.flac"],
                    }
                ),
                encoding="utf-8",
            )

            state_payload = json.loads(state.read_text(encoding="utf-8"))
            state_payload["started_at"] = "1970-01-01T00:00:00+0000"
            state.write_text(json.dumps(state_payload), encoding="utf-8")
            playhead = playhead_ms_from_state(state, now=5)

        self.assertEqual(playhead, 35_000)

    def test_session_allows_non_contiguous_overlapping_clips_on_different_decks(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "session.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1", "deck-2"],
                        "clips": [
                            {
                                "id": "a",
                                "deck": "deck-1",
                                "path": "/music/a.flac",
                                "start": "00:00.000",
                                "trim_start": "00:32.000",
                                "duration": "00:30.000",
                            },
                            {
                                "id": "b",
                                "deck": "deck-2",
                                "path": "/music/b.flac",
                                "start": "00:10.000",
                                "trim_start": "01:12.000",
                                "duration": "00:16.000",
                            },
                            {
                                "id": "c",
                                "deck": "deck-1",
                                "path": "/music/c.flac",
                                "start": "01:20.000",
                                "duration": "00:20.000",
                            },
                        ],
                        "mic_lean_ins": [
                            {
                                "id": "mic-1",
                                "start": "00:08.000",
                                "text": "quick note",
                                "ducking": {
                                    "target": "master",
                                    "param": "duck_volume",
                                    "points": [
                                        {"at": "00:07.900", "value": 0.5},
                                        {"at": "00:10.000", "value": 1.0},
                                    ],
                                },
                                "lowpass": {
                                    "target": "master",
                                    "param": "lowpass_hz",
                                    "points": [
                                        {"at": "00:07.900", "value": 1400},
                                        {"at": "00:10.000", "value": 22050},
                                    ],
                                },
                            }
                        ],
                        "automations": [
                            {
                                "target": "b",
                                "param": "gain_db",
                                "points": [
                                    {"at": "00:10.000", "value": -18.0},
                                    {"at": "00:14.000", "value": -4.0},
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            session = load_session(path)
            summary = session_summary(session)

        self.assertEqual(summary["clip_count"], 3)
        self.assertEqual(summary["mic_lean_in_count"], 1)
        self.assertEqual(summary["automation_count"], 3)
        self.assertEqual(summary["clips_by_deck"]["deck-1"][1]["id"], "c")

    def test_session_rejects_same_deck_overlap_when_durations_are_known(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "session.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "clips": [
                            {"id": "a", "deck": "deck-1", "path": "/music/a.flac", "start": 0, "duration": 20_000},
                            {"id": "b", "deck": "deck-1", "path": "/music/b.flac", "start": 10_000, "duration": 20_000},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "overlap"):
                load_session(path)

    def test_session_rejects_unplanned_manual_param(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "session.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "clips": [
                            {"id": "a", "deck": "deck-1", "path": "/music/a.flac", "start": 0, "duration": 20_000}
                        ],
                        "automations": [
                            {"target": "a", "param": "panic_button", "points": [{"at": 0, "value": 1}]}
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "not an automatable param"):
                load_session(path)

    def test_cli_edits_session_plan(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "session.json"
            load_action = {
                "type": "load_track",
                "id": "intro",
                "deck": "deck-1",
                "source_path": "/music/intro.flac",
                "at": "00:00.000",
                "trim_start": "00:32.000",
                "duration": "00:16.000",
                "gain_db": -2,
                "stems": {
                    "vocals": "/stems/intro/vocals.flac",
                    "drums": "/stems/intro/drums.flac",
                    "bass": "/stems/intro/bass.flac",
                    "other": "/stems/intro/other.flac",
                },
            }
            self.assertEqual(
                run_cli(["slime_audio_session.py", "add-action", str(path), "--create", "--action-json", json.dumps(load_action)]),
                0,
            )
            self.assertEqual(
                run_cli(
                    [
                    "slime_audio_session.py",
                    "add-mic",
                    str(path),
                    "--id",
                    "mic-1",
                    "--start",
                    "00:08.000",
                    "--text",
                    "incoming",
                    "--duck-volume",
                    "0.5",
                    "--lowpass-hz",
                    "1200",
                    ]
                ),
                0,
            )
            self.assertEqual(
                run_cli(
                    [
                    "slime_audio_session.py",
                    "move",
                    str(path),
                    "--id",
                    "intro",
                    "--start",
                    "00:04.000",
                    ]
                ),
                0,
            )
            self.assertEqual(
                run_cli(
                    [
                    "slime_audio_session.py",
                    "automate",
                    str(path),
                    "--target",
                    "intro",
                    "--param",
                    "gain_db",
                    "--points-json",
                    '[{"at":"00:04.000","value":-18},{"at":"00:08.000","value":-3}]',
                    ]
                ),
                0,
            )

            session = load_session(path)
            summary = session_summary(session)

        self.assertEqual(summary["clip_count"], 0)
        self.assertEqual(summary["stem_group_count"], 1)
        self.assertEqual(summary["mic_lean_in_count"], 1)
        self.assertEqual(summary["automation_count"], 3)
        self.assertIn("deck-5", summary["decks"])
        self.assertEqual(session.mic_lean_ins[0].deck, "deck-5")
        self.assertEqual(summary["fader_routing"]["deck-5"], "THRU")
        self.assertEqual(session.stem_groups[0].gain_db, -2.0)
        self.assertEqual(summary["stem_groups_by_deck"]["deck-1"][0]["start_ms"], 4_000)
        lean_in = session.mic_lean_ins[0]
        self.assertEqual([effect.param for effect in lean_in.effects], ["duck_volume", "lowpass_hz"])

    def test_session_defaults_lean_ins_to_dedicated_vocal_deck(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "session.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1", "deck-2", "deck-3", "deck-4"],
                        "clips": [],
                        "mic_lean_ins": [{"id": "drop", "start": "00:04.000", "text": "short drop"}],
                    }
                ),
                encoding="utf-8",
            )

            session = load_session(path)

        self.assertEqual(session.mic_lean_ins[0].deck, "deck-5")
        self.assertIn("deck-5", session.decks)
        self.assertEqual(session.fader_routing["deck-5"], "THRU")

    def test_cli_import_playlist_is_not_available(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            playlist = temp / "playlist.txt"
            session_path = temp / "session.json"
            playlist.write_text("/music/one.flac\n/music/two.flac\n/music/three.flac\n", encoding="utf-8")

            with self.assertRaises(SystemExit) as exc:
                run_cli(
                    [
                        "slime_audio_session.py",
                        "import-playlist",
                        str(session_path),
                        "--playlist",
                        str(playlist),
                    ]
                )

        self.assertEqual(exc.exception.code, 2)
        self.assertFalse(session_path.exists())

    def test_cli_remove_deletes_targeted_automation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "session.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "clips": [
                            {"id": "a", "deck": "deck-1", "path": "/music/a.flac", "start": 0, "duration": 20_000}
                        ],
                        "automations": [
                            {"target": "a", "param": "gain_db", "points": [{"at": 0, "value": -6}]}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(run_cli(["slime_audio_session.py", "remove", str(path), "--id", "a"]), 0)

            payload = json.loads(path.read_text(encoding="utf-8"))
            session = load_session(path)

        self.assertEqual(payload["clips"], [])
        self.assertEqual(payload["automations"], [])
        self.assertEqual(session.clips, [])

    def test_live_edit_lock_rejects_past_edits_without_force(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "session.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "clips": [
                            {"id": "past", "deck": "deck-1", "path": "/music/a.flac", "start": 0, "duration": 20_000},
                            {"id": "future", "deck": "deck-1", "path": "/music/b.flac", "start": 30_000, "duration": 20_000},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "live edit lock"):
                run_cli(
                    [
                        "slime_audio_session.py",
                        "move",
                        str(path),
                        "--id",
                        "past",
                        "--start",
                        "00:12.000",
                        "--lock-before",
                        "00:10.000",
                    ]
                )
            self.assertEqual(
                run_cli(
                    [
                        "slime_audio_session.py",
                        "move",
                        str(path),
                        "--id",
                        "future",
                        "--start",
                        "00:35.000",
                        "--lock-before",
                        "00:10.000",
                    ]
                ),
                0,
            )

            session = load_session(path)

        self.assertEqual(session.clips[1].start_ms, 35_000)

    def test_cli_beat_jump_offsets_trim_by_half_beat_from_cached_grid(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            path = temp / "session.json"
            cache = temp / "dj-cache.json"
            track = "/music/a.flac"
            write_analysis_cache(cache, track, bpm=120, beat_offset_ms=0)
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "clips": [
                            {
                                "id": "double",
                                "deck": "deck-1",
                                "path": track,
                                "start": 10_000,
                                "trim_start": 1_000,
                                "duration": 20_000,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                run_cli(
                    [
                        "slime_audio_session.py",
                        "beat-jump",
                        str(path),
                        "--id",
                        "double",
                        "--beats",
                        "1/2",
                        "--cache",
                        str(cache),
                    ]
                ),
                0,
            )
            session = load_session(path)
            shifted = shift_session_window(session, 10_500)

        self.assertEqual(session.clips[0].trim_start_ms, 1_250)
        self.assertEqual(shifted.clips[0].trim_start_ms, 1_750)

    def test_cli_beat_jump_offsets_start_by_half_beat_from_cached_grid(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            path = temp / "session.json"
            cache = temp / "dj-cache.json"
            track = "/music/b.flac"
            write_analysis_cache(cache, track, bpm=128, beat_offset_ms=0)
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "clips": [
                            {"id": "offset", "deck": "deck-1", "path": track, "start": 8_000, "trim_start": 0, "duration": 20_000}
                        ],
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                run_cli(
                    [
                        "slime_audio_session.py",
                        "beat-jump",
                        str(path),
                        "--id",
                        "offset",
                        "--beats",
                        "1/2",
                        "--field",
                        "start",
                        "--cache",
                        str(cache),
                    ]
                ),
                0,
            )
            session = load_session(path)

        self.assertEqual(session.clips[0].start_ms, 8_234)

    def test_cli_beat_jump_rejects_low_confidence_grid_without_force(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            path = temp / "session.json"
            cache = temp / "dj-cache.json"
            track = "/music/loose.flac"
            write_analysis_cache(cache, track, bpm=120, confidence=0.1)
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "clips": [
                            {"id": "loose", "deck": "deck-1", "path": track, "start": 10_000, "trim_start": 1_000, "duration": 20_000}
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "confidence too low"):
                run_cli(
                    [
                        "slime_audio_session.py",
                        "beat-jump",
                        str(path),
                        "--id",
                        "loose",
                        "--beats",
                        "1",
                        "--cache",
                        str(cache),
                    ]
                )

    def test_cli_instant_double_clones_current_clip_position_to_free_deck(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            path = temp / "session.json"
            cache = temp / "dj-cache.json"
            track = "/music/current.flac"
            write_analysis_cache(cache, track, bpm=120)
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1", "deck-2", "deck-3"],
                        "clips": [
                            {
                                "id": "current",
                                "deck": "deck-1",
                                "path": track,
                                "start": 0,
                                "trim_start": 2_000,
                                "duration": 60_000,
                                "tempo_shift_pct": 5,
                                "pitch_shift_semitones": 1,
                                "trim_db": -4,
                                "gain_db": -3,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                run_cli(
                    [
                        "slime_audio_session.py",
                        "instant-double",
                        str(path),
                        "--source-id",
                        "current",
                        "--id",
                        "current-double",
                        "--start",
                        "00:10.000",
                        "--duration",
                        "00:08.000",
                        "--gate-beats",
                        "1/2",
                        "--cut-source",
                        "--cache",
                        str(cache),
                    ]
                ),
                0,
            )
            session = load_session(path)
            double = next(clip for clip in session.clips if clip.id == "current-double")
            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(double.deck, "deck-2")
        self.assertEqual(double.path, track)
        self.assertEqual(double.trim_start_ms, 12_500)
        self.assertEqual(double.tempo_shift_pct, 5)
        self.assertEqual(double.pitch_shift_semitones, 1)
        self.assertEqual(double.trim_db, -4)
        self.assertEqual(double.gain_db, -3)
        self.assertEqual(len([automation for automation in session.automations if automation.target == "crossfader"]), 32)
        self.assertEqual(payload["fader_routing"]["deck_assignments"]["deck-1"], "A")
        self.assertEqual(payload["fader_routing"]["deck_assignments"]["deck-2"], "B")

    def test_cli_sets_fader_routing_and_crossfader_automation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "session.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1", "deck-2", "deck-3", "deck-4"],
                        "clips": [
                            {"id": "a", "deck": "deck-1", "path": "/music/a.flac", "start": 0, "duration": 20_000},
                            {"id": "b", "deck": "deck-2", "path": "/music/b.flac", "start": 0, "duration": 20_000},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                run_cli(
                    [
                        "slime_audio_session.py",
                        "fader-routing",
                        str(path),
                        "--assign",
                        "deck-1=A",
                        "--assign",
                        "deck-3=A",
                        "--assign",
                        "deck-2=B",
                        "--assign",
                        "deck-4=B",
                    ]
                ),
                0,
            )
            self.assertEqual(
                run_cli(
                    [
                        "slime_audio_session.py",
                        "crossfader",
                        str(path),
                        "--points-json",
                        '[{"at_ms": 0, "value": -1}, {"at_ms": 10000, "value": 0}, {"at_ms": 20000, "value": 1}]',
                    ]
                ),
                0,
            )
            session = load_session(path)

        self.assertEqual(session.fader_routing["deck-1"], "A")
        self.assertEqual(session.fader_routing["deck-4"], "B")
        self.assertEqual(session.automations[-1].target, "crossfader")
        self.assertEqual(session.automations[-1].param, "position")

    def test_cli_instant_double_routine_plans_offbeat_crossfader_swaps_at_multiple_bpms(self):
        for bpm, half_beat_ms in ((120, 250), (90, 333)):
            with self.subTest(bpm=bpm), tempfile.TemporaryDirectory() as temp_dir:
                temp = Path(temp_dir)
                path = temp / "session.json"
                cache = temp / "dj-cache.json"
                track = f"/music/offbeat-{bpm}.flac"
                write_analysis_cache(cache, track, bpm=bpm)
                path.write_text(
                    json.dumps(
                        {
                            "version": 1,
                            "decks": ["deck-1", "deck-2", "deck-3", "deck-4"],
                            "clips": [
                                {"id": "source", "deck": "deck-1", "path": track, "start": 0, "trim_start": 0, "duration": 40_000}
                            ],
                        }
                    ),
                    encoding="utf-8",
                )

                self.assertEqual(
                    run_cli(
                        [
                            "slime_audio_session.py",
                            "instant-double-routine",
                            str(path),
                            "--source-id",
                            "source",
                            "--id",
                            "routine-offbeat",
                            "--recipe",
                            "offbeat-swaps",
                            "--start",
                            "00:00.000",
                            "--cache",
                            str(cache),
                        ]
                    ),
                    0,
                )
                payload = json.loads(path.read_text(encoding="utf-8"))
                crossfader = [automation for automation in payload["automations"] if automation["target"] == "crossfader"]

            self.assertEqual(payload["fader_routing"]["deck_assignments"]["deck-1"], "A")
            self.assertEqual(payload["fader_routing"]["deck_assignments"]["deck-2"], "B")
            self.assertEqual(crossfader[0]["planner_role"], "instant-double-crossfader-hold")
            self.assertEqual(crossfader[0]["points"], [{"at_ms": 0, "value": -1.0}, {"at_ms": half_beat_ms, "value": -1.0}])
            self.assertEqual(
                crossfader[1]["points"],
                [{"at_ms": half_beat_ms, "value": 1.0}, {"at_ms": half_beat_ms * 2, "value": 1.0}],
            )
            self.assertEqual(
                crossfader[2]["points"],
                [{"at_ms": half_beat_ms * 2, "value": -1.0}, {"at_ms": half_beat_ms * 3, "value": -1.0}],
            )
            self.assertEqual(crossfader[3]["points"][0]["at_ms"], half_beat_ms * 3)
            self.assertTrue(all(automation["target"] == "crossfader" for automation in payload["automations"]))

    def test_cli_instant_double_clones_future_clip_and_rejects_busy_deck(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "session.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1", "deck-2"],
                        "clips": [
                            {
                                "id": "future",
                                "deck": "deck-1",
                                "path": "/music/future.flac",
                                "start": "01:00.000",
                                "trim_start": "00:20.000",
                                "duration": "00:40.000",
                            },
                            {"id": "busy", "deck": "deck-2", "path": "/music/busy.flac", "start": "01:10.000", "duration": "00:12.000"},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "not free"):
                run_cli(
                    [
                        "slime_audio_session.py",
                        "instant-double",
                        str(path),
                        "--source-id",
                        "future",
                        "--id",
                        "future-double-busy",
                        "--start",
                        "01:12.000",
                        "--deck",
                        "deck-2",
                    ]
                )
            self.assertEqual(
                run_cli(
                    [
                        "slime_audio_session.py",
                        "instant-double",
                        str(path),
                        "--source-id",
                        "future",
                        "--id",
                        "future-double",
                        "--start",
                        "01:24.000",
                        "--duration",
                        "00:08.000",
                    ]
                ),
                0,
            )
            session = load_session(path)
            double = next(clip for clip in session.clips if clip.id == "future-double")

        self.assertEqual(double.deck, "deck-2")
        self.assertEqual(double.trim_start_ms, 44_000)
        self.assertEqual(double.duration_ms, 8_000)

    def test_cli_instant_double_routine_plans_named_recipe(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            path = temp / "session.json"
            cache = temp / "dj-cache.json"
            track = "/music/routine.flac"
            write_analysis_cache(cache, track, bpm=120)
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1", "deck-2"],
                        "clips": [
                            {"id": "source", "deck": "deck-1", "path": track, "start": 0, "trim_start": 8_000, "duration": 40_000}
                        ],
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                run_cli(
                    [
                        "slime_audio_session.py",
                        "instant-double-routine",
                        str(path),
                        "--source-id",
                        "source",
                        "--id",
                        "routine-a",
                        "--recipe",
                        "stabs",
                        "--start",
                        "00:12.000",
                        "--cache",
                        str(cache),
                    ]
                ),
                0,
            )
            session = load_session(path)
            double = next(clip for clip in session.clips if clip.id == "routine-a-double")
            payload = json.loads(path.read_text(encoding="utf-8"))
            double_payload = next(clip for clip in payload["clips"] if clip["id"] == "routine-a-double")

        self.assertEqual(double.duration_ms, 8_000)
        self.assertEqual(double.trim_start_ms, 20_000)
        self.assertEqual(double_payload["planner_role"], "instant-double")
        self.assertEqual(double_payload["routine_recipe"], "stabs")
        self.assertTrue(all(automation.get("routine_id") == "routine-a" for automation in payload["automations"]))

    def test_cli_add_effect_writes_reverb_event(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "session.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "clips": [{"id": "lead", "deck": "deck-1", "path": "/music/lead.flac", "start": 0, "duration": 30_000}],
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                run_cli(
                    [
                        "slime_audio_session.py",
                        "add-effect",
                        str(path),
                        "--id",
                        "lead-reverb",
                        "--type",
                        "reverb",
                        "--target",
                        "lead",
                        "--start",
                        "00:08.000",
                        "--duration",
                        "00:02.000",
                        "--tail-ms",
                        "3000",
                        "--wet",
                        "0.4",
                        "--room-size",
                        "0.72",
                        "--damping",
                        "0.55",
                    ]
                ),
                0,
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            session = load_session(path)

        self.assertEqual(len(session.effects), 1)
        self.assertEqual(payload["effects"][0]["id"], "lead-reverb")
        self.assertEqual(payload["effects"][0]["type"], "reverb")
        self.assertEqual(payload["effects"][0]["target"], "lead")
        self.assertEqual(payload["effects"][0]["tail_ms"], 3000)
        self.assertEqual(payload["effects"][0]["room_size"], 0.72)
        self.assertEqual(payload["effects"][0]["damping"], 0.55)

    def test_cli_add_effect_delay_beats_syncs_to_rendered_tempo(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            path = temp / "session.json"
            cache = temp / "dj-cache.json"
            # Analyzed 120 BPM; the clip plays 25% faster, so the rendered
            # tempo is 150 BPM and one beat is 400 ms.
            cache.write_text(
                json.dumps(
                    {
                        "key": {
                            "path": "/music/lead.flac",
                            "bpm": 120.0,
                            "beat_offset_ms": 0,
                            "confidence": {"bpm": 0.9},
                            "beatgrid": {"bpm": 120.0, "beat_offset_ms": 0, "phrase_beats": 32, "phrase_ms": 16_000},
                        }
                    }
                ),
                encoding="utf-8",
            )
            path.write_text(
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
                                "duration": 30_000,
                                "tempo_shift_pct": 25.0,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                run_cli(
                    [
                        "slime_audio_session.py",
                        "add-effect",
                        str(path),
                        "--id",
                        "lead-echo",
                        "--type",
                        "echo",
                        "--target",
                        "lead",
                        "--start",
                        "00:08.000",
                        "--duration",
                        "00:01.000",
                        "--delay-beats",
                        "0.75",
                        "--cache",
                        str(cache),
                    ]
                ),
                0,
            )
            payload = json.loads(path.read_text(encoding="utf-8"))

        effect = payload["effects"][0]
        # dotted eighth at rendered 150 BPM: 400 ms * 0.75 = 300 ms
        self.assertEqual(effect["delay_ms"], 300)
        self.assertEqual(effect["delay_beats"], 0.75)

    def test_cli_add_effect_delay_beats_requires_analyzable_target(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            path = temp / "session.json"
            cache = temp / "dj-cache.json"
            cache.write_text("{}", encoding="utf-8")
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "clips": [{"id": "lead", "deck": "deck-1", "path": "/music/lead.flac", "start": 0, "duration": 30_000}],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(ValueError) as raised:
                run_cli(
                    [
                        "slime_audio_session.py",
                        "add-effect",
                        str(path),
                        "--id",
                        "deck-echo",
                        "--type",
                        "echo",
                        "--target",
                        "deck:deck-1",
                        "--start",
                        "00:08.000",
                        "--duration",
                        "00:01.000",
                        "--delay-beats",
                        "1",
                        "--cache",
                        str(cache),
                    ]
                )

        self.assertIn("delay in ms for deck/master targets", str(raised.exception))

    def test_cli_add_effect_uses_audacity_like_reverb_defaults(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "session.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "clips": [{"id": "lead", "deck": "deck-1", "path": "/music/lead.flac", "start": 0, "duration": 30_000}],
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                run_cli(
                    [
                        "slime_audio_session.py",
                        "add-effect",
                        str(path),
                        "--id",
                        "lead-reverb",
                        "--type",
                        "reverb",
                        "--target",
                        "lead",
                        "--start",
                        "00:08.000",
                        "--duration",
                        "00:02.000",
                    ]
                ),
                0,
            )
            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(payload["effects"][0]["tail_ms"], 6000)
        self.assertEqual(payload["effects"][0]["wet"], 0.89)
        self.assertEqual(payload["effects"][0]["gain_db"], -1.0)
        self.assertEqual(payload["effects"][0]["delay_ms"], 10)
        self.assertEqual(payload["effects"][0]["feedback"], 0.5)
        self.assertEqual(payload["effects"][0]["room_size"], 0.75)
        self.assertEqual(payload["effects"][0]["damping"], 0.5)

    def test_cli_add_effect_can_start_from_audacity_reverb_preset(self):
        self.assertIn("church-hall", AUDACITY_REVERB_PRESETS)
        self.assertIn("big-cave", AUDACITY_REVERB_PRESETS)

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "session.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "clips": [{"id": "lead", "deck": "deck-1", "path": "/music/lead.flac", "start": 0, "duration": 30_000}],
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                run_cli(
                    [
                        "slime_audio_session.py",
                        "add-effect",
                        str(path),
                        "--id",
                        "lead-hall",
                        "--type",
                        "reverb",
                        "--preset",
                        "church-hall",
                        "--target",
                        "lead",
                        "--start",
                        "00:08.000",
                        "--duration",
                        "00:02.000",
                        "--wet",
                        "0.62",
                    ]
                ),
                0,
            )
            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(payload["effects"][0]["preset"], "church-hall")
        self.assertEqual(payload["effects"][0]["tail_ms"], 7500)
        self.assertEqual(payload["effects"][0]["delay_ms"], 32)
        self.assertEqual(payload["effects"][0]["feedback"], 0.6)
        self.assertEqual(payload["effects"][0]["room_size"], 0.9)
        self.assertEqual(payload["effects"][0]["damping"], 0.5)
        self.assertEqual(payload["effects"][0]["wet"], 0.62)

    def test_cli_instant_double_routine_can_add_echo_effect(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            path = temp / "session.json"
            cache = temp / "dj-cache.json"
            track = "/music/routine.flac"
            write_analysis_cache(cache, track, bpm=120)
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1", "deck-2"],
                        "clips": [
                            {"id": "source", "deck": "deck-1", "path": track, "start": 0, "trim_start": 8_000, "duration": 40_000}
                        ],
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                run_cli(
                    [
                        "slime_audio_session.py",
                        "instant-double-routine",
                        str(path),
                        "--source-id",
                        "source",
                        "--id",
                        "routine-echo",
                        "--recipe",
                        "echo-stabs",
                        "--start",
                        "00:12.000",
                        "--cache",
                        str(cache),
                    ]
                ),
                0,
            )
            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(payload["effects"][0]["id"], "routine-echo-echo")
        self.assertEqual(payload["effects"][0]["target"], "routine-echo-double")
        self.assertEqual(payload["effects"][0]["routine_recipe"], "echo-stabs")
        self.assertEqual(payload["effects"][0]["tail_ms"], 2000)

    def test_cli_instant_double_routine_can_add_reverb_effect(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            path = temp / "session.json"
            cache = temp / "dj-cache.json"
            track = "/music/routine.flac"
            write_analysis_cache(cache, track, bpm=120)
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1", "deck-2"],
                        "clips": [
                            {"id": "source", "deck": "deck-1", "path": track, "start": 0, "trim_start": 8_000, "duration": 40_000}
                        ],
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                run_cli(
                    [
                        "slime_audio_session.py",
                        "instant-double-routine",
                        str(path),
                        "--source-id",
                        "source",
                        "--id",
                        "routine-reverb",
                        "--recipe",
                        "echo-drop",
                        "--start",
                        "00:12.000",
                        "--cache",
                        str(cache),
                    ]
                ),
                0,
            )
            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(payload["effects"][0]["id"], "routine-reverb-reverb")
        self.assertEqual(payload["effects"][0]["type"], "reverb")
        self.assertEqual(payload["effects"][0]["target"], "routine-reverb-double")
        self.assertEqual(payload["effects"][0]["routine_recipe"], "echo-drop")
        self.assertEqual(payload["effects"][0]["tail_ms"], 3500)
        self.assertTrue(any(automation["target"] == "crossfader" for automation in payload["automations"]))
        self.assertFalse(any(automation["target"] == "routine-reverb-double" for automation in payload["automations"]))

    def test_cli_slip_records_resume_position_for_manipulated_deck(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "session.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1", "deck-2"],
                        "clips": [
                            {"id": "source", "deck": "deck-1", "path": "/music/a.flac", "start": 0, "trim_start": 10_000, "duration": 30_000},
                            {"id": "scratch", "deck": "deck-2", "path": "/music/a.flac", "start": 8_000, "trim_start": 18_000, "duration": 2_000},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                run_cli(
                    [
                        "slime_audio_session.py",
                        "slip",
                        str(path),
                        "--id",
                        "scratch-slip",
                        "--source-id",
                        "source",
                        "--target-id",
                        "scratch",
                        "--start",
                        "00:08.000",
                        "--duration",
                        "00:02.000",
                    ]
                ),
                0,
            )
            session = load_session(path)
            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(len(session.slip_events), 1)
        self.assertEqual(session.slip_events[0].source_start_ms, 18_000)
        self.assertEqual(session.slip_events[0].source_resume_ms, 20_000)
        self.assertEqual(payload["slip_events"][0]["target_clip_id"], "scratch")

    def test_cli_slip_brake_routine_adds_slip_and_one_beat_vinyl_brake(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            path = temp / "session.json"
            cache = temp / "dj-cache.json"
            track = "/music/routine.flac"
            write_analysis_cache(cache, track, bpm=120)
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1", "deck-2", "deck-3"],
                        "clips": [
                            {"id": "source", "deck": "deck-2", "path": track, "start": 0, "trim_start": 8_000, "duration": 40_000},
                            {"id": "bed", "deck": "deck-3", "path": track, "start": 0, "trim_start": 20_000, "duration": 40_000},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                run_cli(
                    [
                        "slime_audio_session.py",
                        "instant-double-routine",
                        str(path),
                        "--source-id",
                        "source",
                        "--id",
                        "routine-slip-brake",
                        "--recipe",
                        "slip-brake",
                        "--start",
                        "00:12.000",
                        "--cache",
                        str(cache),
                    ]
                ),
                0,
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            session = load_session(path)
            double = next(clip for clip in payload["clips"] if clip["id"] == "routine-slip-brake-double")
            brake_cut = next(automation for automation in payload["automations"] if automation.get("planner_role") == "vinyl-brake-crossfader-cut")

        self.assertEqual(payload["effects"][0]["type"], "vinyl_brake")
        self.assertEqual(payload["effects"][0]["duration_ms"], 500)
        self.assertEqual(payload["effects"][0]["target"], "routine-slip-brake-double")
        self.assertEqual(double["gain_db"], -96.0)
        self.assertEqual(double["kind"], "effect-track")
        self.assertEqual(double["attached_deck"], "deck-2")
        self.assertEqual(double["effect_parent_clip_id"], "source")
        self.assertEqual(payload["fader_routing"]["deck_assignments"], {"deck-1": "B", "deck-2": "B", "deck-3": "B"})
        self.assertEqual(brake_cut["target"], "crossfader")
        self.assertEqual(brake_cut["param"], "position")
        self.assertEqual(brake_cut["points"], [{"at_ms": 12_000, "value": -1.0}, {"at_ms": 12_500, "value": -1.0}])
        self.assertFalse(any(automation.get("planner_role") == "vinyl-brake-source-duck" for automation in payload["automations"]))
        self.assertFalse(any(automation.get("planner_role") == "instant-double-gate" for automation in payload["automations"]))
        self.assertEqual(payload["slip_events"][0]["source_clip_id"], "source")
        self.assertEqual(payload["slip_events"][0]["target_clip_id"], "routine-slip-brake-double")
        self.assertEqual(payload["slip_events"][0]["source_start_ms"], 20_000)
        self.assertEqual(payload["slip_events"][0]["source_resume_ms"], 20_500)
        self.assertEqual(session.effects[0].type, "vinyl_brake")

    def test_cli_brake_drop_routine_delays_source_resume_without_slip(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            path = temp / "session.json"
            cache = temp / "dj-cache.json"
            track = "/music/routine.flac"
            write_analysis_cache(cache, track, bpm=120)
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1", "deck-2", "deck-3"],
                        "clips": [
                            {"id": "source", "deck": "deck-2", "path": track, "start": 0, "trim_start": 8_000, "duration": 40_000},
                            {"id": "bed", "deck": "deck-3", "path": track, "start": 0, "trim_start": 20_000, "duration": 40_000},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                run_cli(
                    [
                        "slime_audio_session.py",
                        "instant-double-routine",
                        str(path),
                        "--source-id",
                        "source",
                        "--id",
                        "routine-brake",
                        "--recipe",
                        "brake-drop",
                        "--start",
                        "00:12.000",
                        "--cache",
                        str(cache),
                    ]
                ),
                0,
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            source = next(clip for clip in payload["clips"] if clip["id"] == "source")
            resume = next(clip for clip in payload["clips"] if clip["id"] == "routine-brake-resume")
            double = next(clip for clip in payload["clips"] if clip["id"] == "routine-brake-double")

        self.assertEqual(payload.get("slip_events", []), [])
        self.assertEqual(source["duration_ms"], 12_000)
        self.assertEqual(source["fade_out_ms"], 0)
        self.assertEqual(resume["start_ms"], 12_500)
        self.assertEqual(resume["trim_start_ms"], 20_000)
        self.assertEqual(resume["duration_ms"], 28_000)
        self.assertEqual(resume["fade_out_ms"], 0)
        self.assertEqual(resume["planner_role"], "timing-brake-resume")
        self.assertEqual(double["kind"], "effect-track")
        self.assertEqual(double["attached_deck"], "deck-2")
        self.assertEqual(payload["effects"][0]["target"], "routine-brake-double")

    def test_cli_scratch_cuts_routine_adds_attached_reverse_scratch_clips(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            path = temp / "session.json"
            cache = temp / "dj-cache.json"
            track = "/music/routine.flac"
            write_analysis_cache(cache, track, bpm=120)
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1", "deck-2", "deck-3"],
                        "fader_routing": {"deck_assignments": {"deck-1": "A", "deck-2": "B", "deck-3": "A"}},
                        "clips": [
                            {"id": "source", "deck": "deck-2", "path": track, "start": 0, "trim_start": 8_000, "duration": 40_000},
                            {"id": "bed", "deck": "deck-3", "path": track, "start": 0, "trim_start": 20_000, "duration": 40_000},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                run_cli(
                    [
                        "slime_audio_session.py",
                        "instant-double-routine",
                        str(path),
                        "--source-id",
                        "source",
                        "--id",
                        "routine-scratch",
                        "--recipe",
                        "scratch-cuts",
                        "--start",
                        "00:12.000",
                        "--cache",
                        str(cache),
                    ]
                ),
                0,
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            scratch_clips = [clip for clip in payload["clips"] if clip.get("routine_id") == "routine-scratch"]
            source_ducks = [automation for automation in payload["automations"] if automation.get("planner_role") == "scratch-source-duck"]

        self.assertEqual(len(scratch_clips), 4)
        self.assertTrue(any(clip.get("reverse") for clip in scratch_clips))
        self.assertTrue(any(float(clip.get("playback_rate", 1.0)) > 1.0 for clip in scratch_clips))
        self.assertEqual([clip["duration_ms"] for clip in scratch_clips], [200, 160, 240, 180])
        self.assertEqual([clip["start_ms"] for clip in scratch_clips], [12_000, 14_000, 16_000, 18_500])
        self.assertTrue(all(clip["fade_in_ms"] == 18 for clip in scratch_clips))
        self.assertTrue(all(clip["kind"] == "effect-track" for clip in scratch_clips))
        self.assertTrue(all(clip["deck"] == "deck-2" for clip in scratch_clips))
        self.assertTrue(all(clip["attached_deck"] == "deck-2" for clip in scratch_clips))
        self.assertEqual(payload["slip_events"][0]["routine_recipe"], "scratch-cuts")
        self.assertEqual(payload["fader_routing"]["deck_assignments"], {"deck-1": "A", "deck-2": "B", "deck-3": "A"})
        self.assertEqual(len(source_ducks), 4)
        self.assertTrue(all(automation["target"] == "source" for automation in source_ducks))
        self.assertEqual(source_ducks[0]["points"], [{"at_ms": 12_000, "value": -96.0}, {"at_ms": 12_200, "value": -96.0}])

    def test_cli_loop_roll_routine_adds_slip_loop_effect_tracks(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            path = temp / "session.json"
            cache = temp / "dj-cache.json"
            track = "/music/routine.flac"
            write_analysis_cache(cache, track, bpm=120)
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1", "deck-2"],
                        "clips": [
                            {"id": "source", "deck": "deck-1", "path": track, "start": 0, "trim_start": 8_000, "duration": 40_000}
                        ],
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                run_cli(
                    [
                        "slime_audio_session.py",
                        "instant-double-routine",
                        str(path),
                        "--source-id",
                        "source",
                        "--id",
                        "routine-loop",
                        "--recipe",
                        "loop-roll",
                        "--start",
                        "00:12.000",
                        "--cache",
                        str(cache),
                    ]
                ),
                0,
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            loop_clips = [clip for clip in payload["clips"] if clip.get("routine_id") == "routine-loop"]
            source_duck = next(automation for automation in payload["automations"] if automation.get("planner_role") == "loop-roll-source-duck")

        self.assertEqual(len(loop_clips), 8)
        self.assertEqual([clip["start_ms"] for clip in loop_clips], [12_000, 12_500, 13_000, 13_500, 14_000, 14_500, 15_000, 15_500])
        self.assertEqual([clip["trim_start_ms"] for clip in loop_clips], [20_000] * 8)
        self.assertTrue(all(clip["kind"] == "effect-track" for clip in loop_clips))
        self.assertTrue(all(clip["attached_deck"] == "deck-1" for clip in loop_clips))
        self.assertEqual(source_duck["target"], "source")
        self.assertEqual(source_duck["points"], [{"at_ms": 12_000, "value": -96.0}, {"at_ms": 16_000, "value": -96.0}])
        self.assertEqual(payload["slip_events"][0]["routine_recipe"], "loop-roll")
        self.assertEqual(payload["slip_events"][0]["source_resume_ms"], 24_000)

    def test_cli_instant_double_routine_can_start_from_persisted_cue_kind(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            path = temp / "session.json"
            cache = temp / "dj-cache.json"
            db = temp / "library.sqlite3"
            track = temp / "routine.flac"
            track.write_bytes(b"fake audio")
            write_analysis_cache(cache, str(track), bpm=120)
            write_cue_db(db, track, kind="hook", at_ms=24_000)
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1", "deck-2"],
                        "clips": [
                            {"id": "source", "deck": "deck-1", "path": str(track), "start": 60_000, "trim_start": 8_000, "duration": 40_000}
                        ],
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                run_cli(
                    [
                        "slime_audio_session.py",
                        "instant-double-routine",
                        str(path),
                        "--source-id",
                        "source",
                        "--id",
                        "routine-hook",
                        "--recipe",
                        "hook-tease",
                        "--cue-db",
                        str(db),
                        "--cache",
                        str(cache),
                    ]
                ),
                0,
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            double_payload = next(clip for clip in payload["clips"] if clip["id"] == "routine-hook-double")

        self.assertEqual(double_payload["start_ms"], 76_000)
        self.assertEqual(double_payload["trim_start_ms"], 24_000)
        self.assertEqual(double_payload["cue_kind"], "hook")
        self.assertEqual(double_payload["routine_recipe"], "hook-tease")

    def test_cli_instant_double_routine_refuses_unknown_recipe(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            path = temp / "session.json"
            cache = temp / "dj-cache.json"
            track = "/music/routine.flac"
            write_analysis_cache(cache, track, bpm=120)
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1", "deck-2"],
                        "clips": [
                            {"id": "source", "deck": "deck-1", "path": track, "start": 0, "trim_start": 8_000, "duration": 40_000}
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "unknown instant-double recipe"):
                run_cli(
                    [
                        "slime_audio_session.py",
                        "instant-double-routine",
                        str(path),
                        "--source-id",
                        "source",
                        "--id",
                        "routine-b",
                        "--recipe",
                        "not-a-real-routine",
                        "--cache",
                        str(cache),
                    ]
                )

    def test_cli_automate_deck_writes_deck_automation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "session.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1", "deck-2"],
                        "clips": [
                            {"id": "bed", "deck": "deck-2", "path": "/music/bed.flac", "start": 0, "duration": 60_000},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                run_cli(
                    [
                        "slime_audio_session.py",
                        "automate",
                        str(path),
                        "--target",
                        "deck-2",
                        "--param",
                        "gain_db",
                        "--points-json",
                        '[{"at":"00:00.000","value":-9},{"at":"01:00.000","value":-8}]',
                    ]
                ),
                0,
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            session = load_session(path)

        self.assertEqual(payload.get("automations", []), [])
        self.assertEqual(payload["deck_automations"][0]["target"], "deck-2")
        self.assertEqual(session.deck_automations[0].param, "gain_db")


if __name__ == "__main__":
    unittest.main()
