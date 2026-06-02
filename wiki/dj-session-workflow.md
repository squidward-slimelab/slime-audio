# DJ Session Workflow

SlimeAudio DJ sets are planned session data. Do not treat them as a flat playlist or a hand-built ffmpeg graph.

## Core Files

- Active session: `runtime/mix-session.json`
- Active state: `runtime/mix-session-state.json`
- Named set archive: `runtime/sets/<slug>/`
- Active set pointer: `runtime/active-set.json`
- Playback/edit history: `runtime/play-history.jsonl`

Treat `runtime/mix-session.json` and `runtime/mix-session-state.json` as active pointers, not as the identity of the set. Durable set identity lives under `runtime/sets/<slug>/`; load or replay those sets intentionally with `scripts/slime_audio_sets.py`.

## Session Model

Sessions use absolute mix timestamps. Clips can overlap across up to four decks, and each clip can have trim, gain, EQ, filters, tempo/pitch changes, reverse/playback-rate flags, fades, and routine metadata.

Common clip controls:

- `start_ms` places the clip on the mix timeline.
- `trim_start_ms` chooses the source-file cue.
- `duration_ms` limits the section used.
- `trim_db` matches source loudness before performance moves.
- `gain_db` and `gain_db` automation are channel fader moves.
- `eq_low_db`, `eq_mid_db`, and `eq_high_db` carve beds and leads.
- `lowpass_hz` and `highpass_hz` are filter moves and bed carving controls.
- `tempo_shift_pct` and `pitch_shift_semitones` are rendered beat/key correction controls. Keep them conservative and intentional.
- `reverse`, `playback_rate`, and `scratch_motion` are record-motion/scratch controls where speed and pitch move together.

Deck convention for creative sets:

- Main full tracks normally alternate on `deck-2` and `deck-3`.
- `deck-1` and `deck-4` are useful for doubles, shadows, beds, and utility moves.
- Crossfader routing normally follows controller-style sides: `deck-1`/`deck-3` on `A`, `deck-2`/`deck-4` on `B`.

## Main Tools

Validate and inspect sessions:

```bash
python3 scripts/slime_audio_session.py validate runtime/mix-session.json
python3 scripts/slime_audio_session.py summary runtime/mix-session.json
```

Make safe live edits against the active session:

```bash
python3 scripts/slime_audio_live_edit.py add-clip --id break-loop --deck deck-1 --path /mnt/rockhouse/Music/example.flac --start 01:12.000 --trim-start 02:04.000 --duration 00:32.000 --trim-db -3 --gain-db -6 --reason "add future bed"
python3 scripts/slime_audio_live_edit.py automate --target break-loop --param gain_db --points-json '[{"at":"01:12.000","value":-18},{"at":"01:16.000","value":-2}]' --reason "shape bed entrance"
python3 scripts/slime_audio_live_edit.py fader-routing --assign deck-1=A --assign deck-3=A --assign deck-2=B --assign deck-4=B --reason "DDJ-style routing"
python3 scripts/slime_audio_live_edit.py crossfader --points-json '[{"at_ms":84000,"value":-1},{"at_ms":88000,"value":1}]' --reason "planned fader cut"
```

Use `--force` only for deliberate repairs before the playhead.

Do not run multiple live-edit writes against the same active session in parallel. These commands perform read-modify-write updates; serialize them or one edit can overwrite another.

## Named Sets

Use `scripts/slime_audio_sets.py` for set identity and review artifacts:

```bash
python3 scripts/slime_audio_sets.py archive --title "Named set" --slug named-set --session runtime/mix-session.json
python3 scripts/slime_audio_sets.py list --json
python3 scripts/slime_audio_sets.py new --title "Scratch set"
python3 scripts/slime_audio_sets.py activate named-set --reset-state
python3 scripts/slime_audio_sets.py replay named-set --target all --reset-state
python3 scripts/slime_audio_sets.py save-loaded
python3 scripts/slime_audio_sets.py fork named-set --title "Named set revision"
python3 scripts/slime_audio_sets.py render --slug named-set --format mp3 --mp3-bitrate 128k --keep 3 --max-total-mb 256
python3 scripts/slime_audio_sets.py cleanup-renders --keep 3 --max-age-hours 12 --max-total-mb 256
```

Viewing an archived set in the dashboard must not overwrite the active pointers. `activate` and `replay` are the explicit loading steps. After editing a loaded set through live edit commands, run `save-loaded`.

## Mix Planning

Analyze tracks first, then apply planner edits:

```bash
python3 scripts/slime_audio_dj.py plan --playlist runtime/late-friday-fresh-playlist.txt
python3 scripts/slime_audio_mix_planner.py --session runtime/mix-session.json --state runtime/mix-session-state.json --max-render-tempo-shift-pct 4 --max-render-pitch-shift-semitones 2 --apply
```

The planner can add phrase-aware overlays, drop doubles, rendered tempo/key correction, and transition automation. A straight playlist import is not a finished DJ set.

For overlapping transitions, key fit is the default target. Use TuneBat-backed DB metadata where available, prefer exact same-key or relative major/minor compatible overlaps, and use conservative rendered correction only when it makes the overlap better. Unsafe transitions should remain hard cuts.

## Effects And Routines

Use the session tools for effects and proofs. Important primitives:

- `echo` renders clean delayed wet taps.
- `reverb` renders a wet effect copy, typically through LADSPA `zita-reverb`.
- `vinyl_brake` renders deterministic speed/pitch slowdown.
- `scratch-cuts` uses attached effect-track clips on the scratched deck and locally ducks the parent.
- `slip-brake` is phrase-safe color that resumes where the source would have been.
- `brake-drop` is a timing-changing brake that resumes late.

Effect-track clips use `kind`, `attached_deck`, and `effect_parent_clip_id` metadata. They should render on child lanes such as `deck-2-fx`, not consume normal music decks.

Example routine:

```bash
python3 scripts/slime_audio_live_edit.py instant-double-routine --source-id break-loop --id break-loop-scratch --recipe scratch-cuts --start 01:24.000 --cache runtime/dj-analysis-cache.json
```

## Rendering And Playback

Render proof clips through SlimeAudio:

```bash
python3 scripts/slime_audio_session_mixdown.py runtime/mix-session.json --from 01:10.000 --duration 00:45.000 --output runtime/mix-review.mp3 --format mp3 --verify
python3 scripts/slime_audio_session_mixdown.py runtime/mix-session.json --routine-id break-loop-stabs --output runtime/routine-proof.mp3 --format mp3 --report-output runtime/routine-proof.json --verify
```

Start native playback:

```bash
python3 scripts/slime_audio_session_runner.py --session runtime/mix-session.json --state runtime/mix-session-state.json --target all
```

Named set operations use `scripts/slime_audio_sets.py` for archive, activate, replay, save, fork, render, and cleanup.

Do not stream a rendered review MP3 as the main set unless the operator explicitly asks for file-only playback. Normal playback should use the native timestamped session runner so the dashboard and live-edit lock see the real set.
