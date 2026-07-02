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

The authoring model is time-based performance actions. Treat `clips` and `stem_groups` as renderable session data, not the editing primitive. New DJ edits should be expressed as actions such as `load_track`, `set_cue`, `jump_to_cue`, `pause`, `play`, `cue`, `seek`, `loop_start`, `loop_exit`, `stem_toggle`, and `knob_lerp`, then compiled into renderable `stem_groups`, clips, and automations.

There is no manual clip-drop command. Loading music starts with `load_track`, which resolves to real `vocals`, `drums`, `bass`, and `other` stem slots for the conceptual deck load. If an added `load_track` action does not already include stem paths, `add-action` pre-generates or reuses ready stem artifacts before saving the action so playback never waits on Demucs at load time.

Actions use absolute mix timestamps, but each loaded deck also has a deck clock. A `load_track` starts that deck clock at `trim_start_ms`; `set_cue` stores named source positions for that loaded track; `jump_to_cue` ends the current deck-clock segment and resumes the same conceptual load from the cue at the jump time; `pause` closes the audible deck segment and parks the source position; `play` resumes a parked deck or starts from an explicit cue/position; `cue` and `cue_seek` park a deck at a source cue without playing; `seek` jumps and resumes immediately unless `"play": false` is set; `loop_start` repeats a source-clock window until its exit time, then resumes the deck clock after the source loop window. When a load has `tempo_shift_pct`, loop `length_ms` remains source-clock length and the compiler converts it to rendered deck-clock duration; planners must quantize `exit_ms` to whole rendered loop repeats so the final loop is not a partial off-phrase tail. The compiler materializes those deck-clock moves into renderable stem-group segments while preserving the source action id. Transport targets can be a load id or a deck name.

Do not rely on implicit auto-crossfades. The renderer never invents fades; crossfader movement, overlap blends, and long fades must be explicit performance actions, planner transition plans, or named routines with a musical reason. The mix planner decides blend versus cut per adjacent pair from analyzed compatibility: compatible-key/tempo pairs get planned overlaps with transition carving, and pairs with missing analysis or incompatible keys/tempo stay explicit hard cuts. There is no global blends switch — the safety lives in the per-pair decision, not in a mode. Protective click fades are fine when authored intentionally, but hidden default handoff fades are not.

Sessions still support render-output clips and stem groups. Clips can overlap across up to four decks, and each clip can have trim, gain, EQ, filters, tempo/pitch changes, reverse/playback-rate flags, fades, and routine metadata.

Clip-level `play_stems` is honored by the renderer: mixdown premixes the ready stem artifacts for that source and renders only the requested stems, and it fails loudly when the artifacts are not ready instead of silently playing the full track. New planning code should still prefer `load_track` actions for stem work; autodj beds are always stem-resolved `load_track` actions and are skipped (with a recorded reason) when the bed source has no ready stems, rather than lying about being drums-only.

Common clip controls:

- `start_ms` places the clip on the mix timeline.
- `trim_start_ms` chooses the source-file cue.
- `duration_ms` limits the section used.
- `trim_db` matches source loudness before performance moves.
- `gain_db` is static clip placement.
- Top-level `deck_automations` targeted to `deck-1`, `deck-2`, `deck-3`, `deck-4`, or `deck-5` are the canonical channel fader and knob moves. Linear-curve point pairs render as actual glides (finely subdivided steps), so a fader ride or filter sweep is audible motion, not a value locked until the next point. Use deck `gain_db` for fader moves, deck `eq_low_db`/`eq_mid_db`/`eq_high_db` for EQ, and deck `lowpass_hz`/`highpass_hz` for filters. Mixdown resolves those absolute-timeline points onto the clip currently occupying that deck.
- Clip-targeted `gain_db` automation is reserved for clip-local gates/replacement moves, not normal deck fader state.
- `tempo_shift_pct` and `pitch_shift_semitones` are rendered beat/key correction controls. Keep them conservative and intentional. For overlapped full-track loads, key-fit must use full-track DB/TuneBat key metadata. Convert minor keys to their relative major by moving up 3 semitones, then pitch-shift the incoming load by the shortest semitone distance to the current load. If a song is missing full-track key metadata, run analysis and populate the DB before layering it. Only non-song drops/effects can bypass harmonic key metadata, and they should not be treated as full-track musical layers. Excessive required shifts mean the decks should not overlap.
- `reverse`, `playback_rate`, and `scratch_motion` are record-motion/scratch controls where speed and pitch move together.

