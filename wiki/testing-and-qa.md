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

## Dashboard QA

Update dashboard fixtures when the web API/session view model changes:

- `tests/fixtures/slime-audio-web-active-session.json`
- `tests/fixtures/slime-audio-web-active-state.json`

Run:

```bash
PYTHONPATH=src:scripts python3 -m unittest tests.test_slime_audio_web -v
python3 scripts/slime_audio_web_smoke.py
```

## Artifact Cleanup

The repo often runs with tight root disk space. Keep generated proofs small and clean up stale renders, temporary runner directories, and generated audio when no longer needed. Do not commit generated runtime audio unless it is intentionally preserved as a small fixture or review artifact.
