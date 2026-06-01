---
name: slime-audio-dj
description: Use when planning, extending, or hosting SlimeAudio DJ sets from a local music library, including database-backed song selection, transition planning, live queue updates, and tasteful spoken commentary.
---

# SlimeAudio DJ

Use this skill as the operator guide for DJ work in this repository. Keep domain logic in the repo scripts; the skill should coordinate those tools instead of reimplementing them.

## Privacy

Keep this skill generic and portable.

- Do not include private hostnames, room names, share names, people, playback habits, credentials, or local network details.
- Do not mention specific song or artist examples in this skill. Keep examples generic so the workflow stays portable.
- Store environment-specific defaults in local notes, ignored runtime config, or operator memory outside this skill.
- Treat public repo files as shareable by default.

## Core Tools

- `scripts/slime_music_library.py`: scan and query the SQLite music database.
- `scripts/slime_audio_candidates.py`: choose database-backed future tracks from preferred files, recent playback history, and live operator constraints.
- `scripts/slime_audio_dj.py`: analyze BPM, beat offset, key, Camelot code, energy, and transition compatibility.
- `scripts/slime_audio_mix_planner.py`: rewrite future mix-session clips into phrase-aware blends, drop doubles, and planned transition automation.
- `scripts/slime_audio_stream.py`: discover receivers and stream local files.
- `scripts/slime_audio_session.py`: maintain planned mix-session clips, mic lean-ins, and automation.
- `scripts/slime_audio_lean_ins.py`: add scheduled lean-ins to a mix session.
- `scripts/slime_audio_commentary_planner.py`: add tasteful future commentary lean-ins with spacing, context, and logs.
- `scripts/slime_audio_session_mixdown.py`: render session clips and lean-ins into one Snapcast-ready mix file.
- `scripts/slime_audio_session_runner.py`: run the native timestamped session in live-editable render windows.
- `scripts/slime_audio_tts.py` and `scripts/slime_audio_drops.py`: legacy Spotify/drop helpers; do not use them for Snapcast-era mix lean-ins unless explicitly working on legacy Spotify playback.

## Default Workflow

1. Check current state before acting:

   ```bash
   python3 scripts/slime_audio_stream.py discover
   tail -n 80 runtime/play-history.jsonl
   ```

2. Use the music database as the authority for song selection:

   ```bash
   python3 scripts/slime_music_library.py stats
   python3 scripts/slime_music_library.py search "query"
   python3 scripts/slime_audio_candidates.py candidates "query" --recent-limit 40 --limit 12
   ```

   If the database is missing or stale, rescan configured sources before falling back to ad hoc filesystem search. Prefer candidates from `preferred_files`; they avoid recently played tracks, skip untagged root files by default, respect excludes, and include reasons for why each track was selected.

3. Build a short runway, not a whole night. Prefer 30-60 minutes of music so the live set can adapt.

4. Record operator steering before extending the future set:

   ```bash
   python3 scripts/slime_audio_candidates.py constraints --init
   python3 scripts/slime_audio_candidates.py set-constraints \
     --vibe "fresh daytime" \
     --direction "brighter but not corny" \
     --energy-target 0.65 \
     --exclude-artist "Artist Name" \
     --exclude-term "avoid this" \
     --reason "operator steering"
   ```

   Keep current vibe, requested direction, energy target, excluded artists/terms, notes, and change reasons in the runtime constraints file. Candidate selection must read this scratchpad so steering survives restarts and affects future picks.

