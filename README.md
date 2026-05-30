# Slime Audio

LAN audio broadcast and local Spotify control experiments for Slime Lab.

The main app is `apps/SlimeAudio`: a Windows tray receiver plus sender CLI for synchronized TTS / audio broadcast to machines like `SPATULA` and `SPONGEBOT`.

Download the Windows installer from GitHub releases.

## Slime Audio

- `SlimeAudio.Tray`: Windows tray app that listens on UDP `47777`.
- `SlimeAudio.Send`: sends a PCM WAV to one or more devices with a shared future start timestamp.
- `scripts/slime_audio_drops.py`: watches Spotify playback and sends timed phrase drops during specific songs.
- `scripts/slime_audio_stream.py`: decodes a local audio file and streams it to any combo of discovered receivers with one synced start timestamp.
- `scripts/slime_audio_dj.py`: analyzes local tracks for BPM, beat offset, key, Camelot code, energy, and transition compatibility.
- `scripts/slime_music_library.py`: indexes mounted Samba music shares into SQLite, combines duplicates, and picks the strongest server copy.
- `SlimeAudioSetup.exe`: real Windows installer with Start Menu shortcut and optional startup launch.
- LAN discovery: `SlimeAudio.Send.exe discover`
- Updates: tray menu `Check for updates`, or `SlimeAudio.Send.exe update --target HOST:47777`
- Shared stream listeners: server-controlled with `shared-start` / `shared-stop`, or automatic from `scripts/slime_audio_stream.py --mode multicast`.

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

Stream any local audio file through the same SlimeAudio UDP path as voice. The script discovers receivers, accepts receiver names, `host:port`, or `all`, and decodes with FFmpeg.

```bash
python3 scripts/slime_audio_stream.py ./song.flac --target SPATULA --target SPONGEBOT
python3 scripts/slime_audio_stream.py ./mix.mp3 --target all --delay-ms 3000
```

All targets receive one shared session id and start timestamp, so connected rooms begin together. Use `--dry-run` to see resolved receivers without sending audio.

For multi-room music, use multicast mode. This is one live FFmpeg UDP stream instead of separate per-host packet playback. The streamer starts shared stream listeners on the selected receivers before playback; add `--stop-listeners-when-done` when you want it to shut them down after the file exits.

```bash
python3 scripts/slime_audio_stream.py ./mix.flac --target all --mode multicast
python3 scripts/slime_audio_stream.py --target all --start-listeners
python3 scripts/slime_audio_stream.py --target all --stop-listeners
```

Linux debugging receiver:

```bash
dotnet run --project apps/SlimeAudio/src/SlimeAudio.Headless/SlimeAudio.Headless.csproj -c Release -- --port 47777 --no-audio
```

The headless receiver uses the same SlimeAudio UDP/control protocol as the tray app and gives Linux/CI a buildable receiver target.

## DJ Analysis And Keymatching

SlimeAudio has a first-pass DJ brain for local files. It caches track metadata in `runtime/dj-analysis-cache.json`, estimates BPM/key/energy, and plans adjacent transitions. The key planner treats relative minor/major as a real rotation of the same pitch set, so A minor to C major is a strong zero-pitch-shift match instead of a dumb "different mode" clash.

```bash
python3 scripts/slime_audio_dj.py analyze ./track-a.wav ./track-b.wav
python3 scripts/slime_audio_dj.py plan --playlist runtime/late-friday-fresh-playlist.txt
python3 scripts/slime_audio_dj.py rank ./now-playing.wav --playlist runtime/candidates.txt --limit 8
python3 scripts/slime_audio_playlist_runner.py --playlist runtime/late-friday-fresh-playlist.txt --target all --dj-plan --dry-run
```

Transition plans include:

- tempo shift for the next track
- pitch shift in semitones when a small shift improves compatibility
- relative major/minor rotation matches
- phrase wait target, currently 32 beats
- notes when a transition needs a longer blend or bridge

The current analyzer is intentionally dependency-light and works through the existing FFmpeg decode path. It is good enough to give Squidward ears for planning. A later Essentia/librosa backend can improve detection accuracy without changing the cache or transition-plan JSON.

## Samba Music Library

The local library index lives at `runtime/slime-music-library.sqlite3`. It scans mounted Samba music roots, stores every audio file in SQLite, groups duplicates by normalized artist/album/title, and exposes two useful views:

- `tracks`: one row per unique song with copy count, server count, preferred path, and every known location.
- `preferred_files`: one chosen file per song, routed to the strongest server copy first.

Default source priority:

- `patrick:/mnt/rockhouse/Music` priority `100`
- `robokrabs:/mnt/chum-bucket/Music` priority `90`
- `spongebot:/mnt/pineapple/Music` priority `60`
- `spatula:/mnt/krusty-krab/Music` priority `50`

```bash
python3 scripts/slime_music_library.py scan
python3 scripts/slime_music_library.py stats
python3 scripts/slime_music_library.py search "brianstorm"
python3 scripts/slime_music_library.py tracks --duplicates-only --limit 25
python3 scripts/slime_music_library.py copies "song title"
python3 scripts/slime_music_library.py route /mnt/krusty-krab/Music/Artist/Album/song.flac
```

`scripts/slime_audio_playlist_runner.py` uses this database by default when it exists. If a playlist points at a weaker duplicate, the runner resolves it to the preferred copy before streaming and records both `track` and `resolved_track` in playback history. Disable that with `--no-prefer-library-source`.

Custom mounts can be supplied as `server:share:priority:/absolute/path`:

```bash
python3 scripts/slime_music_library.py scan --source robokrabs:chum-bucket:90:/mnt/chum-bucket/Music
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
