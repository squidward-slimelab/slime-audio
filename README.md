# Spotify Brain

Local agent-safe Spotify control service.

The first backend is [`spogo`](https://spogo.sh/): a scriptable Spotify CLI that can use the browser cookies already present on the machine. This repo wraps it with a small JSON command layer so agents can search, inspect playback, control devices, and edit playlists without ever logging cookies or Discord-pasting secrets.

## Goals

- One stable local interface for agents and scripts.
- `spogo --json` underneath for the quick practical path.
- Guardrails around playlist/library mutations.
- Dry-run support for edits before anything touches Spotify.
- Redacted logs and error payloads by default.

## Install

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

Install and authenticate `spogo` separately. Keep cookie import local.

```bash
spogo status --json
```

## CLI

```bash
spotify-brain run status
spotify-brain run devices
spotify-brain run search "aphex twin" --type track --limit 5
spotify-brain run play --uri spotify:track:...
spotify-brain run playlist-add PLAYLIST_ID spotify:track:... --dry-run
spotify-brain run playlist-add PLAYLIST_ID spotify:track:... --confirm
spotify-brain run playlist-remove PLAYLIST_ID spotify:track:... --dry-run
spotify-brain run playlist-remove PLAYLIST_ID spotify:track:... --confirm
```

All commands print JSON.

For a no-install smoke test from the repo:

```bash
PYTHONPATH=src python3 -m spotify_brain run status --dry-run
```

## Daemon

```bash
spotify-brain serve --host 127.0.0.1 --port 8765
```

Health check:

```bash
curl http://127.0.0.1:8765/health
```

Run a command:

```bash
curl -s http://127.0.0.1:8765/v1/command \
  -H 'content-type: application/json' \
  -d '{"action":"search","args":{"query":"burial","type":"track","limit":3}}'
```

## Safety Model

Read-only commands run directly.

Mutating commands support `dry_run`. Destructive commands, such as playlist removals and library removals, require `confirm: true` unless they are dry-runs.

Secrets are redacted from subprocess output before returning JSON.

## Slime Audio

`apps/SlimeAudio` contains the Windows tray receiver and sender CLI for LAN TTS / audio broadcast experiments.

- `SlimeAudio.Tray`: tray app that listens on UDP `47777`.
- `SlimeAudio.Send`: sends a PCM WAV to one or more devices with a shared future start timestamp.

GitHub Actions builds win-x64 artifacts from `.github/workflows/slime-audio.yml`.

## Tests

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```