5. Analyze and rank transitions:

   ```bash
   python3 scripts/slime_audio_dj.py structure ./track.flac
   python3 scripts/slime_audio_dj.py tension --session runtime/mix-session.json --state runtime/mix-session-state.json --horizon-ms 2700000 > runtime/tension-windows.json
   python3 scripts/slime_audio_dj.py plan --playlist runtime/current-playlist.txt
   python3 scripts/slime_audio_dj.py rank ./current-track.flac --playlist runtime/candidates.txt --limit 12
   ```

   Analysis must prefer music-database TuneBat metadata over raw local estimates. Do not trust filename tags like `8B - 126`; if the database lacks BPM/key/Camelot for a track, run/store TuneBat analysis first:

   ```bash
   python3 scripts/slime_music_library.py analyze-tunebat-local DUPLICATE_KEY
   ```

   `slime_audio_dj.py` and `slime_audio_mix_planner.py` hydrate from `runtime/slime-music-library.sqlite3` and update `runtime/dj-analysis-cache.json` with the stored TuneBat values. Treat the raw analyzer as fallback structure help, not as authority for beat/key decisions.

   Then run the real mix planner against the future session, especially after importing a straight playlist:

   ```bash
   python3 scripts/slime_audio_mix_planner.py \
     --session runtime/mix-session.json \
     --state runtime/mix-session-state.json \
     --double-every 2 \
     --max-render-tempo-shift-pct 4 \
     --max-render-pitch-shift-semitones 2 \
     --apply
   ```

   The planner must respect the live lock from runner state, keep already-rendered audio intact, and only rewrite clips safely in the future.
   Use `--max-render-pitch-shift-semitones 0` when a routine should preserve original keys and avoid rendered key correction.
   For overlapping transitions, treat key-fit as the default target, not a nice-to-have. Prefer exact same-key blends after conservative rendered pitch correction. When matching major against minor, compare against the relative major/minor pitch set first, then choose the smallest tasteful transpose that lands the overlap in the intended key. Camelot-neighbor compatibility is acceptable for non-overlapped handoffs or naturally resolving musical moves, but it should not be the default excuse for layered vocals, doubles, or long blends.

6. Treat `runtime/mix-session.json` as the canonical live set state. Clips live on an absolute mix timeline with `start_ms`, `trim_start_ms`, and optional `duration_ms`; they are not playlist slots. Multiple decks may overlap like an Ableton arrangement.

7. If starting from an old ordered playlist, immediately import it into a timestamped session and do future edits against the session:

   ```bash
   python3 scripts/slime_audio_session.py import-playlist runtime/mix-session.json \
     --playlist runtime/current-playlist.txt \
     --start 00:00.000 \
     --decks deck-1,deck-2,deck-3,deck-4
   ```

8. Add, move, trim, overlap, or automate clips by timestamp. During playback, include `--state runtime/<active-state>.json` or `--lock-before <mix-time>` so edits before the playhead are rejected unless `--force` is explicit:

   ```bash
   python3 scripts/slime_audio_session.py add-clip runtime/mix-session.json \
     --create \
     --id next-drop \
     --deck deck-2 \
     --path ./track.flac \
     --start 02:16.000 \
     --trim-start 01:04.000 \
     --duration 00:32.000 \
     --state runtime/saturday-4h-dj-mix-state.json
   ```

   For doubles and rhythmic offsets, use cached beatgrid analysis instead of hand-entered millisecond nudges:

   ```bash
   python3 scripts/slime_audio_session.py beat-jump runtime/mix-session.json \
     --id doubled-hook \
     --beats 1/2 \
     --field start \
     --cache runtime/dj-analysis-cache.json \
     --state runtime/mix-session-state.json
   ```

   Use `--field start` to delay/advance a clip on the mix timeline and `--field trim-start` to jump the source position while keeping the clip anchored. The command must reject weak BPM grids unless `--force` is explicit.

   Plan mashups, not straight playlists. A normal basic move is to keep a rhythmically and harmonically compatible track as a filtered bed under a lead vocal/hook/section:

   ```bash
   python3 scripts/slime_audio_session.py mashup-bed runtime/mix-session.json \
     --bed-id bed-loop \
     --start 01:16.000 \
     --end 01:48.000 \
     --gain-db -8 \
     --lowpass-hz 1800 \
     --highpass-hz 100 \
     --state runtime/mix-session-state.json
   ```

   Use filtered full-track beds for now. Proper vocal/drum/bass/other stem mashups belong to the deferred stem-separation workflow and should not block basic filter-bed routines.

