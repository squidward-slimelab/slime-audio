# Dashboard

The SlimeAudio dashboard is a local web UI served by `scripts/slime_audio_web.py` with frontend assets in `web/slime-audio/`.

## Responsibilities

- Show active runner state and stale/playback status.
- Provide basic live transport controls for the active session: play, pause, restart, and seek. These controls call `POST /api/transport`; the backend freezes the authoritative state playhead, stops any active runner/stream processes, and relaunches `slime_audio_session_runner.py` from the stored playhead for play/restart/seek.
- Render the canonical DJ session timeline, including normal decks, attached effect lanes, the dedicated `deck-5` vocal lane, fader assignments, automation, effects, slip events, and mic lean-ins.
- Order deck lanes and the mixer mirror as `deck-3`, `deck-1`, `MIC`, `deck-2`, `deck-4`, matching the physical/operator view.
- Overlay per-deck mixer state curves on timeline rows. Each music deck row synthesizes full-mix lines for level, gain, trim, high/mid/low EQ, and filter state from `deck_automations`, clip occupancy, and legacy clip automation fallback. Top-level `deck_automations` target deck names directly and render on that deck row; legacy clip-owned EQ/filter automation is still remapped from generic automation events back onto the target clip lane, while legacy clip `gain_db` automation is collapsed into one deck gain/fader curve for compatibility. Crossfader automation remains on the fader lane. Hovering a curve shows the deck values at the hovered timestamp, rather than every future automation event on that deck.
- Update the mixer mirror from the animated playhead, not only from `/api/state` polling. Knobs and faders should interpolate smoothly during active deck automation and highlight while a moving automation segment is under the playhead.
- Draw clip waveforms in timeline blocks through the lazy `/api/waveform` endpoint. The endpoint decodes a trimmed clip segment with `ffmpeg`, returns normalized bass/mid/high peak bands, and caches results under `runtime/waveform-cache.json` keyed by file identity plus trim/duration/bin count. The frontend requests bins from clip pixel width so waveform bar density stays visually consistent across short and long timeline blocks, and renders bass/mid/high as red/green/blue overlays.
- Keep dashboard traffic conservative while playback is active: `/api/state` polls are non-overlapping and intentionally slow, archive/set refreshes are less frequent, waveform hydration is capped to a small batch, and noisy high-frequency API request logs are suppressed.
- Expose named set archive browsing without loading archived sets into playback.
- Capture operator feedback about song selection, transitions, effects, vibe, and technical issues from the dashboard. Feedback is timeline-aware: it defaults to the live playhead/current event and timeline event clicks can retarget the note to a specific clip, effect, or automation.
- Provide a compact operational view of what the native session runner is about to play.
- Treat `runtime/active-set.json` as the single active-playback pointer for both native session runner playback and direct `slime_audio_stream.py` fallback playback. The frontend should not require manual pointer edits after audio starts.
- Serve `/tv` as the living-room display view. It consumes the same `/api/state` payload, avoids archive/edit controls, and renders a full-screen animated canvas driven by the current load/event waveform from `/api/waveform`, with large now-playing, progress, upcoming, and runner-signal overlays for a TV display.

The dashboard must track the current session schema. When new mix controls ship, the API and frontend should expose them instead of hiding real mixer state behind summary text.

## Important Files

- Server: `scripts/slime_audio_web.py`
- Smoke runner: `scripts/slime_audio_web_smoke.py`
- Frontend: `web/slime-audio/index.html`, `web/slime-audio/app.js`, `web/slime-audio/styles.css`
- TV display: `web/slime-audio/tv.html`, `web/slime-audio/tv.js`, `web/slime-audio/tv.css`
- Test fixtures: `tests/fixtures/slime-audio-web-active-session.json`, `tests/fixtures/slime-audio-web-active-state.json`
- Tests: `tests/test_slime_audio_web.py`
- Older detailed doc: [docs/slime-audio-dashboard.md](../docs/slime-audio-dashboard.md)

## API Contract

The frontend consumes the server view model from `/api/state`. Keep this contract updated when session fields change. Recent expected concepts include:

- clip trim and gain
- load-time tempo and pitch transforms on `load_track` action rows
- automation values
- per-track EQ automation
- EDM/mashup bed styling
- effect params
- style flags
- fader assignments
- crossfader automation
- deck automation for mixer fader/EQ/filter moves
- slip events
- attached effect lane metadata
- dedicated vocal lane metadata for mic lean-ins/TTS drops
- echo, reverb, and vinyl brake events
- summary counts

Direct stream playback writes `runtime/active-stream-state.json`, `runtime/active-stream-session.json`, and updates `runtime/active-set.json` by default. If the stream is a rendered DJ session file, start it with `--source-session path/to/session.json` so the dashboard shows the real arrangement timeline while the streamed render plays. Use `--no-active-pointer` only for isolated tests where the frontend should intentionally ignore the stream.

Unknown `/api/*` routes must return JSON errors, not static HTML. The frontend should parse responses defensively and report endpoint/status details when a response is not JSON.

`/api/waveform?path=...&trim_start_ms=...&duration_ms=...&bins=...` returns `{available, peaks, bands}` JSON, where `bands.low`, `bands.mid`, and `bands.high` are normalized arrays used for red/green/blue waveform rendering. Missing files or decode failures should return a JSON payload with `available: false`, not break the timeline.

`POST /api/feedback` appends one JSON object per line to `runtime/dashboard-feedback.jsonl`. The request body uses `category`, optional `rating`, optional `note`, and a `context` object with `session_path`, `active_set`, `transport.playhead_ms`, and the selected normalized timeline event. `GET /api/feedback?limit=8` returns recent feedback for the operator panel. This file is runtime data for later selector/planner review and should not be committed.

`POST /api/transport` accepts `{"action":"play"|"pause"|"restart"|"seek","position_ms":12345,"target":["all"]}`. `position_ms` is required only for `seek`. `pause` writes `runtime/dj-watchdog.paused`, stops the current runner/stream, clears live window anchors, and leaves `runner_status` as `paused`. `play`, `restart`, and `seek` remove the pause file, store the requested playhead, and spawn the native session runner against the active session/state pointers without `--reset-state`.

## Verification

When dashboard behavior changes, update fixtures and run at least:

```bash
PYTHONPATH=src:scripts python3 -m unittest tests.test_slime_audio_web -v
python3 scripts/slime_audio_web_smoke.py
```

For visual changes, inspect the browser dashboard or a screenshot when possible. Confirm labels fit, attached effect lanes render under their parent deck, and automation/effect indicators match the session data.

If a browser reports a JSON parse error against a known-good commit, check the running service before changing code. A stale `slime-audio-web.service` process can keep serving old Python after commits land. Restart the service, then verify from the same hostname/browser path the operator used:

```bash
curl -i http://DASHBOARD_HOST:8765/api/sets
curl -i http://DASHBOARD_HOST:8765/api/not-a-real-endpoint
```
