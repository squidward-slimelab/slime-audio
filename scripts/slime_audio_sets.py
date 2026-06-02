#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from slime_audio_session import load_payload, parse_ms, parse_session, write_payload

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SETS_DIR = REPO_ROOT / "runtime" / "sets"
DEFAULT_ACTIVE_SET = REPO_ROOT / "runtime" / "active-set.json"
DEFAULT_SESSION = REPO_ROOT / "runtime" / "mix-session.json"
DEFAULT_STATE = REPO_ROOT / "runtime" / "mix-session-state.json"
DEFAULT_HISTORY = REPO_ROOT / "runtime" / "play-history.jsonl"
DEFAULT_RENDER_DIR = REPO_ROOT / "runtime" / "set-renders"


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.lower()).strip("-")
    return slug or "set"


def load_json(path: Path, default: dict[str, Any] | None = None) -> dict[str, Any]:
    if not path.exists():
        return {} if default is None else dict(default)
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def set_dir(sets_dir: Path, slug: str) -> Path:
    clean = slugify(slug)
    path = (sets_dir / clean).resolve()
    if sets_dir.resolve() not in path.parents:
        raise ValueError(f"invalid set slug: {slug}")
    return path


def manifest_path(sets_dir: Path) -> Path:
    return sets_dir / "manifest.json"


def load_root_manifest(sets_dir: Path) -> dict[str, Any]:
    return load_json(manifest_path(sets_dir), {"version": 1, "sets": {}})


def save_root_manifest(sets_dir: Path, manifest: dict[str, Any]) -> None:
    manifest["version"] = 1
    manifest["updated_at"] = iso_now()
    manifest.setdefault("sets", {})
    write_json(manifest_path(sets_dir), manifest)


