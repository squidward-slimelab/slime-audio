# Slime Audio

LAN audio broadcast and local Spotify control experiments for Slime Lab.

The main app is `apps/SlimeAudio`: a Windows tray receiver plus sender CLI for synchronized TTS / audio broadcast to machines like `SPATULA` and `SPONGEBOT`.

Download the Windows installer from GitHub releases.

## Slime Audio

- `SlimeAudio.Tray`: Windows tray app that listens on UDP `47777`.
- `SlimeAudio.Send`: sends a PCM WAV to one or more devices with a shared future start timestamp.
- `scripts/slime_audio_drops.py`: watches Spotify playback and sends timed phrase drops during specific songs.
- `scripts/slime_audio_stream.py`: decodes a local audio file and streams it to any combo of discovered receivers with one synced start timestamp.
- `scripts/slime_audio_session.py`: validates planned live-mix sessions with up to four decks, arbitrary clip start times, trims, mic lean-ins, and automation envelopes.
- `scripts/slime_audio_web.py`: serves the read-only now-playing, scrub bar, and canonical DJ session timeline dashboard.
- `scripts/slime_audio_candidates.py`: keeps live set constraints and ranks database-backed candidate tracks for future queue/session planning.
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

Stream any local audio file through the shared SlimeAudio backends. The script discovers receivers, accepts receiver names, `host:port`, or `all`, and decodes with FFmpeg.

```bash
python3 scripts/slime_audio_stream.py ./song.flac --target SPATULA --target SPONGEBOT --mode snapcast
python3 scripts/slime_audio_stream.py ./mix.mp3 --target all --mode snapcast
```

Use `--dry-run` to see resolved receivers without sending audio.

Receiver discovery reports Snapcast client telemetry from each tray: server host, snapclient PID, start time, exit count, last stderr time, last status, and the local `%LOCALAPPDATA%\SlimeAudio\telemetry.jsonl` path. Use that after audible skips to see whether the receiver process exited/restarted or merely stayed connected while the sender/window changed.

For multi-room music, use Snapcast or multicast mode. These are shared streams instead of per-host packet playback. Multicast starts shared stream listeners on the selected receivers before playback; add `--stop-listeners-when-done` when you want it to shut them down after the file exits.

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

SlimeAudio has a first-pass DJ brain for local files. It persists stable beatgrid, phrase-grid, structure, and drop-candidate metadata in `runtime/slime-music-library.sqlite3`, keeps `runtime/dj-analysis-cache.json` as a compatibility mirror, estimates BPM/key/energy, and plans adjacent transitions. The key planner treats relative minor/major as a real rotation of the same pitch set, so A minor to C major is a strong zero-pitch-shift match instead of a dumb "different mode" clash.

```bash
python3 scripts/slime_audio_dj.py analyze ./track-a.wav ./track-b.wav
python3 scripts/slime_audio_dj.py structure ./track-a.wav
python3 scripts/slime_audio_dj.py cues ./track-a.wav --kind drop --kind hook
python3 scripts/slime_audio_dj.py tension --session runtime/mix-session.json --state runtime/mix-session-state.json --horizon-ms 2700000 > runtime/tension-windows.json
python3 scripts/slime_audio_dj.py plan --playlist runtime/late-friday-fresh-playlist.txt
python3 scripts/slime_audio_dj.py rank ./now-playing.wav --playlist runtime/candidates.txt --limit 8
python3 scripts/slime_audio_mix_planner.py --session runtime/mix-session.json --state runtime/mix-session-state.json --max-render-tempo-shift-pct 4 --max-render-pitch-shift-semitones 2 --apply
```

Transition plans include:

- tempo shift for the next track
- pitch shift in semitones when a small shift improves compatibility
- relative major/minor rotation matches
- phrase wait target, currently 32 beats
- notes when a transition needs a longer blend or bridge

Structure analysis adds a rough beat grid and phrase-aware windows such as intro, breakdown, build, drop, and outro. Running it once stores reusable rows in `track_dj_analysis`, `track_dj_structure`, `track_dj_drop_candidates`, and `track_dj_cues`; unchanged files are reused from SQLite before raw audio is decoded again, and size/mtime changes force recomputation. It also emits lean-in suggestions, especially pre-drop points where commentary can land before getting out of the way. This is heuristic raw-audio analysis, not full Rekordbox-grade beatgrid editing yet, but it gives the agent concrete windows to plan trims, overlays, and vocal drops against.

