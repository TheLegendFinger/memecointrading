"""A small, dependency-light HTTP helper with retries and rate limiting.

Every outbound API call in the bot goes through this so that back-off,
timeouts and request budgets are handled in exactly one place.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections import deque
from typing import Any, Dict, Optional

import requests

log = logging.getLogger(__name__)


class HttpError(RuntimeError):
    """Raised when a request ultimately fails after all retries."""

    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


class RateLimiter:
    """Simple thread-safe sliding-window limiter."""

    def __init__(self, max_per_minute: int) -> None:
        self.max_per_minute = max(1, int(max_per_minute))
        self._calls: deque = deque()
        self._lock = threading.Lock()

    def acquire(self, sleep=time.sleep) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                while self._calls and now - self._calls[0] > 60.0:
                    self._calls.popleft()
                if len(self._calls) < self.max_per_minute:
                    self._calls.append(now)
                    return
                wait = 60.0 - (now - self._calls[0])
            sleep(max(0.01, wait))


class HttpClient:
    """requests.Session wrapper: retries idempotent failures with jittered backoff."""

    RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

    def __init__(
        self,
        base_url: str = "",
        timeout: float = 12.0,
        max_retries: int = 3,
        backoff_seconds: float = 1.0,
        rate_limit_per_minute: int = 120,
        headers: Optional[Dict[str, str]] = None,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max(0, int(max_retries))
        self.backoff_seconds = backoff_seconds
        self.limiter = RateLimiter(rate_limit_per_minute)
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "memebot/1.0", "Accept": "application/json"})
        if headers:
            self.session.headers.update(headers)

    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self.base_url}/{path.lstrip('/')}"

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        url = self._url(path)
        last_error: Optional[str] = None
        last_status: Optional[int] = None

        for attempt in range(self.max_retries + 1):
            self.limiter.acquire()
            try:
                resp = self.session.request(
                    method.upper(),
                    url,
                    params=params,
                    json=json_body,
                    timeout=timeout or self.timeout,
                )
            except requests.RequestException as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                last_status = None
            else:
                if resp.status_code < 400:
                    if not resp.content:
                        return None
                    try:
                        return resp.json()
                    except ValueError as exc:
                        raise HttpError(f"Non-JSON response from {url}: {exc}", resp.status_code)
                last_status = resp.status_code
                last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                if resp.status_code not in self.RETRY_STATUS:
                    raise HttpError(f"{method.upper()} {url} failed - {last_error}", last_status)

            if attempt < self.max_retries:
                delay = self.backoff_seconds * (2**attempt) * (0.8 + 0.4 * random.random())
                log.debug("Retrying %s %s in %.2fs (%s)", method, url, delay, last_error)
                time.sleep(delay)

        raise HttpError(f"{method.upper()} {url} failed after {self.max_retries + 1} attempts - {last_error}", last_status)

    def get(self, path: str, **kwargs: Any) -> Any:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Any:
        return self.request("POST", path, **kwargs)
