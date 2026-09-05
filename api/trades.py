"""GET /api/trades?limit=50 - recent fills."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api._common import JsonHandler, int_param, load_bot_config, open_portfolio, query_params  # noqa: E402


class handler(JsonHandler):  # noqa: N801
    def payload(self):
        limit = int_param(query_params(self.path), "limit", 50, 500)
        config = load_bot_config()
        storage, _portfolio = open_portfolio(config)
        try:
            trades = [
                {
                    "ts": t["ts"],
                    "side": t["side"],
                    "symbol": t["symbol"] or (t["token_address"] or "")[:6],
                    "address": t["token_address"],
                    "price": t["price"],
                    "token_amount": t["token_amount"],
                    "usd_amount": t["usd_amount"],
                    "fee_usd": t["fee_usd"],
                    "slippage_bps": t["slippage_bps"],
                    "realized_pnl": t["realized_pnl"],
                    "reason": t["reason"],
                    "tx_signature": t["tx_signature"],
                    "mode": t["mode"],
                }
                for t in storage.list_trades(limit=limit)
            ]
            return {"trades": trades, "count": len(trades)}
        finally:
            storage.close()
