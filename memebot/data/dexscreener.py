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
  GET /tokens/v1/{chain}/{addresses}     (up to 30 tokens per request)
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Iterable, List, Optional

from ..http import HttpClient, HttpError
from ..models import PairSnapshot, Token
from .geckoterminal import GeckoTerminalClient, merge_addresses

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
        gecko: Optional[GeckoTerminalClient] = None,
    ) -> None:
        self.chain = chain
        self.cache_ttl = cache_ttl_seconds
        self._gecko = gecko
        # What each feed contributed on the last discover(), for diagnostics.
        self.last_sources: Dict[str, int] = {}
        self.http = http or HttpClient(
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
            backoff_seconds=backoff_seconds,
            rate_limit_per_minute=rate_limit_per_minute,
        )
        self._cache: Dict[str, tuple] = {}

    @property
    def gecko(self) -> Optional[GeckoTerminalClient]:
        """The pool feeds, if this client was given them.

        Deliberately not built on demand: a client handed a transport - a test,
        a probe - must not quietly reach a second API behind it. Production
        builds it in memebot.data.build_dexscreener.
        """
        return self._gecko

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

    def pairs_for_tokens(self, addresses: Iterable[str]) -> List[PairSnapshot]:
        """Pairs for many tokens at once - 30 per request instead of one each.

        This is what makes a wide scan affordable: pulling 300 tokens one at a
        time would blow the rate limit, and in ten batched calls it does not.
        """
        wanted = [a for a in addresses if a]
        out: List[PairSnapshot] = []
        for i in range(0, len(wanted), 30):
            chunk = wanted[i : i + 30]
            data = self._get(f"/tokens/v1/{self.chain}/{','.join(chunk)}")
            if isinstance(data, dict):
                data = data.get("pairs")
            out.extend(self._pairs_for_chain(data))
        return out

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
        max_candidates: int = 400,
        feed_limit: int = 120,
        use_trending_pools: bool = False,
        use_top_pools: bool = False,
        use_new_pools: bool = False,
        max_per_symbol: int = 0,
    ) -> List[PairSnapshot]:
        """Build the candidate universe for one scan cycle.

        Two kinds of feed, and the difference matters:

        *By name* - DexScreener full-text search. Broad and cheap, but it finds
        text, not trades: ask for a fashionable word and you get every coin
        called that, which is how one family of copycats ends up filling a
        whole scan.

        *By activity* - the boost leaderboards (what is being promoted) and
        GeckoTerminal's trending / busiest / newest pools (what is actually
        being traded). These do not care what anything is called.

        The pool feeds default to off here so this client never reaches a
        second API unless it was asked to; the config turns them on.

        Feed tokens are resolved through DexScreener in batches of 30, so every
        candidate is priced by one source. Results are de-duplicated by base
        token keeping the deepest pool, thinned so no ticker family can crowd
        out the rest, and returned busiest-first by 1h volume.
        """
        by_token: Dict[str, PairSnapshot] = {}
        counts: Dict[str, int] = {}

        def add(snap: PairSnapshot, source: str) -> None:
            existing = by_token.get(snap.base.address)
            if existing is not None and existing.liquidity_usd >= snap.liquidity_usd:
                return
            snap.source = source if existing is None else existing.source
            by_token[snap.base.address] = snap

        for term in search_terms:
            for snap in self.search(term):
                add(snap, "search")

        # Each feed is kept separate so one long list cannot crowd out the
        # others when they are interleaved.
        feeds: Dict[str, List[str]] = {}
        if use_boosted_feed:
            feeds["boosts"] = self.boosted_tokens(limit=feed_limit)
        if use_token_profiles:
            feeds["profiles"] = self.token_profiles(limit=feed_limit)
        gecko = self.gecko
        if gecko is None and (use_trending_pools or use_top_pools or use_new_pools):
            log.debug("Pool feeds asked for but no GeckoTerminal client was given.")
        elif gecko is not None:
            if use_trending_pools:
                feeds["trending"] = gecko.trending_tokens(limit=feed_limit)
            if use_top_pools:
                feeds["busiest"] = gecko.top_volume_tokens(limit=feed_limit)
            if use_new_pools:
                feeds["new"] = gecko.new_pool_tokens(limit=feed_limit)

        origin: Dict[str, str] = {}
        for name, addresses in feeds.items():
            for address in addresses:
                origin.setdefault(address, name)

        unseen = [a for a in merge_addresses(*feeds.values()) if a not in by_token]
        for snap in self.pairs_for_tokens(unseen):
            add(snap, origin.get(snap.base.address, "feed"))

        candidates = sorted(by_token.values(), key=lambda p: p.vol("h1"), reverse=True)
        if max_per_symbol > 0:
            candidates = _thin_by_symbol(candidates, max_per_symbol)
        candidates = candidates[:max_candidates]

        for snap in candidates:
            counts[snap.source] = counts.get(snap.source, 0) + 1
        self.last_sources = counts
        return candidates


def symbol_family(pair: PairSnapshot) -> str:
    """A coarse key for "the same coin, again".

    Copycats do not pick unrelated names: STONK, STONKS, STONK2 and $STONK all
    turn up at once when one of them catches. The first few letters of the
    ticker group them without needing a list of what is currently fashionable.
    """
    root = "".join(ch for ch in pair.base.symbol.lower() if ch.isalnum())[:5]
    return root or pair.base.address[:8].lower()


def _thin_by_symbol(pairs: List[PairSnapshot], limit: int) -> List[PairSnapshot]:
    """Keep at most `limit` per ticker family, busiest first."""
    kept: List[PairSnapshot] = []
    seen: Dict[str, int] = {}
    for pair in pairs:
        family = symbol_family(pair)
        if seen.get(family, 0) >= limit:
            continue
        seen[family] = seen.get(family, 0) + 1
        kept.append(pair)
    return kept
