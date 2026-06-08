# DJ Session Workflow

SlimeAudio DJ sets are planned session data. Do not treat them as a flat playlist or a hand-built ffmpeg graph.

## Core Files

- Active session: `runtime/mix-session.json`
- Active state: `runtime/mix-session-state.json`
- Named set archive: `runtime/sets/<slug>/`
- Active set pointer: `runtime/active-set.json`
- Playback/edit history: `runtime/play-history.jsonl`

Treat `runtime/mix-session.json` and `runtime/mix-session-state.json` as active pointers, not as the identity of the set. Durable set identity lives under `runtime/sets/<slug>/`; load or replay those sets intentionally with `scripts/slime_audio_sets.py`.

The live runner updates `runtime/active-set.json` when real playback starts, including when a named `--session` or `--state` path is used. The web dashboard chooses that active pointer before defaulting to `runtime/mix-session*`, so starting audio and updating the frontend are the same operation. If you intentionally run an isolated proof or dry run, use `--dry-run` or `--no-active-pointer`.

## Session Model

Sessions use absolute mix timestamps. Clips can overlap across up to four decks, and each clip can have trim, gain, EQ, filters, tempo/pitch changes, reverse/playback-rate flags, fades, and routine metadata.

Common clip controls:

- `start_ms` places the clip on the mix timeline.
- `trim_start_ms` chooses the source-file cue.
- `duration_ms` limits the section used.
- `trim_db` matches source loudness before performance moves.
- `gain_db` is static clip placement.
- Top-level `deck_automations` targeted to `deck-1`, `deck-2`, `deck-3`, `deck-4`, or `deck-5` are the canonical channel fader and knob moves. Use deck `gain_db` for fader moves, deck `eq_low_db`/`eq_mid_db`/`eq_high_db` for EQ, and deck `lowpass_hz`/`highpass_hz` for filters. Mixdown resolves those absolute-timeline points onto the clip currently occupying that deck.
- Clip-targeted `gain_db` automation is reserved for clip-local gates/replacement moves, not normal deck fader state.
- `tempo_shift_pct` and `pitch_shift_semitones` are rendered beat/key correction controls. Keep them conservative and intentional.
- `reverse`, `playback_rate`, and `scratch_motion` are record-motion/scratch controls where speed and pitch move together.

Deck convention for creative sets:

- Main full tracks normally alternate on `deck-2` and `deck-3`.
- `deck-1` and `deck-4` are useful for doubles, shadows, beds, and utility moves.
- `deck-5` is the dedicated vocal lane for mic lean-ins/TTS drops. Do not use it for music beds or doubles.
- Crossfader routing normally follows controller-style sides: `deck-1`/`deck-3` on `A`, `deck-2`/`deck-4` on `B`, and `deck-5` on `THRU`.

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
python3 scripts/slime_audio_live_edit.py fader-routing --assign deck-1=A --assign deck-3=A --assign deck-2=B --assign deck-4=B --assign deck-5=THRU --reason "DDJ-style routing plus vocal lane"
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
python3 scripts/slime_audio_analysis_preflight.py --session runtime/mix-session.json --from-ms 0 --horizon-ms 1800000
python3 scripts/slime_audio_mix_planner.py --session runtime/mix-session.json --state runtime/mix-session-state.json --max-render-tempo-shift-pct 4 --max-render-pitch-shift-semitones 2 --apply
```

The planner can add phrase-aware overlays, drop doubles, rendered tempo/key correction, transition automation, and top-level `transition_plans` explaining each adjacent-pair decision. For live repair passes, add `--cached-analysis-only --horizon-ms 1200000` so the planner only uses existing DB analysis and touches a bounded future block. A straight playlist import is not a finished DJ set.

For overlapping transitions, key fit is the default target. Use TuneBat-backed DB metadata where available, prefer exact same-key or relative major/minor compatible overlaps, and use conservative rendered correction only when it makes the overlap better. Unsafe transitions should remain hard cuts.

Before the planner runs, selection needs a taste pass:

- Write a one- or two-sentence intent for the next block.
- Check recent play history and the current/upcoming set for artist/crate repeats.
- Build candidates from multiple sources rather than one habitual search term.
- Reject tracks that are only technically compatible but do not support the block intent.
- For any strange left turn, write the bridge reason before keeping it.
- If the block leans on obvious fallback artists, novelty records, or compilation crates, rebuild it.

## Live Buffer

For live DJ requests, prioritize immediacy: get one suitable starter track playing through the native session runner, then build the larger set while the room already has music. Do not wait for a polished multi-track plan before starting unless the operator explicitly asked for offline prep.

After the starter is moving, add a brief smooth voiceover intro on `deck-5` when appropriate, then extend the active session with follow-up tracks, transitions, and flavor before the starter runs out. Downloads, elaborate commentary, full proof renders, and deeper crate work can happen while playback continues. If the remaining timeline is near the end of the current starter or buffer, extending continuity is higher priority than polishing already-safe future material.

## Mixing Pass

After the first live buffer is playing, keep running dedicated mixing passes on future material. The pass should classify each clip by role, set `trim_db` for source loudness, then use `gain_db`, EQ, filters, and fader automation to make the intended relationship audible.

For heartbeat/live extensions, run a creative pass before calling the block done. Add at least a couple of intentional moves when time and tooling allow: a beat/key matched bed, drum loop, double, stab, hook tease, echo/reverb throw, brake/scratch/loop roll, filter/EQ ride, crossfader move, or `deck-5` vocal drop. A straight playlist extension is only acceptable with an explicit restraint or emergency-runway reason.

Rhythm beds that are supposed to change the groove should normally start around `-6` to `-9 dB` under a full lead, then be adjusted by proof render. Dubstep, dnb, bass music, and other drop-forward beds often need to be closer to the lead than soft support textures. A bed at `-12 dB` or lower is a special-case ghost texture, not a normal groove layer. A bass/rhythm bed around `-13 dB` should fail review unless the operator explicitly asked for barely-there texture and the reason is documented.

Use EQ/filter carving before hiding a bed with fader level. If a proof still sounds like a straight playlist, or if the dashboard shows beds that cannot be heard, the set is not mixed yet.

## Effects And Routines

Use the session tools for effects and proofs. Important primitives:

- `echo` renders clean delayed wet taps.
- `reverb` renders a wet effect copy, typically through LADSPA `zita-reverb`.
- `vinyl_brake` renders deterministic speed/pitch slowdown.
- `scratch-cuts` uses attached effect-track clips on the scratched deck and locally ducks the parent.
- `loop-roll` repeats a one-beat source slice as attached effect-track clips and records a slip event so the source resumes in time.
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
