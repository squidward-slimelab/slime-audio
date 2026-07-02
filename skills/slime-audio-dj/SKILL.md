---
name: slime-audio-dj
description: Use when planning, extending, or hosting SlimeAudio DJ sets from a local music library, including immediate starter playback, database-backed song selection, transition planning, live queue updates, and tasteful spoken commentary.
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
- `scripts/slime_audio_analysis_preflight.py`: verify selected tracks already have local analyzer/DB coverage before live planning.
- `scripts/slime_audio_mix_planner.py`: rewrite future mix-session actions/render data into phrase-aware blends, drop doubles, and planned transition automation.
- `scripts/slime_audio_stream.py`: discover receivers and stream local files.
- `scripts/slime_audio_session.py`: maintain planned mix-session actions, rendered stem groups/clips, mic lean-ins, and automation.
- `scripts/slime_audio_live_edit.py`: safely edit the active live session through the existing state-locked edit primitives and record edit history.
- `scripts/slime_audio_lean_ins.py`: add scheduled lean-ins to a mix session.
- `scripts/slime_audio_commentary_planner.py`: add tasteful future commentary lean-ins with spacing, context, and logs.
- `scripts/slime_audio_session_mixdown.py`: render compiled session actions/stem groups/clips and lean-ins into one Snapcast-ready mix file.
- `scripts/slime_audio_session_runner.py`: run the native timestamped session in live-editable render windows.
- `scripts/slime_audio_tts.py` and `scripts/slime_audio_drops.py`: legacy Spotify/drop helpers; do not use them for Snapcast-era mix lean-ins unless explicitly working on legacy Spotify playback.

## Immediacy First

When the operator asks for a live mix, get pleasant audio playing quickly, then build the larger set while that first track buys time. Do not spend 10-15 minutes silently planning before the room hears anything unless the operator explicitly asked for offline prep or a proof render.

Default live sequence:

1. Pick one suitable starter track from the library or active request context and start playback fast through the normal live path for the target room(s).
2. Add or schedule a short smooth voiceover intro on `deck-5` once audio is moving. Keep it relaxed, clear, and brief; it should introduce the vibe without blocking the music.
3. While the starter plays, build the real native session behind it: select follow-up tracks, analyze structure/cues, plan transitions, and add mix flavor on future windows.
4. Use the live edit API to extend the active set before the starter runs out. Prefer clean simple continuity over waiting for a perfect complex plan.
5. After continuity is secured, keep improving the upcoming material with beds, doubles, EQ/filter rides, effects, and commentary.

The quality bar still matters, but latency is part of quality. A decent first song now plus a better mix in progress is usually better than a polished idea arriving after the room has been waiting in silence.

Frontend state is part of the live-start contract. When starting real playback from a named session/state, use the native session runner so it writes the active dashboard pointer, or explicitly pass `--active-pointer runtime/active-set.json`. The dashboard should show the same session the room is hearing. Use `--no-active-pointer` only for isolated proofs or debugging where the frontend should not follow playback.

## Edit API Mix-Building Playbook

When the operator asks for a mix, proof, routine, or revision, build it through the session edit API. The job is not to make an external audio collage; the job is to express the mix as editable session data so the runner, dashboard, review renders, and future agents all see the same thing.

Use `scripts/slime_audio_live_edit.py` for active or future edits against the live session. Use `scripts/slime_audio_session.py` for offline setup against a named session file. These commands expose the same mix primitives: `add-action` for `load_track`/`stem_toggle`/`knob_lerp`, `move`, `remove`, `automate`, `add-effect`, `slip`, `fader-routing`, `crossfader`, `beat-jump`, `instant-double`, `instant-double-routine`, and `mashup-bed`.

The normal offline/proof sequence is:

1. Select database-backed tracks, analyze cues/structure, and create or activate a named set.
2. Build the base timeline with `load_track` actions on the middle decks. The edit command must resolve each load to real `vocals`, `drums`, `bass`, and `other` stem slots before saving; if stems are missing, it pre-generates them instead of deferring Demucs to playback time.
3. Use the edit API to add trims, faders, EQ, beds, doubles, routines, effects, lean-ins, and crossfader motion.
4. Audit the session data before rendering: deck layout, load actions, stem slots, attached effect lanes, source trims, gain automation, EQ, effect targets, and unexplained hard ducks.
5. Render the actual session with `slime_audio_session_mixdown.py --verify`, inspect the report or levels, then send the MP3 if the operator asked for proof.
6. Save the named set after live edits with `slime_audio_sets.py save-loaded`.

Never render a QA sample by directly hand-writing an ffmpeg filter graph that bypasses the SlimeAudio session/edit tools. That can prove DSP in isolation, but it does not prove the product. If the edit API cannot express the move, add or fix the API first, then make the proof through the session renderer.

### Required Validation Gate

Before starting a newly generated set, run the same product-level checks that protect live playback. Do not rely on a clean raw render alone.

Minimum gate for normal/autodj sessions:

```bash
python3 scripts/slime_audio_session.py validate runtime/mix-session.json
python3 scripts/slime_audio_autodj.py validate-session runtime/mix-session.json
# For stem-aware/remix sets, add:
python3 scripts/slime_audio_autodj.py validate-session runtime/mix-session.json --require-stem-loads
python3 scripts/slime_audio_vocal_cues.py audit-vocal-overlap runtime/mix-session.json
python3 scripts/slime_audio_vocal_cues.py audit-session runtime/mix-session.json
python3 scripts/slime_audio_session_mixdown.py runtime/mix-session.json \
  --output runtime/proof.wav \
  --duration 00:02:00 \
  --verify \
  --audit-balance \
  --fail-balance-audit
```

Autodj `continue` runs its internal playlist, transition-decision, harmonic-overlap, and vocal guards before launching. Keep those guards enabled for live work. Manual session and live-edit output must pass `validate-session` before playback. A failure means the set is not ready; fix keys, tempo shifts, trims, vocal overlap, cue alignment, or bed balance before streaming.

For any set described as stem-aware, remix-focused, mashup-first, or using stem controls, `validate-session --require-stem-loads` is mandatory. Database music tracks must be loaded as `load_track` actions with resolved stems. Clip events are not a music-loading route; reserve them only for short non-musical samples, drops, scratches, and effects when they are marked with an appropriate sample/effect role.

