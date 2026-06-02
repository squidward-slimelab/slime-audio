# Spotify Brain

`src/spotify_brain/` is the older Python Spotify wrapper. It remains in the repo because it is useful for playlist and playback experiments.

## Purpose

Spotify Brain wraps `spogo --json` with a small JSON command layer so agents can:

- inspect Spotify status
- list devices
- search tracks
- start playback
- dry-run playlist mutations
- confirm playlist additions/removals with guardrails

It should not log or paste cookies or secrets.

## Install

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

Install and authenticate `spogo` separately on the machine.

## Commands

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

No-install smoke test:

```bash
PYTHONPATH=src python3 -m spotify_brain run status --dry-run
```

## Tests

Core tests live in:

- `tests/test_app.py`
- `tests/test_commands.py`
- `tests/test_redact.py`
