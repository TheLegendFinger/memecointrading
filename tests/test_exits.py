"""Getting out.

A buy that does not happen costs nothing. An exit that does not happen leaves
you holding a coin the bot has already decided to be out of, while it keeps
falling - and, on a five-second position tick, retrying the same doomed sell
twelve times a minute.

    23:36  FAIL  Sell SOLCAT failed
                 price impact 2.85% exceeds tolerance 2.00%

That is this file: exits are allowed to cost more than entries, a pool too
thin for the whole position gets sold in parts, and a route that is refused
outright is backed off rather than hammered.
"""

import random
import time

import pytest

from conftest import FakeDexScreener, make_pair
from memebot.engine import TradingEngine
from memebot.models import Side
from memebot.storage import Storage
from tests.fakes import SimulatedExecutor


@pytest.fixture
def hot_pair():
    return make_pair("SOLCAT", chg_m5=12.0, chg_h1=55.0, vol_m5=60_000, vol_h1=400_000,
                     vol_h24=960_000, buys_m5=90, sells_m5=10, buys_h1=800, sells_h1=200,
                     liquidity=700_000, price=0.01)


def build(config, pairs, seed=11):
    market = FakeDexScreener(pairs)
    engine = TradingEngine(
        config, storage=Storage(":memory:"), data=market,
        executor=SimulatedExecutor(config, data=market, rng=random.Random(seed)),
    )
    return engine, market


class Refusing:
    """An executor that refuses every sell below a tolerance, like a thin pool."""

    mode = "refusing"

    def __init__(self, inner, needs_bps):
        self.inner = inner
        self.needs_bps = needs_bps
        self.attempts = []

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def execute(self, order):
        if order.side is Side.SELL:
            self.attempts.append((order.slippage_bps, order.token_amount))
            if order.slippage_bps < self.needs_bps:
                from memebot.models import Fill

                return Fill(order=order, ok=False,
                            error=f"price impact 2.85% exceeds tolerance "
                                  f"{order.slippage_bps / 100:.2f}%")
        return self.inner.execute(order)


# ---- exits are not entries -------------------------------------------------------
def test_getting_out_may_cost_more_than_getting_in(config, hot_pair):
    """The reported failure: a sell refused at the *buy* tolerance."""
    config.execution.slippage_bps = 200
    config.execution.exit_slippage_bps = 500
    engine, market = build(config, [hot_pair])
    engine.run_cycle()
    engine.executor = Refusing(engine.executor, needs_bps=400)

    market.set_price(hot_pair.base.address, 0.005)     # -50%: stop loss
    report = engine.run_position_tick()

    assert len(report.closed) == 1, "it got out"
    assert engine.executor.attempts[0][0] >= 500, "and started at the exit tolerance"


def test_the_config_refuses_an_exit_tighter_than_an_entry(config):
    config.execution.slippage_bps = 500
    config.execution.exit_slippage_bps = 200
    with pytest.raises(ValueError, match="never be harder than getting in"):
        config.validate()


def test_a_refused_exit_widens_before_giving_up(config, hot_pair):
    config.execution.exit_slippage_bps = 500
    config.execution.max_exit_slippage_bps = 1500
    engine, market = build(config, [hot_pair])
    engine.run_cycle()
    engine.executor = Refusing(engine.executor, needs_bps=1200)

    market.set_price(hot_pair.base.address, 0.005)
    report = engine.run_position_tick()

    assert len(report.closed) == 1
    tolerances = [bps for bps, _ in engine.executor.attempts]
    assert tolerances[0] == 500 and 1500 in tolerances, tolerances


def test_a_pool_too_thin_for_the_whole_position_is_sold_in_parts(config, hot_pair):
    """Impact scales with size, so half a position can go where all of it cannot."""
    engine, market = build(config, [hot_pair])
    engine.run_cycle()
    position = engine.portfolio.position(hot_pair.base.address)
    whole = position.quantity

    class TooBig(Refusing):
        def execute(self, order):
            if order.side is Side.SELL and order.token_amount > whole * 0.6:
                from memebot.models import Fill

                self.attempts.append((order.slippage_bps, order.token_amount))
                return Fill(order=order, ok=False, error="price impact too high")
            return self.inner.execute(order)

    engine.executor = TooBig(engine.executor, needs_bps=0)
    market.set_price(hot_pair.base.address, 0.005)
    report = engine.run_position_tick()

    assert len(report.closed) == 1
    assert report.closed[0].order.token_amount == pytest.approx(whole * 0.5)
    assert "selling 50%" in report.closed[0].order.reason


