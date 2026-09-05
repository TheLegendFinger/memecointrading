import random

import pytest

from memebot.config import BotConfig
from tests.fakes import SimulatedExecutor
from memebot.models import Order, Side, Token
from tests.conftest import make_pair


def _order(side=Side.BUY, price=0.01, usd=100.0, tokens=0.0, liquidity=250_000.0, slippage_bps=150):
    pair = make_pair(liquidity=liquidity, price=price)
    return Order(
        token=pair.base, side=side, reference_price=price, usd_amount=usd,
        token_amount=tokens, slippage_bps=slippage_bps, pair=pair,
    )


@pytest.fixture
def executor(config: BotConfig) -> SimulatedExecutor:
    return SimulatedExecutor(config, rng=random.Random(42))


def test_buy_fill_pays_above_reference_and_charges_fees(executor):
    order = _order()
    fill = executor.execute(order)

    assert fill.ok
    assert fill.price > order.reference_price, "buys should slip against us"
    assert fill.usd_amount == pytest.approx(100.0)
    assert fill.token_amount == pytest.approx(100.0 / fill.price)
    expected_fee = 100.0 * 0.0025 + 0.05 + 0.35
    assert fill.fee_usd == pytest.approx(expected_fee)
    assert fill.cash_delta == pytest.approx(-(100.0 + expected_fee))
    assert fill.tx_signature.startswith("sim-")


def test_sell_fill_receives_below_reference(executor):
    order = _order(side=Side.SELL, tokens=10_000.0, usd=0.0)
    fill = executor.execute(order)

    assert fill.ok
    assert fill.price < order.reference_price
    assert fill.token_amount == pytest.approx(10_000.0)
    assert fill.usd_amount == pytest.approx(10_000.0 * fill.price)
    assert fill.cash_delta == pytest.approx(fill.usd_amount - fill.fee_usd)


def test_thin_liquidity_produces_more_slippage(executor):
    deep = executor.execute(_order(liquidity=5_000_000.0))
    thin = executor.execute(_order(liquidity=60_000.0))
    assert thin.slippage_bps > deep.slippage_bps


def test_order_larger_than_tolerance_is_rejected(executor):
    # $5k into a $50k pool is ~20% impact - far past a 1.5% tolerance.
    fill = executor.execute(_order(usd=5_000.0, liquidity=50_000.0, slippage_bps=150))
    assert not fill.ok
    assert "slippage" in fill.error
    assert fill.cash_delta == 0.0


def test_simulated_transaction_failures_happen(config):
    executor = SimulatedExecutor(config, rng=random.Random(1), failure_rate=1.0)
    fill = executor.execute(_order())
    assert not fill.ok
    assert "failed" in fill.error


def test_missing_reference_price_is_rejected(executor):
    order = _order()
    order.reference_price = 0.0
    fill = executor.execute(order)
    assert not fill.ok
    assert fill.error == "no reference price"


def test_zero_size_orders_are_rejected(executor):
    assert not executor.execute(_order(usd=0.0)).ok
    assert not executor.execute(_order(side=Side.SELL, usd=0.0, tokens=0.0)).ok


def test_unknown_pool_depth_assumes_thin_market(config):
    executor = SimulatedExecutor(config, rng=random.Random(3))
    order = Order(token=Token("mint", "X"), side=Side.BUY, reference_price=1.0,
                  usd_amount=50.0, slippage_bps=300)
    fill = executor.execute(order)
    assert fill.ok
    assert fill.slippage_bps >= 100  # the 1% default impact floor


def test_seeded_runs_are_reproducible(config):
    a = SimulatedExecutor(config, rng=random.Random(99)).execute(_order())
    b = SimulatedExecutor(config, rng=random.Random(99)).execute(_order())
    assert a.price == b.price
    assert a.slippage_bps == b.slippage_bps
