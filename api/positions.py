"""GET /api/positions - the open book."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api._common import JsonHandler, load_bot_config, open_portfolio  # noqa: E402


class handler(JsonHandler):  # noqa: N801
    def payload(self):
        config = load_bot_config()
        storage, portfolio = open_portfolio(config)
        try:
            positions = [
                {
                    "symbol": p.token.symbol or p.token.address[:6],
                    "address": p.token.address,
                    "quantity": p.quantity,
                    "avg_price": p.avg_price,
                    "last_price": p.last_price,
                    "high_price": p.high_price,
                    "cost_usd": p.cost_usd,
                    "market_value_usd": p.market_value,
                    "unrealized_pnl_usd": p.unrealized_pnl_usd,
                    "unrealized_pnl_pct": p.unrealized_pnl_pct * 100.0,
                    "drawdown_from_high_pct": p.drawdown_from_high * 100.0,
                    "age_minutes": p.age_minutes,
                    "opened_at": p.opened_at,
                    "pair_url": f"https://dexscreener.com/{config.data.chain}/{p.pair_address}"
                    if p.pair_address else "",
                }
                for p in sorted(portfolio.open_positions, key=lambda x: x.opened_at, reverse=True)
            ]
            return {"positions": positions, "count": len(positions)}
        finally:
            storage.close()
