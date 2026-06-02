# Dashboard

The SlimeAudio dashboard is a local web UI served by `scripts/slime_audio_web.py` with frontend assets in `web/slime-audio/`.

## Responsibilities

- Show active runner state and stale/playback status.
- Render the canonical DJ session timeline, including normal decks, attached effect lanes, fader assignments, automation, effects, slip events, and mic lean-ins.
- Expose named set archive browsing without loading archived sets into playback.
- Provide a compact operational view of what the native session runner is about to play.

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
- effect params
- style flags
- fader assignments
- attached effect lane metadata
- summary counts

## Verification

When dashboard behavior changes, update fixtures and run at least:

```bash
PYTHONPATH=src:scripts python3 -m unittest tests.test_slime_audio_web -v
python3 scripts/slime_audio_web_smoke.py
```

For visual changes, inspect the browser dashboard or a screenshot when possible. Confirm labels fit, attached effect lanes render under their parent deck, and automation/effect indicators match the session data.
