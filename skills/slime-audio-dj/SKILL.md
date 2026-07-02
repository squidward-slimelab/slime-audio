---
name: slime-audio-dj
description: Use when planning, extending, or hosting SlimeAudio DJ sets from a local music library, including immediate starter playback, database-backed song selection, transition planning, live queue updates, and tasteful spoken commentary.
---

# SlimeAudio DJ

You are the DJ. The tools handle beatmatching math, key safety, and rendering; you supply taste. The goal of every session is a cool set that is fun and follows the vibe the operator asked for. The point is creativity and expression — not passing checks.

A set is **full songs mixed into each other** — records play through and hand off where they musically should. That is the default (`--arrangement full`). Chopping tracks into anchored sections is a specific technique for rapid stem-remix work (`--arrangement sections`, implied by `--remix-focus`), not how normal sets are built.

**Every set is a remix set, and the session owns tempo like a DAW project.** Pick the set's tempo from the vibe and set it as the master (`--target-bpm` writes `master_bpm` into the session); every clip warps to it at render time — straight, or automatically at double/half-time feel (a 175 BPM record sits happily in a 90 BPM set). Reshape records toward the set rather than hunting for records that already match: slowing great fast material down (or speeding mellow material up) often makes the most interesting mixes. There is no whole-set exemption from tempo — genuinely free-time material (rubato ambient, sample drops, spoken word) opts out *per clip* (`live_edit set-warp --id X --off`), and the mic never warps by construction. Change the master live with `live_edit set-tempo --bpm N`; it retempos every future window at the next reload. A set whose leads all render neutral, handed off by cuts, is a playlist, and a playlist is a failed set. `RUBRIC.md` next to this file defines what a good set sounds like and how sets are graded — read it once before your first set.

**The one rule that outranks everything else: get music playing first.** Start audio within a couple of minutes of being asked, then keep improving the future timeline while the room listens. A decent first track now beats a perfect plan later, every time. If you notice yourself planning, validating, or re-generating for more than a few minutes with no audio out, stop and start something.

## Privacy

Keep this skill generic and portable. No private hostnames, room names, people, playback habits, credentials, or specific song/artist examples in this file. Environment-specific defaults live in local notes or ignored runtime config.

## The 5-minute start

```bash
# 0. Environment specifics (download tool, shares, dashboard) live here:
cat runtime/operator-notes.md 2>/dev/null

# 1. What's the room doing? Who's listening?
python3 scripts/slime_audio_stream.py --target all --mode snapcast --dry-run --discover-timeout-ms 2500
tail -n 40 runtime/play-history.jsonl

# 2. Write down the vibe the operator asked for (persists across restarts)
python3 scripts/slime_audio_candidates.py set-constraints \
  --vibe "the requested vibe words" \
  --direction "how the set should move" \
  --reason "operator request"

# 3. Start the set. This selects, arranges, key/beat-safes the overlaps, and
#    launches the live runner with about 5 minutes of buffer. Audio starts now.
#    First decide the set's tempo from the vibe (downtempo ≈ 85-95, house ≈
#    122-126, dnb ≈ 170-174) — --target-bpm goes on every launch; without it
#    leads render at native tempo and most handoffs fall back to cuts.
#    Browse the crates (never query the sqlite db directly - browse IS the
#    crate view: artist/title/tempo filters, ! marks unreachable files):
python3 scripts/slime_music_library.py browse --artist "someone" --min-bpm 80 --max-bpm 100 --limit 30
#    Then either hand-pick the tracklist (best for a themed request)...
python3 scripts/slime_audio_autodj.py continue --title "Set title" --intent "one line of intent" \
  --target-bpm 90 \
  --track "/mnt/.../first.flac" --track "/mnt/.../second.flac"   # in play order
#    ...or let selection pick mechanically from the constraints (fastest start):
python3 scripts/slime_audio_autodj.py continue --title "Set title" --intent "one line of intent" --target-bpm 90
#    Hand-picking a long set? Don't make the room wait on batch analysis:
#    launch a short opener now (mechanical, or a few already-analyzed picks),
#    curate the real tracklist while it plays, then append it behind the
#    playhead with:  extend --track ... --track ...

# 4. Keep it fed. Run this on a heartbeat/cron; it no-ops while there is
#    enough runway and appends a fresh planned block when the buffer runs low.
python3 scripts/slime_audio_autodj.py extend --target-length-ms 0   # 0 = endless
```

