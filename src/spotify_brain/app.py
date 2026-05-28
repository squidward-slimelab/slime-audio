from __future__ import annotations

from typing import Any

from .commands import CommandError, plan_command
from .spogo import SpogoRunner, SpogoUnavailable


class SpotifyBrain:
    def __init__(self, runner: SpogoRunner | None = None) -> None:
        self.runner = runner or SpogoRunner()

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            planned = plan_command(
                str(payload.get("action", "")),
                payload.get("args") or {},
                dry_run=bool(payload.get("dry_run", False)),
                confirm=bool(payload.get("confirm", False)),
            )
            return self.runner.run(planned)
        except (CommandError, SpogoUnavailable) as exc:
            return {"ok": False, "error": str(exc)}
        except TimeoutError as exc:
            return {"ok": False, "error": f"spogo timed out: {exc}"}
