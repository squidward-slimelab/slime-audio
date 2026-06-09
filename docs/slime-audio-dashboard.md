# SlimeAudio Dashboard

## Purpose

The dashboard is a live operations view for native SlimeAudio mix sessions. It should help the DJ/operator answer five questions quickly:

- What is playing now?
- Where is the mix playhead?
- What render window is active?
- What clips, commentary, and automation are coming next?
- Is the runner state healthy enough to trust?

It is not a marketing page, a generic playlist browser, or a manual performance surface. Timing-impacting moves should be planned in `mix-session.json` before playback reaches them.

The same server also exposes `/tv` for the living-room TV on SPONGEBOT. That view is passive and room-facing: a full-screen animated waveform canvas driven by the current clip's `/api/waveform` bass/mid/high bands, large now-playing text, whole-session progress, upcoming tracks, active set, render window, update time, and runner health. It intentionally omits archive controls, timeline editing affordances, and dense mixer detail.

## Required Views

- Transport strip: runner status, playhead, render window, and last update.
- Now panel: current clip title, source context, status, and whole-session progress.
- Upcoming panels: future song clips, commentary lean-ins, and runner/receiver health.
- Arrangement timeline: absolute mix timeline with stable deck lanes ordered `3 1 2 4`, plus utility lanes for voice and automation.
- Feedback panel: quick category/rating controls and a note field for song-selection, transition, effects, vibe, and technical feedback. Notes default to the live playhead/current event; clicking a timeline item targets the note to that specific clip/effect.
- Details below the timeline: upcoming automation and session summary.

The first desktop viewport should show the transport, now/next/commentary/health row, and the start of the arrangement timeline without scrolling. Narrow screens should remain readable and stack status panels before the timeline.

## State Contract

`/api/state` keeps the older `now` and `session` fields for compatibility, and adds `dashboard.schema_version = 1` for the rebuilt frontend.

The dashboard view model includes:

- `transport`: status, stale flag, playhead, duration, update time, completed time, and active render window.
- `session`: timeline mode, total duration, event counts, and deck list.
- `now`: normalized current event, or `null`.
- `events`: normalized timed/untimed events with `kind`, `lane`, `start_ms`, `end_ms`, `duration_ms`, `status`, `display_title`, and `display_meta`.
- `lanes`: render-ready lane groups, including empty decks.
- `upcoming`, `commentary`, and `automation`: bounded lists for the top panels.
- `health`: runner state, current clips, and receiver telemetry when available.

The frontend should render this view model directly instead of reconstructing timeline semantics from raw session JSON.

`/api/feedback` stores operator feedback in `runtime/dashboard-feedback.jsonl` as append-only JSONL. Each entry includes `created_at`, category, optional rating/note, playhead, active set, session path, and the selected normalized timeline event so future selector/planner fixes can use concrete evidence instead of vague complaints.

## Verification

Run these before restarting the live dashboard:

```bash
node --check web/slime-audio/app.js
PYTHONPATH=scripts:src python3 -m unittest tests.test_slime_audio_web
PYTHONPATH=scripts:src python3 scripts/slime_audio_web_smoke.py
```

The smoke check starts a fixture-backed server, renders desktop, mobile, and TV headless Chrome screenshots into `runtime/web-smoke/`, and verifies that the timeline, playhead, planned vocal marker, and `/tv` display render without an active room playback session.
