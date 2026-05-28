from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .app import SpotifyBrain


def serve(host: str, port: int) -> None:
    brain = SpotifyBrain()

    class Handler(BaseHTTPRequestHandler):
        server_version = "SpotifyBrain/0.1"

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def do_GET(self) -> None:
            if self.path == "/health":
                self._write({"ok": True})
                return
            self._write({"ok": False, "error": "not found"}, status=404)

        def do_POST(self) -> None:
            if self.path != "/v1/command":
                self._write({"ok": False, "error": "not found"}, status=404)
                return
            try:
                length = int(self.headers.get("content-length", "0"))
                body = self.rfile.read(length).decode("utf-8")
                payload = json.loads(body) if body else {}
            except (ValueError, json.JSONDecodeError) as exc:
                self._write({"ok": False, "error": f"invalid json: {exc}"}, status=400)
                return
            self._write(brain.execute(payload))

        def _write(self, payload: dict[str, Any], status: int = 200) -> None:
            data = json.dumps(payload, sort_keys=True).encode("utf-8")
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    ThreadingHTTPServer((host, port), Handler).serve_forever()
