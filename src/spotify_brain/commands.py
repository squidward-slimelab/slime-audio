from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class CommandError(ValueError):
    """Raised when a command is invalid or unsafe."""


@dataclass(frozen=True)
class CommandSpec:
    name: str
    mutates: bool
    destructive: bool


SPECS: dict[str, CommandSpec] = {
    "status": CommandSpec("status", mutates=False, destructive=False),
    "devices": CommandSpec("devices", mutates=False, destructive=False),
    "search": CommandSpec("search", mutates=False, destructive=False),
    "play": CommandSpec("play", mutates=True, destructive=False),
    "pause": CommandSpec("pause", mutates=True, destructive=False),
    "next": CommandSpec("next", mutates=True, destructive=False),
    "previous": CommandSpec("previous", mutates=True, destructive=False),
    "queue": CommandSpec("queue", mutates=True, destructive=False),
    "volume": CommandSpec("volume", mutates=True, destructive=False),
    "playlist-create": CommandSpec("playlist-create", mutates=True, destructive=False),
    "playlist-add": CommandSpec("playlist-add", mutates=True, destructive=False),
    "playlist-remove": CommandSpec("playlist-remove", mutates=True, destructive=True),
    "library-save": CommandSpec("library-save", mutates=True, destructive=False),
    "library-remove": CommandSpec("library-remove", mutates=True, destructive=True),
}


@dataclass(frozen=True)
class PlannedCommand:
    action: str
    argv: list[str]
    mutates: bool
    destructive: bool
    dry_run: bool


def plan_command(
    action: str,
    args: dict[str, Any] | None = None,
    *,
    dry_run: bool = False,
    confirm: bool = False,
) -> PlannedCommand:
    args = args or {}
    if action not in SPECS:
        raise CommandError(f"unknown action: {action}")

    spec = SPECS[action]
    argv = _argv_for(action, args)

    if spec.destructive and not dry_run and not confirm:
        raise CommandError(f"{action} is destructive; pass confirm=true or dry_run=true")

    return PlannedCommand(
        action=action,
        argv=argv,
        mutates=spec.mutates,
        destructive=spec.destructive,
        dry_run=dry_run,
    )


def _argv_for(action: str, args: dict[str, Any]) -> list[str]:
    if action == "status":
        return ["status"]
    if action == "devices":
        return ["device", "list"]
    if action == "search":
        query = _required_str(args, "query")
        argv = ["search", query]
        if kind := args.get("type"):
            argv.extend(["--type", str(kind)])
        if limit := args.get("limit"):
            argv.extend(["--limit", str(limit)])
        return argv
    if action == "play":
        uri = args.get("uri")
        return ["play", str(uri)] if uri else ["play"]
    if action == "pause":
        return ["pause"]
    if action == "next":
        return ["next"]
    if action == "previous":
        return ["previous"]
    if action == "queue":
        return ["queue", "add", _required_str(args, "uri")]
    if action == "volume":
        return ["volume", str(_required_int(args, "percent"))]
    if action == "playlist-create":
        argv = ["playlist", "create", _required_str(args, "name")]
        if description := args.get("description"):
            argv.extend(["--description", str(description)])
        return argv
    if action == "playlist-add":
        return ["playlist", "add", _required_str(args, "playlist_id"), *_required_list(args, "uris")]
    if action == "playlist-remove":
        return ["playlist", "remove", _required_str(args, "playlist_id"), *_required_list(args, "uris")]
    if action == "library-save":
        return ["library", "save", *_required_list(args, "uris")]
    if action == "library-remove":
        return ["library", "remove", *_required_list(args, "uris")]
    raise CommandError(f"unplanned action: {action}")


def _required_str(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise CommandError(f"missing required string arg: {key}")
    return value


def _required_int(args: dict[str, Any], key: str) -> int:
    value = args.get(key)
    if not isinstance(value, int):
        raise CommandError(f"missing required integer arg: {key}")
    return value


def _required_list(args: dict[str, Any], key: str) -> list[str]:
    value = args.get(key)
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not value:
        raise CommandError(f"missing required list arg: {key}")
    result = [str(item) for item in value if str(item).strip()]
    if not result:
        raise CommandError(f"missing required list arg: {key}")
    return result