9. Dry-run the timestamped mix render before starting, or render only the future window when applying a live swap from the current playhead:

   ```bash
   python3 scripts/slime_audio_session_mixdown.py runtime/mix-session.json --output runtime/mix-render.wav --dry-run
   python3 scripts/slime_audio_session_mixdown.py runtime/mix-session.json --from 02:10.000 --output runtime/mix-render-from-playhead.wav --dry-run
   ```

   To send the operator a verifiable mix artifact, render an MP3 review file directly from the planned session:

   ```bash
   python3 scripts/slime_audio_session_mixdown.py runtime/mix-session.json \
     --output runtime/mix-review.mp3 \
     --format mp3 \
     --mp3-bitrate 192k \
     --verify
   ```

   Use `--from` and `--duration` for a short proof clip around a transition. Keep `--verify` on so empty/silent renders fail before upload.

10. Start playback from the native timestamped session runner:

   ```bash
   python3 scripts/slime_audio_session_runner.py \
     --session runtime/mix-session.json \
     --state runtime/mix-session-state.json \
     --target TARGET
   ```

Do not use legacy slot queues for DJ sets.

## Live Set Rules

- Treat the mix session, playback history, and commentary plan as live state.
- Extend the future queue while playback continues whenever possible.
- Add, remove, trim, move, or automate future timestamped clips; do not disturb audio already under the playhead unless explicitly asked.
- When extending, re-rank from the current or next track so the transition still makes sense.
- Treat complaints or steering from the operator as hard constraints for future selections.
- Keep a small scratchpad of current vibe, banned artists or genres, energy target, and planned arc in ignored runtime files.

## Shipped DJ Capabilities

These are part of the normal workflow, not future wishes.

- Database-backed candidate selection: use `slime_audio_candidates.py` against the library DB, recent `runtime/play-history.jsonl`, preferred-file routing, excludes, vibe/direction, and energy target. Candidate output should carry reasons the DJ can explain.
- Live future editing: use timestamped `mix-session.json` clips, not legacy queue slots. The session runner reloads future render windows, records `session_window_*` history, and future `add-clip`, `move`, `remove`, `add-mic`, and automation edits should use `--state` or `--lock-before` to protect audio under the playhead.
- Live commentary planning: use `slime_audio_commentary_planner.py` to add future mic lean-ins independently of music selection. It writes normal session lean-ins with ducking/low-pass automation and appends `commentary_planned` logs tying text to timing, track context, and reason.
- Tension-aware vocal windows: use `slime_audio_dj.py structure` for per-track intro/breakdown/build/drop/outro and `slime_audio_dj.py tension` for absolute mix-session drop windows with grounded `reason` and `talking_points`. Feed `runtime/tension-windows.json` to the commentary planner when available.
- Real mix planning: use `slime_audio_mix_planner.py` before playback and during future edits. It consumes cached track analysis, transition scores, beat-grid phrase lengths, detected build/drop windows, and live runner locks. It may create overlapped blends, drop-double clips, explicit clip fades, and master duck automation only when the transition clears tempo/key compatibility gates. Unsafe transitions should remain hard cuts; do not rely on renderer auto-crossfades or layer incompatible tracks just because two clips can overlap on the timeline.
- Rendered tempo/key correction: mixdown honors clip `tempo_shift_pct` and `pitch_shift_semitones`, so the planner may allow small beat/key-matched overlays when the renderer limits permit it. Keep correction ranges conservative, document the reason in planner move output, and set `--max-render-pitch-shift-semitones 0` for routines where key preservation matters more than harmonic correction.
- Key-fit policy: when more than one track plays at once, aim for exact key fit whenever the rendered correction is tasteful. For major/minor combinations, use the relative major/minor relationship to decide the correct transpose steps. Prefer keeping a compatible key lane for a run of tracks; only change key deliberately when the source song naturally modulates, the transition is short/non-overlapped, or the move is musically justified and documented.
- Mashup-first planning: DJ sets should be planned as mashups rather than playlists. Prefer one or more compatible rhythm/EDM clips as filtered beds under another lead track or section. Use `slime_audio_session.py mashup-bed` for gain plus low-pass/high-pass bed shaping, and render review files to verify the bed supports the lead instead of fighting it.
- Quantized beat jumps: use `slime_audio_session.py beat-jump` for +/-1/2, +/-1, +/-2, +/-4, and +/-8 beat offsets from cached BPM/beat-offset analysis. Prefer it over manual millisecond edits whenever planning instant doubles, half-beat delays, phrase jumps, or off-beat cuts. Do not use `--force` for normal DJ planning; forced low-confidence grids are only for debugging failed analysis.
- Metadata authority: BPM/key/Camelot must come from the music DB TuneBat fields. Ignore filename tags. If metadata is missing, use the local TuneBat analyzer to populate the DB before planning overlays, beat jumps, or doubles.
- Review file export: use `slime_audio_session_mixdown.py --output runtime/mix-review.mp3 --format mp3 --verify` to render the actual planned mix to a shareable file before or after playback. For transition QA, render a shorter window with `--from` and `--duration`, then upload or link that artifact for operator review.
- Live set constraints: use `slime_audio_candidates.py set-constraints` for persistent operator steering. Future candidate generation must respect the scratchpad after restarts.