# ---- not panicking ---------------------------------------------------------------
def test_a_hopeless_exit_backs_off_instead_of_retrying_every_tick(config, hot_pair):
    """Twelve identical failures a minute is not persistence, it is noise."""
    config.execution.exit_retry_seconds = 60.0
    engine, market = build(config, [hot_pair])
    engine.run_cycle()
    engine.executor = Refusing(engine.executor, needs_bps=99_000)   # never fills

    market.set_price(hot_pair.base.address, 0.005)
    first = engine.run_position_tick()
    attempts_after_first = len(engine.executor.attempts)
    second = engine.run_position_tick()
    third = engine.run_position_tick()

    assert first.errors, "the first failure is reported"
    assert len(engine.executor.attempts) == attempts_after_first, "no retry storm"
    assert not second.errors and not third.errors, "and it is reported once, not thrice"
    assert any("waiting to retry" in reason for reason in second.skipped)


def test_the_backoff_expires_so_the_position_is_not_abandoned(config, hot_pair):
    config.execution.exit_retry_seconds = 30.0
    engine, market = build(config, [hot_pair])
    engine.run_cycle()
    engine.executor = Refusing(engine.executor, needs_bps=99_000)
    market.set_price(hot_pair.base.address, 0.005)
    engine.run_position_tick()
    tried = len(engine.executor.attempts)

    engine._exit_retry_after[hot_pair.base.address] = time.time() - 1   # 30s later
    engine.run_position_tick()

    assert len(engine.executor.attempts) > tried, "it comes back to it"


def test_a_successful_exit_clears_the_backoff(config, hot_pair):
    engine, market = build(config, [hot_pair])
    engine.run_cycle()
    engine._exit_retry_after[hot_pair.base.address] = 0.0
    market.set_price(hot_pair.base.address, 0.005)

    engine.run_position_tick()

    assert hot_pair.base.address not in engine._exit_retry_after


def test_manual_liquidation_ignores_the_backoff(config, hot_pair):
    """"Close everything" means now, whatever the loop decided to wait for."""
    engine, market = build(config, [hot_pair])
    engine.run_cycle()
    engine._exit_retry_after[hot_pair.base.address] = time.time() + 600

    report = engine.liquidate_all()

    assert len(report.closed) == 1
    assert not engine.portfolio.open_positions


# ---- not buying what cannot be sold ----------------------------------------------
class FakeQuote:
    def __init__(self, impact, out_amount=1_000_000, in_mint="A", out_mint="B"):
        self.price_impact_pct = impact
        self.out_amount = out_amount
        self.input_mint = in_mint
        self.output_mint = out_mint


def make_live_executor(config):
    from memebot.execution.live import LiveExecutor

    executor = LiveExecutor.__new__(LiveExecutor)
    executor.cfg = config.execution
    return executor


def test_a_coin_with_no_way_out_is_not_bought(config):
    """The check that would have kept the bot out of the position it could not
    sell: quote the round trip before committing to it."""
    executor = make_live_executor(config)

    class Jupiter:
        def quote(self, in_mint, out_mint, amount, slippage_bps):
            return FakeQuote(impact=40.0)      # 40% to sell it back

    executor.jupiter = Jupiter()
    blocked = executor._exit_would_be_blocked(FakeQuote(impact=0.5))

    assert blocked and "too thin to get out of" in blocked


def test_a_coin_that_can_be_sold_back_is_allowed(config):
    executor = make_live_executor(config)

    class Jupiter:
        def quote(self, *a, **kw):
            return FakeQuote(impact=1.2)

    executor.jupiter = Jupiter()
    assert executor._exit_would_be_blocked(FakeQuote(impact=0.5)) is None


def test_no_route_back_is_refused(config):
    executor = make_live_executor(config)

    class Jupiter:
        def quote(self, *a, **kw):
            return None

    executor.jupiter = Jupiter()
    assert "no route back out" in executor._exit_would_be_blocked(FakeQuote(impact=0.5))


def test_a_timed_out_exit_quote_does_not_block_the_buy(config):
    """The check is a safeguard, not a gate on the network being perfect."""
    from memebot.http import HttpError

    executor = make_live_executor(config)

    class Jupiter:
        def quote(self, *a, **kw):
            raise HttpError("timed out", 504)

    executor.jupiter = Jupiter()
    assert executor._exit_would_be_blocked(FakeQuote(impact=0.5)) is None


def test_the_exit_check_can_be_switched_off(config):
    config.execution.check_exit_route = False
    assert config.execution.check_exit_route is False


