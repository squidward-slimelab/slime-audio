# Stem Management

SlimeAudio supports first-class 4-stem artifacts for DJ-focused vocal remixing and low-end control. Stems are optional: normal full-track sessions still work, and stem groups fall back to the source track when no stem artifact path is available.

## Artifact Layout

Stem artifacts live under `runtime/stems/<stem-set-id>/` by default:

```text
runtime/stems/<stem-set-id>/
  manifest.json
  vocals.wav
  drums.wav
  bass.wav
  other.wav
```

The stem-set id is derived from source path, duplicate key when known, source size, source mtime, model, profile, sample rate, and channel count. Changing the source file or separation settings produces a new id instead of reusing stale stems.

## Database Tables

`runtime/slime-music-library.sqlite3` now includes:

- `track_stem_sets`: one row per source/model/profile artifact set, with `ready`, `running`, or `failed` status.
- `track_stems`: one row per canonical stem with artifact path, loudness, peak, and basic confidence fields.
- `track_stem_windows`: planner-facing windows such as `vocal_present`, `vocal_absent`, `instrumental_pocket`, `bass_active`, and `drums_active`.

Stem analysis inherits the source track's musical grid. Do not infer independent BPM/key from individual stems unless doing explicit QA.

## CLI

Status for a path or duplicate key:

```bash
python3 scripts/slime_audio_stems.py status TRACK_OR_DUPLICATE_KEY
```

Split through Demucs. Real separation runs over SSH by default on `squidward@patrick` so this machine does not eat the CPU/GPU bill:

```bash
python3 scripts/slime_audio_stems.py split TRACK_OR_DUPLICATE_KEY --model htdemucs --profile 4stem --jobs 1
```

Override the remote host or force local execution:

```bash
SLIME_AUDIO_DEMUCS_HOST=squidward@robokrabs python3 scripts/slime_audio_stems.py split TRACK_OR_DUPLICATE_KEY
python3 scripts/slime_audio_stems.py split TRACK_OR_DUPLICATE_KEY --demucs-host squidward@robokrabs
python3 scripts/slime_audio_stems.py split TRACK_OR_DUPLICATE_KEY --local-demucs
```

If the remote host can read the same `/mnt/.../Music` path, the CLI runs Demucs against that shared path. If not, it rsyncs the source into a remote temp directory, runs Demucs there, then rsyncs only the separated output folder back before persisting artifacts locally.

Ingest an already-separated Demucs-style folder for tests or manual recovery:

```bash
python3 scripts/slime_audio_stems.py split ./source.wav --source-stems-dir /tmp/demucs/source
```

Recompute stem windows and verify artifacts:

```bash
python3 scripts/slime_audio_stems.py analyze TRACK_OR_STEM_SET_ID
python3 scripts/slime_audio_stems.py verify TRACK_OR_STEM_SET_ID
```

The CLI records failed separations in the DB instead of marking partial artifacts as ready.

## Session Stem Groups

Use `stem_groups` for one conceptual deck event with independently controlled child stems:

```json
{
  "stem_groups": [
    {
      "id": "vocal-hook",
      "deck": "deck-2",
      "source_path": "/mnt/rockhouse/Music/Artist/Track.flac",
      "manifest_path": "runtime/stems/abc123/manifest.json",
      "start_ms": 64000,
      "trim_start_ms": 32000,
      "duration_ms": 16000,
      "gain_db": -3,
      "stems": {
        "vocals": {"enabled": true, "gain_db": -1, "highpass_hz": 180},
        "drums": {"enabled": false},
        "bass": {"enabled": false},
        "other": {"enabled": false}
      }
    }
  ]
}
```

Supported per-stem controls are `gain_db`, `mute`, `solo`, `eq_low_db`, `eq_mid_db`, `eq_high_db`, `lowpass_hz`, `highpass_hz`, `send_echo`, and `send_reverb`. Rendering currently applies gain, mute/solo, EQ, and filters. Group-level `tempo_shift_pct`, `pitch_shift_semitones`, `reverse`, `playback_rate`, fades, deck automation, and crossfader routing apply to all child stems together so they stay locked.

Per-stem automation target format:

```text
stem-group:<group-id>:vocals.gain_db
stem-group:<group-id>:vocals.highpass_hz
stem-group:<group-id>:bass.mute
```

## Dashboard

The dashboard timeline surfaces stem groups as `stem-group` events on their parent deck and includes child stem state, enabled/muted/solo flags, gain, stem-set id, and manifest path. This is for debugging agent decisions, not manual DAW editing.

## Planner Rules

For vocal-heavy techno, dnb, and dubstep planning:

- Prefer a cached `vocals` stem over full-band EQ hacks.
- Use `vocal_present` and `vocal_absent` windows to avoid accidental vocal-on-vocal clashes.
- Use `instrumental_pocket` windows for acapella overlays.
- For double drops, keep only one `bass` stem active unless the routine explicitly chops or trades bass.
- Proof-render stem routines before live use when stem quality or timing is uncertain.
