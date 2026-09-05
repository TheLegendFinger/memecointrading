"""Shared helpers for the Vercel serverless functions.

Each function is a plain `BaseHTTPRequestHandler` subclass, which is what
Vercel's Python runtime looks for - no web framework, so the deployment stays
dependency-light and cold starts stay short.
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from http.server import BaseHTTPRequestHandler
from typing import Any, Callable, Dict, Optional
from urllib.parse import parse_qs, urlparse

# The repository root, so `import memebot` works from inside api/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memebot.config import BotConfig, load_config  # noqa: E402
from memebot.portfolio import Portfolio  # noqa: E402
from memebot.storage import is_postgres_dsn, open_storage  # noqa: E402

CACHE_HEADER = "public, max-age=5, stale-while-revalidate=30"


def is_serverless() -> bool:
    """True when running on Vercel/Lambda, where the filesystem is ephemeral."""
    return bool(os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))


def load_bot_config(overrides: Optional[Dict[str, Any]] = None) -> BotConfig:
    """Config for one request.

    There is no config file in a deployment, so everything comes from
    environment variables; `state_db` resolves to POSTGRES_URL / DATABASE_URL
    automatically (see memebot.storage.resolve_state_target). Locally - the dev
    server - a SQLite file is fine and is left alone.
    """
    config = load_config(None, overrides)
    if is_serverless() and not is_postgres_dsn(config.state_db):
        # A serverless filesystem is ephemeral: SQLite here would silently lose
        # every trade between invocations. Say so instead of pretending.
        raise RuntimeError(
            "No Postgres connection string found. Set POSTGRES_URL (or DATABASE_URL) "
            "in the deployment's environment variables - serverless functions have no "
            "durable filesystem, so SQLite state would be discarded after each request."
        )
    return config


def open_portfolio(config: BotConfig):
    storage = open_storage(config.state_db)
    return storage, Portfolio(storage, config.risk.starting_cash_usd, mode=config.mode)


def query_params(path: str) -> Dict[str, str]:
    parsed = urlparse(path)
    return {key: values[0] for key, values in parse_qs(parsed.query).items()}


def int_param(params: Dict[str, str], name: str, default: int, maximum: int) -> int:
    try:
        return max(1, min(maximum, int(params.get(name, default))))
    except (TypeError, ValueError):
        return default


def is_authorized(handler: BaseHTTPRequestHandler) -> bool:
    """Guard for endpoints that change state.

    Vercel Cron sends `Authorization: Bearer $CRON_SECRET` when CRON_SECRET is
    set on the project. We accept that, or the same value as `?key=`, so an
    external scheduler (GitHub Actions, cron-job.org) can trigger it too.
    """
    secret = os.environ.get("CRON_SECRET", "").strip()
    if not secret:
        return False
    header = handler.headers.get("Authorization", "")
    if header == f"Bearer {secret}":
        return True
    return query_params(handler.path).get("key", "") == secret


class _Request:
    """The only two things a payload() needs: the path and the headers."""

    def __init__(self, path: str, headers: Any) -> None:
        self.path = path
        self.headers = headers


def render(handler_cls: type, path: str, headers: Any) -> tuple:
    """Run a handler's payload() outside of any socket.

    Returns (status, body). The deployed handler and the local dev server both
    go through here, so there is exactly one place where a request becomes a
    response - and one place where errors become status codes.
    """
    started = time.time()
    try:
        body = handler_cls.payload(_Request(path, headers))
    except PermissionError as exc:
        return 401, {"error": str(exc)}
    except RuntimeError as exc:
        return 503, {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - never leak a stack trace to the client
        traceback.print_exc()
        return 500, {"error": f"{type(exc).__name__}: {exc}"}
    if isinstance(body, dict):
        body.setdefault("generated_at", time.time())
        body.setdefault("took_ms", round((time.time() - started) * 1000, 1))
    return 200, body


class JsonHandler(BaseHTTPRequestHandler):
    """Base handler that renders JSON and turns exceptions into clean errors."""

    protocol_version = "HTTP/1.1"
    cache_control = CACHE_HEADER

    # Subclasses implement this.
    def payload(self) -> Any:  # pragma: no cover - overridden
        raise NotImplementedError

    def _send(self, status: int, body: Any, content_type: str = "application/json") -> None:
        if content_type == "application/json":
            encoded = json.dumps(body, default=str).encode("utf-8")
        else:
            encoded = body if isinstance(body, bytes) else str(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", self.cache_control)
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        self.handle_request()

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        self.handle_request()

    def handle_request(self) -> None:
        status, body = render(type(self), self.path, self.headers)
        self._send(status, body)

    def log_message(self, fmt: str, *args: Any) -> None:  # keep Vercel logs readable
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def with_portfolio(fn: Callable[[BotConfig, Portfolio, Any], Any]) -> Callable[[Any], Any]:
    """Open state, run `fn`, and always close the connection."""

    def wrapper(handler: Any) -> Any:
        config = load_bot_config()
        storage, portfolio = open_portfolio(config)
        try:
            return fn(config, portfolio, handler)
        finally:
            storage.close()

    return wrapper
