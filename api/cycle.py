"""POST|GET /api/cycle - run one trading cycle.

This is the endpoint a scheduler calls. It is the whole bot: refresh open
positions, apply exits, scan the live market, open what qualifies.

It is protected: without a matching CRON_SECRET the request is rejected, so a
stranger who finds the URL cannot make your bot trade.

One cycle is a single pass, not a loop - the serverless execution limit
(10-60s on most plans) is nowhere near enough to run the bot continuously.
See the deployment notes in the README for what that means in practice.
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api._common import JsonHandler, is_authorized, load_bot_config  # noqa: E402
from memebot.engine import TradingEngine  # noqa: E402
from memebot.logging_utils import setup_logging  # noqa: E402


class handler(JsonHandler):  # noqa: N801
    cache_control = "no-store"

    def payload(self):
        if not is_authorized(self):
            raise PermissionError(
                "unauthorized - this endpoint requires the CRON_SECRET bearer token"
            )

        config = load_bot_config()
        config.log_file = None
        setup_logging(config.log_level)

        engine = TradingEngine(config)
        try:
            blocked = engine.preflight()
            if blocked:
                raise RuntimeError(blocked)

            report = engine.run_cycle()

            engine.storage.set_state("last_cycle_at", time.time())
            engine.storage.set_state(
                "cycles_run", int(engine.storage.get_state("cycles_run", 0) or 0) + 1
            )

            return {
                "ok": True,
                "mode": config.mode,
                "scanned": report.scanned,
                "passed_filters": report.passed_filters,
                "signals": report.signals,
                "opened": [
                    {"symbol": f.token.symbol, "usd": f.usd_amount, "price": f.price}
                    for f in report.opened
                ],
                "closed": [
                    {
                        "symbol": f.token.symbol,
                        "usd": f.usd_amount,
                        "price": f.price,
                        "reason": f.order.reason,
                    }
                    for f in report.closed
                ],
                "skipped": report.skipped,
                "errors": report.errors,
                "halted_reason": report.halted_reason,
                "equity_usd": engine.portfolio.equity,
                "cash_usd": engine.portfolio.cash,
                "open_positions": len(engine.portfolio.positions),
            }
        finally:
            engine.storage.close()
