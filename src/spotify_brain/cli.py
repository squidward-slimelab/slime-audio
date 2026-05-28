from __future__ import annotations

import argparse
import json
from typing import Any

from .app import SpotifyBrain
from .server import serve


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="spotify-brain")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="run a spotify command")
    run_parser.add_argument("action", choices=_actions())
    run_parser.add_argument("values", nargs="*")
    run_parser.add_argument("--type", dest="kind")
    run_parser.add_argument("--limit", type=int)
    run_parser.add_argument("--description")
    run_parser.add_argument("--uri")
    run_parser.add_argument("--dry-run", action="store_true")
    run_parser.add_argument("--confirm", action="store_true")

    serve_parser = subparsers.add_parser("serve", help="start the local JSON daemon")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8765)

    args = parser.parse_args(argv)

    if args.command == "serve":
        serve(args.host, args.port)
        return 0

    payload = {
        "action": args.action,
        "args": _args_for_cli(args),
        "dry_run": args.dry_run,
        "confirm": args.confirm,
    }
    result = SpotifyBrain().execute(payload)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 1


def _actions() -> list[str]:
    return [
        "status",
        "devices",
        "search",
        "play",
        "pause",
        "next",
        "previous",
        "queue",
        "volume",
        "playlist-create",
        "playlist-add",
        "playlist-remove",
        "library-save",
        "library-remove",
    ]


def _args_for_cli(args: argparse.Namespace) -> dict[str, Any]:
    action = args.action
    values = args.values

    if action == "search":
        return {"query": " ".join(values), "type": args.kind, "limit": args.limit}
    if action == "play":
        return {"uri": args.uri or (values[0] if values else None)}
    if action == "queue":
        return {"uri": args.uri or _first(values)}
    if action == "volume":
        return {"percent": int(_first(values))}
    if action == "playlist-create":
        return {"name": " ".join(values), "description": args.description}
    if action in {"playlist-add", "playlist-remove"}:
        return {"playlist_id": _first(values), "uris": values[1:]}
    if action in {"library-save", "library-remove"}:
        return {"uris": values}
    return {}


def _first(values: list[str]) -> str:
    if not values:
        raise SystemExit("missing required positional value")
    return values[0]
