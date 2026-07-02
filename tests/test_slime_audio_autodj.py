import json
import sqlite3
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import slime_audio_autodj as autodj
from slime_audio_autodj import (
    SelectedTrack,
    add_structural_beds,
    apply_recent_material_policy,
    apply_scratch_material_policy,
    continue_set,
    ensure_utility_deck,
    filter_defensible_source_tracks,
    is_edm_bed_candidate,
    is_downloaded_candidate,
    load_taste_profile,
    rhythm_bed_score,
    session_payload,
    select_tracks,
    structural_bed_balance_profile,
    stem_readiness_report,
    validate_component_bed_balance,
    validate_decision_audit_trail,
    taste_affinity,
    validate_harmonic_overlaps,
    validate_no_vanilla_leads,
    validate_stem_load_usage,
    validate_transition_decisions,
    validate_vocal_guards,
)
from slime_audio_dj import BeatGrid, StructureWindow, TrackAnalysis


def autodj_args(**overrides):
    values = {
        "max_tracks": 1,
        "default_track_ms": 240_000,
        "max_lead_clip_ms": 90_000,
        "max_fast_lead_clip_ms": 64_000,
        "min_section_clip_ms": 32_000,
        "min_anchor_section_ms": 8_000,
        "min_section_confidence": 0.45,
        "require_section_analysis": False,
        "remix_focus": False,
        "stem_aware_remix": False,
        "fade_in_ms": 0,
        "fade_out_ms": 0,
        "base_overlap_ms": 0,
        "title": "test",
        "intent": "test",
        "min_tracks": 1,
        "min_runway_ms": 0,
        "selection_jitter": 0.0,
        "skip_term": [],
        "require_analysis": False,
        "min_score": None,
        "max_per_artist": 1,
        "min_track_ms": 1,
        "structured_source_only": False,
        "taste_profile": Path("/missing/taste-profile.json"),
        "pause_file": Path("/missing/dj-watchdog.paused"),
        "ignore_pause": False,
        "downloaded_track_ratio": 0.10,
        "leftfield_download_ratio": 0.10,
        "scratch_source_file": None,
        "scratch_material_policy": "ban",
        "scratch_material_penalty": 0.8,
        "recent_material_policy": "penalty",
        "db": Path("/missing/library.sqlite3"),
        "vocal_drop_count": None,
        "vocal_drop_volume": 1.65,
        "vocal_drop_duck_volume": 0.42,
        "vocal_drop_lowpass_hz": 1400.0,
        "vocal_drop_duck_ms": 3200,
        "runner_single_window": True,
        "bed_duration_ms": 72_000,
        "bed_trim_start_ms": 30_000,
        "bed_gain_db": -6.0,
        "bed_fade_in_ms": 1_000,
        "bed_fade_out_ms": 1_000,
        "bed_lowpass_hz": 1_800.0,
        "bed_highpass_hz": 90.0,
        "max_structural_beds": 4,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def create_ready_stem_db(temp: Path, sources: list[str]) -> Path:
    from slime_music_library import connect

    db_path = temp / "library.sqlite3"
    conn = connect(db_path)
    for index, source in enumerate(sources):
        set_id = f"set-{index:03d}"
        artifact_root = temp / "stems" / set_id
        artifact_root.mkdir(parents=True, exist_ok=True)
        (artifact_root / "manifest.json").write_text("{}", encoding="utf-8")
        conn.execute(
            """
            INSERT INTO track_stem_sets(
                id, duplicate_key, source_path, source_size, source_mtime, model, profile, artifact_root,
                sample_rate, channels, duration_ms, status, error, created_at, updated_at
            )
            VALUES (?, NULL, ?, 1, 1, 'htdemucs', '4stem', ?, 44100, 2, 1000, 'ready', NULL, 'now', 'now')
            """,
            (set_id, source, str(artifact_root)),
        )
        for stem in ("vocals", "drums", "bass", "other"):
            conn.execute(
                "INSERT INTO track_stems(stem_set_id, stem_name, path) VALUES (?, ?, ?)",
                (set_id, stem, str(artifact_root / f"{stem}.wav")),
            )
    conn.commit()
    conn.close()
    return db_path


def selected_track(path="/music/lead.flac"):
    return SelectedTrack(
        path=path,
        artist="Artist",
        title="Long Lead",
        album="Album",
        score=1.0,
        duration_ms=240_000,
        last_played_at=None,
        plays_seen=0,
        reasons=[],
    )


def analysis(path="/music/lead.flac"):
    return TrackAnalysis(
        path=path,
        duration_s=240.0,
        sample_rate=44_100,
        channels=2,
        bpm=120.0,
        beat_offset_ms=0,
        key=None,
        tonic=None,
        mode=None,
        camelot=None,
        energy=0.5,
        loudness_db=-12.0,
        confidence={"bpm": 0.9},
        beatgrid=BeatGrid(bpm=120.0, beat_offset_ms=0, phrase_beats=32, phrase_ms=16_000),
        structure=[
            StructureWindow("intro", 0, 32_000, 0.5, "opening"),
            StructureWindow("drop", 64_000, 128_000, 0.9, "release"),
        ],
    )


class SlimeAudioAutodjTests(unittest.TestCase):
    def test_continue_set_respects_pause_file_before_selection(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            pause_file = temp / "pause"
            pause_file.write_text("debug pause", encoding="utf-8")
            args = autodj_args(runtime=temp, pause_file=pause_file, ignore_pause=False)

            with patch("slime_audio_autodj.candidate_pool") as candidate_pool:
                result = continue_set(args)

        self.assertEqual(result, 0)
        candidate_pool.assert_not_called()

    def test_taste_profile_scores_top_artists_and_tracks(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            profile_path = Path(temp_dir) / "taste.json"
            profile_path.write_text(
                json.dumps(
                    {
                        "top_artists": [{"name": "Refused"}],
                        "top_tracks": [{"artist": "The Killers", "title": "Mr. Brightside"}],
                    }
                ),
                encoding="utf-8",
            )

            profile = load_taste_profile(profile_path)

        self.assertGreater(taste_affinity({"artist_guess": "Refused", "title_guess": "New Noise"}, profile), 0)
        self.assertGreater(taste_affinity({"artist_guess": "The Killers", "title_guess": "Mr. Brightside"}, profile), 0.7)
        self.assertEqual(taste_affinity({"artist_guess": "Unranked", "title_guess": "Deep Cut"}, profile), 0)

    def test_select_tracks_reserves_download_and_leftfield_download_lanes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            taste_profile = temp / "taste.json"
            taste_profile.write_text(json.dumps({"top_artists": ["Known Artist"]}), encoding="utf-8")
            normal = temp / "library" / "Known Artist" / "Album" / "normal.flac"
            known_download = temp / "_Slime Incoming" / "Known Artist" / "known.flac"
            leftfield_download = temp / "_Slime Incoming" / "Odd Artist" / "odd.flac"
            filler = temp / "library" / "Other Artist" / "Album" / "filler.flac"
            for path in [normal, known_download, leftfield_download, filler]:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"a")
            rows = [
                {
                    "duplicate_key": "normal",
                    "preferred_path": str(normal),
                    "artist_guess": "Known Artist",
                    "title_guess": "Normal",
                    "album_guess": "Album",
                    "score": 1.0,
                    "reasons": [],
                },
                {
                    "duplicate_key": "known-download",
                    "preferred_path": str(known_download),
                    "artist_guess": "Known Artist",
                    "title_guess": "Known Download",
                    "album_guess": "Album",
                    "score": 0.9,
                    "reasons": [],
                },
                {
                    "duplicate_key": "leftfield-download",
                    "preferred_path": str(leftfield_download),
                    "artist_guess": "Odd Artist",
                    "title_guess": "Odd",
                    "album_guess": "Album",
                    "score": 0.2,
                    "reasons": [],
                },
                {
                    "duplicate_key": "filler",
                    "preferred_path": str(filler),
                    "artist_guess": "Other Artist",
                    "title_guess": "Filler",
                    "album_guess": "Album",
                    "score": 0.8,
                    "reasons": [],
                },
            ]
            args = autodj_args(
                max_tracks=10,
                max_per_artist=2,
                taste_profile=taste_profile,
                downloaded_track_ratio=0.20,
                leftfield_download_ratio=0.50,
            )
            with patch("slime_audio_autodj.candidate_pool", return_value=rows), patch("slime_audio_autodj.probe_duration_ms", return_value=120_000):
                selected = select_tracks(args)

        reasons = [reason for track in selected for reason in track.reasons]
        self.assertEqual(selected[0].title, "Normal")
        self.assertTrue(any("downloaded material lane" in reason for reason in reasons))
        self.assertTrue(any("spotify left-field download lane" in reason for reason in reasons))
        self.assertTrue(is_downloaded_candidate({"preferred_path": str(leftfield_download)}))
        self.assertTrue(
            is_downloaded_candidate(
                {
                    "preferred_path": str(temp / "library" / "New Artist" / "Fresh Dig 20260612" / "new.flac"),
                    "album_guess": "Fresh Dig 20260612",
                    "title_guess": "New",
                }
            )
        )

    def test_edm_download_beds_use_discretion_not_spotify_leftfield(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            taste_profile = temp / "taste.json"
            taste_profile.write_text(json.dumps({"top_artists": ["Known Artist"]}), encoding="utf-8")
            lead = temp / "library" / "Known Artist" / "Album" / "lead.flac"
            bed = temp / "_Slime Incoming" / "Techno Sound Beds" / "Rrose" / "Waterfall.flac"
            leftfield_song = temp / "_Slime Incoming" / "Songs" / "Odd Artist" / "Song.flac"
            for path in [lead, bed, leftfield_song]:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"a")
            rows = [
                {
                    "duplicate_key": "lead",
                    "preferred_path": str(lead),
                    "artist_guess": "Known Artist",
                    "title_guess": "Lead",
                    "album_guess": "Album",
                    "score": 1.0,
                    "reasons": [],
                },
                {
                    "duplicate_key": "bed",
                    "preferred_path": str(bed),
                    "artist_guess": "Rrose",
                    "title_guess": "Waterfall",
                    "album_guess": "Techno Sound Beds",
                    "tunebat_bpm": 132.0,
                    "score": 0.9,
                    "reasons": ["rhythm lane query: hard techno"],
                },
                {
                    "duplicate_key": "leftfield-song",
                    "preferred_path": str(leftfield_song),
                    "artist_guess": "Odd Artist",
                    "title_guess": "Song",
                    "album_guess": "Album",
                    "score": 0.2,
                    "reasons": [],
                },
            ]
            args = autodj_args(
                max_tracks=10,
                max_per_artist=2,
                taste_profile=taste_profile,
                downloaded_track_ratio=0.20,
                leftfield_download_ratio=0.50,
            )
            with patch("slime_audio_autodj.candidate_pool", return_value=rows), patch("slime_audio_autodj.probe_duration_ms", return_value=120_000):
                selected = select_tracks(args)

        bed_track = next(track for track in selected if track.title == "Waterfall")
        self.assertTrue(is_edm_bed_candidate(rows[1]))
        self.assertIn("edm bed discretion lane", bed_track.reasons)
        self.assertIn("downloaded material lane", bed_track.reasons)
        self.assertNotIn("spotify left-field download lane", bed_track.reasons)

    def test_select_tracks_bans_scratch_material_when_fresh_alternative_exists(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            scratch_path = temp / "scratch" / "Old Idea" / "old.flac"
            fresh_path = temp / "fresh" / "Flex Artist" / "fresh.flac"
            for path in (scratch_path, fresh_path):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"a")
            scratch_source = temp / "scratch.json"
            scratch_source.write_text(json.dumps({"actions": [{"source_path": str(scratch_path)}]}), encoding="utf-8")
            rows = [
                {
                    "duplicate_key": "scratch",
                    "preferred_path": str(scratch_path),
                    "artist_guess": "Old Idea",
                    "title_guess": "Debug Loop",
                    "album_guess": "Proofs",
                    "score": 9.0,
                    "reasons": [],
                },
                {
                    "duplicate_key": "fresh",
                    "preferred_path": str(fresh_path),
                    "artist_guess": "Flex Artist",
                    "title_guess": "Fresh Pick",
                    "album_guess": "Real Set",
                    "score": 1.0,
                    "reasons": [],
                },
            ]
            args = autodj_args(max_tracks=1, scratch_source_file=[scratch_source], scratch_material_policy="ban")
            with patch("slime_audio_autodj.candidate_pool", return_value=rows), patch("slime_audio_autodj.probe_duration_ms", return_value=120_000):
                selected = select_tracks(args)

        self.assertEqual([track.title for track in selected], ["Fresh Pick"])

    def test_structural_beds_are_sparse_and_avoid_adjacent_lead_sources(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            session_path = temp / "session.json"
            lead_paths = [str(temp / f"lead-{index}.flac") for index in range(6)]
            payload = {
                "version": 1,
                "decks": ["deck-2", "deck-3"],
                "clips": [
                    {
                        "id": f"lead-{index:03d}",
                        "planner_role": "lead",
                        "deck": "deck-2" if index % 2 else "deck-3",
                        "path": path,
                        "start_ms": index * 120_000,
                        "duration_ms": 100_000,
                    }
                    for index, path in enumerate(lead_paths)
                ],
                "actions": [],
                "deck_automations": [],
                "fader_routing": {"deck_assignments": {}},
                "notes": {},
            }
            session_path.write_text(json.dumps(payload), encoding="utf-8")
            selected = [
                SelectedTrack(
                    path=path,
                    artist=f"Artist {index}",
                    title=f"D Track {index}",
                    album="Dnb Test",
                    score=1.0 - index / 10,
                    duration_ms=180_000,
                    last_played_at=None,
                    plays_seen=0,
                    reasons=["rhythm lane query: dnb", "bpm 140.0"],
                )
                for index, path in enumerate(lead_paths)
            ]
            db_path = create_ready_stem_db(temp, lead_paths)
            args = autodj_args(max_structural_beds=2, db=db_path)

            result = add_structural_beds(session_path, selected, args)
            updated = json.loads(session_path.read_text(encoding="utf-8"))

        self.assertEqual(result["added"], 2)
        bed_actions = [action for action in updated["actions"] if "bed" in str(action.get("planner_role"))]
        self.assertEqual(len(bed_actions), 2)
        self.assertLess(len(bed_actions), len(lead_paths))
        used_sources = [action["source_path"] for action in bed_actions]
        self.assertEqual(len(used_sources), len(set(used_sources)))
        for action in bed_actions:
            self.assertEqual(action["type"], "load_track")
            self.assertEqual(set(action["stems"]), {"vocals", "drums", "bass", "other"})
            target_id = str(action["bed_under"])
            target_index = int(target_id.rsplit("-", 1)[1])
            adjacent = set(lead_paths[max(0, target_index - 1) : min(len(lead_paths), target_index + 2)])
            self.assertNotIn(action["source_path"], adjacent)
            plan = action.get("stem_layer_plan")
            self.assertIsInstance(plan, dict)
            self.assertEqual(plan["source_stems"], ["drums"])
            self.assertIn("not a terminal", plan["entry_intent"])
            target = next(item for item in payload["clips"] if item["id"] == target_id)
            target_end = target["start_ms"] + target["duration_ms"]
            self.assertLessEqual(action["start_ms"] + action["duration_ms"], target_end - 24_000)
        automation_roles = {automation.get("planner_role") for automation in updated["deck_automations"]}
        self.assertIn("planned-stem-layer-automation", automation_roles)

    def test_drum_only_structural_beds_are_not_buried_by_generic_bed_curve(self):
        args = autodj_args(bed_gain_db=-6.0, bed_lowpass_hz=1_800.0, bed_highpass_hz=90.0)
        profile = structural_bed_balance_profile(["drums"], args)

        self.assertGreaterEqual(profile["gain_db"], -4.0)
        self.assertGreaterEqual(min(profile["lowpass_points"]), 4_200.0)
        self.assertLessEqual(max(profile["highpass_points"]), 150.0)

        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            session_path = temp / "session.json"
            lead_paths = [str(temp / f"lead-{index}.flac") for index in range(3)]
            payload = {
                "version": 1,
                "decks": ["deck-2", "deck-3"],
                "clips": [
                    {
                        "id": f"lead-{index:03d}",
                        "planner_role": "lead",
                        "deck": "deck-2" if index % 2 else "deck-3",
                        "path": path,
                        "start_ms": index * 120_000,
                        "duration_ms": 100_000,
                    }
                    for index, path in enumerate(lead_paths)
                ],
                "actions": [],
                "deck_automations": [],
                "fader_routing": {"deck_assignments": {}},
                "notes": {},
            }
            session_path.write_text(json.dumps(payload), encoding="utf-8")
            selected = [
                SelectedTrack(
                    path=str(temp / "plain-song.flac"),
                    artist="Plain Artist",
                    title="Plain Song",
                    album="Songs",
                    score=1.0,
                    duration_ms=180_000,
                    last_played_at=None,
                    plays_seen=0,
                    reasons=[],
                )
            ]
            db_path = create_ready_stem_db(temp, [str(temp / "plain-song.flac")])
            result = add_structural_beds(
                session_path, selected, autodj_args(remix_focus=True, max_structural_beds=1, bed_gain_db=-6.0, db=db_path)
            )
            updated = json.loads(session_path.read_text(encoding="utf-8"))

        self.assertEqual(result["added"], 1)
        drum_bed = next(action for action in updated["actions"] if action.get("planner_role") == "drum-bed")
        self.assertEqual(drum_bed["play_stems"], ["drums"])
        self.assertGreaterEqual(drum_bed["gain_db"], -4.0)
        self.assertIn("component-aware drum bed", drum_bed["component_balance_strategy"])
        self.assertEqual(drum_bed["stem_layer_plan"]["source_stems"], ["drums"])
        gain_points = [
            point["value"]
            for automation in updated["deck_automations"]
            if automation.get("source_clip_id") == drum_bed["id"] and automation.get("param") == "gain_db"
            for point in automation["points"]
        ]
        lowpass_points = [
            point["value"]
            for automation in updated["deck_automations"]
            if automation.get("source_clip_id") == drum_bed["id"] and automation.get("param") == "lowpass_hz"
            for point in automation["points"]
        ]
        self.assertGreaterEqual(min(gain_points), -6.0)
        self.assertGreaterEqual(min(lowpass_points), 4_200.0)

    def test_component_bed_balance_guard_rejects_old_buried_drum_beds(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1", "deck-4"],
                        "clips": [
                            {
                                "id": "lead-a",
                                "deck": "deck-1",
                                "path": "/music/lead.flac",
                                "start_ms": 0,
                                "duration_ms": 64_000,
                                "planner_role": "lead",
                            },
                            {
                                "id": "buried-drums",
                                "deck": "deck-4",
                                "path": "/music/bed.flac",
                                "start_ms": 16_000,
                                "duration_ms": 32_000,
                                "gain_db": -6.5,
                                "planner_role": "drum-bed",
                                "play_stems": ["drums"],
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(SystemExit) as raised:
                validate_component_bed_balance(session_path, SimpleNamespace())

        self.assertIn("component bed balance guard failed", str(raised.exception))

    def test_component_bed_balance_guard_accepts_component_aware_drum_beds(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1", "deck-4"],
                        "actions": [
                            {
                                "type": "load_track",
                                "id": "balanced-drums",
                                "deck": "deck-4",
                                "source_path": "/music/bed.flac",
                                "at_ms": 16_000,
                                "duration_ms": 32_000,
                                "gain_db": -4.0,
                                "planner_role": "drum-bed",
                                "play_stems": ["drums"],
                                "component_balance_strategy": "component-aware drum bed: fader first, preserve kick/snare/hat attack",
                                "stem_layer_plan": {
                                    "source_stems": ["drums"],
                                    "target_stems": ["vocals", "drums", "bass", "other"],
                                    "entry_intent": "planned",
                                    "exit_intent": "planned",
                                    "beatmatch_evidence": {"status": "analyzed", "target_tempo_shift_pct": 0.0},
                                    "keymatch_evidence": {"status": "drums-only", "drums_only_exemption": True},
                                    "automation_intent": {"gain_db": "planned"},
                                },
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result = validate_component_bed_balance(session_path, SimpleNamespace())

        self.assertEqual(result["checked"], 1)

    def test_component_bed_balance_guard_rejects_balanced_bed_without_layer_plan(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1", "deck-4"],
                        "actions": [
                            {
                                "type": "load_track",
                                "id": "unplanned-drums",
                                "deck": "deck-4",
                                "source_path": "/music/bed.flac",
                                "at_ms": 16_000,
                                "duration_ms": 32_000,
                                "gain_db": -4.0,
                                "planner_role": "drum-bed",
                                "play_stems": ["drums"],
                                "component_balance_strategy": "component-aware drum bed: fader first",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(SystemExit) as raised:
                validate_component_bed_balance(session_path, SimpleNamespace())

        self.assertIn("no explicit stem-layer plan", str(raised.exception))

    def test_component_bed_balance_guard_rejects_drum_bed_without_beatmatch_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1", "deck-4"],
                        "actions": [
                            {
                                "type": "load_track",
                                "id": "unmatched-drums",
                                "deck": "deck-4",
                                "source_path": "/music/bed.flac",
                                "at_ms": 16_000,
                                "duration_ms": 32_000,
                                "gain_db": -4.0,
                                "planner_role": "drum-bed",
                                "play_stems": ["drums"],
                                "component_balance_strategy": "component-aware drum bed: fader first",
                                "stem_layer_plan": {
                                    "source_stems": ["drums"],
                                    "target_stems": ["vocals", "drums", "bass", "other"],
                                    "entry_intent": "planned",
                                    "exit_intent": "planned",
                                    "automation_intent": {"gain_db": "planned"},
                                },
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(SystemExit) as raised:
                validate_component_bed_balance(session_path, SimpleNamespace())

        self.assertIn("lacks beatmatch evidence", str(raised.exception))

    def test_component_bed_balance_guard_rejects_unjustified_terminal_drum_preview(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1", "deck-4"],
                        "actions": [
                            {
                                "type": "load_track",
                                "id": "terminal-drums",
                                "deck": "deck-4",
                                "source_path": "/music/bed.flac",
                                "source_duration_ms": 240_000,
                                "at_ms": 16_000,
                                "trim_start_ms": 210_000,
                                "duration_ms": 30_000,
                                "gain_db": -4.0,
                                "planner_role": "drum-bed",
                                "play_stems": ["drums"],
                                "component_balance_strategy": "component-aware drum bed: fader first",
                                "stem_layer_plan": {
                                    "source_stems": ["drums"],
                                    "target_stems": ["vocals", "drums", "bass", "other"],
                                    "entry_intent": "generic groove injection",
                                    "exit_intent": "planned",
                                    "beatmatch_evidence": {"status": "analyzed", "target_tempo_shift_pct": 0.0},
                                    "keymatch_evidence": {"status": "drums-only", "drums_only_exemption": True},
                                    "automation_intent": {"gain_db": "planned"},
                                },
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(SystemExit) as raised:
                validate_component_bed_balance(session_path, SimpleNamespace())

        self.assertIn("terminal source window", str(raised.exception))

    def test_component_bed_balance_guard_rejects_canned_deck_gain_ramps(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1", "deck-3", "deck-4"],
                        "actions": [
                            {
                                "type": "load_track",
                                "id": "deck-four-drums",
                                "deck": "deck-4",
                                "source_path": "/music/bed-a.flac",
                                "at_ms": 16_000,
                                "duration_ms": 32_000,
                                "gain_db": -4.0,
                                "planner_role": "drum-bed",
                                "play_stems": ["drums"],
                                "component_balance_strategy": "component-aware drum bed: fader first",
                                "stem_layer_plan": {
                                    "source_stems": ["drums"],
                                    "target_stems": ["vocals", "drums", "bass", "other"],
                                    "entry_intent": "planned",
                                    "exit_intent": "planned",
                                    "beatmatch_evidence": {"status": "analyzed", "target_tempo_shift_pct": 0.0},
                                    "keymatch_evidence": {"status": "drums-only", "drums_only_exemption": True},
                                    "automation_intent": {"gain_db": "planned"},
                                },
                            },
                            {
                                "type": "load_track",
                                "id": "deck-three-drums",
                                "deck": "deck-3",
                                "source_path": "/music/bed-b.flac",
                                "at_ms": 64_000,
                                "duration_ms": 32_000,
                                "gain_db": -4.0,
                                "planner_role": "drum-bed",
                                "play_stems": ["drums"],
                                "component_balance_strategy": "component-aware drum bed: fader first",
                                "stem_layer_plan": {
                                    "source_stems": ["drums"],
                                    "target_stems": ["vocals", "drums", "bass", "other"],
                                    "entry_intent": "planned",
                                    "exit_intent": "planned",
                                    "beatmatch_evidence": {"status": "analyzed", "target_tempo_shift_pct": 0.0},
                                    "keymatch_evidence": {"status": "drums-only", "drums_only_exemption": True},
                                    "automation_intent": {"gain_db": "planned"},
                                },
                            },
                        ],
                        "deck_automations": [
                            {
                                "target": "deck-4",
                                "source_clip_id": "deck-four-drums",
                                "param": "gain_db",
                                "points": [{"at_ms": 16_000, "value": -5.4}, {"at_ms": 24_000, "value": 0.0}],
                            },
                            {
                                "target": "deck-3",
                                "source_clip_id": "deck-three-drums",
                                "param": "gain_db",
                                "points": [{"at_ms": 64_000, "value": -4.8}, {"at_ms": 72_000, "value": 0.0}],
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(SystemExit) as raised:
                validate_component_bed_balance(session_path, SimpleNamespace())

        self.assertIn("known canned ramp", str(raised.exception))

    def test_decision_audit_guard_rejects_generated_session_without_audit_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "timeline_mode": "autodj-arrangement",
                        "notes": {"selection_process": "database candidates"},
                        "clips": [],
                        "actions": [],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(SystemExit) as raised:
                validate_decision_audit_trail(session_path, SimpleNamespace())

        self.assertIn("no notes.audit_trail_path", str(raised.exception))

    def test_decision_audit_guard_rejects_receipt_style_audit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            audit_path = temp / "decision-audit.json"
            audit_path.write_text(
                json.dumps(
                    {
                        "request_summary": "new set",
                        "selected_records": [{"artist": "Artist", "title": "Track"}],
                        "stem_status": "split",
                        "launch_plan": "runner",
                    }
                ),
                encoding="utf-8",
            )
            session_path = temp / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "notes": {"audit_trail_path": "decision-audit.json"},
                        "clips": [],
                        "actions": [],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(SystemExit) as raised:
                validate_decision_audit_trail(session_path, SimpleNamespace())

        self.assertIn("missing", str(raised.exception))
        self.assertIn("tempo_key_decisions", str(raised.exception))

    def test_decision_audit_guard_accepts_full_audit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            audit_path = temp / "decision-audit.json"
            audit_path.write_text(
                json.dumps(
                    {
                        "request_summary": {"intent": "test"},
                        "acquisition_summary": {"mode": "database"},
                        "candidate_pool": {"selected": ["track"]},
                        "analysis_source": [{"bpm": 120.0}],
                        "tempo_key_decisions": [{"decision": "cut"}],
                        "stem_role_plan": [{"id": "lead-1", "role": "lead"}],
                        "source_windows": [{"id": "lead-1", "trim_start_ms": 0}],
                        "entry_exit_plan": [{"id": "lead-1", "transition_plan": {"decision": "cut"}}],
                        "balance_proof": {"component_bed_balance_guard": {"checked": 0}},
                        "render_proof_checks": {"guards": {"vanilla_guard": {"checked": 1}}},
                        "launch_facts": {"session": "session.json"},
                    }
                ),
                encoding="utf-8",
            )
            session_path = temp / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "notes": {"audit_trail_path": "decision-audit.json"},
                        "clips": [],
                        "actions": [],
                    }
                ),
                encoding="utf-8",
            )

            result = validate_decision_audit_trail(session_path, SimpleNamespace())

        self.assertTrue(result["required"])
        self.assertEqual(result["checked"], 11)

    def test_scratch_material_policy_falls_back_when_only_scratch_exists(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            scratch_path = temp / "scratch" / "Old Idea" / "old.flac"
            scratch_path.parent.mkdir(parents=True, exist_ok=True)
            scratch_path.write_bytes(b"a")
            scratch_source = temp / "scratch.txt"
            scratch_source.write_text(str(scratch_path) + "\n", encoding="utf-8")
            rows = [
                {
                    "duplicate_key": "scratch",
                    "preferred_path": str(scratch_path),
                    "artist_guess": "Old Idea",
                    "title_guess": "Debug Loop",
                    "album_guess": "Proofs",
                    "score": 9.0,
                    "reasons": [],
                }
            ]
            args = autodj_args(scratch_source_file=[scratch_source], scratch_material_policy="ban")

            filtered = apply_scratch_material_policy(rows, args)

        self.assertEqual(len(filtered), 1)
        self.assertTrue(filtered[0]["scratch_material"])
        self.assertIn("scratch/proof material policy: ban", filtered[0]["reasons"])

    def test_recent_material_policy_bans_previously_seen_tracks_when_fresh_exists(self):
        rows = [
            {
                "duplicate_key": "recent",
                "preferred_path": "/music/recent.flac",
                "artist_guess": "Repeat Artist",
                "title_guess": "Again",
                "plays_seen": 2,
            },
            {
                "duplicate_key": "fresh",
                "preferred_path": "/music/fresh.flac",
                "artist_guess": "Fresh Artist",
                "title_guess": "New One",
                "plays_seen": 0,
            },
        ]
        args = autodj_args(recent_material_policy="ban")

        filtered = apply_recent_material_policy(rows, args)

        self.assertEqual([row["duplicate_key"] for row in filtered], ["fresh"])

    def test_select_tracks_refuses_too_few_tracks_even_with_enough_runway(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            long_path = temp / "fresh" / "Artist" / "long.flac"
            long_path.parent.mkdir(parents=True, exist_ok=True)
            long_path.write_bytes(b"a")
            rows = [
                {
                    "duplicate_key": "long",
                    "preferred_path": str(long_path),
                    "artist_guess": "Artist",
                    "title_guess": "Long Enough",
                    "album_guess": "Album",
                    "score": 1.0,
                    "reasons": [],
                }
            ]
            args = autodj_args(min_tracks=2, min_runway_ms=60_000, max_tracks=2)
            with patch("slime_audio_autodj.candidate_pool", return_value=rows), patch("slime_audio_autodj.probe_duration_ms", return_value=120_000):
                with self.assertRaises(SystemExit):
                    select_tracks(args)

    def test_select_tracks_keeps_selecting_until_min_tracks_before_runway_stop(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            rows = []
            for index in range(2):
                path = temp / "fresh" / f"Artist {index}" / f"track-{index}.flac"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"a")
                rows.append(
                    {
                        "duplicate_key": f"track-{index}",
                        "preferred_path": str(path),
                        "artist_guess": f"Artist {index}",
                        "title_guess": f"Track {index}",
                        "album_guess": "Album",
                        "score": 1.0 - (index * 0.1),
                        "reasons": [],
                    }
                )
            args = autodj_args(min_tracks=2, min_runway_ms=60_000, max_tracks=2)
            with patch("slime_audio_autodj.candidate_pool", return_value=rows), patch("slime_audio_autodj.probe_duration_ms", return_value=120_000):
                selected = select_tracks(args)

        self.assertEqual([track.title for track in selected], ["Track 0", "Track 1"])

    def test_stem_aware_remix_relaxes_artist_cap_for_thin_inventory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            rows = []
            for index in range(3):
                path = temp / "fresh" / "Repeat Artist" / f"track-{index}.flac"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"a")
                rows.append(
                    {
                        "duplicate_key": f"repeat-{index}",
                        "preferred_path": str(path),
                        "artist_guess": "Repeat Artist",
                        "title_guess": f"Repeat {index}",
                        "album_guess": "Album",
                        "score": 1.0 - (index * 0.1),
                        "reasons": [],
                    }
                )
            args = autodj_args(
                min_tracks=3,
                min_runway_ms=60_000,
                max_tracks=3,
                max_per_artist=1,
                stem_aware_remix=True,
                remix_focus=True,
            )
            with patch("slime_audio_autodj.candidate_pool", return_value=rows), patch(
                "slime_audio_autodj.ready_stem_source_paths", return_value={row["preferred_path"] for row in rows}
            ), patch("slime_audio_autodj.probe_duration_ms", return_value=120_000):
                selected = select_tracks(args)

        self.assertEqual([track.title for track in selected], ["Repeat 0", "Repeat 1", "Repeat 2"])

    def test_stem_aware_remix_overselects_before_runway_stop(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            rows = []
            for index in range(4):
                path = temp / "fresh" / f"Artist {index}" / f"track-{index}.flac"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"a")
                rows.append(
                    {
                        "duplicate_key": f"track-{index}",
                        "preferred_path": str(path),
                        "artist_guess": f"Artist {index}",
                        "title_guess": f"Track {index}",
                        "album_guess": "Album",
                        "score": 1.0 - (index * 0.1),
                        "reasons": [],
                    }
                )
            args = autodj_args(
                min_tracks=2,
                min_runway_ms=60_000,
                max_tracks=4,
                stem_aware_remix=True,
                remix_focus=True,
            )
            with patch("slime_audio_autodj.candidate_pool", return_value=rows), patch(
                "slime_audio_autodj.ready_stem_source_paths", return_value={row["preferred_path"] for row in rows}
            ), patch("slime_audio_autodj.probe_duration_ms", return_value=120_000):
                selected = select_tracks(args)

        self.assertEqual([track.title for track in selected], ["Track 0", "Track 1", "Track 2", "Track 3"])

    def test_remix_session_payload_adds_multiple_vocal_drops_on_vocal_deck(self):
        tracks = [
            selected_track("/music/one.flac"),
            selected_track("/music/two.flac"),
            selected_track("/music/three.flac"),
            selected_track("/music/four.flac"),
        ]
        tracks = [
            replace(track, title=f"Lead {index}", artist=f"Artist {index}", path=f"/music/lead-{index}.flac")
            for index, track in enumerate(tracks, start=1)
        ]
        args = autodj_args(remix_focus=True, vocal_drop_count=3, min_tracks=4, max_tracks=4, base_overlap_ms=0)

        payload = session_payload(tracks, args, analyses={track.path: analysis(track.path) for track in tracks})

        self.assertEqual(len(payload["mic_lean_ins"]), 3)
        self.assertTrue(all(drop["deck"] == "deck-5" for drop in payload["mic_lean_ins"]))
        self.assertEqual(payload["fader_routing"]["deck_assignments"]["deck-5"], "THRU")
        self.assertEqual(payload["notes"]["vocal_drop_count"], 3)
        self.assertTrue(all("ducking" in drop and "lowpass" in drop for drop in payload["mic_lean_ins"]))

    def test_vanilla_lead_guard_rejects_untouched_long_lead(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-2"],
                        "clips": [
                            {
                                "id": "lead",
                                "deck": "deck-2",
                                "path": "/music/lead.flac",
                                "start_ms": 0,
                                "duration_ms": 240_000,
                                "planner_role": "lead",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(SystemExit):
                validate_no_vanilla_leads(
                    session_path,
                    SimpleNamespace(min_vanilla_check_ms=90_000, max_vanilla_lead_ms=90_000),
                )

    def test_echo_effect_alone_does_not_satisfy_vanilla_guard(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-2"],
                        "clips": [
                            {
                                "id": "lead",
                                "deck": "deck-2",
                                "path": "/music/lead.flac",
                                "start_ms": 0,
                                "duration_ms": 120_000,
                                "planner_role": "lead",
                            }
                        ],
                        "effects": [
                            {
                                "id": "lead-echo",
                                "type": "echo",
                                "target": "lead",
                                "start_ms": 45_000,
                                "duration_ms": 2_000,
                                "tail_ms": 3_000,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(SystemExit) as raised:
                validate_no_vanilla_leads(
                    session_path,
                    SimpleNamespace(min_vanilla_check_ms=90_000, max_vanilla_lead_ms=90_000),
                )

        self.assertIn("no material DJ move", str(raised.exception))

    def test_session_payload_caps_leads_to_short_sections(self):
        track = selected_track()
        args = autodj_args()

        payload = session_payload([track], args)

        self.assertEqual(payload["clips"][0]["duration_ms"], 90_000)
        self.assertEqual(payload["notes"]["max_lead_clip_ms"], 90_000)

    def test_session_payload_prefers_detected_structure_window(self):
        track = selected_track()
        args = autodj_args(require_section_analysis=True)

        payload = session_payload([track], args, {track.path: analysis()})

        self.assertEqual(payload["clips"][0]["trim_start_ms"], 64_000)
        self.assertEqual(payload["clips"][0]["duration_ms"], 90_000)
        self.assertEqual(payload["clips"][0]["source_window_reason"], "structure:drop")

    def test_session_payload_extends_short_drop_anchor_to_phrase_window(self):
        track = selected_track()
        args = autodj_args(require_section_analysis=True)
        short_drop = replace(
            analysis(),
            structure=[
                StructureWindow("intro", 0, 32_000, 0.8, "opening"),
                StructureWindow("drop", 80_000, 90_000, 0.9, "short drop marker"),
            ],
        )

        payload = session_payload([track], args, {track.path: short_drop})

        self.assertEqual(payload["clips"][0]["trim_start_ms"], 80_000)
        self.assertEqual(payload["clips"][0]["duration_ms"], 90_000)
        self.assertEqual(payload["clips"][0]["source_window_reason"], "structure:drop")

    def test_session_payload_records_remix_focus_policy(self):
        track = selected_track()
        args = autodj_args(remix_focus=True)

        payload = session_payload([track], args)

        self.assertTrue(payload["notes"]["remix_focus"])
        self.assertIn("hard-techno", payload["notes"]["remix_policy"])

    def test_stem_readiness_report_is_optional_without_stem_aware_mode(self):
        track = selected_track()
        args = autodj_args(stem_aware_remix=False)

        report = stem_readiness_report([track], args)

        self.assertEqual(report, {"required": False})

    def test_rhythm_bed_score_uses_candidate_bpm_reason(self):
        track = selected_track()
        track = replace(track, reasons=["bpm 172.0", "rhythm lane query: drum and bass"])

        self.assertGreater(rhythm_bed_score(track), 0)

    def test_ensure_utility_deck_adds_deck_and_routing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-2", "deck-3"],
                        "clips": [
                            {
                                "id": "lead",
                                "deck": "deck-2",
                                "path": "/music/lead.flac",
                                "start_ms": 0,
                                "duration_ms": 60_000,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            ensure_utility_deck(session_path, "deck-4")
            payload = json.loads(session_path.read_text(encoding="utf-8"))

        self.assertIn("deck-4", payload["decks"])
        self.assertEqual(payload["fader_routing"]["deck_assignments"]["deck-4"], "THRU")

    def test_session_payload_requires_structure_when_configured(self):
        track = selected_track()
        args = autodj_args(require_section_analysis=True)

        with self.assertRaises(SystemExit):
            session_payload([track], args)

    def test_session_payload_rejects_phrase_only_analysis_when_structure_required(self):
        track = selected_track()
        args = autodj_args(require_section_analysis=True)
        phrase_only = replace(analysis(), structure=[])

        with self.assertRaises(SystemExit):
            session_payload([track], args, {track.path: phrase_only})

    def test_filter_defensible_source_tracks_drops_unstructured_tracks(self):
        good = selected_track("/music/good.flac")
        bad = selected_track("/music/bad.flac")
        args = autodj_args(require_section_analysis=True)

        accepted, rejected = filter_defensible_source_tracks([bad, good], {good.path: analysis(good.path)}, args)

        self.assertEqual([track.path for track in accepted], [good.path])
        self.assertEqual(rejected[0]["path"], bad.path)

    def test_legacy_filter_rides_do_not_satisfy_vanilla_guard(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-2"],
                        "clips": [
                            {
                                "id": "lead",
                                "deck": "deck-2",
                                "path": "/music/lead.flac",
                                "start_ms": 0,
                                "duration_ms": 240_000,
                                "planner_role": "lead",
                            }
                        ],
                        "deck_automations": [
                            {
                                "target": "deck-2",
                                "param": "highpass_hz",
                                "source_clip_id": "lead",
                                "planner_role": "autodj-lead-filter-ride",
                                "points": [
                                    {"at_ms": 69_000, "value": 30},
                                    {"at_ms": 75_000, "value": 220},
                                    {"at_ms": 85_000, "value": 30},
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(SystemExit):
                validate_no_vanilla_leads(
                    session_path,
                    SimpleNamespace(min_vanilla_check_ms=90_000, max_vanilla_lead_ms=90_000),
                )

    def test_action_load_tracks_without_material_moves_do_not_satisfy_vanilla_guard(self):
        stems = {
            "vocals": {"path": "/stems/vocals.wav"},
            "drums": {"path": "/stems/drums.wav"},
            "bass": {"path": "/stems/bass.wav"},
            "other": {"path": "/stems/other.wav"},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1", "deck-2"],
                        "actions": [
                            {
                                "type": "load_track",
                                "id": "lead-a",
                                "deck": "deck-1",
                                "source_path": "/music/a.flac",
                                "at_ms": 0,
                                "duration_ms": 120_000,
                                "stems": stems,
                                "planner_role": "keyfixed-full-no-overlap",
                            },
                            {
                                "type": "knob_lerp",
                                "id": "lead-a-filter-open",
                                "target": "deck-1",
                                "param": "lowpass_hz",
                                "at_ms": 0,
                                "duration_ms": 20_000,
                                "from": 2400,
                                "to": 15000,
                            },
                            {
                                "type": "knob_lerp",
                                "id": "lead-a-filter-exit",
                                "target": "deck-1",
                                "param": "highpass_hz",
                                "at_ms": 105_000,
                                "duration_ms": 12_000,
                                "from": 70,
                                "to": 420,
                            },
                            {
                                "type": "load_track",
                                "id": "lead-b",
                                "deck": "deck-2",
                                "source_path": "/music/b.flac",
                                "at_ms": 121_000,
                                "duration_ms": 120_000,
                                "stems": stems,
                                "planner_role": "keyfixed-full-no-overlap",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(SystemExit) as raised:
                validate_no_vanilla_leads(
                    session_path,
                    SimpleNamespace(min_vanilla_check_ms=90_000, max_vanilla_lead_ms=90_000),
                )

        self.assertIn("no material DJ move", str(raised.exception))

    def test_action_load_track_with_rhythm_bed_satisfies_vanilla_guard(self):
        full_stems = {
            "vocals": {"path": "/stems/vocals.wav"},
            "drums": {"path": "/stems/drums.wav"},
            "bass": {"path": "/stems/bass.wav"},
            "other": {"path": "/stems/other.wav"},
        }
        bed_stems = {
            "vocals": {"path": "/stems/bed-vocals.wav", "enabled": False},
            "drums": {"path": "/stems/bed-drums.wav"},
            "bass": {"path": "/stems/bed-bass.wav"},
            "other": {"path": "/stems/bed-other.wav"},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1", "deck-2"],
                        "actions": [
                            {
                                "type": "load_track",
                                "id": "lead",
                                "deck": "deck-1",
                                "source_path": "/music/lead.flac",
                                "at_ms": 0,
                                "duration_ms": 120_000,
                                "stems": full_stems,
                                "planner_role": "lead",
                            },
                            {
                                "type": "load_track",
                                "id": "bed",
                                "deck": "deck-2",
                                "source_path": "/music/bed.flac",
                                "at_ms": 40_000,
                                "duration_ms": 48_000,
                                "stems": bed_stems,
                                "play_stems": ["drums", "bass", "other"],
                                "planner_role": "rhythm-bed",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result = validate_no_vanilla_leads(
                session_path,
                SimpleNamespace(min_vanilla_check_ms=90_000, max_vanilla_lead_ms=90_000),
            )

        self.assertEqual(result["checked"], 1)

    def test_harmonic_guard_rejects_unshifted_vocal_over_mismatched_bed(self):
        stems = {
            "vocals": {"path": "/stems/vocals.wav"},
            "drums": {"path": "/stems/drums.wav"},
            "bass": {"path": "/stems/bass.wav"},
            "other": {"path": "/stems/other.wav"},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1", "deck-2"],
                        "actions": [
                            {
                                "type": "load_track",
                                "id": "ipod-bed",
                                "deck": "deck-1",
                                "source_path": "/music/ipod.flac",
                                "at_ms": 0,
                                "duration_ms": 120_000,
                                "stems": stems,
                                "play_stems": ["drums", "bass", "other"],
                                "planner_role": "lead",
                                "tonic": 5,
                                "mode": "minor",
                                "key": "F minor",
                            },
                            {
                                "type": "load_track",
                                "id": "digits-vocal",
                                "deck": "deck-2",
                                "source_path": "/music/digits.mp3",
                                "at_ms": 16_000,
                                "duration_ms": 32_000,
                                "stems": stems,
                                "play_stems": ["vocals"],
                                "planner_role": "vocal-trade",
                                "tonic": 10,
                                "mode": "minor",
                                "key": "Bb minor",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(SystemExit) as raised:
                validate_harmonic_overlaps(
                    session_path,
                    SimpleNamespace(min_harmonic_overlap_ms=8_000),
                )

        self.assertIn("effective keys do not share", str(raised.exception))

    def test_harmonic_guard_rejects_short_mismatched_overlap_by_default(self):
        stems = {
            "vocals": {"path": "/stems/vocals.wav"},
            "drums": {"path": "/stems/drums.wav"},
            "bass": {"path": "/stems/bass.wav"},
            "other": {"path": "/stems/other.wav"},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1", "deck-2"],
                        "actions": [
                            {
                                "type": "load_track",
                                "id": "lead-a",
                                "deck": "deck-1",
                                "source_path": "/music/a.flac",
                                "at_ms": 0,
                                "duration_ms": 30_000,
                                "stems": stems,
                                "planner_role": "lead",
                                "tonic": 0,
                                "mode": "minor",
                                "key": "C minor",
                            },
                            {
                                "type": "load_track",
                                "id": "lead-b",
                                "deck": "deck-2",
                                "source_path": "/music/b.flac",
                                "at_ms": 28_000,
                                "duration_ms": 30_000,
                                "stems": stems,
                                "planner_role": "lead",
                                "tonic": 6,
                                "mode": "minor",
                                "key": "Gb minor",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(SystemExit) as raised:
                validate_harmonic_overlaps(session_path, SimpleNamespace())

        self.assertIn('"overlap_ms": 2000', str(raised.exception))

    def test_harmonic_guard_accepts_pitch_shifted_vocal_to_bed_key(self):
        stems = {
            "vocals": {"path": "/stems/vocals.wav"},
            "drums": {"path": "/stems/drums.wav"},
            "bass": {"path": "/stems/bass.wav"},
            "other": {"path": "/stems/other.wav"},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1", "deck-2"],
                        "actions": [
                            {
                                "type": "load_track",
                                "id": "ipod-bed",
                                "deck": "deck-1",
                                "source_path": "/music/ipod.flac",
                                "at_ms": 0,
                                "duration_ms": 120_000,
                                "stems": stems,
                                "play_stems": ["drums", "bass", "other"],
                                "planner_role": "lead",
                                "tonic": 5,
                                "mode": "minor",
                                "key": "F minor",
                            },
                            {
                                "type": "load_track",
                                "id": "digits-vocal",
                                "deck": "deck-2",
                                "source_path": "/music/digits.mp3",
                                "at_ms": 16_000,
                                "duration_ms": 32_000,
                                "stems": stems,
                                "play_stems": ["vocals"],
                                "planner_role": "vocal-trade",
                                "pitch_shift_semitones": -5,
                                "tonic": 10,
                                "mode": "minor",
                                "key": "Bb minor",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result = validate_harmonic_overlaps(
                session_path,
                SimpleNamespace(min_harmonic_overlap_ms=8_000),
            )

        self.assertEqual(result["checked"], 1)

    def test_harmonic_guard_rejects_zero_checked_overlap_when_required(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "clips": [
                            {
                                "id": "lead-a",
                                "path": "/music/a.flac",
                                "start_ms": 0,
                                "duration_ms": 90_000,
                                "planner_role": "lead",
                                "key": "C minor",
                            },
                            {
                                "id": "lead-b",
                                "path": "/music/b.flac",
                                "start_ms": 91_000,
                                "duration_ms": 90_000,
                                "planner_role": "lead",
                                "key": "G minor",
                            },
                        ],
                        "transition_plans": [
                            {
                                "from_clip_id": "lead-a",
                                "to_clip_id": "lead-b",
                                "decision": "hard cut",
                                "tempo_shift_pct": 0.0,
                                "pitch_shift_semitones": 0,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(SystemExit) as raised:
                validate_harmonic_overlaps(
                    session_path,
                    SimpleNamespace(min_harmonic_overlap_ms=500, min_harmonic_checks=1),
                )

        self.assertIn("no key-checked musical overlap", str(raised.exception))

    def test_transition_decision_guard_rejects_zero_shift_handoff_without_plan(self):
        stems = {
            "vocals": {"path": "/stems/vocals.wav"},
            "drums": {"path": "/stems/drums.wav"},
            "bass": {"path": "/stems/bass.wav"},
            "other": {"path": "/stems/other.wav"},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1", "deck-2"],
                        "actions": [
                            {
                                "type": "load_track",
                                "id": "lead-a",
                                "deck": "deck-1",
                                "source_path": "/music/a.flac",
                                "at_ms": 0,
                                "duration_ms": 120_000,
                                "stems": stems,
                                "planner_role": "lead",
                            },
                            {
                                "type": "load_track",
                                "id": "lead-b",
                                "deck": "deck-2",
                                "source_path": "/music/b.flac",
                                "at_ms": 118_000,
                                "duration_ms": 120_000,
                                "stems": stems,
                                "planner_role": "lead",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(SystemExit) as raised:
                validate_transition_decisions(session_path, SimpleNamespace())

        self.assertIn("missing explicit beat/key transition decision", str(raised.exception))

    def test_transition_decision_guard_accepts_explicit_exact_match_plan(self):
        stems = {
            "vocals": {"path": "/stems/vocals.wav"},
            "drums": {"path": "/stems/drums.wav"},
            "bass": {"path": "/stems/bass.wav"},
            "other": {"path": "/stems/other.wav"},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1", "deck-2"],
                        "actions": [
                            {
                                "type": "load_track",
                                "id": "lead-a",
                                "deck": "deck-1",
                                "source_path": "/music/a.flac",
                                "at_ms": 0,
                                "duration_ms": 120_000,
                                "stems": stems,
                                "planner_role": "lead",
                            },
                            {
                                "type": "load_track",
                                "id": "lead-b",
                                "deck": "deck-2",
                                "source_path": "/music/b.flac",
                                "at_ms": 118_000,
                                "duration_ms": 120_000,
                                "stems": stems,
                                "planner_role": "lead",
                            },
                        ],
                        "transition_plans": [
                            {
                                "id": "transition-lead-b",
                                "planner_role": "mix-planner-transition-plan",
                                "from_clip_id": "lead-a",
                                "to_clip_id": "lead-b",
                                "decision": "blend",
                                "tempo_shift_pct": 0.0,
                                "pitch_shift_semitones": 0,
                                "reason": "explicit exact bpm/key match",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result = validate_transition_decisions(session_path, SimpleNamespace())

        self.assertEqual(result["checked"], 1)

    def test_transition_decision_guard_accepts_manual_nonzero_transform(self):
        stems = {
            "vocals": {"path": "/stems/vocals.wav"},
            "drums": {"path": "/stems/drums.wav"},
            "bass": {"path": "/stems/bass.wav"},
            "other": {"path": "/stems/other.wav"},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1", "deck-2"],
                        "actions": [
                            {
                                "type": "load_track",
                                "id": "lead-a",
                                "deck": "deck-1",
                                "source_path": "/music/a.flac",
                                "at_ms": 0,
                                "duration_ms": 120_000,
                                "stems": stems,
                                "planner_role": "lead",
                            },
                            {
                                "type": "load_track",
                                "id": "lead-b",
                                "deck": "deck-2",
                                "source_path": "/music/b.flac",
                                "at_ms": 118_000,
                                "duration_ms": 120_000,
                                "stems": stems,
                                "planner_role": "lead",
                                "tempo_shift_pct": 2.5,
                                "pitch_shift_semitones": -1,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result = validate_transition_decisions(session_path, SimpleNamespace())

        self.assertEqual(result["checked"], 1)

    def test_stem_load_guard_rejects_database_music_track_clip_events(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            with sqlite3.connect(db_path) as db:
                db.execute("CREATE TABLE files(path TEXT PRIMARY KEY)")
                db.execute("INSERT INTO files(path) VALUES (?)", ("/music/a.flac",))
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1", "deck-2"],
                        "clips": [
                            {
                                "id": "lead-a",
                                "deck": "deck-1",
                                "path": "/music/a.flac",
                                "start_ms": 0,
                                "duration_ms": 120_000,
                                "planner_role": "lead",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(SystemExit) as raised:
                validate_stem_load_usage(session_path, SimpleNamespace(require_stem_loads=True, db=db_path))

        message = str(raised.exception)
        self.assertIn("database music tracks must use load_track", message)
        self.assertIn("clip events are reserved", message)

    def test_stem_load_guard_allows_non_musical_sample_clip(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            with sqlite3.connect(db_path) as db:
                db.execute("CREATE TABLE files(path TEXT PRIMARY KEY)")
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1", "deck-2"],
                        "clips": [
                            {
                                "id": "drop-a",
                                "deck": "deck-4",
                                "path": "/samples/airhorn.wav",
                                "start_ms": 0,
                                "duration_ms": 2_000,
                                "planner_role": "sample-drop",
                            }
                        ],
                        "actions": [
                            {
                                "type": "load_track",
                                "id": "lead-a",
                                "deck": "deck-1",
                                "source_path": "/music/a.flac",
                                "at_ms": 0,
                                "duration_ms": 120_000,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result = validate_stem_load_usage(session_path, SimpleNamespace(require_stem_loads=True, db=db_path))

        self.assertEqual(result["checked"], 1)

    def test_stem_load_guard_accepts_load_track_actions(self):
        stems = {
            "vocals": {"path": "/stems/vocals.wav"},
            "drums": {"path": "/stems/drums.wav"},
            "bass": {"path": "/stems/bass.wav"},
            "other": {"path": "/stems/other.wav"},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1", "deck-2"],
                        "actions": [
                            {
                                "type": "load_track",
                                "id": "lead-a",
                                "deck": "deck-1",
                                "source_path": "/music/a.flac",
                                "at_ms": 0,
                                "duration_ms": 120_000,
                                "stems": stems,
                                "planner_role": "lead",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result = validate_stem_load_usage(session_path, SimpleNamespace(require_stem_loads=True, db=Path("/missing/library.sqlite3")))

        self.assertEqual(result["checked"], 1)

    def test_vocal_guard_rejects_unplanned_vocal_overlap(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "decks": ["deck-1", "deck-2"],
                        "actions": [
                            {
                                "type": "load_track",
                                "id": "lead-a",
                                "deck": "deck-1",
                                "source_path": "/music/lead-a.flac",
                                "at_ms": 0,
                                "duration_ms": 30_000,
                                "planner_role": "lead",
                            },
                            {
                                "type": "load_track",
                                "id": "lead-b",
                                "deck": "deck-2",
                                "source_path": "/music/lead-b.flac",
                                "at_ms": 10_000,
                                "duration_ms": 30_000,
                                "planner_role": "lead",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(SystemExit) as raised:
                validate_vocal_guards(session_path, SimpleNamespace(db=Path("/missing/db.sqlite3"), analysis_cache=Path("/missing/cache.json")))

        self.assertIn("vocal overlap guard failed", str(raised.exception))


class SlimeAudioAutodjExtendTests(unittest.TestCase):
    @staticmethod
    def _live_session_payload() -> dict:
        return {
            "version": 1,
            "decks": ["deck-1", "deck-2"],
            "clips": [
                {"id": "lead-a", "deck": "deck-1", "path": "/music/a.flac", "start_ms": 0, "duration_ms": 60_000},
                {"id": "lead-b", "deck": "deck-2", "path": "/music/b.flac", "start_ms": 60_000, "duration_ms": 60_000},
            ],
        }

    def _extend_args(self, temp: Path, session_path: Path, state_path: Path, *extra: str):
        argv = [
            "slime_audio_autodj.py",
            "extend",
            "--session",
            str(session_path),
            "--state",
            str(state_path),
            "--runtime",
            str(temp / "runtime"),
            "--history",
            str(temp / "history.jsonl"),
            "--pause-file",
            str(temp / "pause"),
            "--constraints",
            str(temp / "constraints.json"),
            "--db",
            str(temp / "library.sqlite3"),
            "--no-require-section-analysis",
            "--no-analyze-missing-sections",
            "--min-tracks",
            "1",
            "--max-tracks",
            "2",
            *extra,
        ]
        with patch.object(sys, "argv", argv):
            return autodj.parse_args()

    def test_extend_noops_when_enough_runway_remains(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            session_path = temp / "live.json"
            state_path = temp / "live-state.json"
            session_path.write_text(json.dumps(self._live_session_payload()), encoding="utf-8")
            state_path.write_text(json.dumps({"playhead_ms": 10_000}), encoding="utf-8")
            args = self._extend_args(temp, session_path, state_path, "--ahead-ms", "60000", "--target-length-ms", "0")

            from io import StringIO
            from contextlib import redirect_stdout

            output = StringIO()
            with patch.object(autodj, "select_tracks", side_effect=AssertionError("selection should not run")):
                with redirect_stdout(output):
                    self.assertEqual(autodj.extend_set(args), 0)

            self.assertIn("enough runway ahead", output.getvalue())

    def test_extend_noops_at_target_length(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            session_path = temp / "live.json"
            state_path = temp / "live-state.json"
            session_path.write_text(json.dumps(self._live_session_payload()), encoding="utf-8")
            state_path.write_text(json.dumps({"playhead_ms": 115_000}), encoding="utf-8")
            args = self._extend_args(temp, session_path, state_path, "--target-length-ms", "100000")

            from io import StringIO
            from contextlib import redirect_stdout

            output = StringIO()
            with patch.object(autodj, "select_tracks", side_effect=AssertionError("selection should not run")):
                with redirect_stdout(output):
                    self.assertEqual(autodj.extend_set(args), 0)

            self.assertIn("target length reached", output.getvalue())

    def _pipeline_patches(self, selected):
        captured: dict = {}

        def fake_select(args, *, exclude_paths=None):
            captured["exclude_paths"] = set(exclude_paths or set())
            return selected

        return captured, [
            patch.object(autodj, "select_tracks", side_effect=fake_select),
            patch.object(autodj, "load_or_analyze_selected", return_value={}),
            patch.object(autodj, "run_planner", return_value={"returncode": 0, "stdout": "", "stderr": "", "command": []}),
            patch.object(autodj, "add_structural_beds", return_value={"added": 0}),
            patch.object(autodj, "apply_creative_pass", return_value={"required": True, "moves": [], "failures": []}),
            patch.object(autodj, "validate_no_vanilla_leads", return_value={}),
            patch.object(autodj, "validate_stem_load_usage", return_value={}),
            patch.object(autodj, "validate_transition_decisions", return_value={}),
            patch.object(autodj, "validate_harmonic_overlaps", return_value={}),
            patch.object(autodj, "validate_vocal_guards", return_value={}),
            patch.object(autodj, "validate_component_bed_balance", return_value={}),
        ]

    def test_extend_appends_prefixed_block_after_session_end(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            session_path = temp / "live.json"
            state_path = temp / "live-state.json"
            session_path.write_text(json.dumps(self._live_session_payload()), encoding="utf-8")
            state_path.write_text(json.dumps({"playhead_ms": 30_000, "window_end_ms": 60_000}), encoding="utf-8")
            args = self._extend_args(temp, session_path, state_path, "--ahead-ms", "600000", "--target-length-ms", "0")
            selected = [
                SelectedTrack(
                    path="/music/c.flac",
                    artist="artist-c",
                    title="track-c",
                    album="",
                    score=1.0,
                    duration_ms=240_000,
                    last_played_at=None,
                    plays_seen=0,
                    reasons=[],
                )
            ]

            from io import StringIO
            from contextlib import redirect_stdout

            captured, patches = self._pipeline_patches(selected)
            from contextlib import ExitStack

            with ExitStack() as stack:
                for item in patches:
                    stack.enter_context(item)
                with redirect_stdout(StringIO()):
                    self.assertEqual(autodj.extend_set(args), 0)

            self.assertEqual(captured["exclude_paths"], {"/music/a.flac", "/music/b.flac"})
            published = json.loads(session_path.read_text(encoding="utf-8"))
            new_clips = [clip for clip in published["clips"] if clip["path"] == "/music/c.flac"]
            self.assertEqual(len(new_clips), 1)
            self.assertEqual(new_clips[0]["start_ms"], 120_000)
            self.assertTrue(str(new_clips[0]["id"]).startswith("ext-"))
            self.assertEqual(len(published["notes"]["extensions"]), 1)
            history_lines = [json.loads(line) for line in (temp / "history.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertIn("autodj_set_extended", {line.get("event") for line in history_lines})

    def test_extend_refuses_to_publish_over_concurrent_live_edit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            session_path = temp / "live.json"
            state_path = temp / "live-state.json"
            session_path.write_text(json.dumps(self._live_session_payload()), encoding="utf-8")
            state_path.write_text(json.dumps({"playhead_ms": 30_000}), encoding="utf-8")
            args = self._extend_args(temp, session_path, state_path, "--ahead-ms", "600000", "--target-length-ms", "0")
            selected = [
                SelectedTrack(
                    path="/music/c.flac",
                    artist="artist-c",
                    title="track-c",
                    album="",
                    score=1.0,
                    duration_ms=240_000,
                    last_played_at=None,
                    plays_seen=0,
                    reasons=[],
                )
            ]
            concurrent = self._live_session_payload()
            concurrent["clips"].append(
                {"id": "live-edit", "deck": "deck-1", "path": "/music/d.flac", "start_ms": 120_000, "duration_ms": 30_000}
            )

            def planner_with_live_edit(*_args, **_kwargs):
                session_path.write_text(json.dumps(concurrent), encoding="utf-8")
                return {"returncode": 0, "stdout": "", "stderr": "", "command": []}

            from io import StringIO
            from contextlib import ExitStack, redirect_stdout

            captured, patches = self._pipeline_patches(selected)
            patches[2] = patch.object(autodj, "run_planner", side_effect=planner_with_live_edit)
            with ExitStack() as stack:
                for item in patches:
                    stack.enter_context(item)
                with redirect_stdout(StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        autodj.extend_set(args)

            self.assertIn("changed while the extension was being built", str(raised.exception))
            preserved = json.loads(session_path.read_text(encoding="utf-8"))
            self.assertEqual(len(preserved["clips"]), 3)

    def test_merge_block_shifts_lean_ins_and_merges_decks(self):
        payload = self._live_session_payload()
        block = {
            "decks": ["deck-2", "deck-4"],
            "clips": [{"id": "x", "deck": "deck-2", "path": "/music/x.flac", "start_ms": 0, "duration_ms": 30_000}],
            "actions": [],
            "transition_plans": [],
            "deck_automations": [],
            "mic_lean_ins": [
                {
                    "id": "drop-1",
                    "deck": "deck-5",
                    "start": "00:12.000",
                    "text": "hello",
                    "ducking": {"points": [{"at": 11_750, "value": 0.4}, {"at": 15_000, "value": 1.0}]},
                }
            ],
            "fader_routing": {"deck_assignments": {"deck-4": "THRU"}},
        }
        merged = autodj.merge_block_into_payload(payload, block, offset_ms=120_000)

        self.assertEqual(merged["decks"], ["deck-1", "deck-2", "deck-4"])
        self.assertEqual(merged["fader_routing"]["deck_assignments"]["deck-4"], "THRU")
        self.assertEqual(merged["clips"][-1]["start_ms"], 120_000)
        lean = merged["mic_lean_ins"][-1]
        self.assertEqual(lean["start"], "02:12.000")
        self.assertEqual(lean["ducking"]["points"][0]["at"], 131_750)

    def test_prefix_block_ids_rewrites_transition_plan_refs(self):
        block = {
            "clips": [{"id": "lead-001", "deck": "deck-2", "path": "/music/x.flac", "start_ms": 0}],
            "actions": [{"id": "lead-002", "type": "load_track"}],
            "mic_lean_ins": [{"id": "autodj-vocal-drop-001"}],
            "transition_plans": [{"id": "transition-lead-002", "from_clip_id": "lead-001", "to_clip_id": "lead-002", "to_action_id": "lead-002"}],
        }
        autodj.prefix_block_ids(block, "ext-42")

        self.assertEqual(block["clips"][0]["id"], "ext-42-lead-001")
        self.assertEqual(block["mic_lean_ins"][0]["id"], "ext-42-autodj-vocal-drop-001")
        plan = block["transition_plans"][0]
        self.assertEqual(plan["from_clip_id"], "ext-42-lead-001")
        self.assertEqual(plan["to_action_id"], "ext-42-lead-002")


if __name__ == "__main__":
    unittest.main()
