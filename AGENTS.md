# AGENTS.md - SlimeAudio

## QA Render Discipline

When producing QA samples, proof renders, or review MP3s for SlimeAudio behavior, use the SlimeAudio session/planning/rendering tools and the `slime-audio-dj` skill workflow.

- Build samples as real session JSON with clips, effects, automations, fader routing, slip events, attached effect tracks, and routine metadata as appropriate.
- Render through `scripts/slime_audio_session_mixdown.py`, `scripts/slime_audio_sets.py render`, or the native session runner path.
- Do not hand-render behavior directly in ad hoc ffmpeg filter graphs when the point is to validate SlimeAudio features. That bypasses the engine and can make broken or missing tools look like they work.
- If the desired musical move cannot be represented by the current session model or skill workflow, update the repo tools and `skills/slime-audio-dj/SKILL.md` first, then render the QA sample through those tools.
- Keep QA artifacts small and clean up stale renders when disk is tight.