def read_history_refs(history_path: Path) -> dict[str, Any]:
    if not history_path.exists():
        return {"path": str(history_path), "event_count": 0}
    lines = [line for line in history_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return {"path": str(history_path), "event_count": len(lines)}


def session_summary(payload: dict[str, Any]) -> dict[str, Any]:
    parse_session(payload)
    counts = {
        "clips": len(payload.get("clips", [])),
        "mic_lean_ins": len(payload.get("mic_lean_ins", payload.get("micLeanIns", []))),
        "effects": len(payload.get("effects", [])),
        "automations": len(payload.get("automations", [])),
    }
    ends: list[int] = []
    for clip in payload.get("clips", []):
        duration = clip.get("duration_ms", clip.get("duration"))
        if duration is None:
            continue
        start_ms = parse_ms(clip.get("start_ms", clip.get("start", 0)), "clip start")
        ends.append(start_ms + parse_ms(duration, "clip duration"))
    for lean_in in payload.get("mic_lean_ins", payload.get("micLeanIns", [])):
        starts_at = parse_ms(lean_in.get("start_ms", lean_in.get("start", 0)), "lean-in start")
        ends.append(starts_at + 5000)
    for effect in payload.get("effects", []):
        starts_at = parse_ms(effect.get("start_ms", effect.get("start", 0)), "effect start")
        duration_ms = parse_ms(effect.get("duration_ms", effect.get("duration", 0)), "effect duration")
        tail_ms = parse_ms(effect.get("tail_ms", effect.get("tail", 0)), "effect tail")
        ends.append(starts_at + duration_ms + tail_ms)
    return {"duration_ms": max(ends, default=0), "counts": counts}


def set_metadata(
    *,
    title: str,
    slug: str,
    session_path: Path,
    sets_dir: Path,
    notes: str = "",
    constraints_path: Path | None = None,
    history_path: Path = DEFAULT_HISTORY,
    created_at: str | None = None,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = load_payload(session_path)
    summary = session_summary(payload)
    constraints: dict[str, Any] | None = None
    if constraints_path is not None and constraints_path.exists():
        constraints = load_json(constraints_path)
    now = iso_now()
    base = dict(existing or {})
    base.update(
        {
            "title": title,
            "slug": slug,
            "created_at": created_at or base.get("created_at") or now,
            "updated_at": now,
            "session_path": str(session_path),
            "notes": notes or base.get("notes", ""),
            "duration_ms": summary["duration_ms"],
            "counts": summary["counts"],
            "history": read_history_refs(history_path),
        }
    )
    if constraints is not None:
        base["constraints"] = constraints
    base.setdefault("rendered_review_artifact", None)
    base["archive_dir"] = str(set_dir(sets_dir, slug))
    return base


def write_set_manifest(sets_dir: Path, metadata: dict[str, Any]) -> None:
    slug = str(metadata["slug"])
    directory = set_dir(sets_dir, slug)
    write_json(directory / "manifest.json", metadata)
    root = load_root_manifest(sets_dir)
    root.setdefault("sets", {})[slug] = metadata
    save_root_manifest(sets_dir, root)


def archive_set(
    *,
    session: Path,
    sets_dir: Path,
    title: str,
    slug: str | None,
    notes: str = "",
    constraints: Path | None = None,
    history: Path = DEFAULT_HISTORY,
    overwrite: bool = False,
) -> dict[str, Any]:
    payload = load_payload(session)
    parse_session(payload)
    clean_slug = slugify(slug or title)
    directory = set_dir(sets_dir, clean_slug)
    archived_session = directory / "session.json"
    if directory.exists() and not overwrite:
        raise FileExistsError(f"set already exists: {clean_slug}")
    directory.mkdir(parents=True, exist_ok=True)
    write_payload(archived_session, payload)
    metadata = set_metadata(
        title=title,
        slug=clean_slug,
        session_path=archived_session,
        sets_dir=sets_dir,
        notes=notes,
        constraints_path=constraints,
        history_path=history,
    )
    write_set_manifest(sets_dir, metadata)
    return metadata


def get_set(sets_dir: Path, slug: str) -> dict[str, Any]:
    clean = slugify(slug)
    path = set_dir(sets_dir, clean) / "manifest.json"
    if not path.exists():
        raise FileNotFoundError(f"unknown set: {slug}")
    return load_json(path)


def list_sets(sets_dir: Path) -> list[dict[str, Any]]:
    manifest = load_root_manifest(sets_dir)
    sets = list((manifest.get("sets") or {}).values())
    return sorted(sets, key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)


def write_active_pointer(active_path: Path, metadata: dict[str, Any], *, active_session: Path, active_state: Path) -> dict[str, Any]:
    pointer = {
        "slug": metadata["slug"],
        "title": metadata["title"],
        "archive_session_path": metadata["session_path"],
        "active_session_path": str(active_session),
        "active_state_path": str(active_state),
        "loaded_at": iso_now(),
    }
    write_json(active_path, pointer)
    return pointer


def activate_set(
    *,
    sets_dir: Path,
    slug: str,
    active_session: Path = DEFAULT_SESSION,
    active_state: Path = DEFAULT_STATE,
    active_pointer: Path = DEFAULT_ACTIVE_SET,
    reset_state: bool = False,
) -> dict[str, Any]:
    metadata = get_set(sets_dir, slug)
    archive_session = Path(str(metadata["session_path"]))
    payload = load_payload(archive_session)
    write_payload(active_session, payload)
    if reset_state:
        write_json(active_state, {"playhead_ms": 0, "loaded_at": iso_now(), "session": str(active_session)})
    pointer = write_active_pointer(active_pointer, metadata, active_session=active_session, active_state=active_state)
    return {"set": metadata, "active": pointer}


def save_loaded_set(
    *,
    sets_dir: Path,
    active_pointer: Path = DEFAULT_ACTIVE_SET,
    active_session: Path = DEFAULT_SESSION,
    history: Path = DEFAULT_HISTORY,
) -> dict[str, Any]:
    pointer = load_json(active_pointer)
    slug = pointer.get("slug")
    if not slug:
        raise ValueError(f"no active set pointer found at {active_pointer}")
    existing = get_set(sets_dir, str(slug))
    archive_session = Path(str(existing["session_path"]))
    payload = load_payload(active_session)
    write_payload(archive_session, payload)
    metadata = set_metadata(
        title=str(existing["title"]),
        slug=str(existing["slug"]),
        session_path=archive_session,
        sets_dir=sets_dir,
        notes=str(existing.get("notes") or ""),
        history_path=history,
        created_at=str(existing.get("created_at") or ""),
        existing=existing,
    )
    write_set_manifest(sets_dir, metadata)
    return metadata


def new_set(
    *,
    sets_dir: Path,
    title: str,
    slug: str | None,
    active_session: Path = DEFAULT_SESSION,
    active_state: Path = DEFAULT_STATE,
    active_pointer: Path = DEFAULT_ACTIVE_SET,
    overwrite: bool = False,
) -> dict[str, Any]:
    clean_slug = slugify(slug or title)
    directory = set_dir(sets_dir, clean_slug)
    if directory.exists() and not overwrite:
        raise FileExistsError(f"set already exists: {clean_slug}")
    payload = {
        "version": 1,
        "decks": ["deck-1", "deck-2", "deck-3", "deck-4"],
        "clips": [],
        "mic_lean_ins": [],
        "effects": [],
        "automations": [],
        "deck_automations": [],
    }
    session_path = directory / "session.json"
    write_payload(session_path, payload)
    metadata = set_metadata(title=title, slug=clean_slug, session_path=session_path, sets_dir=sets_dir)
    write_set_manifest(sets_dir, metadata)
    write_payload(active_session, payload)
    pointer = write_active_pointer(active_pointer, metadata, active_session=active_session, active_state=active_state)
    write_json(active_state, {"playhead_ms": 0, "loaded_at": iso_now(), "session": str(active_session)})
    return {"set": metadata, "active": pointer}


def fork_set(*, sets_dir: Path, source_slug: str, title: str, slug: str | None, overwrite: bool = False) -> dict[str, Any]:
    source = get_set(sets_dir, source_slug)
    clean_slug = slugify(slug or title)
    directory = set_dir(sets_dir, clean_slug)
    if directory.exists() and not overwrite:
        raise FileExistsError(f"set already exists: {clean_slug}")
    directory.mkdir(parents=True, exist_ok=True)
    source_session = Path(str(source["session_path"]))
    target_session = directory / "session.json"
    shutil.copy2(source_session, target_session)
    metadata = set_metadata(
        title=title,
        slug=clean_slug,
        session_path=target_session,
        sets_dir=sets_dir,
        notes=f"forked from {source['slug']}",
        existing={"forked_from": source["slug"]},
    )
    write_set_manifest(sets_dir, metadata)
    return metadata


def render_files(render_dir: Path) -> list[Path]:
    if not render_dir.exists():
        return []
    return sorted((path for path in render_dir.iterdir() if path.is_file()), key=lambda path: path.stat().st_mtime, reverse=True)


def prune_renders(render_dir: Path, *, keep: int, max_age_hours: float, max_total_mb: float) -> list[str]:
    deleted: list[str] = []
    now = time.time()
    files = render_files(render_dir)
    protected = set(files[: max(0, keep)])
    for path in list(files):
        age_hours = (now - path.stat().st_mtime) / 3600
        if path not in protected and age_hours > max_age_hours:
            deleted.append(str(path))
            path.unlink(missing_ok=True)
    files = render_files(render_dir)
    total_bytes = sum(path.stat().st_size for path in files)
    max_bytes = int(max_total_mb * 1024 * 1024)
    for path in sorted(files, key=lambda item: item.stat().st_mtime):
        if total_bytes <= max_bytes or path in protected:
            continue
        size = path.stat().st_size
        deleted.append(str(path))
        path.unlink(missing_ok=True)
        total_bytes -= size
    return deleted


def render_set(
    *,
    sets_dir: Path,
    slug: str | None,
    session: Path | None,
    output: Path | None,
    render_dir: Path,
    output_format: str,
    mp3_bitrate: str,
    from_time: str,
    duration: str | None,
    skip_tts: bool,
    dry_run: bool,
    keep: int,
    max_age_hours: float,
    max_total_mb: float,
) -> dict[str, Any]:
    metadata = get_set(sets_dir, slug) if slug else None
    session_path = session or Path(str(metadata["session_path"])) if metadata else session
    if session_path is None:
        raise ValueError("render requires --slug or --session")
    render_dir.mkdir(parents=True, exist_ok=True)
    stem = slugify(slug or session_path.stem)
    rendered = output or (render_dir / f"{stem}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.mp3")
    report = rendered.with_suffix(".json")
    command = [
        "python3",
        "scripts/slime_audio_session_mixdown.py",
        str(session_path),
        "--output",
        str(rendered),
        "--format",
        output_format,
        "--mp3-bitrate",
        mp3_bitrate,
        "--from",
        from_time,
        "--report-output",
        str(report),
    ]
    if duration:
        command.extend(["--duration", duration])
    if skip_tts:
        command.append("--skip-tts")
    if dry_run:
        command.append("--dry-run")
    if not dry_run:
        subprocess.run(command, cwd=REPO_ROOT, check=True)
    deleted = [] if dry_run else prune_renders(render_dir, keep=keep, max_age_hours=max_age_hours, max_total_mb=max_total_mb)
    result = {"command": command, "output": str(rendered), "report": str(report), "deleted": deleted}
    if metadata is not None and not dry_run:
        metadata["rendered_review_artifact"] = {"path": str(rendered), "report": str(report), "created_at": iso_now()}
        write_set_manifest(sets_dir, metadata)
    return result


def replay_set(
    *,
    sets_dir: Path,
    slug: str,
    target: list[str],
    dry_run: bool,
    reset_state: bool,
) -> dict[str, Any]:
    activated = activate_set(sets_dir=sets_dir, slug=slug, reset_state=reset_state)
    command = [
        "python3",
        "scripts/slime_audio_session_runner.py",
        "--session",
        DEFAULT_SESSION.as_posix(),
        "--state",
        DEFAULT_STATE.as_posix(),
    ]
    for value in target:
        command.extend(["--target", value])
    if reset_state:
        command.append("--reset-state")
    if dry_run:
        command.append("--dry-run")
        return {"active": activated["active"], "command": command}
    process = subprocess.Popen(command, cwd=REPO_ROOT)
    return {"active": activated["active"], "command": command, "pid": process.pid}


def main() -> int:
    parser = argparse.ArgumentParser(description="Archive, load, replay, and render named SlimeAudio sets.")
    parser.add_argument("--sets-dir", type=Path, default=DEFAULT_SETS_DIR)
    sub = parser.add_subparsers(dest="command", required=True)

    list_parser = sub.add_parser("list")
    list_parser.add_argument("--json", action="store_true")

    show_parser = sub.add_parser("show")
    show_parser.add_argument("slug")

    archive_parser = sub.add_parser("archive")
    archive_parser.add_argument("--session", type=Path, default=DEFAULT_SESSION)
    archive_parser.add_argument("--title", required=True)
    archive_parser.add_argument("--slug")
    archive_parser.add_argument("--notes", default="")
    archive_parser.add_argument("--constraints", type=Path)
    archive_parser.add_argument("--history", type=Path, default=DEFAULT_HISTORY)
    archive_parser.add_argument("--overwrite", action="store_true")

    new_parser = sub.add_parser("new")
    new_parser.add_argument("--title", required=True)
    new_parser.add_argument("--slug")
    new_parser.add_argument("--overwrite", action="store_true")

    activate_parser = sub.add_parser("activate")
    activate_parser.add_argument("slug")
    activate_parser.add_argument("--reset-state", action="store_true")

    save_parser = sub.add_parser("save-loaded")
    save_parser.add_argument("--history", type=Path, default=DEFAULT_HISTORY)

    fork_parser = sub.add_parser("fork")
    fork_parser.add_argument("source_slug")
    fork_parser.add_argument("--title", required=True)
    fork_parser.add_argument("--slug")
    fork_parser.add_argument("--overwrite", action="store_true")

    replay_parser = sub.add_parser("replay")
    replay_parser.add_argument("slug")
    replay_parser.add_argument("--target", action="append", default=None)
    replay_parser.add_argument("--reset-state", action="store_true")
    replay_parser.add_argument("--dry-run", action="store_true")

    render_parser = sub.add_parser("render")
    render_parser.add_argument("--slug")
    render_parser.add_argument("--session", type=Path)
    render_parser.add_argument("--output", type=Path)
    render_parser.add_argument("--render-dir", type=Path, default=DEFAULT_RENDER_DIR)
    render_parser.add_argument("--format", choices=["auto", "wav", "mp3", "flac"], default="mp3")
    render_parser.add_argument("--mp3-bitrate", default="128k")
    render_parser.add_argument("--from", dest="from_time", default="0")
    render_parser.add_argument("--duration")
    render_parser.add_argument("--skip-tts", action="store_true")
    render_parser.add_argument("--dry-run", action="store_true")
    render_parser.add_argument("--keep", type=int, default=3)
    render_parser.add_argument("--max-age-hours", type=float, default=12)
    render_parser.add_argument("--max-total-mb", type=float, default=256)

    cleanup_parser = sub.add_parser("cleanup-renders")
    cleanup_parser.add_argument("--render-dir", type=Path, default=DEFAULT_RENDER_DIR)
    cleanup_parser.add_argument("--keep", type=int, default=3)
    cleanup_parser.add_argument("--max-age-hours", type=float, default=12)
    cleanup_parser.add_argument("--max-total-mb", type=float, default=256)

    args = parser.parse_args()
    if args.command == "list":
        sets = list_sets(args.sets_dir)
        print(json.dumps({"sets": sets}, indent=2, sort_keys=True) if args.json else "\n".join(f"{item['slug']}\t{item['title']}" for item in sets))
        return 0
    if args.command == "show":
        print(json.dumps(get_set(args.sets_dir, args.slug), indent=2, sort_keys=True))
        return 0
    if args.command == "archive":
        print(
            json.dumps(
                archive_set(
                    session=args.session,
                    sets_dir=args.sets_dir,
                    title=args.title,
                    slug=args.slug,
                    notes=args.notes,
                    constraints=args.constraints,
                    history=args.history,
                    overwrite=args.overwrite,
                ),
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if args.command == "new":
        print(json.dumps(new_set(sets_dir=args.sets_dir, title=args.title, slug=args.slug, overwrite=args.overwrite), indent=2, sort_keys=True))
        return 0
    if args.command == "activate":
        print(json.dumps(activate_set(sets_dir=args.sets_dir, slug=args.slug, reset_state=args.reset_state), indent=2, sort_keys=True))
        return 0
    if args.command == "save-loaded":
        print(json.dumps(save_loaded_set(sets_dir=args.sets_dir, history=args.history), indent=2, sort_keys=True))
        return 0
    if args.command == "fork":
        print(json.dumps(fork_set(sets_dir=args.sets_dir, source_slug=args.source_slug, title=args.title, slug=args.slug, overwrite=args.overwrite), indent=2, sort_keys=True))
        return 0
    if args.command == "replay":
        print(json.dumps(replay_set(sets_dir=args.sets_dir, slug=args.slug, target=args.target or ["all"], dry_run=args.dry_run, reset_state=args.reset_state), indent=2, sort_keys=True))
        return 0
    if args.command == "render":
        print(
            json.dumps(
                render_set(
                    sets_dir=args.sets_dir,
                    slug=args.slug,
                    session=args.session,
                    output=args.output,
                    render_dir=args.render_dir,
                    output_format=args.format,
                    mp3_bitrate=args.mp3_bitrate,
                    from_time=args.from_time,
                    duration=args.duration,
                    skip_tts=args.skip_tts,
                    dry_run=args.dry_run,
                    keep=args.keep,
                    max_age_hours=args.max_age_hours,
                    max_total_mb=args.max_total_mb,
                ),
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if args.command == "cleanup-renders":
        print(json.dumps({"deleted": prune_renders(args.render_dir, keep=args.keep, max_age_hours=args.max_age_hours, max_total_mb=args.max_total_mb)}, indent=2, sort_keys=True))
        return 0
    raise SystemExit(f"unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