For vocal hooks/acapellas, use `scripts/slime_audio_vocal_cues.py align-session` or equivalent planner output so `vocal_alignment` metadata records the source phrase anchor. The audit must pass whether the vocal enters before the drums, on the drums, or a beat or more after them.

### Music Acquisition

If the requested mix needs material that is not already in the database, acquire it first, put it on a mounted music share, then load it into the library before using it in a session. Do not build sets from temporary download folders.

Use the local Soulseek/sldl wrapper named in operator notes. Pick a destination from the mounted music roots by checking free space and writeability, not by habit:

```bash
df -h /mnt/* 2>/dev/null
find /mnt -maxdepth 3 -type d -name Music 2>/dev/null
```

Prefer the mounted `Music` root with enough free space for the full download plus render headroom. If several roots are healthy, prefer the one that the music library would route as the strongest source. Put new material under a clearly labeled incoming/library folder inside that `Music` root, grouped by purpose, genre, artist, or project so future scans are readable:

```bash
mkdir -p "/mnt/SHARE/Music/_Slime Incoming/Purpose or Genre"
sldl-slimelab "artist title or playlist url" \
  --path "/mnt/SHARE/Music/_Slime Incoming/Purpose or Genre" \
  --write-playlist
```

After downloading, verify files exist, then scan the library and confirm the new tracks are queryable before building the mix:

```bash
python3 scripts/slime_music_library.py scan
python3 scripts/slime_music_library.py search "new track or artist"
python3 scripts/slime_audio_candidates.py candidates "new track or artist" --limit 12
```

Run TuneBat/local DJ analysis on selected downloads before using them for overlays, beat jumps, doubles, or beds:

```bash
python3 scripts/slime_music_library.py analyze-tunebat-local DUPLICATE_KEY
python3 scripts/slime_audio_dj.py structure "/mnt/SHARE/Music/path/to/file.flac"
python3 scripts/slime_audio_dj.py cues "/mnt/SHARE/Music/path/to/file.flac"
```

If a network share is nearly full, mounted read-only, or missing, choose another share and document the reason in the runtime notes or set notes. Clean up failed partial downloads before rescanning so the database does not index junk.

### Mix Knobs

Use these controls deliberately. They are part of the creative surface, not hidden implementation details.

- `trim_db`: input trim for loudness matching. Set this once per load/source so tracks enter the mixer at comparable level.
- `gain_db`: static load placement only. Performance fader/EQ/filter moves belong in `knob_lerp`/top-level `deck_automations` targeted to deck names such as `deck-2`.
- `trim_start_ms` / `duration_ms`: source window. Use these for cueing hooks, drops, loops, and phrase-safe sections.
- `fade_in_ms` / `fade_out_ms`: click protection and short musical fades. Do not use them to fake an effect tail.
- `tempo_shift_pct` / `pitch_shift_semitones`: rendered beat/key correction. Keep it conservative and explain why it helps the overlap.
- `playback_rate` / `reverse`: record-motion scratch material where speed and pitch move together.
- `lowpass_hz` / `highpass_hz`: deck filter moves and bed carving. Use `knob_lerp` or deck automation on the deck, not clip-local automation.
- `eq_low_db` / `eq_mid_db` / `eq_high_db`: deck EQ. Use this before burying a bed with extreme filters.
- `send_reverb`, `duck_volume`, and effect `wet` / `gain_db`: send-style effect balance. If an effect is too loud, lower the send/effect first instead of ducking the lead into a weird hole.
- `crossfader.position` plus `fader_routing.deck_assignments`: controller-style cuts and blends between deck sides.
- `deck-5`: dedicated vocal channel for `add-mic`/TTS lean-ins. Keep it `THRU` in fader routing and do not use it for beds, doubles, scratches, or normal music loads.

### Mixing Pass

After the first live buffer is playing, keep doing dedicated mixing passes on future material. Do not treat this as optional cleanup; it is the step that makes beds, doubles, vocals, and effects actually audible in the final set.

Mix in this order:

1. Set `trim_db` per source for loudness matching before touching fader balance. If a source is already quiet, raise or normalize its trim instead of compensating with strange automation later.
2. Classify each load/stem group as lead, rhythm bed, ghost texture, double/routine, effect, or vocal. The intended role determines its level.
3. Balance with deck `gain_db` fader automation, then carve with deck EQ and filters. For rhythm beds, prefer EQ and high-pass/low-pass carving over burying the whole deck.
4. Render and listen to the overlap windows where beds matter. A dashboard that shows a bed is not proof that the bed is audible.
5. Fix failed windows, then render again.

Practical level guidance:

- Lead loads usually sit near the reference level after trim, with performance fader moves doing the transitions.
- Rhythm beds that are supposed to change the groove should normally start around `-6` to `-9 dB` fader/gain under a full lead, then be adjusted by ear. Dubstep, dnb, bass music, and other drop-forward beds often need to be closer to the lead than a soft pad would be.
- `-10 dB` can work for a supportive bed only when the proof still clearly shows the groove.
- `-12 dB` or lower is a special-case ghost texture, not a normal EDM/dubstep/dnb bed level. If a bass/rhythm bed sits around `-13 dB`, fail the mix review unless the operator explicitly asked for barely-there texture and the reason is documented.
- TTS/vocal drops on `deck-5` should be plainly intelligible over the music. Use deliberate vocal volume plus temporary duck/low-pass automation instead of hoping the vocal cuts through.
- Effects and doubles should read as musical gestures. If they disappear in the full mix, raise the gesture or create space; if they create a volume hole, inspect ducks and fader routing.

Mix review should fail when:

- every bed is hidden below roughly `-12 dB`
- a rhythm/bass bed is described as important but is inaudible in the proof
- low-pass/high-pass settings remove the identity of the bed and the fader is also low
- TTS is unintelligible or routed through a music deck instead of `deck-5`
- hard ducks are present without a replacement move like scratch/brake
- the render report is clean but the musical proof still sounds like a straight playlist

### EQ And Filter Performance

