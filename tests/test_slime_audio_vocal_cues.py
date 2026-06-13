import math
import struct
import sys
import tempfile
import unittest
import wave
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from slime_audio_vocal_cues import (
    VocalCue,
    align_session_payload,
    alignment_plan,
    audit_vocal_alignment_payload,
    audit_vocal_overlap_payload,
    detect_drum_hits,
    detect_vocal_cues,
)


def write_silence_tone_silence(path: Path, *, silence_a_ms: int, tone_ms: int, silence_b_ms: int, sample_rate: int = 8000) -> None:
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        payload = bytearray()
        for _ in range(int(sample_rate * silence_a_ms / 1000)):
            payload.extend(struct.pack("<h", 0))
        for index in range(int(sample_rate * tone_ms / 1000)):
            value = int(32767 * 0.35 * math.sin(2 * math.pi * 440 * index / sample_rate))
            payload.extend(struct.pack("<h", value))
        for _ in range(int(sample_rate * silence_b_ms / 1000)):
            payload.extend(struct.pack("<h", 0))
        audio.writeframes(bytes(payload))


def write_pulse_train(path: Path, *, pulses_ms: list[int], duration_ms: int, pulse_ms: int = 120, sample_rate: int = 8000) -> None:
    total_samples = int(sample_rate * duration_ms / 1000)
    samples = [0] * total_samples
    for pulse_start_ms in pulses_ms:
        pulse_start = int(sample_rate * pulse_start_ms / 1000)
        pulse_len = int(sample_rate * pulse_ms / 1000)
        for offset in range(pulse_len):
            index = pulse_start + offset
            if 0 <= index < total_samples:
                samples[index] = int(32767 * 0.45 * math.sin(2 * math.pi * 90 * offset / sample_rate))
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(b"".join(struct.pack("<h", sample) for sample in samples))


