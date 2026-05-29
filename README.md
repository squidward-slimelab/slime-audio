# Slime Audio

LAN audio broadcast and local Spotify control experiments for Slime Lab.

The main app is `apps/SlimeAudio`: a Windows tray receiver plus sender CLI for synchronized TTS / audio broadcast to machines like `SPATULA` and `SPONGEBOT`.

Download the Windows installer from GitHub releases.

## Slime Audio

- `SlimeAudio.Tray`: Windows tray app that listens on UDP `47777`.
- `SlimeAudio.Send`: sends a PCM WAV to one or more devices with a shared future start timestamp.
- `scripts/slime_audio_drops.py`: watches Spotify playback and sends timed phrase drops during specific songs.
- `scripts/slime_audio_stream.py`: decodes a local audio file and streams it to any combo of discovered receivers with one synced start timestamp.
- `SlimeAudioSetup.exe`: real Windows installer with Start Menu shortcut and optional startup launch.
- LAN discovery: `SlimeAudio.Send.exe discover`
- Updates: tray menu `Check for updates`, or `SlimeAudio.Send.exe update --target HOST:47777`

GitHub Actions builds win-x64 artifacts from `.github/workflows/slime-audio.yml`.

## Spotify Brain

The older Python Spotify wrapper still lives in this repo because it is useful for playlist/playback experiments.

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

## Timed Sample Drops

Timed drops are local and playback-aware. Write the phrases once in a JSON plan, then run the dropper while Spotify plays. It polls `spogo status`, matches the active track, and only sends drops while `is_playing` is true. If Spotify is paused, drops wait or skip instead of talking over silence.

Spotify sometimes reports `progress_ms: 0` even when a song is already playing. By default, the runner will not fire timed drops until it has a reliable song clock: either Spotify reports a non-zero progress value, or the runner observes a track change and can treat that as the start. This avoids arming a plan from stale status and dropping samples at the wrong point in a song.

Spotify documents its Web API limit as a rolling 30 second window, but does not publish a fixed request count. In local SPATULA probing, 5 second status polling was clean, 3 second status polling was clean in a short test, and 1.5 second polling correlated with `RESOURCE_EXHAUSTED` failures. The drop runner defaults to 5 seconds and backs off up to 30 seconds on status failures.

```json
{
  "target": "SPATULA:47777",
  "voice": "en-US-GuyNeural",
  "rate": "-14%",
  "volume": 1.7,
  "poll_ms": 5000,
  "max_poll_ms": 30000,
  "require_known_progress": true,
  "drops": [
    {
      "id": "pocket",
      "track_uri": "spotify:track:3hmCHZFkgE4tkJKSqpOUhz",
      "at": "0:42.500",
      "text": "small checkpoint. youre in the pocket now."
    }
  ]
}
```

```bash
python3 scripts/slime_audio_drops.py --plan drops.json --max-minutes 20
```

Use `track_uri` for exact matching. `track_id` and `track_name` also work for quick local plans.

## Local File Streaming

Stream any local audio file through the same SlimeAudio UDP path as voice. The script discovers receivers, accepts receiver names, `host:port`, or `all`, decodes with VLC when installed, and falls back to GStreamer.

```bash
python3 scripts/slime_audio_stream.py ./song.flac --target SPATULA --target SPONGEBOT
python3 scripts/slime_audio_stream.py ./mix.mp3 --target all --delay-ms 3000
```

All targets receive one shared session id and start timestamp, so connected rooms begin together. Use `--dry-run` to see resolved receivers without sending audio.

For multi-room music, use multicast mode after starting the shared stream listener from each tray app. This is one live RTP source instead of separate per-host packet playback.

```bash
python3 scripts/slime_audio_stream.py ./mix.flac --target all --mode multicast
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

## Tests

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```
