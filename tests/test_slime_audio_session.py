import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from slime_audio_session import AUDACITY_REVERB_PRESETS, load_session, parse_ms, playhead_ms_from_state, session_summary
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
    def test_parse_ms_accepts_clock_strings(self):
        self.assertEqual(parse_ms("01:02.500", "time"), 62_500)
        self.assertEqual(parse_ms("1:02:03", "time"), 3_723_000)

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
            self.assertEqual(
                run_cli(
                    [
                    "slime_audio_session.py",
                    "add-clip",
                    str(path),
                    "--create",
                    "--id",
                    "intro",
                    "--deck",
                    "deck-1",
                    "--path",
                    "/music/intro.flac",
                    "--start",
                    "00:00.000",
                    "--trim-start",
                    "00:32.000",
                    "--duration",
                    "00:16.000",
                    "--trim-db",
                    "-4",
                    "--gain-db",
                    "-2",
                    ]
                ),
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

        self.assertEqual(summary["clip_count"], 1)
        self.assertEqual(summary["mic_lean_in_count"], 1)
        self.assertEqual(summary["automation_count"], 3)
        self.assertEqual(session.clips[0].trim_db, -4.0)
        self.assertEqual(session.clips[0].gain_db, -2.0)
        self.assertEqual(summary["clips_by_deck"]["deck-1"][0]["start_ms"], 4_000)
        lean_in = session.mic_lean_ins[0]
        self.assertEqual([effect.param for effect in lean_in.effects], ["duck_volume", "lowpass_hz"])

    def test_cli_imports_playlist_as_timestamped_timeline(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            playlist = temp / "playlist.txt"
            session_path = temp / "session.json"
            playlist.write_text("/music/one.flac\n/music/two.flac\n/music/three.flac\n", encoding="utf-8")

            self.assertEqual(
                run_cli(
                    [
                        "slime_audio_session.py",
                        "import-playlist",
                        str(session_path),
                        "--playlist",
                        str(playlist),
                        "--start",
                        "00:05.000",
                        "--decks",
                        "deck-1,deck-2",
                        "--default-duration",
                        "00:10.000",
                        "--overlap-ms",
                        "2000",
                        "--no-probe",
                    ]
                ),
                0,
            )

            session = load_session(session_path)

        self.assertEqual([clip.start_ms for clip in session.clips], [5_000, 13_000, 21_000])
        self.assertEqual([clip.deck for clip in session.clips], ["deck-1", "deck-2", "deck-1"])
        self.assertEqual([clip.duration_ms for clip in session.clips], [10_000, 10_000, 10_000])

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
            crossfader = next(automation for automation in payload["automations"] if automation.get("planner_role") == "scratch-transform-cuts")

        self.assertGreaterEqual(len(scratch_clips), 8)
        self.assertTrue(any(clip.get("reverse") for clip in scratch_clips))
        self.assertTrue(any(float(clip.get("playback_rate", 1.0)) > 1.0 for clip in scratch_clips))
        self.assertTrue(all(clip.get("scratch_motion") for clip in scratch_clips))
        self.assertTrue(all(clip["kind"] == "effect-track" for clip in scratch_clips))
        self.assertTrue(all(clip["attached_deck"] == "deck-2" for clip in scratch_clips))
        self.assertEqual(payload["slip_events"][0]["routine_recipe"], "scratch-cuts")
        self.assertEqual(payload["fader_routing"]["deck_assignments"], {"deck-1": "A", "deck-2": "B", "deck-3": "B"})
        self.assertEqual(crossfader["target"], "crossfader")
        self.assertIn({"at_ms": 12_001, "value": -1.0}, crossfader["points"])

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

    def test_cli_mashup_bed_adds_filter_and_gain_automation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "session.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1", "deck-2"],
                        "clips": [
                            {"id": "bed", "deck": "deck-1", "path": "/music/bed.flac", "start": 0, "duration": 60_000},
                            {"id": "lead", "deck": "deck-2", "path": "/music/lead.flac", "start": 16_000, "duration": 32_000},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                run_cli(
                    [
                        "slime_audio_session.py",
                        "mashup-bed",
                        str(path),
                        "--bed-id",
                        "bed",
                        "--start",
                        "00:16.000",
                        "--end",
                        "00:48.000",
                        "--gain-db",
                        "-10",
                        "--lowpass-hz",
                        "1800",
                        "--highpass-hz",
                        "100",
                    ]
                ),
                0,
            )
            session = load_session(path)

        self.assertEqual([automation.param for automation in session.automations], ["gain_db", "lowpass_hz", "highpass_hz"])
        self.assertEqual([automation.target for automation in session.automations], ["bed", "bed", "bed"])
        self.assertEqual(session.automations[0].points[0].at_ms, 16_000)
        self.assertEqual(session.automations[0].points[-1].at_ms, 48_000)


if __name__ == "__main__":
    unittest.main()