DJ EQ/filter work is a musical handoff tool, not a checkbox. Tutorial references: Club Ready DJ School, "5 DJ EQ Techniques That Instantly Improve Your Mixes"; Club Ready DJ School, "3 PRO DJ FILTER TECHNIQUES to enhance your transitions"; Club Ready DJ School, "DJ Transitions Masterclass - Master Phrasing, EQ & Filters".

Important lessons to encode in the session:

- EQ is more than swapping bass. Use it to prevent frequency clashes, control energy, make vocals/melodies readable, soften or brighten transitions, and create tension around choruses/drops.
- Low EQ/bass controls weight and kick/bass ownership. Do not leave two full basslines fighting. Time bass swaps to phrase boundaries, new sections, drops, or chorus entries.
- Mid EQ controls vocals, hooks, guitars, and melodic identity. When a bed fights a lead vocal or hook, carve mids on the bed instead of only turning the whole bed down.
- High EQ controls hats, brightness, and perceived sharpness. Pulling highs can soften an incoming/outgoing track; restoring highs can make a section open up.
- Filters should move with intention. Use high-pass/low-pass sweeps to introduce, thin, build tension, or exit a layer. A static low-pass/high-pass value for an entire bed is only basic carving, not a DJ filter move.
- Make changes audible but not cartoonish. Gentle EQ shifts often beat huge cuts; hard kills are for deliberate drops, swaps, or special effects.
- The listener should be able to hear why the knob moved. Every important overlap should have a plan: which source owns bass, which source owns mids/hooks, which source owns highs/texture, and when ownership changes.

Session review should fail when EQ/filter use is only token automation: identical static high-pass/low-pass points on every bed, no mid/high decisions, no phrase-timed bass handoffs, no filter rides, or no audible difference in the proof.

### Beatmatching Acceptance Bar

For any session with layered songs, doubles, beds, or long overlaps, beatmatching is a hard requirement. Do not accept a mix because the loads merely start near plausible timestamps.

- Analyze every overlapped source with `slime_audio_dj.py structure` or equivalent cached analysis so BPM, beat offset, and phrase length are known before arranging.
- Use the local music analyzer and SlimeAudio database as the source of truth for BPM, beat offset, phrase length, key, and duration. Do not use web BPM/key lookups for live planning except as a clearly labeled last-ditch note when the local analyzer/database is unavailable; never write those web values into the mix as authoritative analysis.
- Choose a target tempo and compatible key for each planned overlap before placing the incoming `load_track` action. Apply `tempo_shift_pct` and, when needed, conservative `pitch_shift_semitones` so the tracks do not rhythmically or harmonically clash.
- Treat tempo and pitch correction as part of the default mix plan, not as an emergency rescue. Simultaneous musical material must be beatmatched and keymatched, even if the overlap is short. If sources cannot be made compatible, choose another DJ transition: hard cut, brake, echo out, filter swap, silence gap, drums-only bridge, or another deliberate non-overlap move.
- Every changing-source handoff needs an explicit transition decision. Zero `tempo_shift_pct` plus zero `pitch_shift_semitones` is valid for an overlap only when a `transition_plans` record or action-level beat/key metadata says the analyzed BPM/key already match. Zero shifts are also valid for an explicit non-overlap cut/brake/echo transition, but that does not count as a mashup by itself.
- Align load starts to the same beat grid: start overlays on downbeats or phrase boundaries, not arbitrary seconds. Use the detected beat offset and phrase length to compute starts after tempo correction.
- Verify drift across the overlap. A bed that is aligned on bar 1 but audibly flams by bar 16 is not beatmatched. Shorten the layer, change the target tempo, or choose a better bed.
- For house/club beds under non-house leads, prefer loopable intro/outro/drop sections with stable drums. If the lead has loose live timing, use shorter phrase windows and re-cue at phrase boundaries instead of forcing a long fake sync.
- Render a proof window that includes the overlap start, middle, and exit. If the report is clean but kicks/snares drift or phase badly, fail the session and rebuild.

Default transition build order:

1. Analyze both tracks for BPM, beat offset, phrase length, key, Camelot code, and confidence.
2. Pick the outgoing or section target tempo. If neither track can reach it with a tasteful `tempo_shift_pct`, do not make a long blend.
3. Pick the key relationship. Use small `pitch_shift_semitones` only when it improves harmonic fit without making the source sound wrong.
4. Place the incoming cue on a phrase/downbeat after applying the tempo decision.
5. Add fader, EQ, filter, and optional effect automation to make the handoff intentional.
6. Validate with a proof render or, for live sets, confirm the future prerender window includes the corrected action/render data.

Do not use hidden volume sag as a generic transition. Avoid long automatic `fade_out_ms`, unexplained `gain_db` dips, and low-level master `duck_volume` automation unless there is a clear replacement event such as a vocal lean-in, scratch, brake, or deliberate crossfader cut. If the main record fades down for a few seconds and the listener cannot hear why, the transition is wrong.

### Live Beatmatch Rescue

When a live set is already playing and the operator says drops or blends sound off, fix the future timeline immediately instead of waiting for a full planner pass.

- Do not edit the current or already-prerendered window. Read the runner state and choose a future lock at or after the next safe render boundary, usually `window_end_ms` plus a small safety margin.
- Work on a small upcoming block first, about 8-10 loads/actions. The priority is to make the next few transitions better before they are rendered.
- Use cached BPM/key/structure analysis when it exists. If deeper analysis is too slow for live timing, stop it and apply a conservative rescue rather than missing the transition.
- Before a live repair pass, run `slime_audio_analysis_preflight.py` on the future block. If coverage is missing, use `slime_audio_mix_planner.py --cached-analysis-only --horizon-ms ...` so missing analysis becomes explicit cuts instead of slow decoding or fake blends.
- Remove unsafe generic overlaps. For mismatched material, use a deliberate non-overlap transition such as a hard cut, brake, echo out, filter swap, silence gap, or drums-only bridge. Do not leave two unkeymatched songs playing together just because the overlap is brief.
- Add or adjust future `load_track`, `stem_toggle`, and `knob_lerp` actions through `slime_audio_live_edit.py` with conservative `tempo_shift_pct` only where analysis is credible. Do not invent tempo shifts for unknown tracks.
- Do not add deck gain dips just to make a transition visible. Prefer EQ/filter/crossfader moves; use gain automation only for a clearly audible, intentional cut or replacement move.
- If an effect write fails validation, skip it or retarget correctly; never force invalid session data during live playback.
- Move the downstream tail to preserve the new overlap spacing so the later timeline does not collapse.
- Validate the session and watch `runtime/play-history.jsonl` for the next `session_window_prerendered` event that includes the patched actions/render data. The rescue is not done until the runner has accepted the future window.