## Receiver Health

When playback skips, a tray update fails, or a receiver seems wedged, verify receiver state before changing the set.

- Run discovery and read the reported app version, Snapcast listener state, exit count, last stderr/status, and telemetry path:

  ```bash
  python3 scripts/slime_audio_stream.py --target all --mode snapcast --dry-run --discover-timeout-ms 2500
  ```

- Do not assume an in-app/context-menu update succeeded. If one receiver remains on an older version while others report the release with needed telemetry, have the operator fully quit or kill stale tray/updater processes, install the current release manually, launch from the OS app menu, then re-run discovery.
- If a receiver reports the expected version but `shared_stream_listening=false`, restart only that listener before playback:

  ```bash
  python3 scripts/slime_audio_stream.py --target TARGET --mode snapcast --start-listeners --discover-timeout-ms 2500
  ```

- After updating or restarting listeners, run a short Snapcast file to all targets and re-run discovery. A clean receiver sanity check means the expected version is present, `shared_stream_listening=true`, `shared_stream_exits` did not increase during playback, and no decode failures are reported.
- If receiver telemetry stays clean but audible skips happen during a native session, compare sender/session logs with `session_window_*` history. Skips exactly on render-window boundaries usually point at the session runner or Snapcast FIFO handoff, not the tray receiver.
- In persistent Snapcast mode, keep one parent FIFO writer open across render windows and swap only the ffmpeg child input. Closing the FIFO between windows can make snapserver emit EOF and create audible gaps even while receiver clients remain healthy.

## Commentary

The DJ should host the set, not just play files.

- Prepare a short intro drop near the start of most sets.
- Add tasteful lean-ins every few minutes, with silence between them.
- Keep commentary short and focused on the music: mood, texture, rhythm, genre lineage, energy, transition intent, or why the next track fits.
- Use artist, lyrics, and release context when available, but verify uncertain facts before saying them.
- Look ahead for likely tension points using energy, BPM, key relation, beat offset, track position, and transition notes.
- Prefer `scripts/slime_audio_commentary_planner.py --session runtime/mix-session.json --state runtime/mix-session-state.json --tension-plan runtime/tension-windows.json` for live sets. It writes future mic lean-ins and `runtime/commentary-plan.jsonl` without restarting playback.
- Use `scripts/slime_audio_dj.py structure` to find raw-audio intro, breakdown, build, drop, outro, and pre-drop lean-in windows before placing commentary.
- Use `scripts/slime_audio_dj.py tension` to convert those windows into absolute mix-session timestamps with reasons and talking points. Keep talking points grounded in analysis facts; do not invent artist, release, or lyric context.
- Keep commentary planning separate from the music queue so new lean-ins can be added without restarting playback.