Cue analysis turns structure windows into named musical start points such as `clean_intro`, `build`, `drop`, `hook`, `stabs`, `vocal`, `clean_outro`, `safe_loop`, and `pre_drop`. High-confidence cues are quantized to phrase boundaries, lower-confidence usable cues are quantized to beats, and routine commands can reference cue kinds instead of raw timestamps:

```bash
python3 scripts/slime_audio_session.py instant-double-routine runtime/mix-session.json --source-id lead --id lead-hook --recipe hook-tease --cue-kind hook
```

Tension analysis turns those per-track structure points into absolute mix-session timestamps. `slime_audio_dj.py tension` emits candidate commentary windows with `reason` and `talking_points` fields derived from analysis facts only: track position, detected structure, BPM/key estimates, energy movement, and transition-plan notes. Use that JSON as an input to the commentary planner when a live set should speak around musical pressure instead of generic track starts.

Mix planning turns the analysis into executable session edits. `slime_audio_mix_planner.py` reads the live runner state as a lock, analyzes only current/future clips, and only creates overlays when the transition clears tempo/key compatibility gates. Safe overlays get phrase-sized starts, explicit clip fade lengths, optional drop-double clips from detected build/drop windows, rendered tempo/key correction within configured limits, and master duck automation around handoffs. Use `--max-render-pitch-shift-semitones 0` when a routine should preserve original keys instead of allowing key correction. Unsafe transitions remain hard cuts; the renderer does not add implicit auto-crossfades just because clips overlap. Run the planner immediately after importing a playlist and again when extending the future set; a straight import is not a finished DJ set.

Mashup planning is the target shape for DJ sets. A basic mashup uses one or more compatible tracks as filtered rhythmic/harmonic beds under another lead section. Until stem separation exists, use gain plus low-pass/high-pass automation to carve space for the lead:

```bash
python3 scripts/slime_audio_session.py mashup-bed runtime/mix-session.json --bed-id break-loop --start 01:16.000 --end 01:48.000 --gain-db -8 --lowpass-hz 1800 --highpass-hz 100
```

DJ analysis hydrates BPM/key/Camelot from `runtime/slime-music-library.sqlite3` TuneBat fields before using raw local estimates. Missing DB metadata should be filled with `scripts/slime_music_library.py analyze-tunebat-local DUPLICATE_KEY`; filename tags are ignored. The raw analyzer is useful for structure windows, but TuneBat-backed DB facts are the authority for beat/key planning. Drop/clip export proofs should still render through `slime_audio_session_mixdown.py --verify`, which rejects silent output before anything is shared or played.

The current analyzer is intentionally dependency-light and works through the existing FFmpeg decode path. It is good enough to give Squidward ears for planning. A later Essentia/librosa backend can improve detection accuracy without changing the cache or transition-plan JSON.

## Live Mix Sessions

Live mix sessions are the canonical control-plane shape for DJ sets. They are intentionally planned data, not manual performance gestures: every gain ramp, filter move, pitch/tempo change, TTS lean-in, and ducking move should be encoded as automation before playback reaches it. Clips use absolute mix timestamps like an Ableton arrangement, so tracks do not need to be stacked back-to-back. Use `start`, `trim_start`, and `duration` to pull sections from songs for overlays, hooks, bridges, and loops.

`runtime/mix-session.json` and `runtime/mix-session-state.json` are the active live pointers consumed by the runner and dashboard. They should not be treated as the permanent identity of a set. Keep named set artifacts separately, activate one through the native session runner, then use the live-edit commands below against the active session while playback continues. Directly streaming a rendered review file bypasses dashboard state and should only be used for file-only review playback.

Named sets live under `runtime/sets/<slug>/` with a root `runtime/sets/manifest.json` and an active pointer at `runtime/active-set.json`. Use `scripts/slime_audio_sets.py` for set identity operations: archive the current session, start a blank set, load an archived set into the active pointers, replay it with the native runner, save edits back to the loaded archive, fork a set, render a review artifact, and prune old renders. The dashboard can browse archived sets visually without loading them into playback; the archived view intentionally has no live playhead.

