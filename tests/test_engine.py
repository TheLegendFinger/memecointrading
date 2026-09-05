import random
import time

import pytest

from memebot.engine import TradingEngine
from tests.fakes import SimulatedExecutor
from memebot.models import Fill, Order, Side
from memebot.storage import Storage
from tests.conftest import FakeDexScreener, make_pair


def build_engine(config, pairs, seed=11, failure_rate=0.0):
    market = FakeDexScreener(pairs)
    storage = Storage(":memory:")
    executor = SimulatedExecutor(config, data=market, rng=random.Random(seed),
                                 failure_rate=failure_rate)
    engine = TradingEngine(config, storage=storage, data=market, executor=executor)
    return engine, market


@pytest.fixture
def hot_pair():
    return make_pair("BEST", chg_m5=12.0, chg_h1=55.0, vol_m5=60_000, vol_h1=400_000,
                     vol_h24=960_000, buys_m5=90, sells_m5=10, buys_h1=800, sells_h1=200,
                     liquidity=700_000, price=0.01)


def test_cycle_opens_a_position_on_a_strong_candidate(config, hot_pair):
    engine, _ = build_engine(config, [hot_pair])
    report = engine.run_cycle()

    assert report.scanned == 1
    assert report.passed_filters == 1
    assert len(report.opened) == 1
    assert engine.portfolio.has_position(hot_pair.base.address)
    assert engine.portfolio.cash < 1_000.0


def test_weak_candidates_are_ignored(config):
    dud = make_pair("DUD", chg_m5=-4.0, chg_h1=-15.0, vol_h1=45_000, vol_h24=980_000,
                    buys_m5=10, sells_m5=90, buys_h1=460, sells_h1=540)
    engine, _ = build_engine(config, [dud])
    report = engine.run_cycle()

    assert report.signals == 0
    assert not engine.portfolio.positions


def test_take_profit_closes_the_position(config, hot_pair):
    config.risk.take_profit_pct = 0.30
    engine, market = build_engine(config, [hot_pair])
    engine.run_cycle()
    assert engine.portfolio.has_position(hot_pair.base.address)

    market.set_price(hot_pair.base.address, 0.02)  # +100%
    report = engine.run_cycle()

    assert len(report.closed) == 1
    assert "take profit" in report.closed[0].order.reason
    assert not engine.portfolio.positions
    assert engine.portfolio.cash > 1_000.0


def test_stop_loss_closes_the_position(config, hot_pair):
    engine, market = build_engine(config, [hot_pair])
    engine.run_cycle()

    market.set_price(hot_pair.base.address, 0.005)  # -50%
    report = engine.run_cycle()

    assert len(report.closed) == 1
    assert "stop loss" in report.closed[0].order.reason
    assert engine.portfolio.equity < 1_000.0


def test_liquidity_drain_forces_an_exit(config, hot_pair):
    engine, market = build_engine(config, [hot_pair])
    engine.run_cycle()

    market.set_price(hot_pair.base.address, 0.0102, liquidity=100_000.0)  # pool drained
    report = engine.run_cycle()

    assert len(report.closed) == 1
    assert "liquidity drained" in report.closed[0].order.reason


def test_max_open_positions_is_respected(config):
    config.risk.max_open_positions = 2
    config.strategy.max_new_positions_per_cycle = 10
    pairs = [
        make_pair(f"HOT{i}", chg_m5=12.0, chg_h1=55.0, vol_h1=400_000, vol_h24=960_000,
                  buys_m5=90, sells_m5=10, liquidity=700_000)
        for i in range(5)
    ]
    engine, _ = build_engine(config, pairs)
    engine.run_cycle()

    assert len(engine.portfolio.positions) == 2


def test_per_cycle_entry_cap(config):
    config.risk.max_open_positions = 10
    config.strategy.max_new_positions_per_cycle = 1
    pairs = [
        make_pair(f"HOT{i}", chg_m5=12.0, chg_h1=55.0, vol_h1=400_000, vol_h24=960_000,
                  buys_m5=90, sells_m5=10, liquidity=700_000)
        for i in range(4)
    ]
    engine, _ = build_engine(config, pairs)
    engine.run_cycle()
    assert len(engine.portfolio.positions) == 1


def test_the_bot_does_not_rebuy_a_token_it_already_holds(config, hot_pair):
    config.risk.max_open_positions = 5
    engine, _ = build_engine(config, [hot_pair])
    engine.run_cycle()
    engine.run_cycle()
    assert len(engine.portfolio.positions) == 1


def test_reentry_cooldown_after_an_exit(config, hot_pair):
    config.risk.reentry_cooldown_minutes = 120.0
    config.risk.take_profit_pct = 0.10
    engine, market = build_engine(config, [hot_pair])
    engine.run_cycle()
    market.set_price(hot_pair.base.address, 0.05)
    engine.run_cycle()
    assert not engine.portfolio.positions

    market.set_price(hot_pair.base.address, 0.01)
    report = engine.run_cycle()
    assert not engine.portfolio.positions
    assert any("cooldown" in reason for reason in report.skipped)


def test_halt_on_daily_loss_stops_new_entries(config, hot_pair):
    config.risk.max_daily_loss_pct = 0.05
    engine, _ = build_engine(config, [hot_pair])
    engine.sync_live_balance()          # first read: $1,000
    engine.portfolio.day_start_equity()
    engine.executor._wallet_usd = 800.0  # the wallet is down 20% on the day

    report = engine.run_cycle()
    assert report.halted_reason
    assert not report.opened


