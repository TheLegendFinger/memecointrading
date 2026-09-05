"""Tests for the serverless handlers.

They call `render()` - the same entry point the deployed function and the dev
server use - against a temporary SQLite state file, so routing, serialisation,
authorisation and error mapping are all covered without a network or a cloud
account.
"""

import json
import random
import time

import pytest

from api._common import is_serverless, render
from memebot.config import BotConfig
from memebot.engine import TradingEngine
from tests.fakes import SimulatedExecutor
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
    hot = make_pair("BEST", chg_m5=12.0, chg_h1=55.0, vol_h1=400_000, vol_h24=960_000,
                    buys_m5=90, sells_m5=10, liquidity=700_000)
    market = FakeDexScreener([hot])
    engine = TradingEngine(config, storage=Storage(db), data=market,
                           executor=SimulatedExecutor(config, data=market, rng=random.Random(4)))
    engine.run_cycle()
    engine.storage.close()
    return db


@pytest.fixture(autouse=True)
def no_external_candles(monkeypatch):
    """The candle endpoint must never reach the network during tests."""
    from memebot.data.candles import GeckoTerminalClient

    monkeypatch.setattr(GeckoTerminalClient, "ohlcv", lambda self, *a, **k: [])


def call(module_name, path="/", headers=None):
    module = __import__(f"api.{module_name}", fromlist=["handler"])
    return render(module.handler, path, Headers(headers or {}))


# ---- read endpoints ------------------------------------------------------------
def test_status_reports_the_portfolio(state_db):
    status, body = call("status")
    assert status == 200
    assert body["mode"] == "live"
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


# ---- the deployment is read-only ----------------------------------------------
def test_there_is_no_endpoint_that_can_trade(state_db):
    """A cloud function must never be able to spend the wallet.

    Trading runs on the machine that holds the key; the deployment only reads.
    """
    import pathlib

    api_dir = pathlib.Path(__file__).resolve().parent.parent / "api"
    assert not (api_dir / "cycle.py").exists()

    sources = " ".join(p.read_text() for p in api_dir.glob("*.py"))
    for forbidden in ("TradingEngine", "run_cycle", "liquidate", "LiveExecutor"):
        assert forbidden not in sources, f"{forbidden} must not be reachable from the web"


def test_the_dev_server_exposes_no_trading_route():
    from scripts.dev_server import ROUTES

    assert "/api/cycle" not in ROUTES
    assert set(ROUTES) <= {"/api/status", "/api/positions", "/api/trades", "/api/equity",
                           "/api/scan", "/api/events", "/api/candles", "/api/health"}


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


# ---- the live view's endpoints -------------------------------------------------
def test_events_endpoint_returns_the_feed_newest_first(state_db):
    status, body = call("events")
    assert status == 200
    assert body["count"] >= 1
    assert body["incremental"] is False
    kinds = [e["kind"] for e in body["events"]]
    assert "buy" in kinds or "cycle" in kinds
    assert body["last_id"] > 0


def test_events_endpoint_supports_incremental_polling(state_db):
    _status, first = call("events")
    last_id = first["last_id"]

    _status, empty = call("events", f"/api/events?since_id={last_id}")
    assert empty["incremental"] is True
    assert empty["events"] == [], "nothing new should mean nothing returned"
    assert empty["last_id"] == last_id

    store = Storage(state_db)
    try:
        store.record_event("buy", "Bought LATER", symbol="LATER")
    finally:
        store.close()

    _status, more = call("events", f"/api/events?since_id={last_id}")
    assert [e["message"] for e in more["events"]] == ["Bought LATER"]
    assert more["last_id"] > last_id


def test_events_are_ordered_oldest_first_when_incremental(state_db):
    store = Storage(state_db)
    try:
        base = store.list_events(limit=1)[0]["id"]
        for i in range(3):
            store.record_event("cycle", f"event {i}")
    finally:
        store.close()

    _status, body = call("events", f"/api/events?since_id={base}")
    ids = [e["id"] for e in body["events"]]
    assert ids == sorted(ids)


def test_candles_endpoint_defaults_to_the_open_position(state_db):
    status, body = call("candles")
    assert status == 200
    assert body["symbol"] == "BEST"
    assert body["timeframe"] == "5m"
    assert body["position"] is not None
    assert any(t["open"] for t in body["tokens"])


def test_candles_include_this_bots_fills_as_markers(state_db):
    _status, body = call("candles")
    assert body["markers"], "the entries are the point of the chart"
    marker = body["markers"][0]
    assert marker["side"] in ("buy", "sell")
    assert marker["price"] > 0
    assert "reason" in marker


def test_candles_fall_back_to_the_bots_own_samples(state_db):
    """With no external OHLC source, the chart is still drawn."""
    _status, body = call("candles")
    assert body["source"] == "samples"
    assert body["candles"], "a running bot always has some price history"
    for candle in body["candles"]:
        assert candle["low"] <= candle["open"] <= candle["high"]
        assert candle["low"] <= candle["close"] <= candle["high"]


def test_candles_widen_the_bucket_when_samples_are_sparse(state_db):
    """One sample per bucket would draw flat dashes, not candles."""
    store = Storage(state_db)
    try:
        rows = store.list_trades(limit=1)
        address = rows[0]["token_address"]
        store.execute("DELETE FROM price_samples")
        base = time.time() - 60 * 600
        for i in range(60):
            store.record_price_sample(address, 1.0 + (i % 7) * 0.1, ts=base + i * 600)
        store._commit()
    finally:
        store.close()

    _status, body = call("candles", "/api/candles?timeframe=5m&address=" + address)
    assert body["effective_timeframe"] != "5m", "a 10-minute poll cannot fill 5m candles"
    bodies = [c for c in body["candles"] if c["open"] != c["close"]]
    assert bodies, "widened buckets must produce candles with actual bodies"


def test_candles_handle_a_token_with_no_history(state_db):
    _status, body = call("candles", "/api/candles?address=mint-never-traded")
    assert body["candles"] == []
    assert body["markers"] == []
    assert body["position"] is None


def test_an_unknown_timeframe_falls_back_rather_than_erroring(state_db):
    _status, body = call("candles", "/api/candles?timeframe=banana")
    assert body["timeframe"] == "5m"


def test_candles_with_no_trades_at_all_are_empty_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMEBOT_STATE_DB", str(tmp_path / "fresh.sqlite3"))
    monkeypatch.delenv("VERCEL", raising=False)
    status, body = call("candles")
    assert status == 200
    assert body["candles"] == [] and body["tokens"] == []
