"""Live-mode bankroll tests.

In paper mode the portfolio's cash is authoritative. In live mode it is not -
the wallet is. These cover the sync that makes position sizing reflect what the
wallet actually holds, because getting this wrong means sizing trades against
money that is not there.
"""

import random

import pytest

from memebot.engine import TradingEngine
from tests.fakes import SimulatedExecutor
from memebot.portfolio import Portfolio
from memebot.storage import Storage
from tests.conftest import FakeDexScreener, fund, make_pair


class WalletExecutor(SimulatedExecutor):
    """A paper executor that reports a wallet balance, like the live one does."""

    mode = "live"

    def __init__(self, *args, cash=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._cash = cash

    def available_cash_usd(self):
        return self._cash


def build(config, cash, pairs=None, seed=3):
    market = FakeDexScreener(pairs or [])
    storage = Storage(":memory:")
    executor = WalletExecutor(config, data=market, rng=random.Random(seed), cash=cash)
    engine = TradingEngine(config, storage=storage, data=market, executor=executor)
    return engine, market


def test_cash_comes_from_the_wallet(config):
    """There is no configured bankroll at all - only what the wallet holds."""
    engine, _ = build(config, cash=87.40)

    engine.sync_live_balance()

    assert engine.portfolio.cash == pytest.approx(87.40)


def test_the_first_cycle_anchors_the_return_baseline(config):
    engine, _ = build(config, cash=87.40)

    engine.sync_live_balance()

    assert engine.portfolio.starting_cash == pytest.approx(87.40)
    assert engine.portfolio.total_return_pct == pytest.approx(0.0)


def test_the_baseline_is_anchored_once_not_every_cycle(config):
    engine, _ = build(config, cash=100.0)
    engine.sync_live_balance()

    engine.executor._cash = 130.0             # a winning session
    engine.sync_live_balance()

    assert engine.portfolio.starting_cash == pytest.approx(100.0)
    assert engine.portfolio.total_return_pct == pytest.approx(0.30)


def test_funds_added_to_the_wallet_are_picked_up(config):
    engine, _ = build(config, cash=50.0)
    engine.sync_live_balance()

    engine.executor._cash = 250.0             # you sent more SOL
    engine.sync_live_balance()

    assert engine.portfolio.cash == pytest.approx(250.0)


def test_an_unreadable_balance_keeps_the_last_known_figure(config):
    engine, _ = build(config, cash=120.0)
    engine.sync_live_balance()

    engine.executor._cash = None              # RPC hiccup
    engine.sync_live_balance()

    assert engine.portfolio.cash == pytest.approx(120.0)


def test_an_executor_that_reports_no_balance_leaves_cash_alone(config):
    """An RPC that cannot be read must not silently zero the bankroll."""
    market = FakeDexScreener([])
    executor = WalletExecutor(config, data=market, rng=random.Random(1), cash=None)
    engine = TradingEngine(config, storage=Storage(":memory:"), data=market, executor=executor)
    engine.portfolio.set_cash(123.0)

    assert engine.sync_live_balance() is None
    assert engine.portfolio.cash == pytest.approx(123.0)


def test_sizing_uses_the_wallet_balance(config):
    """The whole point: a small wallet must produce small orders."""
    config.risk.position_size_pct = 0.20
    config.risk.max_position_usd = 25.0
    config.risk.min_position_usd = 5.0
    config.risk.cash_reserve_pct = 0.10
    engine, _ = build(config, cash=60.0)
    engine.sync_live_balance()

    size = engine.risk.position_size_usd(engine.portfolio)
    assert size == pytest.approx(12.0)  # 20% of $60, inside every cap


def test_an_empty_wallet_cannot_open_a_position(config):
    engine, _ = build(config, cash=0.0)
    engine.sync_live_balance()

    allowed, reason = engine.risk.can_open_position(engine.portfolio)
    assert not allowed
    assert "insufficient free cash" in reason


def test_a_live_cycle_sizes_against_the_wallet_end_to_end(config):
    config.risk.position_size_pct = 0.25
    config.risk.max_position_usd = 25.0
    config.risk.min_position_usd = 5.0
    hot = make_pair("BEST", chg_m5=12.0, chg_h1=55.0, vol_h1=400_000, vol_h24=960_000,
                    buys_m5=90, sells_m5=10, liquidity=700_000)
    engine, _ = build(config, cash=80.0, pairs=[hot])

    report = engine.run_cycle()

    assert len(report.opened) == 1
    assert report.opened[0].usd_amount <= 25.0, "a $80 wallet must not place a $250 order"
    assert report.opened[0].usd_amount == pytest.approx(20.0, rel=0.01)  # 25% of $80


def test_wallet_balance_is_persisted_across_restarts(config, tmp_path):
    db = str(tmp_path / "live.sqlite3")
    config.state_db = db
    market = FakeDexScreener([])
    executor = WalletExecutor(config, data=market, rng=random.Random(1), cash=42.0)
    engine = TradingEngine(config, storage=Storage(db), data=market, executor=executor)
    engine.sync_live_balance()
    engine.storage.close()

    revived = Portfolio(Storage(db))
    assert revived.cash == pytest.approx(42.0)
    assert revived.starting_cash == pytest.approx(42.0)


# ---- the real LiveExecutor, driven by the engine -------------------------------
def test_a_full_live_cycle_with_the_real_executor(config, monkeypatch):
    """End to end in live mode: quote -> sign -> send -> confirm -> book.

    Everything outside the bot is faked (Jupiter, the RPC, the signature), but
    the engine, risk sizing, LiveExecutor and portfolio accounting are the real
    code that runs against mainnet.
    """
    from memebot.execution.live import CONFIRM_ENV, CONFIRM_VALUE, LiveExecutor
    from memebot.models import WSOL_MINT
    from tests.test_live_executor import FakeJupiter, FakeRpc

    monkeypatch.setenv(CONFIRM_ENV, CONFIRM_VALUE)

    config.risk.position_size_pct = 0.25
    config.risk.max_position_usd = 25.0
    config.risk.min_position_usd = 5.0

    hot = make_pair("BEST", price=0.01, chg_m5=12.0, chg_h1=55.0, vol_h1=400_000,
                    vol_h24=960_000, buys_m5=90, sells_m5=10, liquidity=700_000)
    market = FakeDexScreener([hot])

    jupiter = FakeJupiter(out_amount=2_000_000)   # 2 tokens at 6 decimals
    jupiter._decimals[hot.base.address] = 6
    rpc = FakeRpc(lamports=400_000_000)           # 0.4 SOL
    executor = LiveExecutor(config, jupiter=jupiter, rpc=rpc, data=market)
    executor._keypair = object()
    executor._pubkey = "Wallet111"
    executor._ensure_wallet = lambda: executor._keypair
    monkeypatch.setattr(executor, "_sign_and_send",
                        lambda tx: rpc.send_raw_transaction(tx))

    engine = TradingEngine(config, storage=Storage(":memory:"), data=market, executor=executor)

    assert engine.preflight() is None, "an armed, funded wallet should be ready"

    report = engine.run_cycle()

    # Bankroll came from the wallet: 0.4 SOL at $150, less the 0.025 reserve.
    expected_cash = (0.4 - config.execution.sol_fee_reserve) * 150.0
    assert engine.portfolio.starting_cash == pytest.approx(expected_cash, rel=1e-6)

    assert len(report.opened) == 1
    fill = report.opened[0]
    assert fill.tx_signature == "SIG123"
    assert rpc.sent, "a transaction must actually have been broadcast"
    # 25% of ~$56 is ~$14 - nowhere near the $2,500 the paper number implies.
    assert fill.usd_amount <= config.risk.max_position_usd
    assert engine.portfolio.has_position(hot.base.address)


def test_live_cycle_books_nothing_when_the_swap_reverts(config, monkeypatch):
    from memebot.execution.live import CONFIRM_ENV, CONFIRM_VALUE, LiveExecutor
    from tests.test_live_executor import FakeJupiter, FakeRpc

    monkeypatch.setenv(CONFIRM_ENV, CONFIRM_VALUE)
    config.risk.position_size_pct = 0.25
    config.risk.min_position_usd = 5.0

    hot = make_pair("BEST", chg_m5=12.0, chg_h1=55.0, vol_h1=400_000, vol_h24=960_000,
                    buys_m5=90, sells_m5=10, liquidity=700_000)
    market = FakeDexScreener([hot])
    rpc = FakeRpc(lamports=400_000_000, status={"err": "InstructionError"})
    executor = LiveExecutor(config, jupiter=FakeJupiter(), rpc=rpc, data=market)
    executor._keypair = object()
    executor._pubkey = "Wallet111"
    executor._ensure_wallet = lambda: executor._keypair
    monkeypatch.setattr(executor, "_sign_and_send", lambda tx: rpc.send_raw_transaction(tx))

    engine = TradingEngine(config, storage=Storage(":memory:"), data=market, executor=executor)
    report = engine.run_cycle()

    assert not report.opened
    assert report.errors, "a reverted swap must be reported, not swallowed"
    assert "reverted" in report.errors[0]
    assert not engine.portfolio.positions, "a failed swap must not book a position"

    # Cash still reflects the chain, untouched by the attempt: the wallet is
    # re-read every cycle, so a reverted swap cannot leave the books drifting.
    wallet_cash = (0.4 - config.execution.sol_fee_reserve) * 150.0
    assert engine.portfolio.cash == pytest.approx(wallet_cash, rel=1e-6)
