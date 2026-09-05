"""GET /api/scan?limit=20 - what the bot sees in the live market right now.

Read-only: it scores candidates and returns them without trading, so it is
safe to leave unauthenticated. It does hit the DexScreener API on every call,
so the response is cached briefly.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api._common import JsonHandler, int_param, load_bot_config, query_params  # noqa: E402
from memebot.data.dexscreener import DexScreenerClient  # noqa: E402
from memebot.strategy import CandidateFilter, build_strategy  # noqa: E402


class handler(JsonHandler):  # noqa: N801
    cache_control = "public, max-age=20, stale-while-revalidate=60"

    def payload(self):
        limit = int_param(query_params(self.path), "limit", 20, 100)
        config = load_bot_config()

        data = DexScreenerClient(
            base_url=config.data.dexscreener_base_url,
            chain=config.data.chain,
            timeout=config.data.request_timeout,
            max_retries=1,
            rate_limit_per_minute=config.data.rate_limit_per_minute,
        )
        candidates = data.discover(
            config.data.search_terms,
            use_boosted_feed=config.data.use_boosted_feed,
            use_token_profiles=config.data.use_token_profiles,
            max_candidates=config.data.max_candidates,
            feed_limit=config.data.feed_limit,
        )
        result = CandidateFilter(config.filters).apply(candidates)
        strategy = build_strategy(config.strategy.name, config.strategy)

        scored = sorted(
            ((strategy.score(pair), pair) for pair in result.passed),
            key=lambda item: item[0],
            reverse=True,
        )[:limit]

        return {
            "scanned": len(candidates),
            "passed_filters": len(result.passed),
            "min_score": config.strategy.min_score,
            "rejections": result.rejections,
            "candidates": [
                {
                    "symbol": pair.base.symbol,
                    "address": pair.base.address,
                    "score": round(score, 4),
                    "tradable": score >= config.strategy.min_score,
                    "price_usd": pair.price_usd,
                    "change_m5": pair.change("m5"),
                    "change_h1": pair.change("h1"),
                    "change_h24": pair.change("h24"),
                    "liquidity_usd": pair.liquidity_usd,
                    "volume_h1_usd": pair.vol("h1"),
                    "buy_ratio_h1": pair.buy_ratio("h1"),
                    "age_hours": round(pair.age_minutes / 60.0, 1)
                    if pair.age_minutes < 1e9 else None,
                    "url": pair.url,
                }
                for score, pair in scored
            ],
        }
