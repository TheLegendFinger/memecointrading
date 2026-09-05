import time

import pytest

from memebot.config import RiskConfig
from memebot.models import Fill, Order, Position, Side, Token
from memebot.portfolio import Portfolio
from memebot.risk import RiskManager
from tests.conftest import make_pair

TOKEN = Token("mint-wif", "WIF")


def _position(avg_price=1.0, last=1.0, high=None, qty=100.0, age_minutes=10.0, peak_liq=200_000.0):
    pos = Position(
        token=TOKEN, quantity=qty, avg_price=avg_price, cost_usd=avg_price * qty,
        opened_at=time.time() - age_minutes * 60, last_price=last,
        high_price=high if high is not None else max(avg_price, last),
        peak_liquidity_usd=peak_liq, last_seen_at=time.time(),
    )
    return pos


def _fund(portfolio, price=1.0, tokens=100.0):
    order = Order(token=TOKEN, side=Side.BUY, reference_price=price, usd_amount=price * tokens)
    portfolio.apply_fill(Fill(order=order, ok=True, price=price, token_amount=tokens,
                              usd_amount=price * tokens, fee_usd=0.0))


# ---- sizing --------------------------------------------------------------------
def test_position_size_uses_equity_fraction(storage):
    risk = RiskManager(RiskConfig(position_size_pct=0.10, max_position_usd=1_000))
    p = Portfolio(storage, 1_000.0)
    assert risk.position_size_usd(p) == pytest.approx(100.0)


def test_position_size_capped_by_pool_liquidity(storage):
    risk = RiskManager(RiskConfig(position_size_pct=0.5, max_position_usd=1_000,
                                  max_position_pct_of_liquidity=0.005))
    p = Portfolio(storage, 1_000.0)
    pair = make_pair(liquidity=40_000.0)
    # 0.5% of a $40k pool is $200, well below the $500 the equity rule wants.
    assert risk.position_size_usd(p, pair) == pytest.approx(200.0)


def test_position_size_respects_cash_reserve(storage):
    risk = RiskManager(RiskConfig(position_size_pct=1.0, max_position_usd=10_000,
                                  cash_reserve_pct=0.10))
    p = Portfolio(storage, 1_000.0)
    assert risk.position_size_usd(p) == pytest.approx(900.0)


def test_size_below_minimum_returns_zero(storage):
    risk = RiskManager(RiskConfig(position_size_pct=0.001, min_position_usd=10.0))
    p = Portfolio(storage, 1_000.0)
    assert risk.position_size_usd(p) == 0.0


# ---- entry gating --------------------------------------------------------------
def test_max_open_positions_blocks_new_entries(storage):
    risk = RiskManager(RiskConfig(max_open_positions=1))
    p = Portfolio(storage, 1_000.0)
    _fund(p)
    ok, reason = risk.can_open_position(p)
    assert not ok and "max_open_positions" in reason


def test_cooldown_after_a_loss(storage):
    risk = RiskManager(RiskConfig(cooldown_minutes_after_loss=30.0))
    p = Portfolio(storage, 1_000.0)
    risk.record_close(-50.0)
    ok, reason = risk.can_open_position(p)
    assert not ok and "cooling down" in reason

    # A winning close does not start a cooldown.
    risk2 = RiskManager(RiskConfig(cooldown_minutes_after_loss=30.0))
    risk2.record_close(25.0)
    assert risk2.can_open_position(p)[0]


def test_daily_loss_limit_halts_trading(storage):
    risk = RiskManager(RiskConfig(max_daily_loss_pct=0.10, max_drawdown_pct=0.0))
    p = Portfolio(storage, 1_000.0)
    p.day_start_equity()          # anchor the day at $1,000
    p.cash = 850.0                # down 15%
    halted, reason = risk.should_halt(p)
    assert halted and "daily loss" in reason


def test_max_drawdown_halts_trading(storage):
    risk = RiskManager(RiskConfig(max_daily_loss_pct=0.0, max_drawdown_pct=0.30))
    p = Portfolio(storage, 1_000.0)
    p.cash = 1_500.0
    p.snapshot_equity()           # peak recorded at $1,500
    p.cash = 900.0                # 40% off the peak
    halted, reason = risk.should_halt(p)
    assert halted and "drawdown" in reason


def test_reentry_cooldown_blocks_the_same_token(storage):
    risk = RiskManager(RiskConfig(reentry_cooldown_minutes=60.0))
    p = Portfolio(storage, 1_000.0)
    _fund(p)
    sell = Order(token=TOKEN, side=Side.SELL, reference_price=1.0, token_amount=100.0)
    p.apply_fill(Fill(order=sell, ok=True, price=1.0, token_amount=100.0, usd_amount=100.0))

    ok, reason = risk.can_enter_token(p, TOKEN.address)
    assert not ok and "cooldown" in reason


def test_cannot_double_up_on_an_open_position(storage):
    risk = RiskManager(RiskConfig())
    p = Portfolio(storage, 1_000.0)
    _fund(p)
    ok, reason = risk.can_enter_token(p, TOKEN.address)
    assert not ok and "already holding" in reason


# ---- exits ---------------------------------------------------------------------
def test_stop_loss_triggers():
    risk = RiskManager(RiskConfig(stop_loss_pct=0.20))
    decision = risk.evaluate_exit(_position(avg_price=1.0, last=0.75))
    assert decision.should_exit and "stop loss" in decision.reason


def test_take_profit_triggers():
    risk = RiskManager(RiskConfig(take_profit_pct=0.50, trailing_stop_pct=0.0))
    decision = risk.evaluate_exit(_position(avg_price=1.0, last=1.6))
    assert decision.should_exit and "take profit" in decision.reason


def test_trailing_stop_only_arms_after_the_profit_threshold():
    risk = RiskManager(RiskConfig(trailing_arm_profit_pct=0.25, trailing_stop_pct=0.20,
                                  take_profit_pct=0.0, stop_loss_pct=0.90))
    # High of +10% never armed the trail, so a pullback is not an exit.
    assert not risk.evaluate_exit(_position(avg_price=1.0, last=1.0, high=1.10)).should_exit
    # High of +50%, now 25% off that high -> exit.
    decision = risk.evaluate_exit(_position(avg_price=1.0, last=1.125, high=1.50))
    assert decision.should_exit and "trailing stop" in decision.reason


def test_liquidity_drain_exits_before_profit_taking():
    risk = RiskManager(RiskConfig(liquidity_drain_pct=0.5, take_profit_pct=0.5))
    pos = _position(avg_price=1.0, last=2.0, peak_liq=400_000.0)
    pair = make_pair(liquidity=100_000.0)
    decision = risk.evaluate_exit(pos, pair)
    assert decision.should_exit and "liquidity drained" in decision.reason


def test_stale_market_data_exits():
    risk = RiskManager(RiskConfig(stale_price_exit_minutes=30.0))
    pos = _position()
    pos.last_seen_at = time.time() - 45 * 60
    decision = risk.evaluate_exit(pos)
    assert decision.should_exit and "no market data" in decision.reason


def test_max_hold_time_exits():
    risk = RiskManager(RiskConfig(max_hold_minutes=60.0, take_profit_pct=0.0,
                                  trailing_stop_pct=0.0, stop_loss_pct=0.9))
    decision = risk.evaluate_exit(_position(age_minutes=120.0))
    assert decision.should_exit and "max hold" in decision.reason


def test_healthy_position_is_left_alone():
    risk = RiskManager(RiskConfig())
    pos = _position(avg_price=1.0, last=1.05, age_minutes=5.0)
    assert not risk.evaluate_exit(pos, make_pair()).should_exit