def test_failed_orders_are_reported_and_leave_cash_untouched(config, hot_pair):
    engine, _ = build_engine(config, [hot_pair], failure_rate=1.0)
    report = engine.run_cycle()

    assert not report.opened
    assert report.errors
    assert engine.portfolio.cash == 1_000.0


def test_missing_market_data_falls_back_to_executor_pricing(config, hot_pair):
    engine, market = build_engine(config, [hot_pair])
    engine.run_cycle()
    entry_price = engine.portfolio.position(hot_pair.base.address).last_price

    market.remove(hot_pair.base.address)          # pair vanishes from DexScreener
    engine.executor.price_for = lambda address: 0.03
    engine.run_cycle()

    pos = engine.portfolio.position(hot_pair.base.address)
    assert pos is None or pos.last_price != entry_price


def test_state_persists_across_engine_restarts(config, hot_pair, tmp_path):
    db = str(tmp_path / "engine.sqlite3")
    config.state_db = db
    market = FakeDexScreener([hot_pair])
    executor = SimulatedExecutor(config, data=market, rng=random.Random(5))
    engine = TradingEngine(config, storage=Storage(db), data=market, executor=executor)
    engine.run_cycle()
    held = dict(engine.portfolio.positions)
    cash = engine.portfolio.cash
    engine.storage.close()

    market2 = FakeDexScreener([hot_pair])
    revived = TradingEngine(
        config, storage=Storage(db), data=market2,
        executor=SimulatedExecutor(config, data=market2, rng=random.Random(5)),
    )
    assert set(revived.portfolio.positions) == set(held)
    assert revived.portfolio.cash == pytest.approx(cash)


def test_liquidate_all_closes_everything(config):
    config.risk.max_open_positions = 3
    config.strategy.max_new_positions_per_cycle = 3
    pairs = [
        make_pair(f"HOT{i}", chg_m5=12.0, chg_h1=55.0, vol_h1=400_000, vol_h24=960_000,
                  buys_m5=90, sells_m5=10, liquidity=700_000)
        for i in range(3)
    ]
    engine, _ = build_engine(config, pairs)
    engine.run_cycle()
    assert len(engine.portfolio.positions) == 3

    report = engine.liquidate_all()
    assert len(report.closed) == 3
    assert not engine.portfolio.positions


def test_run_loop_stops_after_max_cycles(config, hot_pair):
    engine, _ = build_engine(config, [hot_pair])
    slept = []
    engine.run(max_cycles=3, sleep=slept.append)
    assert engine.cycles == 3
    assert len(slept) == 2  # no sleep after the final cycle


def test_run_refuses_to_start_when_preflight_fails(config, hot_pair):
    engine, _ = build_engine(config, [hot_pair])
    engine.executor.preflight = lambda: "wallet not funded"
    with pytest.raises(RuntimeError, match="wallet not funded"):
        engine.run(max_cycles=1, sleep=lambda _s: None)


def test_equity_curve_is_recorded_each_cycle(config, hot_pair):
    engine, _ = build_engine(config, [hot_pair])
    engine.run(max_cycles=2, sleep=lambda _s: None)
    assert len(engine.storage.equity_curve()) >= 1
    assert engine.storage.peak_equity() > 0


# ---- the live view's data --------------------------------------------------
def test_a_cycle_records_the_heartbeat_the_dashboard_reads(config, hot_pair):
    engine, _ = build_engine(config, [hot_pair])
    engine.run_cycle()

    assert engine.storage.get_state("last_cycle_at") > 0
    assert engine.storage.get_state("cycles_run") == 1
    engine.run_cycle()
    assert engine.storage.get_state("cycles_run") == 2


def test_trades_appear_in_the_activity_feed(config, hot_pair):
    config.risk.take_profit_pct = 0.20
    engine, market = build_engine(config, [hot_pair])
    engine.run_cycle()
    market.set_price(hot_pair.base.address, hot_pair.price_usd * 2)
    engine.run_cycle()

    kinds = [e["kind"] for e in engine.storage.list_events(limit=50)]
    assert "buy" in kinds and "sell" in kinds

    sell = next(e for e in engine.storage.list_events(limit=50) if e["kind"] == "sell")
    assert "BEST" in sell["message"]
    assert "take profit" in sell["detail"]
    assert sell["level"] == "win"


def test_a_failed_order_is_reported_in_the_feed(config, hot_pair):
    engine, _ = build_engine(config, [hot_pair], failure_rate=1.0)
    engine.run_cycle()

    errors = [e for e in engine.storage.list_events(limit=50) if e["kind"] == "error"]
    assert errors and errors[0]["level"] == "error"


def test_quiet_cycles_do_not_flood_the_feed(config):
    """A feed of 'nothing happened' is not worth reading."""
    dud = make_pair("DUD", chg_m5=-4.0, chg_h1=-15.0, vol_h1=45_000, vol_h24=980_000,
                    buys_m5=10, sells_m5=90, buys_h1=460, sells_h1=540)
    engine, _ = build_engine(config, [dud])
    for _ in range(12):
        engine.run_cycle()

    cycle_events = [e for e in engine.storage.list_events(limit=100) if e["kind"] == "cycle"]
    assert 0 < len(cycle_events) <= 3, "quiet cycles should be an occasional heartbeat"


def test_prices_are_sampled_for_the_chart(config, hot_pair):
    engine, _ = build_engine(config, [hot_pair])
    engine.run_cycle()

    samples = engine.storage.price_samples(hot_pair.base.address)
    assert samples, "the chart is built from these"
    assert samples[0]["price"] > 0
