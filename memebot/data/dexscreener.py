"""DexScreener market data client.

DexScreener is the discovery + pricing source: it is free, needs no key and
covers essentially every Solana memecoin pair minutes after launch.

Endpoints used (all public, documented at https://docs.dexscreener.com):
  GET /latest/dex/search?q=<term>
  GET /token-pairs/v1/{chain}/{tokenAddress}
  GET /latest/dex/pairs/{chain}/{pairAddresses}
  GET /token-boosts/top/v1
  GET /token-boosts/latest/v1
  GET /token-profiles/latest/v1
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Iterable, List, Optional

from ..http import HttpClient, HttpError
from ..models import PairSnapshot, Token

log = logging.getLogger(__name__)


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _i(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_pair(raw: Dict[str, Any]) -> Optional[PairSnapshot]:
    """Normalise one DexScreener pair object into a PairSnapshot."""
    if not isinstance(raw, dict):
        return None
    base = raw.get("baseToken") or {}
    quote = raw.get("quoteToken") or {}
    if not base.get("address"):
        return None

    created_ms = _f(raw.get("pairCreatedAt"), 0.0)
    volume = {k: _f(v) for k, v in (raw.get("volume") or {}).items()}
    change = {k: _f(v) for k, v in (raw.get("priceChange") or {}).items()}
    txns: Dict[str, Dict[str, int]] = {}
    for window, counts in (raw.get("txns") or {}).items():
        if isinstance(counts, dict):
            txns[window] = {"buys": _i(counts.get("buys")), "sells": _i(counts.get("sells"))}

    return PairSnapshot(
        chain_id=str(raw.get("chainId") or ""),
        dex_id=str(raw.get("dexId") or ""),
        pair_address=str(raw.get("pairAddress") or ""),
        base=Token(
            address=str(base.get("address")),
            symbol=str(base.get("symbol") or ""),
            name=str(base.get("name") or ""),
        ),
        quote=Token(
            address=str(quote.get("address") or ""),
            symbol=str(quote.get("symbol") or ""),
            name=str(quote.get("name") or ""),
        ),
        price_usd=_f(raw.get("priceUsd")),
        price_native=_f(raw.get("priceNative")),
        liquidity_usd=_f((raw.get("liquidity") or {}).get("usd")),
        fdv=_f(raw.get("fdv")),
        market_cap=_f(raw.get("marketCap")),
        volume=volume,
        price_change=change,
        txns=txns,
        pair_created_at=created_ms / 1000.0 if created_ms else 0.0,
        url=str(raw.get("url") or ""),
        fetched_at=time.time(),
    )


class DexScreenerClient:
    """Discovery and pricing. All methods degrade to empty results on API errors."""

    def __init__(
        self,
        base_url: str = "https://api.dexscreener.com",
        chain: str = "solana",
        timeout: float = 12.0,
        max_retries: int = 3,
        backoff_seconds: float = 1.0,
        rate_limit_per_minute: int = 120,
        cache_ttl_seconds: float = 5.0,
        http: Optional[HttpClient] = None,
    ) -> None:
        self.chain = chain
        self.cache_ttl = cache_ttl_seconds
        self.http = http or HttpClient(
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
            backoff_seconds=backoff_seconds,
            rate_limit_per_minute=rate_limit_per_minute,
        )
        self._cache: Dict[str, tuple] = {}

    # ---- internals -------------------------------------------------------------
    def _cached(self, key: str) -> Optional[Any]:
        entry = self._cache.get(key)
        if not entry:
            return None
        ts, value = entry
        if time.time() - ts > self.cache_ttl:
            self._cache.pop(key, None)
            return None
        return value

    def _store(self, key: str, value: Any) -> Any:
        self._cache[key] = (time.time(), value)
        return value

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        try:
            return self.http.get(path, params=params)
        except HttpError as exc:
            log.warning("DexScreener request failed (%s): %s", path, exc)
            return None

    def _pairs_for_chain(self, raw_pairs: Optional[Iterable[Dict[str, Any]]]) -> List[PairSnapshot]:
        out: List[PairSnapshot] = []
        for raw in raw_pairs or []:
            snap = parse_pair(raw)
            if snap and snap.chain_id == self.chain and not snap.is_stale:
                out.append(snap)
        return out

    # ---- public API ------------------------------------------------------------
    def search(self, query: str) -> List[PairSnapshot]:
        """Full-text pair search (symbol, name, address or pair address)."""
        key = f"search:{query}"
        cached = self._cached(key)
        if cached is not None:
            return cached
        data = self._get("/latest/dex/search", params={"q": query})
        pairs = self._pairs_for_chain((data or {}).get("pairs"))
        return self._store(key, pairs)

    def pairs_for_token(self, token_address: str) -> List[PairSnapshot]:
        """Every pool that trades `token_address`, best-liquidity first."""
        key = f"token:{token_address}"
        cached = self._cached(key)
        if cached is not None:
            return cached
        data = self._get(f"/token-pairs/v1/{self.chain}/{token_address}")
        if isinstance(data, dict):
            data = data.get("pairs")
        pairs = self._pairs_for_chain(data)
        pairs.sort(key=lambda p: p.liquidity_usd, reverse=True)
        return self._store(key, pairs)

    def pairs_by_address(self, pair_addresses: Iterable[str]) -> List[PairSnapshot]:
        """Batch refresh of up to 30 pair addresses in one request."""
        addresses = [a for a in pair_addresses if a]
        if not addresses:
            return []
        out: List[PairSnapshot] = []
        for i in range(0, len(addresses), 30):
            chunk = addresses[i : i + 30]
            data = self._get(f"/latest/dex/pairs/{self.chain}/{','.join(chunk)}")
            raw = (data or {}).get("pairs") if isinstance(data, dict) else data
            out.extend(self._pairs_for_chain(raw))
        return out

    def _token_addresses(self, path: str, limit: int) -> List[str]:
        """Pull tokenAddress values for our chain out of a feed endpoint."""
        data = self._get(path)
        if not isinstance(data, list):
            return []
        addresses: List[str] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            if str(item.get("chainId") or "") != self.chain:
                continue
            address = item.get("tokenAddress")
            if address and address not in addresses:
                addresses.append(str(address))
            if len(addresses) >= limit:
                break
        return addresses

    def boosted_tokens(self, limit: int = 30) -> List[str]:
        """Tokens from the paid-boost leaderboard - a proxy for what is being
        promoted right now. Returns token addresses, not pairs."""
        top = self._token_addresses("/token-boosts/top/v1", limit)
        latest = self._token_addresses("/token-boosts/latest/v1", limit)
        merged = top + [a for a in latest if a not in top]
        return merged[:limit]

    def token_profiles(self, limit: int = 30) -> List[str]:
        """The newest tokens to publish a DexScreener profile. Noisy, but it is
        where genuinely new names show up before they trend."""
        return self._token_addresses("/token-profiles/latest/v1", limit)

    def best_pair(self, token_address: str) -> Optional[PairSnapshot]:
        """The deepest pool for a token - the one we would actually route through."""
        pairs = self.pairs_for_token(token_address)
        return pairs[0] if pairs else None

    def price_usd(self, token_address: str) -> float:
        pair = self.best_pair(token_address)
        return pair.price_usd if pair else 0.0

    def discover(
        self,
        search_terms: Iterable[str],
        use_boosted_feed: bool = True,
        use_token_profiles: bool = True,
        max_candidates: int = 120,
    ) -> List[PairSnapshot]:
        """Build the candidate universe for one scan cycle.

        Three funnels feed it: full-text search (broad, catches anything with
        volume), the boost leaderboards (what is being promoted), and the newest
        token profiles (early names). Results are de-duplicated by base token,
        keeping the deepest pool, and sorted by 1h volume.
        """
        by_token: Dict[str, PairSnapshot] = {}

        def add(snap: PairSnapshot) -> None:
            existing = by_token.get(snap.base.address)
            if existing is None or snap.liquidity_usd > existing.liquidity_usd:
                by_token[snap.base.address] = snap

        for term in search_terms:
            for snap in self.search(term):
                add(snap)

        feed_addresses: List[str] = []
        if use_boosted_feed:
            feed_addresses.extend(self.boosted_tokens())
        if use_token_profiles:
            feed_addresses.extend(self.token_profiles())

        for address in feed_addresses:
            if address in by_token:
                continue
            pair = self.best_pair(address)
            if pair:
                add(pair)

        candidates = sorted(by_token.values(), key=lambda p: p.vol("h1"), reverse=True)
        return candidates[:max_candidates]