### Creative Moves

Prefer named edit-api routines when they fit, then customize with automation or effects. A good mix should have audible intent: doubles, stabs, filters, beds, brakes, echoes, scratches, crossfader cuts, lean-ins, or tension/release. If the operator asks for a showpiece, do not let long stretches play vanilla unless the restraint is the actual choice.

- `mashup-bed`: legacy render-data helper for carving an already-loaded rhythm bed under a lead. Prefer `load_track` plus `knob_lerp` for new bed authoring.
- EDM beds: make heavy use of rhythmically stable electronic/techno/house/dubstep/dnb/bass music/club tracks underneath lead songs, vocals, hooks, and pop material. Keep the bed audible enough to change the groove, not so buried that the mix reads as vanilla. Use EQ/filter carving, trim, and fader automation to leave room for the lead.
- `instant-double`: clone a source onto another deck at the same musical position for trades, cuts, and layered emphasis.
- `stabs` / `one-beat-trades` / `offbeat-swaps`: quantized double routines for audible DJ-style motion.
- `hook-tease`: briefly reveal a hook or cue as a future hint.
- Hook teases, callbacks, and third-deck teasers must resolve like DJ moves, not orphaned overlays. Put them on a phrase boundary, keep them intentionally short as a stab/1-2-4-8 bar cue, then cut or echo out cleanly. If the borrowed hook stays longer than a phrase gesture, treat it as a real bed/layer instead: carve with EQ/filtering, set an audible but balanced gain, and plan a phrase-safe exit. Avoid arbitrary 8-12 second teases that appear, drift under the lead, and vanish without musical payoff.
- Do not preview the next track for several bars before its actual entrance as a default transition. Incoming-track preview doubles often sound like spoilers or accidental early drops in room playback. Use them only when the operator explicitly wants teaser/callback DJ tricks, and keep them short, gated, and musically resolved.
- `echo-stabs`: gated double plus echo tail. Echo is a wet send; the dry source should usually keep playing unless the routine intentionally trades it.
- `echo-drop`: gated double plus reverb tail for a larger moment.
- `scratch-cuts`: sparse source-replacing transform scratches. Scratch child clips must stay attached to the deck being scratched, not become independent music decks.
- `slip-brake`: phrase-safe brake color that returns exactly on time.
- `brake-drop`: real timing brake that mutes/replaces the source during the slowdown and resumes late from the pre-brake source position.
- `add-effect`: custom `echo`, `reverb`, or `vinyl_brake` events on a clip, deck, master, or all.
- `add-mic` / commentary planner: short hosted lean-ins on `deck-5` with explicit voice volume, ducking, and low-pass automation.

### Effect Semantics

Treat each effect according to what it is supposed to do in the mix.

- `echo`: a delayed wet copy that decays by feedback. It should sound like repeats, not garbled volume wobble. Validate suspect echo with a simple arpeggio session.
- `reverb`: a spatial wet copy with Audacity-style preset starting points. Pick presets for color, then tune wet level, tail, gain, room size, damping, and low-pass.
- `vinyl_brake`: a replacement effect. During the brake window, the dry target should be muted while the slowed record-motion render plays.
- Scratches: replacement performance gestures. The source deck timeline should keep moving underneath, but the local dry source should be ducked only during the scratch clips.

Hard source ducks are dangerous. They are correct for replacement moves like scratches and vinyl brakes, but they sound broken when placed after an echo stab or bed flourish with no obvious reason. If there is an audible volume cliff, inspect `gain_db` automation before blaming the effect DSP.

## Creative Set Workflow

Do not build a straight playlist and call it a DJ set. The default product is an edited arrangement: lead songs plus key/beat-matched EDM beds, visible routines/effects, and recurring TTS drops about the music. `slime_audio_mix_planner.py` is only a helper for beat/key-safe transitions; it is not the creative pass.

### Mandatory Creative Pass

After continuity/runway is safe, creative DJ work is the default, not a special request. Every new block or heartbeat extension should receive a creative pass unless the operator explicitly asked for restraint or there is a documented live-safety reason to defer.

The creative pass should add intentional musical behavior such as:

- beat/key matched overlays or mashup beds
- drum loops or rhythm beds under lead songs
- instant doubles, stabs, one-beat trades, or hook teases
- echo/reverb throws, slip brakes, vinyl brakes, scratches, or proven cue-specific routines
- deck-level EQ/filter rides and crossfader motion
- short music-aware vocal drops on `deck-5`

A plain sequence of main loads plus minor fades is unfinished DJ work. If a future block has no beds, routines, effects, filter/EQ movement, or vocal lane, write the reason before accepting it. Otherwise keep editing future material until the set has audible intent.

Use proof renders for risky routines and for any bed/overlay whose audibility is uncertain. The test is not whether the dashboard shows the move; the move must be audible and musically justified. Do not use loop rolls as generic automatic decoration; they are only acceptable when a specific loop point, phrase role, and proof render make the move sound intentional.

### Selection Governance

Before choosing a future block, write a short block intent in runtime notes or the set notes. The intent should say the energy target, texture, likely genre lane, and the reason the block belongs after the current music. Do not pick tracks first and invent the story afterward.

Use a candidate pool, not a memory loop:

1. Read recent playback and the active/upcoming set so repeated artists, albums, crates, and moods are visible.
2. Pull candidates from at least three sources when possible: adjacent library search terms, compatible analysis metadata, and deeper local folders or less-used artists.
3. Reject obvious compilation/fallback crates unless the operator explicitly asked for that sound.
4. Prefer one clear lead idea plus supporting alternates. If a left turn is selected, document the bridge: shared rhythm, timbre, key/tempo relationship, lyric mood, historical lineage, or deliberate contrast.
5. Cap repeated artists and obvious fallback names. A second track from the same artist in a short window needs a reason such as a planned mini-feature, callback, stem/routine use, or operator request.
6. Avoid long same-artist or same-crate runs unless the operator asked for a focused feature. Variety should be audible across artists, eras, textures, and energy.

