"""GET /api/health - deployment diagnostics, safe to call from anywhere.

Reports whether the deployment can reach its database and the market APIs.
It deliberately reveals no secrets and no wallet details.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api._common import JsonHandler, load_bot_config  # noqa: E402


class handler(JsonHandler):  # noqa: N801
    cache_control = "no-store"

    def payload(self):
        try:
            config = load_bot_config()
        except RuntimeError as exc:
            return {"ok": False, "database": "not configured", "hint": str(exc)}

        from memebot.doctor import run_checks

        report = run_checks(config, deep=False)
        return {
            "ok": report.healthy,
            "mode": "live",
            "read_only": True,
            "checks": [
                {"name": c.name, "status": c.status, "detail": c.detail,
                 "elapsed_ms": round(c.elapsed_ms, 1)}
                for c in report.checks
            ],
        }