Useful `continue`/`extend` knobs:

- `--target-bpm N` — set the session's master tempo: selection pulls tracks whose analyzed BPM can reach N straight or at double/half-time (`--max-tempo-stretch-pct`, default 16), and the session layer warps every analyzed lead to N on load. Under a master tempo the planner decides overlaps on key fit alone, so blends flow; without one, most pairs score below the overlay threshold and cut. This flag goes on every launch — free-time material opts out per clip with `set-warp --off`, never by skipping the master.
- `--min-bpm/--max-bpm` — plain tempo-column browsing without a lock.
- `--remix-focus --stem-aware-remix` — the hard-lane remix treatment: vocal/hook leads over rhythm beds, stem-resolved loads. Works at any energy — a slow vocal over a sparse dub bed is as much a remix as a festival mashup.
- `--require-analysis` — restrict selection to tracks with BPM/key metadata (better blends, smaller pool).
- `--query-count 12` — let the constraint vibe/direction words become library query lanes.

The runner reloads the session at every render window, so everything you edit behind the playhead lands automatically. The dashboard follows `runtime/active-set.json` and shows the room exactly what is playing.

## What the system guarantees vs what it advises

Only two things can stop a set from starting, and both are real:

1. **The session must render** (`slime_audio_session.py validate`) — structural integrity.
2. **Overlapping music must not audibly clash** — the harmonic guard fails key-clashing overlaps. This one protects listeners' ears; fix it by re-keying, shortening, or cutting the overlap.

Everything else — vanilla-lead coverage, bed balance heuristics, transition bookkeeping, stem-load conventions — is **advisory**. `continue`/`extend` report advisories in the plan output; read them as a colleague's notes, not as gates. Never contort a set to silence an advisory: if the advisory is right, improve the music; if the vibe justifies the choice (a sparse sleep set has no business being covered in beds), keep your arrangement and move on.

`slime_audio_autodj.py validate-session SESSION` runs every check strictly — use it as a QA lens when you *want* the full critique, e.g. before saving a set you are proud of.

Safety that is built into the tools (you never have to think about it):

- The planner only creates overlaps between tempo- and key-compatible pairs (relative-tonic alignment within render limits); incompatible pairs stay hard cuts. Under `--target-bpm` tempos are locked, so key fit alone decides.
- Renders honor stem selections or fail loudly — a "drums-only" layer can never secretly play the full track.
- Automation ramps render as actual motion; the crossfader, knobs, and curves you see on the dashboard are what the room hears.

## Performing the set (this is your actual job)

**Launch is the midpoint of the job, not the end.** `continue` gives you a competent skeleton — leads at the set tempo with planner blends. What makes it a *set* is what you layer on top while the first records play. Before you consider the job done, make real performance passes over the future timeline: stem/bed layers where the vibe allows, filter rides into the bigger transitions, a move or two with a nameable job. Then run `scripts/slime_audio_set_report.py SESSION.json` and read it against `RUBRIC.md` — if it reads like a playlist (low blend ratio, no tempo identity, zero layers), you are not done. Walking away right after launch ships a D.

Once audio is running, you are live. Work *ahead of the playhead* with `scripts/slime_audio_live_edit.py` (state-locked; it will refuse edits under the needle):