Run a taste lint before rendering or extending:

- Can a listener infer what the block is going for?
- Are there too many familiar fallback artists, novelty records, or obvious chart/dance compilations?
- Is every weird pick doing a job, or is it just random?
- Does the block move from the current track naturally enough, or is it a jarring folder-search accident?
- Are there enough fresh names that the set does not sound like the same few local habits?

If the answer is weak, rebuild the candidate pool before mixing. Technical compatibility does not rescue bad taste.

### Immediate Playback Rule

For live DJ requests, get music playing quickly. Do not wait to fully design, render, or QA an entire set before starting playback. Build the smallest credible editable session first, start the native runner, then keep improving the future timeline while audio is already playing.

- Target an initial playable buffer of about 5 minutes (`300_000 ms`) before starting. This can be two or three analyzed lead tracks with planner transitions and basic deck filter/EQ carving.
- Once playback starts, keep at least about 5 minutes of future music ahead of the playhead. If the remaining scheduled timeline is near or below that buffer, extending the queue is the priority.
- Use known-good indexed library material first. Do not block first playback on downloads, deep crate digging, full proof renders, or elaborate commentary. Those can happen while the set is running.
- Do not edit audio already under the playhead. Use `slime_audio_live_edit.py` and the state lock to add, move, automate, and decorate future events.
- Proof renders are for review/debug or high-risk routines. They should not delay starting a normal live set unless the operator explicitly asks for a proof before playback.

### Creative Baseline

For most requested sets, aim for this shape unless the operator explicitly asks for restraint:

- Every lead song should have a compatible EDM/techno/house/dubstep/dnb/bass music/club bed for a meaningful section of its runtime. Use `load_track` actions with real stems, `stem_toggle`, filter/EQ automation, and trim/gain controls to make the bed audible without fighting the lead.
- Use BPM/key/Camelot analysis to construct overlays, not just to sort songs. Prefer exact or corrected key-fit for layered vocals/hooks and rhythm beds.
- Add visible edit-api moves throughout the set: filter rides, EQ carving, echo/reverb throws, instant doubles, stabs, offbeat swaps, hook teases, scratches, slip brakes, or brake drops.
- Add a healthy amount of TTS vocal drops. They should be short, spaced out, and about what is happening in the songs: texture, groove, genre lineage, energy, transition intent, a hook, or why the next layer fits.
- The dashboard should visibly show the arrangement: main decks, bed/utility decks, attached `deck-N-fx` lanes, the `deck-5` vocal lane, effects, fader motion, automation, and mic lean-ins. If it visually looks like one song after another, the set is not done.

### Hard Techno/DNB/Dubstep Vocal Remix Lane

Use this as the default "cool mix" lane when the operator asks for more energetic or intentional DJ work and has not requested another genre direction. The musical target is vocal/hook-forward songs rebuilt over hard-techno, dnb, dubstep, bass, jungle, breakbeat, or related rhythm material.

Planning rules:

- Pick one strong vocal/hook lead and one rhythm/bass bed idea before filling the rest of the block.
- Prefer cached `drop`, `hook`, `build`, and `breakdown` anchors over intro-only windows. Short detected drop/build markers are valid cue anchors when extended into a phrase-safe source window.
- Use 32-beat pre-drop cueing for dnb/dubstep drops when metadata supports it. For fakeouts, use 4/8-bar phrase lengths and resolve into a real drop, hard cut, echo throw, or bass trade.
- Double drops need compatible BPM, key/Camelot, phrase position, and low-end ownership. If both sources have active bass/sub, only one keeps the low end unless the routine deliberately chops or trades bass.
- Do not lay full vocals over full vocals. Use stem `vocal_absent` or `instrumental_pocket` windows, or chop/gate the borrowed vocal so it reads as a routine rather than clutter.
- Full songs count as vocal-bearing unless they are explicitly marked instrumental/no-vocal. Before rendering or playing a set with overlapping full songs, run:

  ```bash
  python3 scripts/slime_audio_vocal_cues.py audit-vocal-overlap runtime/mix-session.json
  ```

  Treat a failure as a real mix failure. Fix it by shortening overlaps, muting/removing one vocal source, using instrumental/stem beds, or arranging call-response with cuts, pauses, beat jumps, echo throws, or replacement routines so only one lead vocal is audible at a time.
- Prefer stem or acapella prep for vocal remix work: split/analyze/verify stems, inherit the full-track beatgrid, then use `load_track` actions that compile to one conceptual deck with individually balanced vocals/drums/bass/other.
- A valid remix block should contain at least one audible rhythm bed or stem group, one phrase-resolved drop/fakeout/handoff, and one additional intentional move such as a hook tease, echo throw, double, chop, brake, or filter/EQ performance.
- If stem coverage is missing for future loads, queue or run stem splitting before the load action reaches playback. Do not block live audio on Demucs.

For autodj/heartbeat work, `continue` starts a fresh set when nothing is playing and `extend` appends a planned, guarded block behind the playhead of the live set. Prefer:

```bash
# dead air: start a fresh set (about 5 minutes of buffer, then extend)
python3 scripts/slime_audio_autodj.py continue \
  --remix-focus \
  --stem-aware-remix \
  --structured-source-only \
  --no-analyze-missing-sections

# live set: top up the future queue; no-ops when enough runway remains
python3 scripts/slime_audio_autodj.py extend \
  --remix-focus \
  --structured-source-only \
  --no-analyze-missing-sections
```

`extend` is safe to run on a short cron/heartbeat cadence: it exits immediately when at least `--ahead-ms` of music is still scheduled ahead of the playhead, and stops adding at `--target-length-ms` (pass `0` for an endless set).

Keep `scripts/slime_audio_structure_backfill.py` running as a background/cron job so the cached cue pool grows without decoding audio on the live recovery path.

### Build Sequence

1. Check receiver/session state and recent history so the new set does not repeat stale material:

   ```bash
   python3 scripts/slime_audio_stream.py --target all --mode snapcast --dry-run --discover-timeout-ms 2500
   tail -n 80 runtime/play-history.jsonl
   python3 scripts/slime_audio_sets.py list --json
   ```