Stem-heavy sessions can add top-level `stem_groups`. A stem group is one conceptual deck event with child `vocals`, `drums`, `bass`, and `other` streams that share timing, trim, tempo, pitch, reverse/rate, deck automation, and crossfader routing. Use this for vocal hooks, acapellas, and bass-controlled doubles instead of pretending full-band EQ is a real stem split. Details live in [Stem management](stem-management.md).

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
python3 scripts/slime_audio_live_edit.py add-action --action-json '{"type":"load_track","id":"break-load","deck":"deck-1","source_path":"/mnt/rockhouse/Music/example.flac","at":"01:12.000","trim_start":"02:04.000","duration":"00:32.000","play_stems":["drums","bass","other"]}' --reason "load future bed"
python3 scripts/slime_audio_live_edit.py add-action --action-json '{"type":"set_cue","target":"break-load","cue_id":"drop","position":"02:36.000","at":"01:13.000"}' --reason "name incoming drop"
python3 scripts/slime_audio_live_edit.py add-action --action-json '{"type":"jump_to_cue","target":"break-load","cue_id":"drop","at":"01:28.000"}' --reason "jump deck clock to drop"
python3 scripts/slime_audio_live_edit.py add-action --action-json '{"type":"pause","id":"pause-break","target":"deck-1","at":"01:36.000"}' --reason "park deck for repair"
python3 scripts/slime_audio_live_edit.py add-action --action-json '{"type":"play","id":"resume-break","target":"deck-1","at":"01:40.000"}' --reason "resume parked deck"
python3 scripts/slime_audio_live_edit.py add-action --action-json '{"type":"seek","id":"seek-break-hook","target":"break-load","position":"03:08.000","at":"01:42.000"}' --reason "skip to hook"
python3 scripts/slime_audio_live_edit.py add-action --action-json '{"type":"loop_start","target":"break-load","at":"01:44.000","position":"02:48.000","length":"00:04.000","exit":"01:52.000"}' --reason "hold 1-bar rhythm loop"
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

Playlist import is not a valid set-building path. All requested sets must go through the full system: selection intent, analysis, stem preparation where needed, native session actions or stem groups, beat/key decisions, proof renders, and an audit trail. If a text playlist exists, treat it as raw source material only.

For overlapping transitions, key fit is the default target. Use TuneBat-backed DB metadata where available, prefer exact same-key or relative major/minor compatible overlaps, and use conservative rendered correction only when it makes the overlap better. If song key metadata is missing, run the analyzer first. Unsafe transitions should remain hard cuts.

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

## Autodj Continue And Extend

`scripts/slime_audio_autodj.py` has two generation commands built around the fast-start/extend-behind-the-playhead model:

- `continue` builds a first playable buffer (about 5 minutes of actual scheduled timeline by default, `--min-runway-ms`) from database candidates, runs the planner, structural beds, the creative pass, and all DJ guards, then launches the native runner in windowed mode. It refuses to stomp healthy playback unless `--force`.
- `extend` appends a fresh planned block (default `--block-ms` 5 minutes) to the live session behind the playhead. It resolves the live session from `runtime/active-set.json` (or `--session`/`--state`), no-ops when at least `--ahead-ms` of music remains ahead of the playhead or when `--target-length-ms` is reached (default 30 minutes; `0` extends forever), excludes tracks already in the session, plans and guards the new block on a working copy, and only publishes atomically if the live file did not change in the meantime. New event ids are namespaced with an `ext-` prefix so repeated extensions never collide.

Run `extend` on a heartbeat or cron for continuous sets:

```bash
python3 scripts/slime_audio_autodj.py extend --target-length-ms 0
```

Each invocation is cheap when the buffer is full (it exits after the runway check), so a 1-2 minute cadence is fine. The runner picks up appended material automatically at the next render-window reload; `extend` never touches audio at or before the currently prerendered window.

## Mixing Pass

After the first live buffer is playing, keep running dedicated mixing passes on future material. The pass should classify each clip by role, set `trim_db` for source loudness, then use `gain_db`, EQ, filters, and fader automation to make the intended relationship audible.

For heartbeat/live extensions, run a creative pass before calling the block done. Add at least a couple of intentional moves when time and tooling allow: a beat/key matched bed, drum loop, double, stab, hook tease, echo/reverb throw, brake/scratch/loop roll, filter/EQ ride, crossfader move, or `deck-5` vocal drop. A straight playlist extension is only acceptable with an explicit restraint or emergency-runway reason.

Rhythm beds that are supposed to change the groove should normally start around `-6` to `-9 dB` under a full lead, then be adjusted by proof render. Dubstep, dnb, bass music, and other drop-forward beds often need to be closer to the lead than soft support textures. A bed at `-12 dB` or lower is a special-case ghost texture, not a normal groove layer. A bass/rhythm bed around `-13 dB` should fail review unless the operator explicitly asked for barely-there texture and the reason is documented.

Use EQ/filter carving before hiding a bed with fader level. If a proof still sounds like a straight playlist, or if the dashboard shows beds that cannot be heard, the set is not mixed yet.

## Effects And Routines

Use the session tools for effects and proofs. Important primitives:

- `echo` renders clean delayed wet taps.
- `reverb` renders a wet effect copy by convolving the source window with an impulse response synthesized from the effect's room/damping/feedback parameters (deterministic, no external plugins).
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
