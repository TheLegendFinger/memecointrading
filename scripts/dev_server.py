#!/usr/bin/env python3
"""Serve the dashboard locally, exactly as Vercel would.

    python scripts/dev_server.py                 # http://localhost:8000
    python scripts/dev_server.py --port 3000 --db data/memebot.sqlite3

Same handler classes as the deployed functions, so what you see here is what
the deployment serves. Useful on Windows: run the bot in one terminal and this
in another to watch it work in the browser, with no cloud account involved.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PUBLIC = ROOT / "public"

# Route prefix -> the module under api/ that serves it.
ROUTES = {
    "/api/status": "status",
    "/api/positions": "positions",
    "/api/trades": "trades",
    "/api/equity": "equity",
    "/api/scan": "scan",
    "/api/events": "events",
    "/api/candles": "candles",
    "/api/cycle": "cycle",
    "/api/health": "health",
}


from api._common import render  # noqa: E402


def load_handler(module_name):
    module = __import__(f"api.{module_name}", fromlist=["handler"])
    return module.handler


class DevHandler(SimpleHTTPRequestHandler):
    """Static files from public/, plus the real API handlers for /api/*."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PUBLIC), **kwargs)

    def _route(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        return ROUTES.get(path)

    def _dispatch(self):
        module_name = self._route()
        if module_name is None:
            return False
        handler_cls = load_handler(module_name)
        status, body = render(handler_cls, self.path, self.headers)
        encoded = json.dumps(body, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)
        return True

    def do_GET(self):  # noqa: N802
        if not self._dispatch():
            super().do_GET()

    def do_POST(self):  # noqa: N802
        if not self._dispatch():
            self.send_error(405, "Method not allowed")

    def log_message(self, fmt, *args):
        sys.stderr.write("  %s\n" % (fmt % args))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Preview the memebot dashboard locally.")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--db", help="state database (path or postgres:// URL)")
    args = parser.parse_args(argv)

    if args.db:
        os.environ["MEMEBOT_STATE_DB"] = args.db
    os.environ.setdefault("CRON_SECRET", "dev")

    server = HTTPServer((args.host, args.port), DevHandler)
    print(f"\n  memebot dashboard  ->  http://{args.host}:{args.port}")
    print(f"  state              ->  {os.environ.get('MEMEBOT_STATE_DB', 'data/memebot.sqlite3')}")
    print(f"  run one cycle      ->  http://{args.host}:{args.port}/api/cycle?key=dev")
    print("\n  Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
