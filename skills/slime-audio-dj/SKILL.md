---
name: slime-audio-dj
description: Use when planning, extending, or hosting SlimeAudio DJ sets from a local music library, including database-backed song selection, transition planning, live queue updates, and tasteful spoken commentary.
---

# SlimeAudio DJ

Use this skill as the operator guide for DJ work in this repository. Keep domain logic in the repo scripts; the skill should coordinate those tools instead of reimplementing them.

## Privacy

Keep this skill generic and portable.

- Do not include private hostnames, room names, share names, people, playback habits, credentials, or local network details.
- Store environment-specific defaults in local notes, ignored runtime config, or operator memory outside this skill.
- Treat public repo files as shareable by default.

## Core Tools

- `scripts/slime_music_library.py`: scan and query the SQLite music database.
- `scripts/slime_audio_dj.py`: analyze BPM, beat offset, key, Camelot code, energy, and transition compatibility.
- `scripts/slime_audio_playlist_runner.py`: run persistent playlists, preserve state, use preferred library files, write playback history, and optionally show DJ transition plans.
- `scripts/slime_audio_stream.py`: discover receivers and stream local files.
- `scripts/slime_audio_session.py`: maintain planned mix-session clips, mic lean-ins, and automation.
- `scripts/slime_audio_lean_ins.py`: add scheduled lean-ins to a mix session.
- `scripts/slime_audio_session_mixdown.py`: render session clips and lean-ins into one Snapcast-ready mix file.
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
   ```

   If the database is missing or stale, rescan configured sources before falling back to ad hoc filesystem search.

3. Build a short runway, not a whole night. Prefer 30-60 minutes of music so the live set can adapt.

4. Filter candidates against recent playback history, explicit operator constraints, and the requested mood or energy.

5. Analyze and rank transitions:

   ```bash
   python3 scripts/slime_audio_dj.py plan --playlist runtime/current-playlist.txt
   python3 scripts/slime_audio_dj.py rank ./current-track.flac --playlist runtime/candidates.txt --limit 12
   ```

6. Dry-run the queue before starting:

   ```bash
   python3 scripts/slime_audio_playlist_runner.py --playlist runtime/current-playlist.txt --target TARGET --dj-plan --dry-run
   ```

7. Start playback only after target selection is explicit:

   ```bash
   python3 scripts/slime_audio_playlist_runner.py --playlist runtime/current-playlist.txt --target TARGET --dj-plan
   ```

## Live Set Rules

- Treat the playlist, state file, playback history, and commentary plan as live state.
- Extend the future queue while playback continues whenever possible.
- Append or swap upcoming tracks; do not disturb the currently playing track unless explicitly asked.
- When extending, re-rank from the current or next track so the transition still makes sense.
- Treat complaints or steering from the operator as hard constraints for future selections.
- Keep a small scratchpad of current vibe, banned artists or genres, energy target, and planned arc in ignored runtime files.

## Commentary

The DJ should host the set, not just play files.

- Prepare a short intro drop near the start of most sets.
- Add tasteful lean-ins every few minutes, with silence between them.
- Keep commentary short and focused on the music: mood, texture, rhythm, genre lineage, energy, transition intent, or why the next track fits.
- Use artist, lyrics, and release context when available, but verify uncertain facts before saying them.
- Look ahead for likely tension points using energy, BPM, key relation, beat offset, track position, and transition notes.
- Keep commentary planning separate from the music queue so new lean-ins can be added without restarting playback.

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
