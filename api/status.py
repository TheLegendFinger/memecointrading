"""GET /api/status - portfolio summary for the dashboard."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api._common import JsonHandler, load_bot_config, open_portfolio  # noqa: E402


class handler(JsonHandler):  # noqa: N801 - Vercel requires this name
    def payload(self):
        config = load_bot_config()
        storage, portfolio = open_portfolio(config)
        try:
            stats = portfolio.stats()
            last_cycle = storage.get_state("last_cycle_at")
            curve = storage.equity_curve(limit=2)
            return {
                "mode": "live",
                "strategy": config.strategy.name,
                "equity_usd": stats["equity_usd"],
                "cash_usd": stats["cash_usd"],
                "positions_value_usd": stats["positions_value_usd"],
                "starting_cash_usd": stats["starting_cash_usd"],
                "total_return_pct": stats["total_return_pct"],
                "realized_pnl_usd": stats["realized_pnl_usd"],
                "unrealized_pnl_usd": stats["unrealized_pnl_usd"],
                "total_fees_usd": stats["total_fees_usd"],
                "open_positions": stats["open_positions"],
                "closed_trades": stats["closed_trades"],
                "wins": stats["wins"],
                "losses": stats["losses"],
                "win_rate": stats["win_rate"],
                "peak_equity_usd": storage.peak_equity(),
                "last_cycle_at": last_cycle,
                "cycles_run": storage.get_state("cycles_run", 0),
                "samples": len(curve),
            }
        finally:
            storage.close()
