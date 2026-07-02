# Operations

This page covers live operational state and local playback behavior.

## Active Playback

The native session runner consumes:

- `runtime/mix-session.json`
- `runtime/mix-session-state.json`

It renders short future windows, streams them through Snapcast/multicast, reloads the session before each window, and records history events in `runtime/play-history.jsonl`.
The state file also carries runner liveness fields: `runner_pid`, `runner_status`, `runner_started_at`, `runner_updated_at`, and, after a caught fatal/signal exit, `runner_exit_at` plus `runner_exit_reason`. A clean completion writes `session_runner_completed`; caught fatal exits and handled stop signals write `session_runner_exit` to history. A hard `SIGKILL` or OOM kill cannot be caught by Python, so diagnose those by comparing stale `runner_updated_at` / missing process with `journalctl --user` around the same timestamp.

Start playback:

```bash
python3 scripts/slime_audio_session_runner.py --session runtime/mix-session.json --state runtime/mix-session-state.json --target all
```

Future live edits take effect on the next render window; audio already under the playhead is not interrupted.

Skips exactly on render-window boundaries usually point at the session runner or Snapcast FIFO handoff, not necessarily the Windows receiver. Compare sender/session logs with `session_window_*` history before blaming the tray.

Silent mid-session stops should be triaged in this order:

- Check whether `runner_pid` still exists with `ps -fp <pid>`.
- Check `runtime/play-history.jsonl` for `session_runner_exit`, `session_window_failed`, and the last `session_window_started`.
- Check the named runner log for a Python traceback or Snapcast errors.
- If history has no fatal record and the pid is gone, check `journalctl --user --since <time> --until <time>` for service restarts, `systemd-oomd`, or signal kills.

## Streaming Local Files

Use `scripts/slime_audio_stream.py` for local file streaming:

```bash
python3 scripts/slime_audio_stream.py ./mix.mp3 --target all --mode snapcast
python3 scripts/slime_audio_stream.py ./mix.flac --target all --mode multicast
python3 scripts/slime_audio_stream.py --target all --start-listeners
python3 scripts/slime_audio_stream.py --target all --stop-listeners
```

Use `--dry-run` to resolve targets without sending audio.

Direct streams publish frontend state by default. `slime_audio_stream.py` writes
`runtime/active-set.json` plus `runtime/active-stream-state.json` so `/api/state`
reflects the audio that is actually being sent. For rendered DJ sessions, pass
the source session too:

```bash
python3 scripts/slime_audio_stream.py runtime/show-render.mp3 \
  --target all \
  --mode snapcast \
  --source-session runtime/show-session.json \
  --dashboard-title "Show Session"
```

To resume a rendered stream from the middle, pass `--start-offset-ms`. The same
offset is published to the dashboard state, so the frontend playhead and current
clip match the audible stream:

```bash
python3 scripts/slime_audio_stream.py runtime/show-render.mp3 \
  --target all \
  --mode snapcast \
  --source-session runtime/show-session.json \
  --start-offset-ms 1482533
```

Use `--no-active-pointer` only for proofs, diagnostics, or tests where the
dashboard should deliberately keep showing the previous live set.

Snapcast mode uses the system Snapserver and writes decoded audio to its FIFO,
`/tmp/snapfifo`. Do not start ad hoc Snapserver instances for normal room
playback.

The session runner holds one persistent write handle on the FIFO for the whole
session (`session_fifo_hold_acquired` in play history) so per-window ffmpeg
writers can exit without the Snapserver seeing EOF between render windows.
Windows after the first stream with `slime_audio_stream.py --continuation`,
which skips receiver discovery, listener control, and snapclient waits so the
window handoff has no multi-second discovery gap. If the hold cannot be
acquired (`session_fifo_hold_unavailable`), the runner falls back to full
re-establishment on every window. Because windowed playback now hands off
cleanly, autodj always launches the runner in windowed mode, which is what
lets live edits land at window boundaries. The runner's own `--single-window`
flag exists only for manual receiver/FIFO debugging; autodj deliberately does
not expose it, because a live set that cannot be live-edited is a degraded
path.

## Services

Systemd service files live in `deploy/systemd/`. Keep service docs here updated when units, paths, environment variables, or runtime expectations change.

After code changes to long-running services, verify the service process actually restarted. A correct commit can still look broken if the service is stale.

The music library timer is intentionally live-playback-safe. It may scan mounted shares during a set, but expensive TuneBat and DJ-analysis backfills skip when `runtime/active-set.json` points at runner state updated within the last six hours. The user service is also low-priority and resource-capped, so analyzer aborts or spikes should fail the maintenance job instead of starving the gateway or live runner.

For the local web dashboard:

```bash
curl -i http://DASHBOARD_HOST:8765/api/sets
curl -i http://DASHBOARD_HOST:8765/api/not-a-real-endpoint
```

Both should return JSON content types for API paths.

## Easter Island Head Deployment

The main SlimeAudio host is deployed by GitHub Actions from `main` using the
self-hosted repository runner on `easter-island-head`. The workflow is
`.github/workflows/deploy-easter-island-head.yml`, and the deploy job targets
the runner labels `self-hosted` and `easter-island-head`.

The workflow checks out `main` on the runner, then rsyncs the repo to
`/home/squidward/.openclaw/workspace/slime-audio`, excluding `.git/`, `.venv/`,
and `runtime/`. Runtime state, databases, generated audio, and caches stay on
the host.

After sync, `scripts/deploy_easter_island_head.sh` runs on the container. It
updates the Python venv, installs the package editable, compiles Python files,
restarts `slime-audio-web.service`, starts `slime-music-library.timer`, and
checks the local dashboard plus required services.

## Disk Hygiene

Root disk may be tight on the SlimeAudio host. Before creating large renders, check free space. Prefer short proof windows and use set render pruning:

```bash
python3 scripts/slime_audio_sets.py cleanup-renders --keep 3 --max-age-hours 12 --max-total-mb 256
```

Avoid leaving generated review audio, stale `/tmp/slime-session-runner-*` directories, or bulky runtime artifacts around.