2. Select lead tracks and bed candidates from the database. Use novelty constraints for fresh sets, but do not let novelty override mixability.

   ```bash
   python3 scripts/slime_music_library.py stats
   python3 scripts/slime_audio_candidates.py candidates "lead vibe" --recent-limit 40 --limit 20
   python3 scripts/slime_audio_candidates.py candidates "edm techno house dubstep dnb bass bed" --recent-limit 40 --limit 30
   ```

   If the material is missing, use the Music Acquisition workflow first. Download to a mounted music share, rescan, confirm search/candidates, then analyze.

3. Analyze the selected lead and bed tracks before arranging. TuneBat/library BPM/key/Camelot is the authority for beat/key work; filename tags are not.

   ```bash
   python3 scripts/slime_music_library.py analyze-tunebat-local DUPLICATE_KEY
   python3 scripts/slime_audio_dj.py structure ./track.flac
   python3 scripts/slime_audio_dj.py cues ./track.flac --kind drop --kind hook --kind clean_intro --kind clean_outro
   python3 scripts/slime_audio_dj.py rank ./lead.flac --playlist runtime/bed-candidates.txt --limit 12
   ```

4. Create or activate a named set and build the first playable buffer. The base timeline is only scaffolding; do not stop here, but also do not delay playback for the whole future set. There is no playlist importer; build the base timeline as explicit `load_track` actions on the middle decks, one per lead, at analyzed cue windows.

   ```bash
   python3 scripts/slime_audio_sets.py new --title "Named set"
   python3 scripts/slime_audio_session.py add-action runtime/mix-session.json \
     --action-json '{"type":"load_track","id":"lead-001","deck":"deck-2","source_path":"./lead.flac","at":"00:00.000","trim_start":"00:32.000","duration":"01:04.000","play_stems":["vocals","drums","bass","other"]}'
   ```

5. Run the mix planner for phrase-aware overlaps, safe transition automation, automatic routines, and filter/EQ carving on the first buffer.

   ```bash
   python3 scripts/slime_audio_mix_planner.py \
     --session runtime/mix-session.json \
     --state runtime/mix-session-state.json \
     --max-render-tempo-shift-pct 4 \
     --max-render-pitch-shift-semitones 2 \
     --apply
   ```

6. Start native playback as soon as the first buffer is valid and visible on the dashboard. Normal live sets should become audible before the entire set is finished.

   ```bash
   python3 scripts/slime_audio_session_runner.py \
     --session runtime/mix-session.json \
     --state runtime/mix-session-state.json \
     --target TARGET
   ```

7. While playback runs, add beds and carve them into future windows. Use `deck-1`/`deck-4` as utility lanes for beds, doubles, shadows, and stabs while `deck-2`/`deck-3` carry the main A/B lead flow. Reserve `deck-5` for vocals only.

   ```bash
   python3 scripts/slime_audio_live_edit.py add-action \
     --action-json '{"type":"load_track","id":"lead-bed-a","deck":"deck-4","source_path":"./bed.flac","at":"01:16.000","trim_start":"00:32.000","duration":"00:48.000","gain_db":-7,"play_stems":["drums","bass","other"]}' \
     --reason "key and beat matched rhythm bed under lead"

   python3 scripts/slime_audio_live_edit.py add-action \
     --action-json '{"type":"knob_lerp","id":"lead-bed-a-filter","target":"deck-4","param":"lowpass_hz","at":"01:16.000","duration":"00:48.000","from":900,"to":1800}' \
     --reason "open rhythm bed filter under lead"

   python3 scripts/slime_audio_live_edit.py automate \
     --target deck-2 \
     --param eq_low_db \
     --points-json '[{"at":"01:16.000","value":-4},{"at":"02:04.000","value":-4}]'
   ```

8. Add routines/effects as audible musical punctuation, not as hidden decorations. Use cue kinds and beatgrid where possible.

   ```bash
   python3 scripts/slime_audio_live_edit.py instant-double-routine \
     --source-id lead-hook \
     --id lead-hook-stabs \
     --recipe stabs \
     --cue-kind hook \
     --cache runtime/dj-analysis-cache.json

   python3 scripts/slime_audio_live_edit.py instant-double-routine \
     --source-id lead-hook \
     --id lead-hook-echo \
     --recipe echo-stabs \
     --start 01:24.000 \
     --cache runtime/dj-analysis-cache.json

   python3 scripts/slime_audio_live_edit.py instant-double-routine \
     --source-id lead-hook \
     --id lead-hook-scratch \
     --recipe scratch-cuts \
     --start 01:40.000 \
     --cache runtime/dj-analysis-cache.json

   python3 scripts/slime_audio_live_edit.py add-effect \
     --id lead-hook-reverb-tail \
     --type reverb \
     --preset medium-room \
     --target lead-hook \
     --start 01:31.500 \
     --duration 00:02.000
   ```

   Use `stabs`, `one-beat-trades`, `offbeat-swaps`, `hook-tease`, `echo-stabs`, `echo-drop`, `scratch-cuts`, `slip-brake`, and `brake-drop` as the normal vocabulary. Scratch/brake generated clips must stay attached to the affected deck as `effect-track` child lanes.

9. Add TTS drops through session lean-ins, not side streams. Write drops like a DJ host who is listening to the arrangement: mention the bed, lead texture, incoming key/energy move, or why two records work together. Do not invent artist facts.

   ```bash
   python3 scripts/slime_audio_lean_ins.py \
     --session runtime/mix-session.json \
     --create \
     --deck deck-5 \
     --start 02:20.000 \
     --text "short music-aware line here" \
     --volume 1.7 \
     --duck-volume 0.45 \
     --lowpass-hz 1400
   ```

   For longer sets, use `slime_audio_commentary_planner.py` with `slime_audio_dj.py tension` output, but still review the generated text so it is about the actual songs and mix moves.

