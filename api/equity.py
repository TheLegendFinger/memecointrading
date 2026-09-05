"""GET /api/equity?limit=300 - the equity curve, oldest first for charting."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api._common import JsonHandler, int_param, load_bot_config, open_portfolio, query_params  # noqa: E402


class handler(JsonHandler):  # noqa: N801
    def payload(self):
        limit = int_param(query_params(self.path), "limit", 300, 2000)
        config = load_bot_config()
        storage, portfolio = open_portfolio(config)
        try:
            rows = storage.equity_curve(limit=limit)
            points = [
                {"ts": r["ts"], "equity": r["equity"], "cash": r["cash"], "positions": r["positions"]}
                for r in reversed(rows)
            ]
            return {
                "points": points,
                "count": len(points),
                "starting_cash_usd": portfolio.starting_cash,
            }
        finally:
            storage.close()
