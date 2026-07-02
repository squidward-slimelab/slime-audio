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

Do not build sets from temporary download folders. Download or move material into a mounted music share first, then scan it into the library.

## Library Index

`scripts/slime_music_library.py` indexes mounted shares into `runtime/slime-music-library.sqlite3`, combines duplicates, and picks the strongest server copy. After adding files, rescan before planning a set:

```bash
python3 scripts/slime_music_library.py scan
```

Verify tracks are searchable before using them in a mix.

If new music is needed, use the local `sldl-slimelab` wrapper named in local operator notes, choose a mounted share by free space/writeability, download into a clearly labeled folder under `Music`, then rescan:

```bash
sldl-slimelab "artist title or playlist url" --path "/mnt/SHARE/Music/_Slime Incoming/Purpose or Genre" --write-playlist
python3 scripts/slime_music_library.py scan
python3 scripts/slime_music_library.py search "new track or artist"
```

## Native Analyzer

The BPM/beat-offset/key/energy/structure DSP has a compiled C++ implementation in `native/slime_dj_analyzer.cpp`. Build it once with:

```bash
make -C native
```

`slime_audio_dj.py analyze`/`structure`/`cues` automatically use `native/slime-dj-analyzer` when the binary exists (override with `SLIME_DJ_NATIVE_ANALYZER`) and fall back to the pure-Python reference implementation when it does not. The port mirrors the Python algorithms exactly — including rounding and tie-break semantics — so cached analyses stay comparable across implementations; `tests/test_slime_audio_dj.py` includes a parity test. ffmpeg still handles decoding in both paths.

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

Persistent DJ analysis currently includes beatgrid, phrase-grid, structure, drop candidates, and named cue rows. `runtime/dj-analysis-cache.json` remains a compatibility mirror for older live-edit commands.

## Stem Analysis

Stem artifacts and planner windows are managed by `scripts/slime_audio_stems.py` and stored in the same SQLite database. See [Stem management](stem-management.md) for Demucs splitting, manifest layout, stem-window kinds, and session usage.

## TuneBat Metadata

TuneBat-backed database facts are the authority for beat/key planning when available. Fill missing local metadata with:

```bash
python3 scripts/slime_music_library.py analyze-tunebat-local DUPLICATE_KEY
```

Filename tags are not authoritative for BPM/key planning.

## Candidate Selection

`scripts/slime_audio_candidates.py` keeps live set constraints and ranks database-backed candidate tracks. Use it to avoid stale/recent material and to select tracks that fit the current set instead of repeating the same easy choices.

Use constraints to preserve operator steering across restarts:

```bash
python3 scripts/slime_audio_candidates.py constraints --init
python3 scripts/slime_audio_candidates.py set-constraints --vibe "fresh daytime" --direction "brighter but not corny" --energy-target 0.65 --exclude-artist "Artist Name" --reason "operator steering"
```

Candidate output should carry reasons the DJ can explain. If the list is too narrow after exclusions, change the query/vibe and search again instead of reusing stale tracks.

Repeat plays now carry real rotation pressure in candidate scoring (up to `-0.30` from `plays_seen` plus recency penalties), so heavily mirrored favorites cannot outrank fresh material forever. Stem readiness never filters candidates; stem-aware selection only applies a small tie-break bonus and queues background splits for what taste actually picked.

`slime_music_library.py stats` includes a `genre_lanes` block (loose text-lane counts plus `edm_share`) for acquisition planning: while the EDM share is low, weight new downloads toward club rhythm material so the remix bed pool grows alongside the leads.

## Agent Playbook

The detailed operating workflow for acquiring music, rescanning, analyzing, selecting candidates, planning creative beds, and rendering proofs lives in [skills/slime-audio-dj/SKILL.md](../skills/slime-audio-dj/SKILL.md).
