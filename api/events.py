"""GET /api/events?since_id=123 - the bot's action feed.

Without `since_id` it returns the most recent entries newest-first (a fresh page
load). With one, it returns everything after that id oldest-first, so the live
view appends instead of redrawing.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api._common import JsonHandler, int_param, load_bot_config, open_portfolio, query_params  # noqa: E402


class handler(JsonHandler):  # noqa: N801
    cache_control = "no-store"

    def payload(self):
        params = query_params(self.path)
        limit = int_param(params, "limit", 150, 500)
        since_id = params.get("since_id")

        config = load_bot_config()
        storage, _portfolio = open_portfolio(config)
        try:
            if since_id and since_id.isdigit():
                rows = storage.list_events(limit=limit, since_id=int(since_id))
                incremental = True
            else:
                rows = list(reversed(storage.list_events(limit=limit)))
                incremental = False

            events = [
                {
                    "id": row["id"],
                    "ts": row["ts"],
                    "kind": row["kind"],
                    "level": row["level"],
                    "symbol": row["symbol"] or "",
                    "address": row["address"] or "",
                    "message": row["message"],
                    "detail": row["detail"] or "",
                }
                for row in rows
            ]
            return {
                "events": events,
                "count": len(events),
                "incremental": incremental,
                "last_id": events[-1]["id"] if events else (int(since_id) if since_id and since_id.isdigit() else 0),
            }
        finally:
            storage.close()
