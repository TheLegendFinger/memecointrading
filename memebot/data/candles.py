"""OHLC candles for the live view.

DexScreener has no public OHLC endpoint, so real candles come from
GeckoTerminal, which is free and needs no key:

  GET https://api.geckoterminal.com/api/v2/networks/{network}/pools/{pool}/ohlcv/{timeframe}

When that is unreachable - or the pool is too new to have history - candles are
built from the price samples the bot recorded itself. That fallback is coarser,
but it means the chart is never empty while the bot is running.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

from ..http import HttpClient, HttpError

log = logging.getLogger(__name__)

GECKO_BASE_URL = "https://api.geckoterminal.com/api/v2"

# GeckoTerminal splits timeframe and aggregate: "5m" is minute/5.
TIMEFRAMES: Dict[str, tuple] = {
    "1m": ("minute", 1, 60),
    "5m": ("minute", 5, 300),
    "15m": ("minute", 15, 900),
    "1h": ("hour", 1, 3600),
    "4h": ("hour", 4, 14400),
    "1d": ("day", 1, 86400),
}


@dataclass
class Candle:
    ts: float          # bucket start, epoch seconds
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    def as_dict(self) -> Dict[str, float]:
        return {
            "ts": self.ts,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }


def bucket_seconds(timeframe: str) -> int:
    return TIMEFRAMES.get(timeframe, TIMEFRAMES["5m"])[2]


def median_spacing(samples: List[Dict[str, Any]]) -> float:
    """Typical gap between observations, which is the bot's polling interval."""
    stamps = sorted(float(s["ts"]) for s in samples if "ts" in s)
    gaps = [b - a for a, b in zip(stamps, stamps[1:]) if b > a]
    if not gaps:
        return 0.0
    gaps.sort()
    middle = len(gaps) // 2
    return gaps[middle] if len(gaps) % 2 else (gaps[middle - 1] + gaps[middle]) / 2.0


def fit_bucket(
    samples: List[Dict[str, Any]],
    timeframe: str,
    min_per_bucket: int = 3,
    target_candles: int = 40,
) -> int:
    """Bucket size that actually produces a readable chart.

    Two failure modes, in both directions:

    * The bot polls slower than the requested timeframe, so each bucket holds
      one price and every candle is a flat dash. Widen.
    * The bot has only been running a few minutes, so the whole history fits in
      one five-minute bucket and the chart is a single candle. Narrow.

    So the bucket is the requested size, pulled towards whatever spreads the
    available history across roughly `target_candles`, with a floor of enough
    observations per bucket to give each candle a body. Callers report the
    result (see label_for_seconds) rather than claiming the requested size.
    """
    requested = bucket_seconds(timeframe)
    spacing = median_spacing(samples)
    if spacing <= 0 or len(samples) < 2:
        return requested

    stamps = [float(s["ts"]) for s in samples if "ts" in s]
    span = max(stamps) - min(stamps) if stamps else 0.0

    floor = spacing * min_per_bucket          # enough samples for a real body
    by_span = span / target_candles if span > 0 else requested
    return int(max(floor, min(requested, by_span)) or requested)


def label_for_seconds(seconds: float) -> str:
    """The closest human label for a bucket size, for honest axis captions."""
    best = min(TIMEFRAMES.items(), key=lambda kv: abs(kv[1][2] - seconds))
    if abs(best[1][2] - seconds) <= best[1][2] * 0.25:
        return best[0]
    minutes = max(1, int(round(seconds / 60)))
    return f"{minutes}m" if minutes < 90 else f"{max(1, int(round(minutes / 60)))}h"


def candles_from_samples(
    samples: Iterable[Dict[str, Any]],
    timeframe: str = "5m",
    limit: int = 120,
    seconds: Optional[int] = None,
) -> List[Candle]:
    """Aggregate observed prices into OHLC buckets.

    Each bucket's open/close are the first and last observations in it, and
    high/low the extremes. With one sample in a bucket all four are equal - a
    flat candle, which is honest about how much was actually seen; pass
    `seconds` (see fit_bucket) to widen the buckets until they hold enough.
    """
    samples = list(samples)
    seconds = seconds or bucket_seconds(timeframe)
    buckets: Dict[int, List[float]] = {}
    for sample in samples:
        try:
            ts = float(sample["ts"])
            price = float(sample["price"])
        except (KeyError, TypeError, ValueError):
            continue
        if price <= 0:
            continue
        buckets.setdefault(int(ts // seconds) * seconds, []).append(price)

    out = [
        Candle(
            ts=float(start),
            open=prices[0],
            high=max(prices),
            low=min(prices),
            close=prices[-1],
        )
        for start, prices in sorted(buckets.items())
    ]
    return out[-limit:]


class GeckoTerminalClient:
    """Keyless OHLCV. Degrades to an empty list rather than raising."""

    def __init__(
        self,
        base_url: str = GECKO_BASE_URL,
        network: str = "solana",
        timeout: float = 12.0,
        max_retries: int = 2,
        rate_limit_per_minute: int = 25,  # the free tier allows ~30/min
        cache_ttl_seconds: float = 20.0,
        http: Optional[HttpClient] = None,
    ) -> None:
        self.network = network
        self.cache_ttl = cache_ttl_seconds
        self.http = http or HttpClient(
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
            rate_limit_per_minute=rate_limit_per_minute,
        )
        self._cache: Dict[str, tuple] = {}

    def ohlcv(self, pool_address: str, timeframe: str = "5m", limit: int = 120) -> List[Candle]:
        if not pool_address:
            return []
        key = f"{pool_address}:{timeframe}:{limit}"
        cached = self._cache.get(key)
        if cached and time.time() - cached[0] < self.cache_ttl:
            return cached[1]

        unit, aggregate, _seconds = TIMEFRAMES.get(timeframe, TIMEFRAMES["5m"])
        try:
            payload = self.http.get(
                f"/networks/{self.network}/pools/{pool_address}/ohlcv/{unit}",
                params={"aggregate": aggregate, "limit": min(int(limit), 1000)},
            )
        except HttpError as exc:
            log.debug("GeckoTerminal OHLCV failed for %s: %s", pool_address, exc)
            return []

        rows = (((payload or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
        candles: List[Candle] = []
        for row in rows:
            # [timestamp, open, high, low, close, volume]
            if not isinstance(row, (list, tuple)) or len(row) < 5:
                continue
            try:
                candle = Candle(
                    ts=float(row[0]),
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[5]) if len(row) > 5 and row[5] is not None else 0.0,
                )
            except (TypeError, ValueError):
                continue
            if candle.close > 0:
                candles.append(candle)

        candles.sort(key=lambda c: c.ts)   # the API returns newest first
        candles = candles[-limit:]
        self._cache[key] = (time.time(), candles)
        return candles
