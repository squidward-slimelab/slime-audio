# DJ Capabilities

This page records the shipped DJ surface agents are expected to use. The detailed operator playbook lives in [skills/slime-audio-dj/SKILL.md](../skills/slime-audio-dj/SKILL.md).

## Live Editing

Use `scripts/slime_audio_live_edit.py` for active/future session edits. It defaults to `runtime/mix-session.json`, reads `runtime/mix-session-state.json` as the playhead lock, and appends `live_edit_applied` records to `runtime/play-history.jsonl`.

Supported edit concepts include:

- `add-action` for `load_track`, transport (`pause`, `play`, `cue`, `seek`), `stem_toggle`, and `knob_lerp`
- `move` and `remove`
- `automate`
- `add-mic`
- `add-effect`
- `beat-jump`
- `instant-double`
- `instant-double-routine`
- `fader-routing`
- `crossfader`

Manual clip dropping is not supported. Clips are render data, not the authoring surface.

Serialize live writes to the same session. Do not launch several live-edit commands in parallel.

## Track Selection

Use the music database and candidate tool instead of remembered favorites:

```bash
python3 scripts/slime_music_library.py stats
python3 scripts/slime_music_library.py search "query"
python3 scripts/slime_audio_candidates.py candidates "query" --recent-limit 40 --limit 12
```

For clean-room or fresh sets, treat novelty as a hard gate. Read recent play history, active/archive set playlists, and constraints before choosing tracks.

Selection must have governance, not just search results:

- Define a block intent before choosing tracks: energy, texture, genre lane, and why the block follows the current record.
- Pull candidates from multiple pools instead of one remembered artist or obvious folder.
- Check recent playback for repeated artists, albums, compilations, and moods.
- Cap repeat artists in short windows unless there is a clear mini-feature, callback, stem/routine use, or operator request.
- Reject obvious fallback compilations and chart/dance crates unless the operator explicitly asked for that.
- Require a reason for weird left turns. The bridge can be rhythm, timbre, key/tempo, lyric mood, lineage, or deliberate contrast.
- Run a taste lint before rendering: the block should have a legible arc, fresh names, and no long same-artist/same-crate rut.

If a block feels random, cheesy, or like the same few artists again, rebuild the candidate pool before mixing.

## Analysis Facts

Shipped persistent DJ analysis includes:

- beatgrid and beat offset
- phrase grid
- structure windows
- drop candidates
- named cues
- BPM, key, Camelot, and energy

Use DB/TuneBat metadata as the authority for BPM/key/Camelot. Raw local analysis is useful for structure windows, but filename tags are not authoritative.

Named cue kinds include `clean_intro`, `build`, `drop`, `hook`, `stabs`, `vocal`, `clean_outro`, `safe_loop`, and `pre_drop`. Use `--cue-kind` with routines when possible instead of hand-entered timestamps.

## Mixer Controls

Use the real mixer surface:

- `trim_db`: source input trim/loudness matching.
- `gain_db`: channel fader or static placement.
- `deck_automations`: canonical deck fader/EQ/filter state. Target deck names directly, for example `deck-2.gain_db`, so the knob move follows the deck timeline instead of a particular clip id.
- Clip-targeted `gain_db` automation: clip-local gates and replacement ducks only.
- `eq_low_db`: low shelf around 120 Hz.
- `eq_mid_db`: 1 kHz bell.
- `eq_high_db`: high shelf around 6.5 kHz.
- `lowpass_hz` and `highpass_hz`: filter moves and broad bed carving.
- `tempo_shift_pct` and `pitch_shift_semitones`: rendered tempo/key correction.
- `crossfader.position`: `-1` hard A, `0` center, `1` hard B.
- `fader_routing.deck_assignments`: normally `deck-1`/`deck-3` on A, `deck-2`/`deck-4` on B, and the dedicated vocal `deck-5` on THRU.

Hard lead ducks are risky. They are correct for replacement moves like scratches and brakes, but they can sound like a broken volume drop around echo stabs or bed flourishes. If an effect is too loud, lower the effect clip/send first.

Run a mixing pass after the creative edit pass. Beds and routines must be audible in the proof render, not merely present in session JSON. Rhythm/bass beds meant to drive the groove should usually sit around `-6` to `-9 dB` under a full lead after source trim and EQ carving; `-12 dB` or lower is a ghost-texture choice that needs a reason. A dubstep/dnb/bass bed at roughly `-13 dB` should fail review unless the operator explicitly asked for barely audible texture.

## Routines

Use named routines when they fit:

- `stabs`: quantized source/double stabs.
- `one-beat-trades`: short deck trades.
- `offbeat-swaps`: first cut on the half-beat, then alternating crossfader sides on half-beat gates.
- `hook-tease`: cue-backed future hook reveal.
- `echo-stabs`: gated double with echo tail.
- `echo-drop`: doubled moment with a larger tail/color.
- `loop-roll`: phrase-safe one-beat loop repeats attached to the source deck while the source keeps advancing underneath.
- `scratch-cuts`: sparse source-replacing transform scratches attached to the scratched deck.
- `slip-brake`: phrase-safe brake color that returns on time.
- `brake-drop`: timing-changing brake that splits the source and resumes late.

Scratch clips should be sparse and continuous, roughly 140-260 ms. Micro-slicing reads as glitch stutter, not scratching.

## Effects

Effects live in the session top-level `effects` collection and can target a clip, `deck:<name>`, `master`, or `all`.

- `echo`: clean delayed wet taps, decayed by `feedback`, optionally low-passed. It should sound like repeats, not comb-filter wobble.
- `reverb`: wet spatial copy via built-in convolution with a parameter-derived impulse response; presets map to room/damping starting points.
- `vinyl_brake`: replacement effect. The dry target must mute during the brake window while the slowed copy plays.

Routine-generated scratch/brake artifacts are `effect-track` clips attached with `attached_deck` and `effect_parent_clip_id`.

## Creative Standard

A DJ set should not be a straight playlist with tiny decorations. Use:

- compatible EDM/techno/house rhythm beds under leads
- EQ/filter carving to make beds audible without fighting the lead
- doubles, beat jumps, loop rolls, stabs, fader cuts, echoes, reverb throws, brakes, scratches, and lean-ins
- proof renders to confirm the moves sound intentional

Overlapped blends should have deck-level filter/EQ automation by default. Raw full-band song-on-song overlap is only acceptable for very short stabs or a deliberately tested mashup.

For showpiece requests, long vanilla stretches need a musical reason.

Creative work is the default for normal heartbeat/live extensions, not something the operator should have to request. Once runway is safe, each new block should include intentional moves such as beat/key matched overlays, drum loops, rhythm beds, doubles, stabs, echo/reverb throws, filter/EQ rides, crossfader motion, hook teases, scratches/brakes, or music-aware vocal drops. If a block remains a plain run of main songs, document the restraint reason or treat it as unfinished.
