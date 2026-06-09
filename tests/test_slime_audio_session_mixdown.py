import json
import sys
import tempfile
import unittest
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
)


class SlimeAudioSessionMixdownTests(unittest.TestCase):
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

        self.assertIn("atrim=start=2.000:duration=0.750,asetpts=PTS-STARTPTS,areverse,asetrate=72000,aresample=48000", filters)
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

    def test_stem_group_falls_back_to_source_when_stem_paths_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1"],
                        "stem_groups": [
                            {
                                "id": "fallback",
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

        self.assertIn("/music/source.flac", command)

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

    def test_mixdown_filter_renders_clip_mashup_bed_automation(self):
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

        self.assertIn("volume=enable='between(t,0.000,28.000)':volume=0.363078", filters)

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
        self.assertIn("atrim=duration=14.000", filters)
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
            filters = build_filter_complex(session, {}, 48_000, 2)

        self.assertIn("atrim=start=16.000:duration=2.000", filters)
        self.assertIn("apad=pad_dur=4.000", filters)
        self.assertIn("ladspa=file=/usr/lib/ladspa/zita-reverbs.so:plugin=zita-reverb", filters)
        self.assertIn("controls='0.080|220|3.351|2.949|5223.303|160|0|2500|0|1'", filters)
        self.assertIn("volume=0.380000", filters)
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
        self.assertIn("atrim=duration=8.000,alimiter", filters)

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


if __name__ == "__main__":
    unittest.main()
