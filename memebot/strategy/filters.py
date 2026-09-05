"""Hard safety gates applied before a pair is ever scored.

These are not alpha - they are the "would I be embarrassed to have bought this"
checks: enough liquidity to exit, real trading activity, a sane
liquidity-to-valuation ratio, and an age that rules out both the first chaotic
minutes and long-dead tokens.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

from ..config import FilterConfig
from ..models import PairSnapshot

log = logging.getLogger(__name__)


@dataclass
class FilterResult:
    passed: List[PairSnapshot] = field(default_factory=list)
    rejections: Dict[str, int] = field(default_factory=dict)
    detail: Dict[str, str] = field(default_factory=dict)  # token address -> reason

    def reject(self, pair: PairSnapshot, reason: str) -> None:
        self.rejections[reason] = self.rejections.get(reason, 0) + 1
        self.detail[pair.base.address] = reason

    def summary(self) -> str:
        if not self.rejections:
            return "no rejections"
        top = sorted(self.rejections.items(), key=lambda kv: kv[1], reverse=True)
        return ", ".join(f"{reason}={count}" for reason, count in top[:6])


class CandidateFilter:
    def __init__(self, config: FilterConfig) -> None:
        self.cfg = config

    def check(self, pair: PairSnapshot) -> Optional[str]:
        """Return a rejection reason, or None if the pair is tradable."""
        cfg = self.cfg

        if pair.is_stale:
            return "no price"
        if pair.base.address in set(cfg.blacklist_mints):
            return "blacklisted mint"
        if pair.base.symbol.upper() in {s.upper() for s in cfg.blacklist_symbols}:
            return "blacklisted symbol"
        if cfg.allowed_quote_mints and pair.quote.address not in set(cfg.allowed_quote_mints):
            return "unsupported quote token"
        if cfg.allowed_dex_ids and pair.dex_id not in set(cfg.allowed_dex_ids):
            return "unsupported dex"

        if pair.liquidity_usd < cfg.min_liquidity_usd:
            return "liquidity too low"
        if cfg.max_liquidity_usd and pair.liquidity_usd > cfg.max_liquidity_usd:
            return "liquidity too high"

        if pair.vol("h1") < cfg.min_volume_h1_usd:
            return "1h volume too low"
        if pair.vol("h24") < cfg.min_volume_h24_usd:
            return "24h volume too low"

        age = pair.age_minutes
        if age < cfg.min_age_minutes:
            return "pair too new"
        if cfg.max_age_minutes and age > cfg.max_age_minutes:
            return "pair too old"

        if cfg.max_fdv_usd and pair.fdv and pair.fdv > cfg.max_fdv_usd:
            return "fdv too high"
        if cfg.min_market_cap_usd and pair.market_cap and pair.market_cap < cfg.min_market_cap_usd:
            return "market cap too low"

        if pair.trades("h1") < cfg.min_trades_h1:
            return "too few trades"
        if pair.buy_ratio("h1") < cfg.min_buy_ratio_h1:
            return "sell pressure"

        # Liquidity vs valuation: a $50m FDV sitting on $30k of liquidity is a
        # pump waiting to be dumped on whoever buys next.
        if cfg.min_liquidity_to_fdv > 0 and pair.fdv > 0:
            if pair.liquidity_usd / pair.fdv < cfg.min_liquidity_to_fdv:
                return "liquidity/fdv too thin"

        if cfg.max_price_change_h1_pct and pair.change("h1") > cfg.max_price_change_h1_pct:
            return "already parabolic (1h)"
        if cfg.max_price_change_h24_pct and pair.change("h24") > cfg.max_price_change_h24_pct:
            return "already parabolic (24h)"

        return None

    def apply(self, pairs: Iterable[PairSnapshot]) -> FilterResult:
        result = FilterResult()
        for pair in pairs:
            reason = self.check(pair)
            if reason:
                result.reject(pair, reason)
            else:
                result.passed.append(pair)
        return result