## Audio Clip Exports

When sending a standalone song section or drop clip to the operator, export it like a DJ edit, not like an arbitrary timestamp crop.

- Pick candidate source tracks from the library or local files first. Prefer tracks likely to have useful tension/release: dance, bass, pop, rock, or anything with strong section contrast.
- Analyze each candidate and read the structure output before cutting:

  ```bash
  python3 scripts/slime_audio_dj.py structure ./track.flac --cache runtime/dj-analysis-cache.json
  ```

- Prefer candidates with explicit `build`, `drop`, or `pre_drop` structure events. If several drops are present, choose the strongest musical moment by looking for:
  - a high-confidence build immediately before the drop
  - a drop start on a phrase boundary
  - a clear energy jump from the preceding section
  - enough room before the drop to include a short build or pre-drop cue
- If the structure detector finds multiple drop windows, compare their timestamps and confidence. A later drop with a stronger build can be better than the first detected drop.
- Quantize clip start and end to the detected beat grid, preferably phrase boundaries. Use the detected BPM and beat offset from the structure output; do not cut raw detector timestamps unless they already land on beat.
- Include enough pre-drop or build context for the cut to make musical sense, but keep the exported clip short.
- When using `ffmpeg`, explicitly map the audio stream and exclude embedded artwork/data streams. Some music files include cover art, and implicit stream selection can produce a silent-looking export:

  ```bash
  ffmpeg -y \
    -ss START_SECONDS \
    -t DURATION_SECONDS \
    -i ./track.flac \
    -map 0:a:0 -vn -sn -dn \
    -af "afade=t=in:st=0:d=0.03,afade=t=out:st=OUT_FADE_START:d=0.22,volume=0.85" \
    -codec:a libmp3lame -b:a 192k \
    runtime/drop-clip.mp3
  ```

- Verify the rendered clip is not silent before sending it:

  ```bash
  ffmpeg -hide_banner -i runtime/drop-clip.mp3 -af volumedetect -f null -
  ```

  Treat `mean_volume: -inf dB`, `max_volume: -inf dB`, or all-zero `astats` output as a failed export. Re-cut before uploading.
- Mention the source track and the beat/phrase alignment when sending the clip, especially if the clip is being used to judge the analyzer.

## Lean-Ins

Lean-ins are planned mix-session events, not immediate side streams.

- Always schedule lean-ins at an explicit mix timeline time with `--start`; do not fire them "now" unless the operator explicitly asks for an immediate test.
- Always set voice level deliberately with `--volume`; use the same gain-staging judgment as the previous working lean-in system.
- Pair lean-ins with music ducking and low-pass automation by default:

  ```bash
  python3 scripts/slime_audio_lean_ins.py \
    --session runtime/mix-session.json \
    --create \
    --start 01:20.000 \
    --text "quick note" \
    --volume 1.7 \
    --duck-volume 0.45 \
    --lowpass-hz 1400
  ```

- For Snapcast playback, render the planned session first, then stream the rendered file:

  ```bash
  python3 scripts/slime_audio_session_mixdown.py runtime/mix-session.json --output runtime/mix-session-render.wav
  python3 scripts/slime_audio_stream.py runtime/mix-session-render.wav --target TARGET --mode snapcast
  ```

- Do not use packet-mode lean-ins, direct UDP packet audio, or receiver-side packet effect envelopes for live mix commentary.
- Lean-ins should be editable future events: add, remove, move, and re-render before playback reaches them.

## Quality Bar

- Prefer database-backed candidates and preferred-file routing.
- Prefer explicit dry runs before live playback.
- Preserve playback state across restarts.
- Log enough history to explain what played, what was skipped, and why future choices were made.
- If a script is missing a capability needed by this workflow, open an issue or implement it in the script rather than encoding fragile behavior in the skill.
