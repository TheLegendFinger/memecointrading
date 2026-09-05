"""A full paper-trading session against a scripted, moving market.

This is the closest thing to 'run the bot for an afternoon' that fits in a
test: prices move every cycle, winners and losers both appear, and the final
books must reconcile - cash + positions == equity, and realized P&L must equal
the sum of the individual closed trades.
"""

import random

import pytest

from memebot.engine import TradingEngine
from tests.fakes import SimulatedExecutor
from memebot.storage import Storage
from tests.conftest import FakeDexScreener, make_pair


def hot(symbol, price=0.01):
    return make_pair(symbol, price=price, chg_m5=11.0, chg_h1=50.0, vol_m5=60_000,
                     vol_h1=400_000, vol_h24=960_000, buys_m5=88, sells_m5=12,
                     buys_h1=800, sells_h1=200, liquidity=700_000)


@pytest.fixture
def engine(config, tmp_path):
    config.state_db = str(tmp_path / "e2e.sqlite3")
    config.risk.max_open_positions = 3
    config.risk.take_profit_pct = 0.40
    config.risk.stop_loss_pct = 0.20
    config.risk.reentry_cooldown_minutes = 0.0
    config.strategy.max_new_positions_per_cycle = 3

    market = FakeDexScreener([hot("MOON"), hot("RUG"), hot("FLAT")])
    storage = Storage(config.state_db)
    executor = SimulatedExecutor(config, data=market, rng=random.Random(2024))
    return TradingEngine(config, storage=storage, data=market, executor=executor), market


def test_full_session_books_reconcile(engine):
    bot, market = engine

    # Cycle 1: open three positions.
    bot.run_cycle()
    assert len(bot.portfolio.positions) == 3
    invested = bot.portfolio.starting_cash - bot.portfolio.cash
    assert invested > 0

    # Cycle 2: one moons past take-profit, one rugs past the stop, one drifts.
    market.set_price("mint-MOON", 0.016)   # +60%
    market.set_price("mint-RUG", 0.007)    # -30%
    market.set_price("mint-FLAT", 0.0101)  # +1%
    report = bot.run_cycle()

    assert len(report.closed) == 2
    reasons = " ".join(f.order.reason for f in report.closed)
    assert "take profit" in reasons and "stop loss" in reasons
    assert set(bot.portfolio.positions) == {"mint-FLAT"}

    # The books must agree with themselves.
    stats = bot.portfolio.stats()
    assert stats["equity_usd"] == pytest.approx(stats["cash_usd"] + stats["positions_value_usd"])
    assert stats["closed_trades"] == 2
    assert stats["wins"] == 1 and stats["losses"] == 1

    trades = bot.storage.list_trades(limit=100)
    realized = sum(t["realized_pnl"] for t in trades if t["side"] == "sell")
    assert realized == pytest.approx(stats["realized_pnl_usd"])

    # Every dollar is accounted for: start = cash + market value - realized pnl
    # - fees + unrealized pnl, which reduces to the equity identity below.
    fees = sum(t["fee_usd"] for t in trades)
    assert fees > 0
    expected_equity = (
        bot.portfolio.starting_cash + realized + bot.portfolio.unrealized_pnl_usd
    )
    assert stats["equity_usd"] == pytest.approx(expected_equity, rel=1e-9)


def test_a_losing_session_stays_within_the_risk_budget(config, tmp_path):
    """Everything the bot buys goes to zero - losses must stay bounded."""
    config.state_db = str(tmp_path / "bad.sqlite3")
    config.risk.max_open_positions = 3
    config.risk.position_size_pct = 0.10
    config.risk.stop_loss_pct = 0.20
    config.risk.reentry_cooldown_minutes = 120.0  # no immediate re-entry
    config.strategy.max_new_positions_per_cycle = 3

    market = FakeDexScreener([hot(f"BAD{i}") for i in range(3)])
    executor = SimulatedExecutor(config, data=market, rng=random.Random(7))
    bot = TradingEngine(config, storage=Storage(config.state_db), data=market, executor=executor)

    bot.run_cycle()
    for address in list(market.pairs):
        market.set_price(address, 0.0075)  # -25%, through every stop
    bot.run_cycle()

    assert not bot.portfolio.positions
    # Three positions of ~10% equity each losing ~25% plus costs: well inside 15%.
    assert bot.portfolio.equity > bot.portfolio.starting_cash * 0.85
    assert bot.portfolio.equity < bot.portfolio.starting_cash


def test_trading_halts_after_a_large_drawdown(config, tmp_path):
    config.state_db = str(tmp_path / "halt.sqlite3")
    config.risk.max_daily_loss_pct = 0.05
    config.risk.cooldown_minutes_after_loss = 0.0
    config.risk.reentry_cooldown_minutes = 0.0

    market = FakeDexScreener([hot("DOOM")])
    executor = SimulatedExecutor(config, data=market, rng=random.Random(3))
    bot = TradingEngine(config, storage=Storage(config.state_db), data=market, executor=executor)

    bot.run_cycle()
    bot.portfolio.day_start_equity()
    bot.portfolio.cash -= bot.portfolio.equity * 0.10  # simulate a bad day
    report = bot.run_cycle()

    assert report.halted_reason
    assert not report.opened
