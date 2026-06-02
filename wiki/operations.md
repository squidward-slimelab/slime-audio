# Operations

This page covers live operational state and local playback behavior.

## Active Playback

The native session runner consumes:

- `runtime/mix-session.json`
- `runtime/mix-session-state.json`

It renders short future windows, streams them through Snapcast/multicast, reloads the session before each window, and records history events in `runtime/play-history.jsonl`.

Start playback:

```bash
python3 scripts/slime_audio_session_runner.py --session runtime/mix-session.json --state runtime/mix-session-state.json --target all
```

Future live edits take effect on the next render window; audio already under the playhead is not interrupted.

Skips exactly on render-window boundaries usually point at the session runner or Snapcast FIFO handoff, not necessarily the Windows receiver. Compare sender/session logs with `session_window_*` history before blaming the tray.

## Streaming Local Files

Use `scripts/slime_audio_stream.py` for local file streaming:

```bash
python3 scripts/slime_audio_stream.py ./mix.mp3 --target all --mode snapcast
python3 scripts/slime_audio_stream.py ./mix.flac --target all --mode multicast
python3 scripts/slime_audio_stream.py --target all --start-listeners
python3 scripts/slime_audio_stream.py --target all --stop-listeners
```

Use `--dry-run` to resolve targets without sending audio.

For persistent Snapcast playback, keep one parent FIFO writer open across render windows and swap only the ffmpeg child input. Closing the FIFO between windows can make snapserver emit EOF and create audible gaps even when receiver clients remain healthy.

## Timed Drops

`scripts/slime_audio_drops.py` watches Spotify playback and sends timed phrase drops from a JSON plan. It polls `spogo status`, matches the active track, and avoids firing over paused or unknown-progress playback.

```bash
python3 scripts/slime_audio_drops.py --plan drops.json --max-minutes 20
```

## Services

Systemd service files live in `deploy/systemd/`. Keep service docs here updated when units, paths, environment variables, or runtime expectations change.

After code changes to long-running services, verify the service process actually restarted. A correct commit can still look broken if the service is stale.

For the local web dashboard:

```bash
curl -i http://DASHBOARD_HOST:8765/api/sets
curl -i http://DASHBOARD_HOST:8765/api/not-a-real-endpoint
```

Both should return JSON content types for API paths.

## Disk Hygiene

Root disk may be tight on the SlimeAudio host. Before creating large renders, check free space. Prefer short proof windows and use set render pruning:

```bash
python3 scripts/slime_audio_sets.py cleanup-renders --keep 3 --max-age-hours 12 --max-total-mb 256
```

Avoid leaving generated review audio, stale `/tmp/slime-session-runner-*` directories, or bulky runtime artifacts around.
