# Testing And QA

SlimeAudio has ordinary unit tests plus audio-specific proof workflows. Use the smallest test gate that covers the change, then widen when shared session/render behavior changes.

## Python Tests

Run the full Python suite from the repo root:

```bash
PYTHONPATH=src:scripts python3 -m unittest discover -s tests -v
```

Target a subsystem while iterating:

```bash
PYTHONPATH=src:scripts python3 -m unittest tests.test_slime_audio_session_mixdown -v
PYTHONPATH=src:scripts python3 -m unittest tests.test_slime_audio_live_edit -v
PYTHONPATH=src:scripts python3 -m unittest tests.test_slime_audio_web -v
```

Compile-check Python scripts when changing broad script behavior:

```bash
python3 -m py_compile scripts/*.py src/spotify_brain/*.py
```

## .NET Tests

```bash
dotnet test apps/SlimeAudio/SlimeAudio.sln
```

## Render Proofs

QA samples, review MP3s, and proof renders must go through the SlimeAudio session/planning/rendering tools. Do not hand-render behavior directly in ad hoc ffmpeg filter graphs when validating SlimeAudio features.

Examples:

```bash
python3 scripts/slime_audio_session_mixdown.py runtime/mix-session.json --from 01:10.000 --duration 00:45.000 --output runtime/mix-review.mp3 --format mp3 --verify
python3 scripts/slime_audio_session_mixdown.py runtime/mix-session.json --routine-id break-loop-stabs --output runtime/routine-proof.mp3 --format mp3 --report-output runtime/routine-proof.json --verify
python3 scripts/slime_audio_sets.py render --slug late-night-draft --format mp3 --mp3-bitrate 128k --keep 3 --max-total-mb 256
```

If a desired DJ move cannot be represented by the current session model, update the tools and [DJ skill](../skills/slime-audio-dj/SKILL.md) first, then render through the real engine.

Proofs should test the product behavior, not just DSP fragments:

- Echo bugs: validate with a simple arpeggio session and session `echo` event. Echo should produce discrete repeats, not recursive wobble.
- Brake bugs: validate with a simple tone or obvious source and a session `vinyl_brake`/brake routine. The dry source must mute during a replacement brake.
- Scratch bugs: validate with an attached effect lane on the source deck. Scratches should be source-replacing, sparse, and continuous.
- Volume cliffs: inspect `gain_db` automation before blaming effect DSP. A misplaced hard duck can sound like a broken effect.

When the operator asks for an audio proof, send the actual MP3/media attachment, not only a local path.

## Dashboard QA

Update dashboard fixtures when the web API/session view model changes:

- `tests/fixtures/slime-audio-web-active-session.json`
- `tests/fixtures/slime-audio-web-active-state.json`

Run:

```bash
PYTHONPATH=src:scripts python3 -m unittest tests.test_slime_audio_web -v
python3 scripts/slime_audio_web_smoke.py
```

The web smoke should exercise current schema features: trim, gain, EQ automation, EDM beds, attached effect lanes, echo/reverb/brake effects, slip events, crossfader routing, and API JSON error responses.

## Lean-In QA

Lean-in TTS failure must fail the render/playback pipeline unless `--skip-tts` was explicitly requested for command validation. Do not silently skip missing or silent TTS in live/review proofs.

Render at least one proof window with real TTS enabled before starting a hosted set, and reject unexplained music ducking without voice.

## Artifact Cleanup

The repo often runs with tight root disk space. Keep generated proofs small and clean up stale renders, temporary runner directories, and generated audio when no longer needed. Do not commit generated runtime audio unless it is intentionally preserved as a small fixture or review artifact.