class SlimeAudioVocalCueTests(unittest.TestCase):
    def test_detects_vocal_entry_and_hook_candidate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "vocals.wav"
            write_silence_tone_silence(path, silence_a_ms=1500, tone_ms=2500, silence_b_ms=1000)

            cues = detect_vocal_cues(path, sample_rate=8000)

        entry = next(cue for cue in cues if cue.kind == "vocal_entry")
        hook = next(cue for cue in cues if cue.kind == "hook_candidate")
        self.assertTrue(1450 <= entry.at_ms <= 1550)
        self.assertEqual(entry.at_ms, hook.at_ms)
        self.assertGreaterEqual(hook.end_ms - hook.at_ms, 2000)

    def test_alignment_plan_lands_cue_on_target_drop(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "vocals.wav"
            write_silence_tone_silence(path, silence_a_ms=2000, tone_ms=2500, silence_b_ms=1000)
            cues = detect_vocal_cues(path, sample_rate=8000)

        plan = alignment_plan(cues, target_drop_ms=64000, pre_roll_ms=1200)

        self.assertEqual(plan["recommended_trim_start_ms"], plan["cue_at_ms"] - 1200)
        self.assertEqual(plan["recommended_at_ms"] + 1200, 64000)

    def test_alignment_plan_allows_pickup_vocal_before_phrase_anchor(self):
        cue = VocalCue("hook_candidate", 2_000, 5_000, 0.8, "test pickup vocal")
        drum_hit = VocalCue("drum_hit", 2_450, 2_560, 0.9, "phrase downbeat")

        plan = alignment_plan([cue], target_drop_ms=64_000, pre_roll_ms=200, drum_hits=[drum_hit])

        self.assertEqual(plan["phrase_anchor_ms"], 2_450)
        self.assertEqual(plan["vocal_lead_in_ms"], 450)
        self.assertEqual(plan["recommended_trim_start_ms"], 1_800)
        self.assertEqual(plan["recommended_at_ms"] + (2_450 - 1_800), 64_000)
        self.assertEqual(plan["recommended_at_ms"] + (2_000 - 1_800), 63_550)

    def test_alignment_plan_accounts_for_tempo_shift(self):
        cue = VocalCue("hook_candidate", 2_000, 5_000, 0.8, "test vocal")
        drum_hit = VocalCue("drum_hit", 2_800, 2_900, 0.9, "phrase downbeat")

        plan = alignment_plan([cue], target_drop_ms=64_000, pre_roll_ms=1_000, drum_hits=[drum_hit], tempo_shift_pct=10.0)

        self.assertEqual(plan["rendered_anchor_offset_ms"], round((2_800 - 1_000) / 1.1))
        self.assertEqual(plan["recommended_at_ms"] + plan["rendered_anchor_offset_ms"], 64_000)

    def test_alignment_plan_prefers_cue_near_existing_trim(self):
        cues = [
            VocalCue("hook_candidate", 2_000, 5_000, 0.8, "first hook"),
            VocalCue("hook_candidate", 54_000, 58_000, 0.7, "intended later hook"),
        ]

        plan = alignment_plan(cues, target_drop_ms=64_000, pre_roll_ms=1_000, preferred_cue_ms=54_200)

        self.assertEqual(plan["cue_at_ms"], 54_000)
        self.assertEqual(plan["recommended_trim_start_ms"], 53_000)

    def test_detects_drum_hit_for_phrase_anchor(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "drums.wav"
            write_pulse_train(path, pulses_ms=[2450], duration_ms=5000)

            hits = detect_drum_hits(path, sample_rate=8000)

        self.assertTrue(any(2400 <= hit.at_ms <= 2500 for hit in hits))

    def test_align_session_moves_vocal_load_and_companion_actions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "vocals.wav"
            write_silence_tone_silence(path, silence_a_ms=2000, tone_ms=2500, silence_b_ms=1000)
            payload = {
                "actions": [
                    {
                        "type": "load_track",
                        "id": "vox",
                        "at_ms": 10_000,
                        "trim_start_ms": 0,
                        "planner_role": "vocal-hook",
                        "stems": {"vocals": {"path": str(path)}},
                    },
                    {"type": "knob_lerp", "id": "vox-hp-exit", "target": "vox", "at_ms": 20_000},
                    {"type": "stem_toggle", "id": "vox-mute-after", "target": "vox", "at_ms": 30_000},
                ]
            }

            aligned, report = align_session_payload(
                payload,
                block_ms=128_000,
                drop_offset_ms=64_000,
                pre_roll_ms=1_200,
                sample_rate=8000,
            )

        load = next(action for action in aligned["actions"] if action["id"] == "vox")
        moved_exit = next(action for action in aligned["actions"] if action["id"] == "vox-hp-exit")
        moved_mute = next(action for action in aligned["actions"] if action["id"] == "vox-mute-after")
        delta = report[0]["delta_ms"]
        self.assertEqual(load["at_ms"] + 1_200, 64_000)
        self.assertEqual(load["trim_start_ms"], report[0]["recommended_trim_start_ms"])
        self.assertEqual(moved_exit["at_ms"], 20_000 + delta)
        self.assertEqual(moved_mute["at_ms"], 30_000 + delta)

    def test_align_session_handles_vocal_entering_after_source_drum_anchor(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            vocal_path = Path(temp_dir) / "vocals.wav"
            drum_path = Path(temp_dir) / "drums.wav"
            write_silence_tone_silence(vocal_path, silence_a_ms=3000, tone_ms=2500, silence_b_ms=1000)
            write_pulse_train(drum_path, pulses_ms=[2000], duration_ms=6500)
            payload = {
                "actions": [
                    {
                        "type": "load_track",
                        "id": "bed",
                        "at_ms": 0,
                        "trim_start_ms": 0,
                        "duration_ms": 96_000,
                        "planner_role": "backing",
                        "beatgrid": {"bpm": 120, "beat_offset_ms": 0, "phrase_beats": 32, "phrase_ms": 16_000},
                    },
                    {
                        "type": "load_track",
                        "id": "vox",
                        "at_ms": 10_000,
                        "trim_start_ms": 0,
                        "duration_ms": 16_000,
                        "planner_role": "vocal-hook",
                        "vocal_pre_roll_ms": 200,
                        "stems": {
                            "vocals": {"path": str(vocal_path)},
                            "drums": {"path": str(drum_path)},
                        },
                    },
                ]
            }

            aligned, report = align_session_payload(
                payload,
                block_ms=128_000,
                drop_offset_ms=64_000,
                pre_roll_ms=200,
                sample_rate=8000,
            )

        load = next(action for action in aligned["actions"] if action["id"] == "vox")
        plan = report[0]
        anchor_timeline_ms = load["at_ms"] + round((plan["phrase_anchor_ms"] - load["trim_start_ms"]) / plan["tempo_factor"])
        vocal_entry_timeline_ms = load["at_ms"] + round((plan["cue_at_ms"] - load["trim_start_ms"]) / plan["tempo_factor"])
        audit = audit_vocal_alignment_payload(aligned, db_path=None, cache_path=None)

        self.assertEqual(plan["phrase_anchor_kind"], "drum_hit")
        self.assertTrue(1950 <= plan["phrase_anchor_ms"] <= 2050)
        self.assertTrue(2950 <= plan["cue_at_ms"] <= 3050)
        self.assertEqual(anchor_timeline_ms, 64_000)
        self.assertTrue(950 <= vocal_entry_timeline_ms - anchor_timeline_ms <= 1050)
        self.assertTrue(audit["ok"])
        self.assertEqual(audit["vocals"][0]["anchor_timeline_ms"], 64_000)

    def test_align_session_handles_vocal_entering_before_source_drum_anchor(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            vocal_path = Path(temp_dir) / "vocals.wav"
            drum_path = Path(temp_dir) / "drums.wav"
            write_silence_tone_silence(vocal_path, silence_a_ms=1000, tone_ms=2500, silence_b_ms=3000)
            write_pulse_train(drum_path, pulses_ms=[2000], duration_ms=6500)
            payload = {
                "actions": [
                    {
                        "type": "load_track",
                        "id": "bed",
                        "at_ms": 0,
                        "trim_start_ms": 0,
                        "duration_ms": 96_000,
                        "planner_role": "backing",
                        "beatgrid": {"bpm": 120, "beat_offset_ms": 0, "phrase_beats": 32, "phrase_ms": 16_000},
                    },
                    {
                        "type": "load_track",
                        "id": "vox",
                        "at_ms": 10_000,
                        "trim_start_ms": 0,
                        "duration_ms": 16_000,
                        "planner_role": "vocal-hook",
                        "vocal_pre_roll_ms": 200,
                        "stems": {
                            "vocals": {"path": str(vocal_path)},
                            "drums": {"path": str(drum_path)},
                        },
                    },
                ]
            }

            aligned, report = align_session_payload(
                payload,
                block_ms=128_000,
                drop_offset_ms=64_000,
                pre_roll_ms=200,
                sample_rate=8000,
            )

        load = next(action for action in aligned["actions"] if action["id"] == "vox")
        plan = report[0]
        anchor_timeline_ms = load["at_ms"] + round((plan["phrase_anchor_ms"] - load["trim_start_ms"]) / plan["tempo_factor"])
        vocal_entry_timeline_ms = load["at_ms"] + round((plan["cue_at_ms"] - load["trim_start_ms"]) / plan["tempo_factor"])
        audit = audit_vocal_alignment_payload(aligned, db_path=None, cache_path=None)

        self.assertEqual(plan["phrase_anchor_kind"], "drum_hit")
        self.assertTrue(1950 <= plan["phrase_anchor_ms"] <= 2050)
        self.assertTrue(950 <= plan["cue_at_ms"] <= 1050)
        self.assertEqual(anchor_timeline_ms, 64_000)
        self.assertTrue(-1050 <= vocal_entry_timeline_ms - anchor_timeline_ms <= -950)
        self.assertTrue(audit["ok"])
        self.assertEqual(audit["vocals"][0]["anchor_timeline_ms"], 64_000)

    def test_default_alignment_includes_decorated_vocal_hook_roles(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "vocals.wav"
            write_silence_tone_silence(path, silence_a_ms=2000, tone_ms=2500, silence_b_ms=1000)
            payload = {
                "actions": [
                    {
                        "type": "load_track",
                        "id": "vox",
                        "at_ms": 10_000,
                        "trim_start_ms": 0,
                        "planner_role": "fresh-vocal-hook-keymatched",
                        "stems": {"vocals": {"path": str(path)}},
                    },
                ]
            }

            aligned, report = align_session_payload(
                payload,
                block_ms=128_000,
                drop_offset_ms=64_000,
                pre_roll_ms=1_200,
                sample_rate=8000,
            )

        load = aligned["actions"][0]
        self.assertEqual(len(report), 1)
        self.assertEqual(load["at_ms"] + 1_200, 64_000)
        self.assertEqual(load["vocal_alignment"]["tempo_shift_pct"], 0.0)

    def test_vocal_alignment_audit_passes_when_anchor_lands_on_backing_phrase(self):
        payload = {
            "actions": [
                {
                    "type": "load_track",
                    "id": "bed",
                    "at_ms": 0,
                    "trim_start_ms": 0,
                    "duration_ms": 80_000,
                    "planner_role": "backing",
                    "beatgrid": {"bpm": 120, "beat_offset_ms": 0, "phrase_beats": 32, "phrase_ms": 16_000},
                },
                {
                    "type": "load_track",
                    "id": "vox",
                    "at_ms": 15_000,
                    "trim_start_ms": 1_000,
                    "duration_ms": 20_000,
                    "planner_role": "fresh-vocal-hook-keymatched",
                    "play_stems": ["vocals"],
                    "vocal_alignment": {"phrase_anchor_ms": 2_000},
                },
            ]
        }

        report = audit_vocal_alignment_payload(payload, db_path=None)

        self.assertTrue(report["ok"])
        self.assertEqual(report["vocals"][0]["anchor_timeline_ms"], 16_000)
        self.assertEqual(report["vocals"][0]["beat_delta_ms"], 0)
        self.assertEqual(report["vocals"][0]["phrase_delta_ms"], 0)

    def test_vocal_alignment_audit_fails_when_anchor_drifts_off_backing_grid(self):
        payload = {
            "actions": [
                {
                    "type": "load_track",
                    "id": "bed",
                    "at_ms": 0,
                    "trim_start_ms": 0,
                    "duration_ms": 80_000,
                    "planner_role": "backing",
                    "beatgrid": {"bpm": 120, "beat_offset_ms": 0, "phrase_beats": 32, "phrase_ms": 16_000},
                },
                {
                    "type": "load_track",
                    "id": "vox",
                    "at_ms": 15_130,
                    "trim_start_ms": 1_000,
                    "duration_ms": 20_000,
                    "planner_role": "vocal-hook",
                    "vocal_alignment": {"phrase_anchor_ms": 2_000},
                },
            ]
        }

        report = audit_vocal_alignment_payload(payload, db_path=None)

        self.assertFalse(report["ok"])
        self.assertEqual(report["vocals"][0]["status"], "fail")
        self.assertEqual(report["vocals"][0]["beat_delta_ms"], 130)
        self.assertEqual(report["vocals"][0]["phrase_delta_ms"], 130)

    def test_vocal_alignment_audit_fails_missing_alignment_metadata(self):
        payload = {
            "actions": [
                {
                    "type": "load_track",
                    "id": "vox",
                    "at_ms": 10_000,
                    "trim_start_ms": 1_000,
                    "planner_role": "fresh-vocal-hook-keymatched",
                    "play_stems": ["vocals"],
                },
            ]
        }

        report = audit_vocal_alignment_payload(payload, db_path=None)

        self.assertFalse(report["ok"])
        self.assertEqual(report["vocals"][0]["reason"], "missing vocal_alignment metadata")

    def test_vocal_overlap_audit_fails_full_song_overlap_by_default(self):
        payload = {
            "clips": [
                {
                    "id": "lead-a",
                    "deck": "deck-1",
                    "path": "/music/vocal-a.flac",
                    "start_ms": 0,
                    "duration_ms": 20_000,
                },
                {
                    "id": "lead-b",
                    "deck": "deck-2",
                    "path": "/music/vocal-b.flac",
                    "start_ms": 12_000,
                    "duration_ms": 20_000,
                },
            ]
        }

        report = audit_vocal_overlap_payload(payload, max_overlap_ms=500)

        self.assertFalse(report["ok"])
        self.assertEqual(report["failed"], 1)
        self.assertEqual(report["overlaps"][0]["overlap_ms"], 8_000)

    def test_vocal_overlap_audit_allows_instrumental_under_vocal(self):
        payload = {
            "clips": [
                {
                    "id": "lead",
                    "deck": "deck-1",
                    "path": "/music/vocal.flac",
                    "start_ms": 0,
                    "duration_ms": 20_000,
                },
                {
                    "id": "bed",
                    "deck": "deck-2",
                    "path": "/music/bed-instrumental.flac",
                    "start_ms": 12_000,
                    "duration_ms": 20_000,
                    "instrumental": True,
                },
            ]
        }

        report = audit_vocal_overlap_payload(payload, max_overlap_ms=500)

        self.assertTrue(report["ok"])
        self.assertEqual(report["checked"], 1)
        self.assertEqual(report["overlaps"], [])


if __name__ == "__main__":
    unittest.main()