```bash
python3 scripts/slime_audio_sets.py archive --title "Late night draft" --slug late-night-draft --session runtime/mix-session.json
python3 scripts/slime_audio_sets.py list --json
python3 scripts/slime_audio_sets.py new --title "Fresh scratch set"
python3 scripts/slime_audio_sets.py activate late-night-draft --reset-state
python3 scripts/slime_audio_sets.py replay late-night-draft --target all --reset-state
python3 scripts/slime_audio_sets.py save-loaded
python3 scripts/slime_audio_sets.py fork late-night-draft --title "Late night revision"
python3 scripts/slime_audio_sets.py render --slug late-night-draft --format mp3 --mp3-bitrate 128k --keep 3 --max-total-mb 256
python3 scripts/slime_audio_sets.py cleanup-renders --keep 3 --max-age-hours 12 --max-total-mb 256
python3 scripts/slime_audio_session.py template > runtime/mix-session.json
python3 scripts/slime_audio_session.py validate runtime/mix-session.json
python3 scripts/slime_audio_session.py summary runtime/mix-session.json
python3 scripts/slime_audio_session.py import-playlist runtime/mix-session.json --playlist runtime/set.txt --start 00:00.000 --decks deck-1,deck-2,deck-3,deck-4
python3 scripts/slime_audio_live_edit.py add-clip --id break-loop --deck deck-1 --path /mnt/rockhouse/Music/example.flac --start 01:12.000 --trim-start 02:04.000 --duration 00:32.000 --reason "add future bed"
python3 scripts/slime_audio_live_edit.py add-mic --id drop-2 --start 01:20.000 --text "quick note" --volume 1.7 --duck-volume 0.45 --reason "scheduled lean-in"
python3 scripts/slime_audio_live_edit.py automate --target break-loop --param gain_db --points-json '[{"at":"01:12.000","value":-18},{"at":"01:16.000","value":-2}]' --reason "shape bed entrance"
python3 scripts/slime_audio_live_edit.py mashup-bed --bed-id break-loop --start 01:16.000 --end 01:48.000 --gain-db -8 --lowpass-hz 1800 --highpass-hz 100
python3 scripts/slime_audio_live_edit.py instant-double-routine --source-id break-loop --id break-loop-stabs --recipe stabs --start 01:24.000 --cache runtime/dj-analysis-cache.json
python3 scripts/slime_audio_live_edit.py instant-double-routine --source-id break-loop --id break-loop-offbeat --recipe offbeat-swaps --start 01:24.000 --cache runtime/dj-analysis-cache.json
python3 scripts/slime_audio_live_edit.py instant-double --source-id break-loop --id break-loop-double --start 01:24.000 --duration 00:08.000 --gate-beats 1/2 --cut-source --cache runtime/dj-analysis-cache.json
python3 scripts/slime_audio_live_edit.py move --id break-loop --start 01:16.000 --reason "align future phrase"
python3 scripts/slime_audio_live_edit.py beat-jump --id break-loop --beats 1/2 --field start --cache runtime/dj-analysis-cache.json --reason "tighten double timing"
python3 scripts/slime_audio_live_edit.py fader-routing --assign deck-1=A --assign deck-3=A --assign deck-2=B --assign deck-4=B --reason "DDJ-style 1/3 vs 2/4 routing"
python3 scripts/slime_audio_live_edit.py crossfader --points-json '[{"at_ms":84000,"value":-1},{"at_ms":88000,"value":1}]' --reason "planned fader cut"
python3 scripts/slime_audio_lean_ins.py --session runtime/mix-session.json --create --start 01:20.000 --text "quick note" --volume 1.7 --duck-volume 0.45 --lowpass-hz 1400
python3 scripts/slime_audio_commentary_planner.py --session runtime/mix-session.json --state runtime/mix-session-state.json --tension-plan runtime/tension-windows.json --count 3
python3 scripts/slime_audio_session_mixdown.py runtime/mix-session.json --from 01:10.000 --output runtime/mix-session-render.wav
python3 scripts/slime_audio_session_mixdown.py runtime/mix-session.json --from 01:10.000 --duration 00:45.000 --output runtime/mix-review.mp3 --format mp3 --verify
python3 scripts/slime_audio_session_mixdown.py runtime/mix-session.json --routine-id break-loop-stabs --output runtime/routine-proof.mp3 --format mp3 --report-output runtime/routine-proof.json --verify
python3 scripts/slime_audio_session_runner.py --session runtime/mix-session.json --state runtime/mix-session-state.json --target all
```

`import-playlist` is a migration helper: it probes track durations with `ffprobe`, assigns clips to decks, and writes absolute `start_ms` values. After import, future edits should add/move/remove timestamped clips instead of mutating playlist slots.