10. Audit and mix the future buffer while playback continues. A creative set should fail review if it only contains main song loads and transition automation, and it should also fail if the arranged beds/routines are technically present but functionally inaudible.

   Look for:

   - lead `load_track` actions primarily on `deck-2`/`deck-3`
   - EDM/dubstep/dnb/club bed loads on utility decks across most lead songs
   - nonzero `effects`, `slip_events`, routines, or attached `effect-track` child lanes
   - TTS lean-ins present and placed around musically sensible moments
   - fader/filter/EQ automation that makes the bed/lead relationship clear
   - rhythm beds normally balanced around `-6` to `-9 dB`, not buried at `-13 dB`
   - any `-12 dB` or lower bed explicitly justified as a ghost texture
   - no hard lead ducks unless tied to a named replacement move with a proof

   ```bash
   python3 scripts/slime_audio_session.py summary runtime/mix-session.json
   python3 scripts/slime_audio_session_mixdown.py runtime/mix-session.json \
     --routine-id ROUTINE_ID \
     --output runtime/routine-proof.mp3 \
     --format mp3 \
     --report-output runtime/routine-proof.json \
     --verify
   ```

11. Render proof from the actual session and save the set when review is needed, or after a meaningful chunk has been improved live. Do not block normal live playback on a full-set proof render.

   ```bash
   python3 scripts/slime_audio_session_mixdown.py runtime/mix-session.json \
     --output runtime/mix-review.mp3 \
     --format mp3 \
     --mp3-bitrate 192k \
     --verify
   python3 scripts/slime_audio_sets.py save-loaded
   ```

   For Discord review requests, send the MP3 media. Do not paste a path and do not hand-render proof audio outside the session toolchain.

Do not use legacy slot queues for DJ sets. Do not stream a review render directly as the main set unless the operator explicitly asks for file-only playback and does not need the dashboard.

## Live Set Rules

- Treat the mix session, playback history, and commentary plan as live state.
- Extend the future queue while playback continues whenever possible. `slime_audio_autodj.py extend` is the default way to top up the buffer; use `slime_audio_live_edit.py` for targeted creative edits on top of it.
- Keep roughly 5 minutes of future scheduled music ahead of the playhead. If the buffer is low, add/plan music before doing polish work.
- Add, remove, trim, move, or automate future timestamped actions/render events; do not disturb audio already under the playhead unless explicitly asked.
- Keep named set artifacts separate from the active live pointers. The active files are for the runner and dashboard; archived set files are for replay, review, and later editing.
- When extending, re-rank from the current or next track so the transition still makes sense.
- Treat complaints or steering from the operator as hard constraints for future selections.
- Keep a small scratchpad of current vibe, banned artists or genres, energy target, and planned arc in ignored runtime files.

## Shipped DJ Capabilities

These are part of the normal workflow, not future wishes.

- Database-backed candidate selection: use `slime_audio_candidates.py` against the library DB, recent `runtime/play-history.jsonl`, preferred-file routing, excludes, vibe/direction, and energy target. Candidate output should carry reasons the DJ can explain.
- Live future editing: use timestamped `mix-session.json` actions/render data, not legacy queue slots. New music loads use `add-action load_track`; stem toggles use `stem_toggle`; fader/EQ/filter moves use `knob_lerp` where possible. The session runner reloads future render windows and records `session_window_*` history. Future edits should use `slime_audio_live_edit.py` so the active state lock, stem pre-generation, and edit history are applied consistently.
- Live commentary planning: use `slime_audio_commentary_planner.py` to add future mic lean-ins independently of music selection. It writes normal session lean-ins with ducking/low-pass automation and appends `commentary_planned` logs tying text to timing, track context, and reason.
- Tension-aware vocal windows: use `slime_audio_dj.py structure` for per-track intro/breakdown/build/drop/outro and `slime_audio_dj.py tension` for absolute mix-session drop windows with grounded `reason` and `talking_points`. Feed `runtime/tension-windows.json` to the commentary planner when available.
- Stem-aware remixing: use `slime_audio_stems.py status/split/analyze/verify` before planning vocal-heavy hard-techno, dnb, or dubstep routines. Real Demucs splitting should run remotely by default (`squidward@robokrabs.tail4cb51.ts.net`); use `--local-demucs` only for tiny/debug jobs. `add-action load_track` must resolve or pre-generate all four stem slots before saving, so playback never waits on Demucs. Prefer `load_track`/compiled `stem_groups` with `vocals` only for hooks/acapellas, use `vocal_present`/`vocal_absent`/`instrumental_pocket` windows to avoid vocal clashes, and keep only one `bass` stem active in doubles unless the routine explicitly chops or trades bass. Proof-render stem routines before live use when stem quality is uncertain.
- Real mix planning: use `slime_audio_mix_planner.py` for the first playable buffer and during future edits. It consumes cached track analysis, transition scores, beat-grid phrase lengths, detected build/drop windows, and live runner locks. It may create overlapped blends, click-safe fade-ins, deck filter/EQ automation, and persisted `transition_plans`. Automatic routines are not a substitute for taste; loop rolls should not be part of unattended/default planning. Incoming drop-double previews are opt-in via `--double-every`, not a default transition move. Unsafe transitions should remain explicit hard cuts; do not rely on renderer auto-crossfades or layer incompatible tracks just because two clips can overlap on the timeline.
- Live-safe planner mode: use `--cached-analysis-only --horizon-ms 1200000` or another bounded horizon during active playback when analysis may be missing. This mode must not decode new tracks on the critical path; it should rewrite only future actions/render events inside the horizon and preserve transition plans outside that block.
- Rendered tempo/key correction: mixdown honors `load_track`/rendered event `tempo_shift_pct` and `pitch_shift_semitones`, so the planner may allow small beat/key-matched overlays when the renderer limits permit it. Keep correction ranges conservative, document the reason in planner move output, and set `--max-render-pitch-shift-semitones 0` only for routines where key preservation matters more than harmonic correction.
- Key-fit policy: when more than one full track plays at once, the overlapping loads must be key matched from full-track DB/TuneBat key metadata, not from isolated stem guesses. For major/minor combinations, convert any minor key to its relative major by moving up 3 semitones, then apply the smallest `pitch_shift_semitones` needed to align the incoming full-track key to the current full-track key. If a song is missing full-track key metadata, run the analyzer and populate the DB before layering it. The only exception is material that is not a song, such as a spoken drop or sound effect; do not treat that as a harmonic full-track layer. If the required shift exceeds the render limit, do not layer the decks; use a hard cut or bridge. Prefer keeping a compatible key lane for a run of tracks; only change key deliberately when the source song naturally modulates, the transition is short/non-overlapped, or the move is musically justified and documented.
- Mashup-first planning: DJ sets should be planned as mashups rather than playlists. Prefer one or more compatible rhythm/EDM/dubstep/dnb `load_track` actions as filtered beds under another lead track or section. Use `knob_lerp`/deck automation for gain plus low-pass/high-pass bed shaping, and render review files to verify the bed supports the lead instead of fighting it.
- True instant doubles: use `slime_audio_session.py instant-double-routine` for named recipes, or raw `instant-double` when hand-building. These commands preserve source path, derived musical position, tempo/pitch settings, gain, and label the dashboard event as an instant double/routine. Optional `--gate-beats` adds quantized on/off gain automation for simple routines; pair it with `--cut-source` when the double should trade against the original instead of phasing on top of it. Use scratch cuts, brakes, or attached effect tracks for generated scratch/brake/audio artifacts instead of treating them as normal music loads. Loop rolls are not a default move.
- Persisted cues: use `slime_audio_dj.py cues` and routine `--cue-kind` starts so hooks, drops, builds, stabs, clean intros/outros, vocal pockets, and safe loops come from DB-backed phrase/beat quantized facts instead of raw timestamps.
- DDJ-style fader routing: use `fader_routing.deck_assignments` plus `crossfader.position` automation for side A/B cuts and gradual fades. Mixdown renders crossfader motion into deterministic deck/clip gains, and the dashboard shows fader motion separately from normal gain automation.
- Transition carving: if two full songs overlap, the planner or edit pass must put real deck-level filter/EQ automation on the overlap. The default shape is outgoing low-pass/low-EQ cut and incoming high-pass opening with low-EQ restored. Raw full-band overlap should fail review unless it is a very short stab or a deliberate tested mashup.
- Bass and high-pass restore points must align to the incoming drop/hook cue when a reliable cue lands near the transition. Do not simply restore bass at overlap end if the musical drop is a few bars later.
- Quantized beat jumps: use `slime_audio_session.py beat-jump` for +/-1/2, +/-1, +/-2, +/-4, and +/-8 beat offsets from cached BPM/beat-offset analysis. Prefer it over manual millisecond edits whenever planning instant doubles, half-beat delays, phrase jumps, or off-beat cuts. Do not use `--force` for normal DJ planning; forced low-confidence grids are only for debugging failed analysis.
- Metadata authority: BPM/key/Camelot must come from the music DB TuneBat/full-track analysis fields. Ignore filename tags. If song metadata is missing, use the local TuneBat/audio analyzer path to populate the DB before planning overlays, beat jumps, or doubles.
- Review file export: use `slime_audio_session_mixdown.py --output runtime/mix-review.mp3 --format mp3 --verify` to render the actual planned mix to a shareable file before or after playback. For transition QA, render a shorter window with `--from` and `--duration`, then upload or link that artifact for operator review.
- Routine auditions: use `slime_audio_session_mixdown.py --routine-id ... --report-output ... --verify` for a 20-40 second proof around planned routines. The report records render timing, audio duration/level, clipping/silence checks, and current taste-rule warnings/errors. Do not send a full routine-heavy mix until the audition report is accepted or the risk is explicitly forced for debugging.
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

