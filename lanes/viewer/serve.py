#!/usr/bin/env python3
"""Loom lane transcript viewer: every agent session on this machine, as chat.

    python3 lanes/viewer/serve.py            # http://localhost:8765/

Binds 127.0.0.1 only and answers only requests addressed to localhost, so the
transcripts (agent output, never published) stay on this machine and a web
page cannot reach the viewer by DNS rebinding. Pages carry a CSP that allows
no scripts at all.

Env: LOOM_HOME (default ~/loom), LOOM_VIEWER_PORT (default 8765).
"""

from __future__ import annotations

import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import render  # noqa: E402
import transcripts  # noqa: E402

CSP = "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'"
ROUTE = re.compile(r"^/t/([a-z-]+)/([A-Za-z0-9._-]+)$")


def rows(loom: Path) -> list[dict]:
    """Every brief across the lanes, newest first, with done/failed/queued."""
    found = []
    for lane in transcripts.LANES:
        inbox = loom / "briefs" / lane
        for status, folder in (("done", inbox / "done"), ("failed", inbox / "failed"), ("queued", inbox)):
            if not folder.is_dir():
                continue
            for brief in folder.glob("*.md"):
                found.append({"lane": lane, "ticket": brief.stem, "status": status,
                              "mtime": brief.stat().st_mtime})
    return sorted(found, key=lambda r: r["mtime"], reverse=True)


def allowed_host(host: str, port: int) -> bool:
    return host in (f"localhost:{port}", f"127.0.0.1:{port}")


def make_handler(loom: Path, port: int):
    home = Path.home()

    class Handler(BaseHTTPRequestHandler):
        def send(self, code: int, body: str) -> None:
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802 (http.server API)
            if not allowed_host(self.headers.get("Host", ""), port):
                self.send(403, "forbidden")
                return
            if self.path in ("/", "/index.html"):
                self.send(200, render.index(rows(loom)))
                return
            m = ROUTE.match(self.path)
            if not m:
                self.send(404, "not found")
                return
            lane, ticket = m.groups()
            try:
                events = transcripts.load_events(lane, ticket, loom, home / ".claude" / "projects",
                                                 home / ".codex" / "sessions")
            except ValueError:
                self.send(404, "not found")
                return
            self.send(200, render.page(ticket, lane, events))

        def log_message(self, format, *args) -> None:  # noqa: A002 (base signature)
            pass

    return Handler


def main() -> None:
    loom = Path(os.environ.get("LOOM_HOME", str(Path.home() / "loom")))
    port = int(os.environ.get("LOOM_VIEWER_PORT", "8765"))
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(loom, port))
    print(f"Loom viewer on http://localhost:{port}/", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