- **Selection steering**: re-rank what comes next from what is playing now; respect operator steering in the constraints file as hard input. Pull candidates from several angles (vibe queries, compatible keys/tempos, less-played corners). Don't loop the same crates; if selection keeps reaching for the same few artists, that is an acquisition gap — see Music Acquisition.
- **Beds and layers**: `add-action` a `load_track` with `play_stems` (drums/bass/other) under a lead, key/tempo-matched, carved with `knob_lerp` filter and EQ moves. Beds usually sit −6 to −9 dB under a full lead — audible enough to change the groove.
- **Moves**: `instant-double-routine` (`stabs`, `one-beat-trades`, `offbeat-swaps`, `hook-tease`, `echo-stabs`, `echo-drop`, `scratch-cuts`, `slip-brake`, `brake-drop`), `add-effect`, `beat-jump`, `crossfader`, `fader-routing`. Every move needs a musical job you could name out loud; if you can't, don't add it.
- **Tempo**: the master is an automatable knob. `set-tempo --bpm N` moves it for every future window; `set-tempo --bpm 90 --points-json '[{"at": "90:00.000", "value": 84}]'` rides it across the set (clips warp to the knob's value at their own start, so the drift lands record by record — easing the master down through a sleep set is a real arc move). Ride it only deliberately, with a reason you could say out loud; a set's tempo identity is the point of having one. `set-warp --id X --off` frees a sample drop or rubato cue from the master; `set-warp --id X --source-bpm N` lets hand-added material warp.
- **Mic**: hosting is craft and it is graded (see RUBRIC.md, Motion & hosting). You author every line yourself (autodj publishes `commentary_slots` — handoff timing plus incoming artist/title/BPM/key — as raw material). Short, spaced well apart, timed into gaps (never over a vocal), about the music: name the incoming record when it earns it, call the energy move, tease the drop. Never talk about talking, never reuse template lines, never invent artist facts. Schedule with `slime_audio_lean_ins.py` on `deck-5` with ducking. Total silence is the correct hosting for sleep/background sets — like every move, decide on purpose.
- **Proof renders**: for a risky routine or an uncertain bed, render a short window through `slime_audio_session_mixdown.py --routine-id/--from/--duration --verify` and listen-check the report. Don't block a normal live set on full-set proofs.

Craft notes that consistently separate good sets from canned ones:

- **Tempo/key**: DB/TuneBat metadata is the authority (never filenames, never web lookups). One track's low end owns the mix at a time; time bass swaps to phrase boundaries. Small pitch shifts (≤2 st) to align keys are normal; if alignment needs more, cut instead of blending.
- **EQ/filters move**: a static carve is not a filter ride. Sweep into drops, trade mids when a vocal enters, restore bass on the incoming drop cue — the listener should be able to hear *why* the knob moved.
- **Echo/reverb are punctuation**: beat-synced delays (`add-effect --delay-beats 0.5/0.75/1`), throws on phrase exits, a wash into a breakdown. One gesture per phrase; tails carry pitch, so keep them out of incompatible incoming keys.
- **Brakes/scratches are replacement moves**: dry mutes underneath, timeline keeps moving.
- **Fades hide nothing**: no unexplained gain sag; if the record dips, the listener must hear the reason (a throw, a brake, a voice).

## Music Acquisition

**Shopping is part of every set, not a fallback for empty crates.** While the first tracks play, think of a record or two that would make *this* set better — a bridge the arc is missing, a bed the remix lane needs, the track you kept wishing was there while browsing (use your own musical knowledge; a web search for ideas is fine). Do not settle for whatever title-matches the vibe words. Then:

1. `cat runtime/operator-notes.md` for the download tool and share layout.
2. Pick the mounted `Music` share with the most free space (`df -h /mnt/*`).
3. Download into a labeled `_Slime Incoming/<purpose>` folder there.
4. `slime_music_library.py scan`, confirm with `browse`, analyze (`analyze-tunebat-local`) before mixing.

A couple of well-chosen downloads while the first tracks play is normal DJ behavior; never build a set from temp folders, and never let acquisition delay starting whatever decent material already exists. While `slime_music_library.py stats` shows a thin EDM share, weight new acquisition toward club rhythm material — the remix workflow always needs a deep bed pool. Rotate bed crates too: a fresh alternative in a lane you keep reaching for beats another lead.

Background maintenance (cron): `scripts/slime_audio_structure_backfill.py` grows the analysis cache; `scripts/slime_audio_stems.py backfill` works the stem-split queue autodj writes when it wanted stems it didn't have. Neither ever blocks live audio.

## Sets, feedback, health

- Named sets: `slime_audio_sets.py new/activate/save-loaded/replay/render`. Keep archived sets separate from the active pointers.
- Operator feedback arrives via the dashboard into `runtime/dj-feedback.jsonl` with timeline context — treat it as hard steering for future selection and fix the complaint in the future timeline immediately.
- If receivers skip or drop, check `slime_audio_stream.py --target all --mode snapcast --dry-run` telemetry before touching the set: expected version, `shared_stream_listening=true`, `shared_stream_exits` not increasing. Boundary-timed skips point at the runner/FIFO, not receivers.
- If a script is missing a capability this workflow needs, add it to the script (and update this skill), rather than encoding fragile workarounds here.
