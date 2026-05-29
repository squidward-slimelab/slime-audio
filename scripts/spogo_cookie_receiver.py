#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import secrets
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


PAGE = """<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>spogo cookie login</title>
  <style>
    body { font-family: system-ui, sans-serif; max-width: 720px; margin: 40px auto; padding: 0 16px; }
    label { display: block; margin: 18px 0 6px; font-weight: 650; }
    input { box-sizing: border-box; width: 100%; padding: 10px; font: inherit; }
    button { margin-top: 20px; padding: 10px 14px; font: inherit; cursor: pointer; }
    .note { color: #555; line-height: 1.45; }
    .warn { color: #8a4b00; }
  </style>
</head>
<body>
  <h1>spogo cookie login</h1>
  <p class="note">Paste Spotify cookie values from DevTools. This page submits once, writes spogo cookies locally, then shuts itself down.</p>
  <p class="note warn">Do not paste passwords. Use only <code>sp_dc</code>, optional <code>sp_key</code>, and recommended <code>sp_t</code>.</p>
  <form method="post" action="/submit?token=__TOKEN__">
    <label for="sp_dc">sp_dc required</label>
    <input id="sp_dc" name="sp_dc" autocomplete="off" autofocus required>
    <label for="sp_key">sp_key optional</label>
    <input id="sp_key" name="sp_key" autocomplete="off">
    <label for="sp_t">sp_t recommended</label>
    <input id="sp_t" name="sp_t" autocomplete="off">
    <button type="submit">save cookies</button>
  </form>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--spogo", default="/home/squidward/.local/bin/spogo")
    parser.add_argument("--ttl", type=int, default=600)
    args = parser.parse_args()

    token = secrets.token_urlsafe(24)
    stop_event = threading.Event()
    started = time.monotonic()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:
            return

        def do_GET(self) -> None:
            if expired():
                self.write("expired", status=410)
                stop_event.set()
                return
            parsed = urlparse(self.path)
            if parsed.path != "/" or parse_qs(parsed.query).get("token", [""])[0] != token:
                self.write("not found", status=404)
                return
            self.write(PAGE.replace("__TOKEN__", html.escape(token)), content_type="text/html")

        def do_POST(self) -> None:
            if expired():
                self.write("expired", status=410)
                stop_event.set()
                return
            parsed = urlparse(self.path)
            if parsed.path != "/submit" or parse_qs(parsed.query).get("token", [""])[0] != token:
                self.write("not found", status=404)
                return
            length = int(self.headers.get("content-length", "0"))
            fields = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
            sp_dc = fields.get("sp_dc", [""])[0].strip()
            sp_key = fields.get("sp_key", [""])[0].strip()
            sp_t = fields.get("sp_t", [""])[0].strip()
            if not sp_dc:
                self.write("sp_dc is required", status=400)
                return
            lines = [f"sp_dc={sp_dc}"]
            if sp_key:
                lines.append(f"sp_key={sp_key}")
            if sp_t:
                lines.append(f"sp_t={sp_t}")
            result = subprocess.run(
                [args.spogo, "auth", "paste", "--no-input"],
                input="\n".join(lines) + "\n",
                text=True,
                capture_output=True,
                check=False,
            )
            if result.returncode != 0:
                self.write("spogo rejected the cookies; re-check the copied values", status=400)
                return
            status = subprocess.run(
                [args.spogo, "auth", "status", "--json", "--no-color"],
                text=True,
                capture_output=True,
                check=False,
            )
            self.write(
                "<h1>saved</h1><p>spogo accepted the cookies. this receiver is shutting down.</p>"
                f"<pre>{html.escape(status.stdout or status.stderr)}</pre>",
                content_type="text/html",
            )
            stop_event.set()

        def write(self, body: str, *, status: int = 200, content_type: str = "text/plain") -> None:
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("content-type", f"{content_type}; charset=utf-8")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    def expired() -> bool:
        return time.monotonic() - started > args.ttl

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    host, port = server.server_address
    print(f"http://{host}:{port}/?token={token}", flush=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    timer = threading.Timer(args.ttl, stop_event.set)
    timer.daemon = True
    timer.start()
    while not stop_event.wait(0.2):
        pass
    timer.cancel()
    server.shutdown()
    server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
