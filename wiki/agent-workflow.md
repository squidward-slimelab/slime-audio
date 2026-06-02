# Agent Workflow

Agents changing this repo must keep code, tests, docs, and operational state aligned.

## Before Editing

```bash
git status --short
```

Read the relevant wiki page, `AGENTS.md`, and any subsystem docs before editing.

For DJ, render, live playback, or music acquisition work, read [skills/slime-audio-dj/SKILL.md](../skills/slime-audio-dj/SKILL.md).

## During Work

- Make focused edits.
- Use SlimeAudio commands rather than ad hoc file mutation when session tools exist.
- Never run parallel writes against the same active session JSON.
- Keep runtime artifacts small.
- Update `wiki/` in the same change when behavior, commands, file formats, workflows, tests, or operational expectations change.

## Before Calling Done

Run the smallest meaningful verification gate:

```bash
git diff --check
PYTHONPATH=src:scripts python3 -m unittest discover -s tests -v
```

Use narrower tests when the full suite is impractical, and state what was run.

Check docs:

```bash
find wiki -type f | sort
```

Confirm that any newly touched subsystem has a matching wiki update or is already accurately documented.

## Commit Hygiene

- Commit often.
- Keep commits focused.
- Include code, tests, and docs for the same behavior in the same commit.
- Do not commit large generated audio, SQLite caches, pyc files, node modules, or temporary runtime output.
- Do not discard user work.
