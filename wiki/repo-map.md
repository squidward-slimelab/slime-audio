# Repo Map

## Root

- `README.md` is the public quick-start and broad overview.
- `AGENTS.md` defines agent rules for documentation, git hygiene, and SlimeAudio QA renders.
- `pyproject.toml` packages the Python `spotify_brain` module and test tooling.
- `package.json` / `package-lock.json` support Node-based tooling, currently including TuneBat analysis support.

## Core Directories

- `.github/workflows/` contains CI/release workflows. `slime-audio.yml` builds Windows SlimeAudio artifacts.
- `apps/SlimeAudio/` contains the .NET Windows receiver/sender app, protocol library, tests, installer assets, and solution file.
- `deploy/systemd/` contains Linux service units for local SlimeAudio services.
- `docs/` contains older durable docs. New durable docs should be mirrored into `wiki/`.
- `scripts/` contains the SlimeAudio operational toolchain: DJ analysis, session editing, live editing, mix planning, mixdown rendering, streaming, TTS lean-ins, dashboard serving, music library indexing, and web smoke checks.
- `skills/slime-audio-dj/` contains the Codex/OpenClaw skill agents should read before planning, rendering, or playing DJ sets.
- `src/spotify_brain/` contains the Python Spotify wrapper around `spogo`.
- `tests/` contains Python unit tests and web/dashboard fixtures.
- `web/slime-audio/` contains the browser dashboard frontend.
- `runtime/` contains active sessions, state, generated plans, local SQLite/cache files, playback history, and generated renders. Treat it as operational state; do not commit large generated artifacts unless intentionally preserving a fixture.
- `wiki/` is the canonical repo documentation.

## Important Runtime Pointers

- `runtime/mix-session.json` is the active live DJ session.
- `runtime/mix-session-state.json` is the active playback state used by the runner, live edit wrapper, and dashboard.
- `runtime/active-set.json` points at the loaded named set.
- `runtime/sets/` stores archived named sets and set metadata.
- `runtime/play-history.jsonl` records playback and live-edit events.
- `runtime/slime-music-library.sqlite3` stores indexed music library rows and DJ analysis metadata.
- `runtime/dj-analysis-cache.json` is a compatibility/cache mirror for analysis workflows.
