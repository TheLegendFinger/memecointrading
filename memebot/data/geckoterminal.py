"""GeckoTerminal pool feeds - discovery by trading activity, not by name.

DexScreener's public API has no "what is hot right now" endpoint, so searching
it means searching *text*: ask for "moon" and you get whatever is called moon
something. One family of copycats with a fashionable ticker can fill a whole
scan that way, which is exactly what it looked like from the outside.

GeckoTerminal ranks pools by what is happening in them, is free and needs no
key (https://api.geckoterminal.com/docs/index.html):

  GET /networks/{network}/trending_pools
  GET /networks/{network}/pools                  (busiest by 24h volume)
  GET /networks/{network}/new_pools

Only base token addresses are taken from it. Every number the bot trades on
still comes from DexScreener, so there is one source of truth for prices,
liquidity and transaction counts, and this module cannot skew them.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional

from ..http import HttpClient, HttpError

log = logging.getLogger(__name__)

GECKO_BASE_URL = "https://api.geckoterminal.com/api/v2"


class GeckoTerminalClient:
    """Pool feeds. Every method returns [] rather than raising: a feed that is
    down should cost the scan some breadth, never the cycle."""

    def __init__(
        self,
        base_url: str = GECKO_BASE_URL,
        network: str = "solana",
        timeout: float = 12.0,
        max_retries: int = 2,
        backoff_seconds: float = 1.0,
        rate_limit_per_minute: int = 30,   # GeckoTerminal's free tier
        http: Optional[HttpClient] = None,
    ) -> None:
        self.network = network
        self.http = http or HttpClient(
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
            backoff_seconds=backoff_seconds,
            rate_limit_per_minute=rate_limit_per_minute,
        )

    # ---- internals -------------------------------------------------------------
    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        try:
            return self.http.get(path, params=params)
        except HttpError as exc:
            log.warning("GeckoTerminal request failed (%s): %s", path, exc)
            return None

    def _strip_network(self, value: str) -> str:
        """GeckoTerminal ids are "{network}_{address}"."""
        prefix = f"{self.network}_"
        return value[len(prefix):] if value.startswith(prefix) else value

    def _base_tokens(self, payload: Any, limit: int) -> List[str]:
        """Base token addresses out of a JSON:API pool listing."""
        rows = (payload or {}).get("data") if isinstance(payload, dict) else None
        out: List[str] = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            token = (
                ((row.get("relationships") or {}).get("base_token") or {}).get("data") or {}
            ).get("id")
            if not token:
                continue
            address = self._strip_network(str(token))
            if address and address not in out:
                out.append(address)
            if len(out) >= limit:
                break
        return out

    def _feed(self, path: str, limit: int, params: Optional[Dict[str, Any]] = None) -> List[str]:
        """Walk pages until `limit` addresses are collected."""
        out: List[str] = []
        for page in (1, 2, 3):
            query = dict(params or {})
            query["page"] = page
            batch = self._base_tokens(self._get(path, params=query), limit)
            new = [a for a in batch if a not in out]
            out.extend(new)
            if len(out) >= limit or not new:
                break
        return out[:limit]

    # ---- feeds -----------------------------------------------------------------
    def trending_tokens(self, limit: int = 60, duration: str = "1h") -> List[str]:
        """What is moving right now, by GeckoTerminal's own trending ranking."""
        return self._feed(
            f"/networks/{self.network}/trending_pools", limit, {"duration": duration}
        )

    def top_volume_tokens(self, limit: int = 60) -> List[str]:
        """The busiest pools on the chain by 24h volume."""
        return self._feed(
            f"/networks/{self.network}/pools", limit, {"sort": "h24_volume_usd_desc"}
        )

    def new_pool_tokens(self, limit: int = 40) -> List[str]:
        """The most recently created pools. The riskiest feed by a distance -
        the age filter throws most of it away, which is the intent."""
        return self._feed(f"/networks/{self.network}/new_pools", limit)

    def reachable(self) -> bool:
        """Whether the API answers at all - used by the health check."""
        try:
            self.http.get(f"/networks/{self.network}/trending_pools", params={"page": 1})
            return True
        except HttpError:
            return False


def merge_addresses(*groups: Iterable[str]) -> List[str]:
    """Interleave feeds so a long one cannot crowd out the others."""
    lists = [list(g) for g in groups]
    out: List[str] = []
    seen = set()
    for i in range(max((len(g) for g in lists), default=0)):
        for group in lists:
            if i >= len(group):
                continue
            address = group[i]
            if address in seen:
                continue
            seen.add(address)
            out.append(address)
    return out
