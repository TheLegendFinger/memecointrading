"""GET /api/candles?address=<mint>&timeframe=5m - OHLC plus this bot's own trades.

Real candles come from GeckoTerminal when the pool is known and reachable;
otherwise they are built from the price samples the bot recorded itself. The
response says which, so the UI can be honest about what is on screen.

The trade markers are the point of the whole thing: they are this bot's actual
fills on that token, so entries and exits sit on the chart where they happened.
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api._common import JsonHandler, int_param, load_bot_config, open_portfolio, query_params  # noqa: E402
from memebot.data.candles import (  # noqa: E402
    GeckoTerminalClient, TIMEFRAMES, bucket_seconds, candles_from_samples, fit_bucket,
    label_for_seconds,
)


class handler(JsonHandler):  # noqa: N801
    cache_control = "public, max-age=10"

    def payload(self):
        params = query_params(self.path)
        address = params.get("address", "").strip()
        timeframe = params.get("timeframe", "5m")
        if timeframe not in TIMEFRAMES:
            timeframe = "5m"
        limit = int_param(params, "limit", 120, 500)

        config = load_bot_config()
        storage, portfolio = open_portfolio(config)
        try:
            # Default to whatever is open, else the most recently traded token.
            if not address:
                positions = portfolio.open_positions
                if positions:
                    address = max(positions, key=lambda p: p.opened_at).token.address
                else:
                    recent = storage.list_trades(limit=1)
                    address = recent[0]["token_address"] if recent else ""
            if not address:
                return {"address": "", "candles": [], "markers": [], "source": "none",
                        "timeframe": timeframe, "tokens": []}

            position = portfolio.position(address)
            symbol = position.token.symbol if position else ""
            pair_address = position.pair_address if position else ""

            trades = [t for t in storage.list_trades(limit=400)
                      if t["token_address"] == address]
            if not symbol and trades:
                symbol = trades[0]["symbol"] or ""

            # Real candles first; our own samples as the fallback.
            candles, source, effective = [], "samples", timeframe
            fitted = bucket_seconds(timeframe)
            if pair_address:
                # Fail fast: this is a chart, and the bot's own samples are a
                # perfectly good fallback. Retrying here would stall the live
                # view behind a third party being slow.
                client = GeckoTerminalClient(
                    network=config.data.chain,
                    timeout=6.0,
                    max_retries=0,
                )
                fetched = client.ohlcv(pair_address, timeframe, limit)
                if fetched:
                    candles, source = [c.as_dict() for c in fetched], "geckoterminal"

            effective = timeframe
            fitted = bucket_seconds(timeframe)
            if not candles:
                seconds = bucket_seconds(timeframe)
                since = time.time() - seconds * limit * 6
                samples = storage.price_samples(address, since=since)
                # Widen the buckets when the bot polls slower than the requested
                # timeframe, so candles have bodies instead of being flat dashes.
                fitted = fit_bucket(samples, timeframe)
                effective = label_for_seconds(fitted)
                candles = [c.as_dict()
                           for c in candles_from_samples(samples, timeframe, limit, seconds=fitted)]

            markers = [
                {
                    "ts": t["ts"],
                    "side": t["side"],
                    "price": t["price"],
                    "usd": t["usd_amount"],
                    "amount": t["token_amount"],
                    "realized_pnl": t["realized_pnl"],
                    "reason": t["reason"] or "",
                }
                for t in trades
            ]

            # Everything chartable: open positions first, then recently traded.
            tokens, seen = [], set()
            for pos in sorted(portfolio.open_positions, key=lambda p: p.opened_at, reverse=True):
                tokens.append({"address": pos.token.address,
                               "symbol": pos.token.symbol or pos.token.address[:6],
                               "open": True})
                seen.add(pos.token.address)
            for trade in storage.list_trades(limit=60):
                if trade["token_address"] in seen:
                    continue
                seen.add(trade["token_address"])
                tokens.append({"address": trade["token_address"],
                               "symbol": trade["symbol"] or trade["token_address"][:6],
                               "open": False})

            return {
                "address": address,
                "symbol": symbol or address[:6],
                "timeframe": timeframe,
                "effective_timeframe": effective,
                "bucket_seconds": fitted,
                "requested_seconds": bucket_seconds(timeframe),
                "source": source,
                "candles": candles,
                "markers": markers,
                "tokens": tokens[:12],
                "position": {
                    "quantity": position.quantity,
                    "avg_price": position.avg_price,
                    "last_price": position.last_price,
                    "unrealized_pnl_pct": position.unrealized_pnl_pct * 100.0,
                } if position else None,
            }
        finally:
            storage.close()
