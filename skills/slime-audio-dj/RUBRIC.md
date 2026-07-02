# What a good set sounds like

This rubric defines the product. Read it before building a set; use it to grade one after.
It grades **the room's experience**, after the fact. It is never a gate: nothing here may
block playback, and padding a metric to farm points (gratuitous stems, pointless knob
moves, a mic line nobody needed) loses more points under *Motion* and *Curation* than it
earns anywhere else. If a category conflicts with the vibe the operator asked for, the
vibe wins and the grader says so.

Grade reports live in `runtime/` next to the set they grade (sets and grades are never
committed to the repo).

## The one-sentence standard

A good set is **one continuous piece of music with an arc** — records reshaped to a
shared tempo, handed off where they musically should be, with layers and moves that each
do a nameable job — not a stack of songs played in a row.

## Score sheet (100 points)

### 1. Shows up on time — 10

Music in the room within minutes of the request. Planning is silent; listening is not.

- 10: first audio ≤ 5 minutes after the request
- 5: ≤ 10 minutes
- 0: > 15 minutes, regardless of how good the set turned out
- Objective: `play-history` delta from the operator request (or first constraint write)
  to the first `session_window_started`.

### 2. One set, not a stack of songs — 25

The defining category. A listener half-paying attention should not be able to point at
where one record ends and the next begins, except where a hard cut was an obvious
deliberate gesture.

- The set has a tempo identity: a master tempo derived from the vibe, with leads
  warped to it straight or at double/half-time (a documented deliberate drift — e.g.
  a sleep set whose master eases down through the night — also counts). Free-time
  material opting out *per clip* is craft; a whole set with no master tempo is not:
  "this material has no tempo" is a per-track claim, never a set-level excuse.
- Handoffs are predominantly blends placed at musical boundaries; cuts exist as
  *choices* with a reason you could say out loud, not as the planner's shrug.
- The set has a nameable arc (rising, sinking, plateau-and-release…) that matches the
  requested vibe from first track to last.
- Objective signals: locked-lead coverage (% of leads rendered at set tempo),
  blend/cut ratio in `transition_plans`, overlap durations. A set where most handoffs
  are bare cuts is a playlist unless the grader can defend each cut musically.
- 25: continuous, tempo-coherent, arc audible. 15: mostly blended but arc is vague or
  lock is partial. 8: sporadic blends, playlist bones showing. 0: a playlist.

### 3. Material reshaped to the vibe — 20

DJing is bending records toward the set, not finding records that already match.
Slowing a great track down (or speeding one up) is first-class craft and often makes
the most interesting mixes.

- Tempo stretches and small key nudges are actually used — a set of all-neutral
  transforms means the material was accepted as-is, which is a playlist symptom.
- Stem and bed work appears where the vibe allows: a rhythm bed under a sparse lead, a
  vocal isolated over an outro, drums-only under a handoff. Each layer has a musical
  job the grader can name.
- Objective signals: % of clips with non-neutral `tempo_shift_pct`/`pitch_shift_semitones`,
  clips with `play_stems`, routine/action counts.
- Calibrate to vibe: an ambient sleep set may earn full marks with few stems if the
  reshaping (tempo, arc) is doing the work; an energetic set with zero stem/remix work
  caps at half.

### 4. Curation — 20

- Picks fit the vibe *and* the arc position; sequencing is an intention, not an
  ordering accident.
- At least one surprise that works — a record the room didn't expect that lands.
- No crate-looping: reaching for the same few artists/albums over and over reads as a
  thin crate, and a thin crate is an acquisition failure, not an excuse.
- Shopping is part of the job: a good set usually includes evidence the DJ considered
  (and when warranted, downloaded, scanned, analyzed) material the library lacked.
  Grade the consideration, not the download count.

### 5. Motion — 15

- Automation audibly moves: filter rides into transitions, EQ trades when voices
  collide, bass swapped on phrase boundaries. A static carve is not a ride.
- Effects are punctuation — beat-synced throws on phrase exits, a wash into a
  breakdown — one gesture per phrase, each with a job.
- Restraint is graded as motion when the vibe demands it: for a sleep set, the
  discipline *not* to decorate scores here. More moves ≠ more points, ever.
- Objective signals: `deck_automations` shape (multi-point ramps vs flat), effect
  actions, their placement relative to transitions.

### 6. Clean execution — 10

- Renders valid; no key clashes on real overlaps; boundaries gapless; no dead air or
  unexplained gain sag — if the record dips, the listener hears why.
- Mic discipline: every line agent-authored, short, spaced, about the music — or total
  silence where the vibe demands it (sleep/background sets). One canned-sounding line
  zeroes this category's mic component.
- Objective signals: `validate` result, runner log boundary events, failure audits,
  `mic_lean_ins` content.

## Grade bands

- **A (90+)** — you'd send this set to someone whose taste you respect.
- **B (80–89)** — a real set with real craft; a category or two underworked.
- **C (70–79)** — pleasant, coherent, but it's still recognizably tracks-in-a-row.
- **D (60–69)** — a playlist with crossfades.
- **F (< 60, or no audio inside 15 minutes)** — the room got silence or a shuffle.

## How to grade

1. `python3 scripts/slime_audio_set_report.py SESSION.json --history runtime/play-history.jsonl`
   for the objective signals (blend ratio, lock coverage, transforms, stems, motion,
   mic, time-to-audio).
2. Listen — or at minimum spot-render two or three transitions and a layered passage
   (`slime_audio_session_mixdown.py --from/--duration --verify`) and read the timeline.
   The numbers locate; only ears grade.
3. Score each category with a one-sentence justification, sum, band it, and write the
   report to `runtime/` next to the set. Say the single biggest thing that would have
   raised the grade.