def test_a_part_sale_is_not_booked_as_the_trade_s_result(config, hot_pair):
    """Half a sale is half a result, and the learner must not see it as one."""
    engine, market = build(config, [hot_pair])
    engine.run_cycle()
    whole = engine.portfolio.position(hot_pair.base.address).quantity

    class TooBig(Refusing):
        def execute(self, order):
            if order.side is Side.SELL and order.token_amount > whole * 0.6:
                from memebot.models import Fill

                return Fill(order=order, ok=False, error="price impact too high")
            return self.inner.execute(order)

    engine.executor = TooBig(engine.executor, needs_bps=0)
    market.set_price(hot_pair.base.address, 0.005)
    engine.run_position_tick()

    assert engine.portfolio.has_position(hot_pair.base.address), "half still held"
    assert engine.storage.closed_outcomes() == [], "not learned from until it is over"

    engine.run_position_tick()      # the rest goes out
    assert not engine.portfolio.has_position(hot_pair.base.address)
    assert len(engine.storage.closed_outcomes()) == 1


# ---- the fee reserve -------------------------------------------------------------
def test_the_reserve_is_sized_from_what_the_config_can_actually_do(config):
    """0.025 SOL was a round number picked for ten concurrent positions when
    the cap is three - $5 sitting idle on a wallet that trades in dollars."""
    from memebot.config import TOKEN_ACCOUNT_RENT_SOL

    config.execution.sol_fee_reserve = 0.0
    config.risk.max_open_positions = 3

    reserve = config.fee_reserve_sol

    assert reserve >= 3 * TOKEN_ACCOUNT_RENT_SOL, "rent for every coin it may hold"
    assert reserve < 0.025, "and far less than the number it replaces"


def test_more_positions_need_more_reserve(config):
    config.execution.sol_fee_reserve = 0.0
    config.risk.max_open_positions = 1
    small = config.fee_reserve_sol
    config.risk.max_open_positions = 8
    assert config.fee_reserve_sol > small


def test_a_higher_priority_fee_needs_more_reserve(config):
    config.execution.sol_fee_reserve = 0.0
    cheap = config.fee_reserve_sol
    config.execution.priority_fee_microlamports = 5_000_000
    assert config.fee_reserve_sol > cheap


def test_the_reserve_never_drops_to_nothing(config):
    """A wallet that cannot pay for a transaction cannot sell what it holds."""
    from memebot.config import MIN_FEE_RESERVE_SOL

    config.execution.sol_fee_reserve = 0.0
    config.risk.max_open_positions = 1
    config.execution.priority_fee_microlamports = 0
    config.execution.compute_unit_limit = 1

    assert config.fee_reserve_sol >= MIN_FEE_RESERVE_SOL


def test_an_explicit_reserve_still_wins(config):
    config.execution.sol_fee_reserve = 0.05
    assert config.fee_reserve_sol == 0.05


def test_the_reserve_is_what_the_wallet_holds_back(config):
    """It is subtracted from the bankroll, so getting it wrong is money idle."""
    from memebot.execution.live import LiveExecutor
    from memebot.models import WSOL_MINT

    config.execution.sol_fee_reserve = 0.0
    config.execution.quote_mint = WSOL_MINT
    executor = LiveExecutor.__new__(LiveExecutor)
    executor.config = config
    executor.cfg = config.execution
    executor.sol_balance = lambda: 0.4
    executor._quote_price_usd = lambda: 150.0

    tradable = executor.available_cash_usd()

    assert tradable == pytest.approx((0.4 - config.fee_reserve_sol) * 150.0)


def test_liquidate_can_be_told_to_accept_a_worse_price(config, hot_pair, monkeypatch, capsys):
    """A coin the 15% ceiling cannot shift is exactly when you want to raise
    it, and editing a config file mid-panic is not a workflow."""
    from argparse import Namespace

    from memebot import cli

    engine, _ = build(config, [hot_pair])
    engine.run_cycle()
    monkeypatch.setattr(cli, "_build_engine", lambda cfg, on_cycle=None: engine)

    cli.cmd_liquidate(Namespace(yes=True, max_slippage_bps=3000), config)

    assert config.execution.max_exit_slippage_bps == 3000
    assert "expect a poor price" in capsys.readouterr().out


def test_liquidate_says_what_to_do_when_a_coin_will_not_sell(config, hot_pair,
                                                             monkeypatch, capsys):
    from argparse import Namespace

    from memebot import cli

    engine, _ = build(config, [hot_pair])
    engine.run_cycle()
    engine.executor = Refusing(engine.executor, needs_bps=99_000)
    monkeypatch.setattr(cli, "_build_engine", lambda cfg, on_cycle=None: engine)

    cli.cmd_liquidate(Namespace(yes=True, max_slippage_bps=None), config)

    out = capsys.readouterr().out
    assert "Still holding: SOLCAT" in out
    assert "--max-slippage-bps" in out
    assert "jup.ag" in out
