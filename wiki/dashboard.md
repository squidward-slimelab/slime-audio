# Dashboard

The SlimeAudio dashboard is a local web UI served by `scripts/slime_audio_web.py` with frontend assets in `web/slime-audio/`.

## Responsibilities

- Show active runner state and stale/playback status.
- Render the canonical DJ session timeline, including normal decks, attached effect lanes, the dedicated `deck-5` vocal lane, fader assignments, automation, effects, slip events, and mic lean-ins.
- Expose named set archive browsing without loading archived sets into playback.
- Provide a compact operational view of what the native session runner is about to play.

The dashboard must track the current session schema. When new mix controls ship, the API and frontend should expose them instead of hiding real mixer state behind summary text.

## Important Files

- Server: `scripts/slime_audio_web.py`
- Smoke runner: `scripts/slime_audio_web_smoke.py`
- Frontend: `web/slime-audio/index.html`, `web/slime-audio/app.js`, `web/slime-audio/styles.css`
- Test fixtures: `tests/fixtures/slime-audio-web-active-session.json`, `tests/fixtures/slime-audio-web-active-state.json`
- Tests: `tests/test_slime_audio_web.py`
- Older detailed doc: [docs/slime-audio-dashboard.md](../docs/slime-audio-dashboard.md)

## API Contract

The frontend consumes the server view model from `/api/state`. Keep this contract updated when session fields change. Recent expected concepts include:

- clip trim and gain
- automation values
- per-track EQ automation
- EDM/mashup bed styling
- effect params
- style flags
- fader assignments
- crossfader automation
- slip events
- attached effect lane metadata
- dedicated vocal lane metadata for mic lean-ins/TTS drops
- echo, reverb, and vinyl brake events
- summary counts

Unknown `/api/*` routes must return JSON errors, not static HTML. The frontend should parse responses defensively and report endpoint/status details when a response is not JSON.

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
