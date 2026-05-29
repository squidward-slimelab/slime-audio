from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
from dataclasses import asdict
from typing import Any

from .commands import PlannedCommand
from .redact import redact, redact_text


class SpogoUnavailable(RuntimeError):
    """Raised when the spogo binary is not installed."""


class SpogoRunner:
    def __init__(self, binary: str = "spogo", timeout: float = 15.0) -> None:
        self.binary = binary
        self.timeout = timeout

    def run(self, planned: PlannedCommand) -> dict[str, Any]:
        binary = self._binary_path()
        command = [binary, "--json", "--no-color", *planned.argv]

        if planned.dry_run:
            return {
                "ok": True,
                "dry_run": True,
                "planned": asdict(planned),
                "command": command,
            }

        if binary is None:
            raise SpogoUnavailable(f"{self.binary} is not installed or not on PATH")

        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=self.timeout,
        )

        stdout = redact_text(completed.stdout.strip())
        stderr = redact_text(completed.stderr.strip())
        payload = _parse_json(stdout)

        return {
            "ok": completed.returncode == 0,
            "returncode": completed.returncode,
            "action": planned.action,
            "stdout": redact(payload if payload is not None else stdout),
            "stderr": stderr,
        }

    def _binary_path(self) -> str | None:
        if found := shutil.which(self.binary):
            return found
        local = Path.home() / ".local" / "bin" / self.binary
        if local.exists():
            return str(local)
        return None


def _parse_json(text: str) -> Any | None:
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None
