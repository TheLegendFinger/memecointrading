import time

from memebot.models import Fill, Order, Position, Side, Token
from tests.conftest import make_pair


def test_pair_accessors():
    pair = make_pair(buys_h1=600, sells_h1=400)
    assert pair.vol("h1") == 140_000
    assert pair.change("m5") == 6.0
    assert pair.trades("h1") == 1000
    assert pair.buy_ratio("h1") == 0.6
    assert 239 < pair.age_minutes < 241


def test_buy_ratio_without_data_is_neutral():
    pair = make_pair()
    pair.txns = {}
    assert pair.buy_ratio("h1") == 0.5


def test_position_marking_tracks_high_and_drawdown():
    token = Token("mint", "WIF")
    pos = Position(token=token, quantity=100, avg_price=1.0, cost_usd=100.0)
    pos.mark(2.0)
    pos.mark(1.5)
    assert pos.high_price == 2.0
    assert pos.last_price == 1.5
    assert pos.drawdown_from_high == 0.25
    assert pos.market_value == 150.0
    assert pos.unrealized_pnl_usd == 50.0
    assert pos.unrealized_pnl_pct == 0.5


def test_fill_cash_delta_signs():
    token = Token("mint", "WIF")
    buy = Fill(
        order=Order(token=token, side=Side.BUY, reference_price=1.0, usd_amount=100),
        ok=True, price=1.0, token_amount=100, usd_amount=100.0, fee_usd=1.0,
    )
    assert buy.cash_delta == -101.0

    sell = Fill(
        order=Order(token=token, side=Side.SELL, reference_price=1.0, token_amount=100),
        ok=True, price=1.0, token_amount=100, usd_amount=100.0, fee_usd=1.0,
    )
    assert sell.cash_delta == 99.0

    failed = Fill(order=buy.order, ok=False, error="nope")
    assert failed.cash_delta == 0.0