The DJ should host the set, not just play files. Hosting is on by default; do not wait for the operator to ask for mic work.

Every mic line is authored by you, live, for this set. No tool in this repo writes mic text, and none should: autodj publishes `commentary_slots` in its plan output (handoff timing plus incoming artist/title/BPM/Camelot/energy) as raw material, and you turn the moments you care about into lean-ins with your own words via `slime_audio_lean_ins.py` or the commentary planner. Templated or reused lines read as canned within two drops; write fresh ones each time.

- Prepare a short intro drop near the start of most sets.
- Add tasteful lean-ins every few minutes, with silence between them. After `autodj continue` or `extend`, read `commentary_slots` from the plan and author drops for the best handoffs; skip slots that don't need a voice.
- Keep commentary short and focused on the music: mood, texture, rhythm, genre lineage, energy, transition intent, or why the next track fits.
- Never talk about talking. No mic checks, no "quick note", no jokes about being a DJ or about the mix existing, no status updates about stems or decks. If a line would still make sense with no music playing, it is the wrong line.
- Hype the actual records: name the incoming artist or track when it earns it, call the tempo or energy move, tease the drop, or point at the bass/filter trade that is about to happen. Ground every claim in known facts — artist, title, BPM, key, energy, and what the arrangement is doing; never invent artist trivia.
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
- Always keep lean-ins on `deck-5`, the dedicated vocal lane. Do not place TTS drops on bed/utility decks.
- Always set voice level deliberately with `--volume`; use the same gain-staging judgment as the previous working lean-in system. Default `add-mic` gain is intentionally audible, and subtle drops should be an explicit choice.
- Pair lean-ins with music ducking and low-pass automation by default:

  ```bash
  python3 scripts/slime_audio_lean_ins.py \
    --session runtime/mix-session.json \
    --create \
    --deck deck-5 \
    --start 01:20.000 \
    --text "quick note" \
    --volume 1.7 \
    --duck-volume 0.45 \
    --lowpass-hz 1400
  ```

- For normal Snapcast-era DJ playback, use the native timestamped session runner so future edits can keep landing while audio plays:

  ```bash
  python3 scripts/slime_audio_session_runner.py --session runtime/mix-session.json --state runtime/mix-session-state.json --target TARGET
  ```

  Rendered-file streaming is only for explicit file playback, review, or receiver debugging.

- Do not use packet-mode lean-ins, direct UDP packet audio, or receiver-side packet effect envelopes for live mix commentary.
- Lean-ins should be editable future events: add, remove, move, and re-render before playback reaches them.

## Quality Bar

- Prefer database-backed candidates and preferred-file routing.
- Prefer explicit dry runs before live playback.
- Preserve playback state across restarts.
- Log enough history to explain what played, what was skipped, and why future choices were made.
- If a script is missing a capability needed by this workflow, open an issue or implement it in the script rather than encoding fragile behavior in the skill.