`scripts/slime_audio_live_edit.py` is the active-session control wrapper. It defaults to `runtime/mix-session.json`, reads `runtime/mix-session-state.json` as the live playhead lock, routes to the existing session edit primitives, and appends `live_edit_applied` events to `runtime/play-history.jsonl`. Use `--force` only for deliberate repair work before the playhead. The lower-level `scripts/slime_audio_session.py` edit commands still exist for setup, tests, and non-active archived sessions.

`beat-jump` is the quantized edit path for doubles and rhythmic offsets. It reads cached BPM/beat-offset analysis, supports +/-1/2, +/-1, +/-2, +/-4, and +/-8 beat moves, and snaps either `--field start` or `--field trim-start` to the track grid. Low-confidence beatgrids are rejected unless `--force` is explicit.

`instant-double-routine` is the named recipe layer for same-track duplicate moves. It starts with recipes that only require existing primitives or persisted cue kinds, and refuses recipes whose prerequisites still need slip, brake, or reverb. Raw `instant-double` remains available for hand-built moves; it clones an active or future source clip onto a free deck at the same musical position, preserving path, trim position, rendered tempo/pitch, and gain. Use `--cue-kind` on routines when the move should start from a stored hook/drop/build cue rather than a hand-entered timestamp. Use `--gate-beats` for simple quantized routines, `--gate-offset-beats 1/2` for first-cut-on-the-AND timing, and `--cut-source` when the duplicate should trade against the original instead of stacking on top of it; the dashboard labels the duplicate as an instant double instead of a normal song transition.

Crossfader routing is the controller-style cut layer. Store deck assignments in `fader_routing.deck_assignments`, normally `deck-1`/`deck-3` on side `A` and `deck-2`/`deck-4` on side `B`, then automate `crossfader.position` from `-1` (hard A) through `0` (center, both sides) to `1` (hard B). Mixdown translates held positions and gradual ramps into deterministic per-clip deck gains, and the dashboard shows the fader lane separately from normal gain automation. Doubled-deck routines with `--cut-source` use crossfader cuts instead of unrelated source/duplicate gain envelopes. The `offbeat-swaps` routine uses the cached beatgrid to wait a half beat, then alternates crossfader sides on each following half-beat gate.

`scripts/slime_audio_session_runner.py` consumes the native timestamped session directly. It renders short future windows, streams them through Snapcast/multicast, reloads `mix-session.json` before each window, and records `session_window_*` history events. Future adds/moves/removes take effect on the next render window without interrupting audio already under the playhead.

For review and verification, render the planned mix directly to a file with `slime_audio_session_mixdown.py`, or use `slime_audio_sets.py render` when the artifact belongs to a named archive. Use WAV/FLAC for lossless checks or MP3 for shareable review artifacts; `--verify` probes duration and rejects silent output. `--from` and `--duration` are the quickest way to export a transition proof clip without rendering the whole set. `--routine-id` renders a padded audition window around one planned routine and can write a JSON report with render timing, audio levels, clipping/silence checks, and current taste-rule warnings/errors. Set-render outputs go to `runtime/set-renders/` by default and are pruned by age/count/total size so proof files do not fill the disk.

Lean-ins are scheduled session events, not immediate side streams. A lean-in has an exact mix timeline `start`, spoken text, voice `volume`, and paired `duck_volume`/`lowpass_hz` automation. `scripts/slime_audio_session_mixdown.py` renders those events into one Snapcast-ready audio file so voice, ducking, and low-pass filtering happen in the shared mix instead of relying on the old receiver packet path.

`scripts/slime_audio_commentary_planner.py` creates and updates the commentary plan independently of music playback. It reads the native session and live state, chooses future tension/intro/transition/track windows with spacing rules, writes mic lean-ins with duck + low-pass automation, and appends `commentary_planned` JSONL records with the text, timing, related track, and reason.

## Web Dashboard

The dashboard is local. It serves current runner state as JSON, exposes named set archive controls, and renders the browser UI from `web/slime-audio/`.
See `docs/slime-audio-dashboard.md` for the dashboard workflow, `/api/state` view-model contract, and frontend verification checklist.

```bash
python3 scripts/slime_audio_web.py --state runtime/mix-session-state.json --session runtime/mix-session.json --port 8765
```

Open `http://127.0.0.1:8765`. The browser polls `/api/state`, shows the current render window/playhead, and draws the native timestamped mix session: clips, vocal drops, and automation points. It also polls `/api/sets` so old named sets can be viewed visually without loading them, loaded into the active pointers, replayed, saved after edits, or rendered to a pruned review file. The dashboard no longer projects legacy playlist state.

