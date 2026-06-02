# DJ Capabilities

This page records the shipped DJ surface agents are expected to use. The detailed operator playbook lives in [skills/slime-audio-dj/SKILL.md](../skills/slime-audio-dj/SKILL.md).

## Live Editing

Use `scripts/slime_audio_live_edit.py` for active/future session edits. It defaults to `runtime/mix-session.json`, reads `runtime/mix-session-state.json` as the playhead lock, and appends `live_edit_applied` records to `runtime/play-history.jsonl`.

Supported edit concepts include:

- `add-clip`, `move`, and `remove`
- `automate`
- `add-mic`
- `add-effect`
- `beat-jump`
- `instant-double`
- `instant-double-routine`
- `mashup-bed`
- `fader-routing`
- `crossfader`

Serialize live writes to the same session. Do not launch several live-edit commands in parallel.

## Track Selection

Use the music database and candidate tool instead of remembered favorites:

```bash
python3 scripts/slime_music_library.py stats
python3 scripts/slime_music_library.py search "query"
python3 scripts/slime_audio_candidates.py candidates "query" --recent-limit 40 --limit 12
```

For clean-room or fresh sets, treat novelty as a hard gate. Read recent play history, active/archive set playlists, and constraints before choosing tracks.

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
- `gain_db` automation: fader moves, fades, cuts, and replacement ducks.
- `eq_low_db`: low shelf around 120 Hz.
- `eq_mid_db`: 1 kHz bell.
- `eq_high_db`: high shelf around 6.5 kHz.
- `lowpass_hz` and `highpass_hz`: filter moves and broad bed carving.
- `tempo_shift_pct` and `pitch_shift_semitones`: rendered tempo/key correction.
- `crossfader.position`: `-1` hard A, `0` center, `1` hard B.
- `fader_routing.deck_assignments`: normally `deck-1`/`deck-3` on A, `deck-2`/`deck-4` on B, and the dedicated vocal `deck-5` on THRU.

Hard lead ducks are risky. They are correct for replacement moves like scratches and brakes, but they can sound like a broken volume drop around echo stabs or bed flourishes. If an effect is too loud, lower the effect clip/send first.

## Routines

Use named routines when they fit:

- `stabs`: quantized source/double stabs.
- `one-beat-trades`: short deck trades.
- `offbeat-swaps`: first cut on the half-beat, then alternating crossfader sides on half-beat gates.
- `hook-tease`: cue-backed future hook reveal.
- `echo-stabs`: gated double with echo tail.
- `echo-drop`: doubled moment with a larger tail/color.
- `scratch-cuts`: sparse source-replacing transform scratches attached to the scratched deck.
- `slip-brake`: phrase-safe brake color that returns on time.
- `brake-drop`: timing-changing brake that splits the source and resumes late.

Scratch clips should be sparse and continuous, roughly 140-260 ms. Micro-slicing reads as glitch stutter, not scratching.

## Effects

Effects live in the session top-level `effects` collection and can target a clip, `deck:<name>`, `master`, or `all`.

- `echo`: clean delayed wet taps, decayed by `feedback`, optionally low-passed. It should sound like repeats, not comb-filter wobble.
- `reverb`: wet spatial copy using Audacity-style defaults/presets and the local `zita-reverb` LADSPA plugin when available.
- `vinyl_brake`: replacement effect. The dry target must mute during the brake window while the slowed copy plays.

Routine-generated scratch/brake artifacts are `effect-track` clips attached with `attached_deck` and `effect_parent_clip_id`.

## Creative Standard

A DJ set should not be a straight playlist with tiny decorations. Use:

- compatible EDM/techno/house rhythm beds under leads
- EQ/filter carving to make beds audible without fighting the lead
- doubles, stabs, fader cuts, echoes, reverb throws, brakes, scratches, and lean-ins
- proof renders to confirm the moves sound intentional

For showpiece requests, long vanilla stretches need a musical reason.
