# AGENTS.md - SlimeAudio

## Documentation Discipline

The repo wiki lives in `./wiki` and is part of the source tree. Treat it as the canonical home for durable project documentation.

- Keep everything in the repo documented in `wiki/`: architecture, apps, scripts, CLI workflows, runtime files, dashboard behavior, tests, deployment, skills, and operational rules.
- When code, commands, data formats, workflows, tests, or behavior change, update the matching wiki page in the same change.
- If you discover undocumented behavior while working, document it before calling the task done. Do not leave "someone should document this" notes unless the missing facts are genuinely unknown.
- Prefer focused wiki pages over dumping everything into `README.md`. Keep `README.md` as the quick-start/front door and link deeper material from `wiki/`.
- Existing docs outside `wiki/` may remain when useful, but new durable documentation should be mirrored or linked from `wiki/` so agents can find it.
- Documentation examples must use the real SlimeAudio tools and current command names. Stale command snippets are bugs.

## Git Hygiene

Use clean, frequent git checkpoints.

- Check `git status --short` before and after edits so you know what you changed and what was already dirty.
- Keep commits focused: code, tests, and wiki updates for the same behavior belong together; unrelated cleanup belongs in a separate commit.
- Commit often enough that working states are recoverable, especially before risky refactors, generated artifact cleanup, or live playback/session changes.
- Do not commit large generated audio, cache, database, or runtime artifacts unless the repo explicitly needs that artifact tracked.
- Never rewrite or discard user work unless explicitly asked. If the tree is dirty, work around unrelated changes.

## QA Render Discipline

When producing QA samples, proof renders, or review MP3s for SlimeAudio behavior, use the SlimeAudio session/planning/rendering tools and the `slime-audio-dj` skill workflow.

- Build samples as real session JSON with clips, effects, automations, fader routing, slip events, attached effect tracks, and routine metadata as appropriate.
- Render through `scripts/slime_audio_session_mixdown.py`, `scripts/slime_audio_sets.py render`, or the native session runner path.
- Do not hand-render behavior directly in ad hoc ffmpeg filter graphs when the point is to validate SlimeAudio features. That bypasses the engine and can make broken or missing tools look like they work.
- If the desired musical move cannot be represented by the current session model or skill workflow, update the repo tools and `skills/slime-audio-dj/SKILL.md` first, then render the QA sample through those tools.
- Keep QA artifacts small and clean up stale renders when disk is tight.
