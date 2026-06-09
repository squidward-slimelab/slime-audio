# SlimeAudio Wiki

This wiki is the durable documentation home for the SlimeAudio repo. Keep it updated with the code. If a subsystem, command, runtime file, test fixture, workflow, or operational convention exists in the repo, it should either be documented here or linked from here.

## Start Here

- [Repo map](repo-map.md) - top-level directories and what each owns.
- [DJ session workflow](dj-session-workflow.md) - session JSON, live edits, mix planning, effects, renders, and playback.
- [Music library and analysis](music-library-and-analysis.md) - mounted shares, SQLite indexing, candidates, TuneBat, and DJ metadata.
- [Stem management](stem-management.md) - Demucs artifacts, stem DB rows, stem groups, and stem-aware planner rules.
- [DJ capabilities](dj-capabilities.md) - shipped mix controls, routines, effects, and operating semantics.
- [Dashboard](dashboard.md) - local web dashboard, API contract, archive browsing, and smoke testing.
- [SlimeAudio Windows app](windows-app.md) - tray receiver, sender CLI, protocol, installer, and release workflow.
- [Spotify Brain](spotify-brain.md) - Python wrapper around `spogo` for Spotify experiments.
- [Testing and QA](testing-and-qa.md) - unit tests, render proofs, dashboard smoke tests, and artifact cleanup.
- [Operations](operations.md) - runtime files, live runners, Snapcast/multicast streaming, deployment, and disk hygiene.
- [Agent workflow](agent-workflow.md) - repo rules for agents changing SlimeAudio.

## Documentation Rules

- Update wiki docs in the same commit as behavior changes.
- Document newly discovered undocumented behavior before finishing a task.
- Keep command examples executable from the repo root unless a page says otherwise.
- Prefer concise, factual pages. This repo changes quickly; stale prose is worse than short prose.
- Link existing external docs when they remain useful, but mirror the operational truth here.

## Existing External Docs

- [README.md](../README.md) remains the quick-start and broad project overview.
- [docs/slime-audio-dashboard.md](../docs/slime-audio-dashboard.md) currently has detailed dashboard notes. Keep dashboard changes mirrored in [dashboard.md](dashboard.md).
- [skills/slime-audio-dj/SKILL.md](../skills/slime-audio-dj/SKILL.md) is the agent operating playbook for building and proving mixes.
