"""Tests for the serverless handlers.

They call `render()` - the same entry point the deployed function and the dev
server use - against a temporary SQLite state file, so routing, serialisation,
authorisation and error mapping are all covered without a network or a cloud
account.
"""

import json
import random

import pytest

from api._common import is_serverless, render
from memebot.config import BotConfig
from memebot.engine import TradingEngine
from memebot.execution.paper import PaperExecutor
from memebot.storage import Storage
from tests.conftest import FakeDexScreener, make_pair


class Headers(dict):
    """Stand-in for the handler's headers mapping."""

    def get(self, key, default=""):
        return dict.get(self, key, default)


@pytest.fixture
def state_db(tmp_path, monkeypatch):
    """A state file with one cycle already traded, wired up via the env."""
    db = str(tmp_path / "api.sqlite3")
    monkeypatch.setenv("MEMEBOT_STATE_DB", db)
    monkeypatch.delenv("VERCEL", raising=False)
    monkeypatch.delenv("POSTGRES_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    config = BotConfig()
    config.state_db = db
    config.execution.paper_failure_rate = 0.0
    hot = make_pair("BEST", chg_m5=12.0, chg_h1=55.0, vol_h1=400_000, vol_h24=960_000,
                    buys_m5=90, sells_m5=10, liquidity=700_000)
    market = FakeDexScreener([hot])
    engine = TradingEngine(config, storage=Storage(db), data=market,
                           executor=PaperExecutor(config, data=market, rng=random.Random(4)))
    engine.run_cycle()
    engine.storage.close()
    return db


@pytest.fixture
def offline(monkeypatch):
    """Keep the cycle endpoint away from the real DexScreener API.

    The engine it builds is the real one; only market access is stubbed, so the
    cycle still exercises auth, state, accounting and the response shape.
    """
    from memebot.data.dexscreener import DexScreenerClient
    from memebot.execution.paper import PaperExecutor as Paper

    monkeypatch.setattr(DexScreenerClient, "discover", lambda self, *a, **k: [])
    monkeypatch.setattr(DexScreenerClient, "best_pair", lambda self, address: None)
    monkeypatch.setattr(DexScreenerClient, "price_usd", lambda self, address: 0.0)
    monkeypatch.setattr(Paper, "price_for", lambda self, address: 0.0)
    return True


def call(module_name, path="/", headers=None):
    module = __import__(f"api.{module_name}", fromlist=["handler"])
    return render(module.handler, path, Headers(headers or {}))


# ---- read endpoints ------------------------------------------------------------
def test_status_reports_the_portfolio(state_db):
    status, body = call("status")
    assert status == 200
    assert body["mode"] == "paper"
    assert body["equity_usd"] > 0
    assert body["open_positions"] == 1
    assert "took_ms" in body and "generated_at" in body


def test_positions_endpoint_lists_the_open_book(state_db):
    status, body = call("positions")
    assert status == 200
    assert body["count"] == 1
    position = body["positions"][0]
    assert position["symbol"] == "BEST"
    assert position["market_value_usd"] > 0
    assert position["pair_url"].startswith("https://dexscreener.com/solana/")


def test_trades_endpoint_honours_the_limit(state_db):
    status, body = call("trades", "/api/trades?limit=1")
    assert status == 200
    assert len(body["trades"]) <= 1
    assert body["trades"][0]["side"] in ("buy", "sell")


def test_trades_limit_is_clamped_not_trusted(state_db):
    _status, body = call("trades", "/api/trades?limit=999999")
    assert body["count"] <= 500
    _status, body = call("trades", "/api/trades?limit=notanumber")
    assert body["count"] >= 0  # falls back to the default instead of erroring


def test_equity_endpoint_returns_points_oldest_first(state_db):
    status, body = call("equity")
    assert status == 200
    assert body["count"] >= 1
    timestamps = [p["ts"] for p in body["points"]]
    assert timestamps == sorted(timestamps)


def test_responses_are_json_serialisable(state_db):
    for name in ("status", "positions", "trades", "equity"):
        _status, body = call(name)
        json.dumps(body)  # would raise on a stray non-serialisable value


# ---- the cycle endpoint is protected -------------------------------------------
def test_cycle_rejects_an_unauthenticated_request(state_db, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "s3cret")
    status, body = call("cycle")
    assert status == 401
    assert "unauthorized" in body["error"]


def test_cycle_rejects_a_wrong_token(state_db, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "s3cret")
    status, _body = call("cycle", "/api/cycle", {"Authorization": "Bearer wrong"})
    assert status == 401


def test_cycle_rejects_everything_when_no_secret_is_configured(state_db, monkeypatch):
    """An unset CRON_SECRET must fail closed, never open."""
    monkeypatch.delenv("CRON_SECRET", raising=False)
    status, _body = call("cycle", "/api/cycle?key=")
    assert status == 401


def test_cycle_accepts_the_bearer_token(state_db, offline, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "s3cret")
    status, body = call("cycle", "/api/cycle", {"Authorization": "Bearer s3cret"})
    assert status == 200
    assert body["ok"] is True
    assert "equity_usd" in body


def test_cycle_accepts_the_query_key_for_external_schedulers(state_db, offline, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "s3cret")
    status, body = call("cycle", "/api/cycle?key=s3cret")
    assert status == 200
    assert body["ok"] is True


def test_cycle_records_its_own_heartbeat(state_db, offline, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "s3cret")
    call("cycle", "/api/cycle?key=s3cret")
    store = Storage(state_db)
    try:
        assert store.get_state("last_cycle_at") > 0
        assert store.get_state("cycles_run") >= 1
    finally:
        store.close()


# ---- failure modes -------------------------------------------------------------
def test_serverless_without_postgres_is_a_clear_503(tmp_path, monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("MEMEBOT_STATE_DB", str(tmp_path / "ephemeral.sqlite3"))
    status, body = call("status")
    assert status == 503
    assert "POSTGRES_URL" in body["error"]
    assert "durable filesystem" in body["error"]


def test_serverless_detection(monkeypatch):
    monkeypatch.delenv("VERCEL", raising=False)
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    assert is_serverless() is False
    monkeypatch.setenv("VERCEL", "1")
    assert is_serverless() is True


def test_unexpected_errors_become_500_not_a_stack_trace(state_db, monkeypatch):
    import api.status

    monkeypatch.setattr(api.status.handler, "payload",
                        lambda self: (_ for _ in ()).throw(ValueError("boom")))
    status, body = call("status")
    assert status == 500
    assert body["error"] == "ValueError: boom"
    assert "Traceback" not in json.dumps(body)


def test_health_endpoint_reports_without_a_database(tmp_path, monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("MEMEBOT_STATE_DB", str(tmp_path / "nope.sqlite3"))
    status, body = call("health")
    assert status == 200
    assert body["ok"] is False
    assert body["database"] == "not configured"