For fixture-backed frontend checks without active room playback:

```bash
PYTHONPATH=scripts:src python3 scripts/slime_audio_web_smoke.py
```

The smoke check starts a temporary dashboard server, renders desktop and mobile headless Chrome screenshots into `runtime/web-smoke/`, and verifies the timeline, playhead, and planned vocal marker are present.

Install the dashboard as a user service:

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/slime-audio-web.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now slime-audio-web.service
```

## Candidate Selection And Set Constraints

Candidate selection reads the Samba music database, recent playback history, and a runtime scratchpad of operator steering. This is where the DJ agent records current vibe, direction, energy target, banned artists, and exclude terms before asking for future tracks. The scratchpad lives under `runtime/` by default so local set state does not leak into the public repo.

```bash
python3 scripts/slime_audio_candidates.py constraints --init
python3 scripts/slime_audio_candidates.py set-constraints --vibe "fresh daytime" --direction "brighter but not corny" --energy-target 0.65 --exclude-artist "Khruangbin" --reason "operator steering"
python3 scripts/slime_audio_candidates.py candidates --limit 12
python3 scripts/slime_audio_candidates.py candidates "jungle" --recent-limit 40 --limit 8
```

Candidates avoid recently played tracks from `runtime/play-history.jsonl` by default, filter excluded artists/terms, prefer stronger duplicate/library copies, and include `reasons` so the agent can explain why a track was chosen.
Root-level or otherwise untagged files are skipped by default so replicated sound effects do not outrank real music; add `--include-untagged` when intentionally searching that bucket.

## Samba Music Library

The local library index lives at `runtime/slime-music-library.sqlite3`. It scans mounted Samba music roots, stores every audio file in SQLite, groups duplicates by normalized artist/album/title, and exposes two useful views:

- `tracks`: one row per unique song with copy count, server count, preferred path, every known location, lyrics availability, and cached TuneBat facts.
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

Lyrics and TuneBat metadata are stored per duplicate group, not per replicated copy. Use the `duplicate_key` returned by `search`, `tracks`, or `copies`:

```bash
python3 scripts/slime_music_library.py set-lyrics DUPLICATE_KEY --lyrics-file lyrics.txt --source "manual"
python3 scripts/slime_music_library.py set-tunebat DUPLICATE_KEY --key "C# major" --mode major --camelot 3B --bpm 126 --url "https://tunebat.com/Analyzer"
python3 scripts/slime_music_library.py analyze-tunebat-local DUPLICATE_KEY
python3 scripts/slime_music_library.py backfill-tunebat-local --limit 12 --max-seconds 1200
python3 scripts/slime_music_library.py show DUPLICATE_KEY
python3 scripts/slime_music_library.py show DUPLICATE_KEY --include-lyrics
python3 scripts/slime_music_library.py import-metadata runtime/music-metadata.json
```

This is for TuneBat Analyzer output from `https://tunebat.com/Analyzer`, not the public TuneBat song database. The analyzer runs in-browser and is the path to use for odd local files, bootlegs, edits, samples, and other tracks that Spotify would never know about. `scripts/slime_tunebat_analyzer.js` runs the same public browser-capable engine family locally through `essentia.js` and FFmpeg, then `analyze-tunebat-local` caches that output into SQLite. It intentionally does not vendor TuneBat's protected site bundle. `essentia.js` is AGPL-3.0, so keep this as an optional local/internal tool unless the repo licensing is changed deliberately.

Use the service wrapper for routine maintenance. It rescans mounted shares, then backfills a bounded number of missing local TuneBat-style rows and missing/stale DJ beatgrid/structure rows so full-library work spreads out over many small runs:

```bash
python3 scripts/slime_music_library_service.py --tunebat-backfill-limit 12 --tunebat-max-seconds 1200 --dj-analysis-backfill-limit 6
mkdir -p ~/.config/systemd/user
cp deploy/systemd/slime-music-library.* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now slime-music-library.timer
```

The service uses `runtime/slime-music-library.lock`, so overlapping timer runs skip instead of fighting SQLite or hammering the network shares. Lyrics are intentionally not scraped by the maintenance service; import verified lyrics or sidecar files explicitly so we do not fill the database with junk.

Session-building tools should use this database when selecting clip paths. Prefer the `preferred_path` from `tracks`/candidate output so playback uses the best available duplicate source.

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
