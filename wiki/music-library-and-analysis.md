# Music Library And Analysis

SlimeAudio uses mounted music shares plus a local SQLite database to pick tracks and plan DJ-compatible transitions.

## Music Shares

Known library roots are mounted under `/mnt/*/Music`, including:

- `/mnt/rockhouse/Music`
- `/mnt/pineapple/Music`
- `/mnt/krusty-krab/Music`
- `/mnt/chum-bucket/Music` when mounted

Before downloading or staging new material, check mounted space and writeability:

```bash
df -h /mnt/*
```

Put new DJ material in a clear folder under the selected music root, commonly `_Slime Incoming/<purpose-or-genre>/`.

## Library Index

`scripts/slime_music_library.py` indexes mounted shares into `runtime/slime-music-library.sqlite3`, combines duplicates, and picks the strongest server copy. After adding files, rescan before planning a set:

```bash
python3 scripts/slime_music_library.py scan
```

Verify tracks are searchable before using them in a mix.

## DJ Analysis

`scripts/slime_audio_dj.py` analyzes local tracks for BPM, beat offset, structure, key, Camelot code, energy, cues, and transition compatibility.

Useful commands:

```bash
python3 scripts/slime_audio_dj.py analyze ./track-a.wav ./track-b.wav
python3 scripts/slime_audio_dj.py structure ./track-a.wav
python3 scripts/slime_audio_dj.py cues ./track-a.wav --kind drop --kind hook
python3 scripts/slime_audio_dj.py rank ./now-playing.wav --playlist runtime/candidates.txt --limit 8
python3 scripts/slime_audio_dj.py tension --session runtime/mix-session.json --state runtime/mix-session-state.json --horizon-ms 2700000 > runtime/tension-windows.json
```

Analysis rows are reused from SQLite when files have not changed. File size or mtime changes force recomputation.

## TuneBat Metadata

TuneBat-backed database facts are the authority for beat/key planning when available. Fill missing local metadata with:

```bash
python3 scripts/slime_music_library.py analyze-tunebat-local DUPLICATE_KEY
```

Filename tags are not authoritative for BPM/key planning.

## Candidate Selection

`scripts/slime_audio_candidates.py` keeps live set constraints and ranks database-backed candidate tracks. Use it to avoid stale/recent material and to select tracks that fit the current set instead of repeating the same easy choices.

## Agent Playbook

The detailed operating workflow for acquiring music, rescanning, analyzing, selecting candidates, planning creative beds, and rendering proofs lives in [skills/slime-audio-dj/SKILL.md](../skills/slime-audio-dj/SKILL.md).
