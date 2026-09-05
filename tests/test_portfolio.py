import pytest

from memebot.models import Fill, Order, Side, Token
from memebot.portfolio import Portfolio
from tests.conftest import fund
from memebot.storage import Storage

TOKEN = Token("mint-wif", "WIF")


def _buy(portfolio, price=1.0, tokens=100.0, fee=1.0):
    order = Order(token=TOKEN, side=Side.BUY, reference_price=price, usd_amount=price * tokens)
    fill = Fill(order=order, ok=True, price=price, token_amount=tokens,
                usd_amount=price * tokens, fee_usd=fee)
    return portfolio.apply_fill(fill)


def _sell(portfolio, price=1.0, tokens=100.0, fee=1.0):
    order = Order(token=TOKEN, side=Side.SELL, reference_price=price, token_amount=tokens)
    fill = Fill(order=order, ok=True, price=price, token_amount=tokens,
                usd_amount=price * tokens, fee_usd=fee)
    return portfolio.apply_fill(fill)


def test_buy_reduces_cash_and_opens_position(storage):
    p = fund(Portfolio(storage))
    _buy(p, price=1.0, tokens=100.0, fee=1.0)

    assert p.cash == pytest.approx(899.0)
    pos = p.position(TOKEN.address)
    assert pos.quantity == 100.0
    assert pos.cost_usd == pytest.approx(101.0)
    assert pos.avg_price == 1.0


def test_full_exit_realizes_pnl_and_closes_position(storage):
    p = fund(Portfolio(storage))
    _buy(p, price=1.0, tokens=100.0, fee=1.0)
    realized = _sell(p, price=1.5, tokens=100.0, fee=1.5)

    # proceeds 150 - 1.5 fee = 148.5, cost basis 101 -> +47.5
    assert realized == pytest.approx(47.5)
    assert p.position(TOKEN.address) is None
    assert p.cash == pytest.approx(1_047.5)
    assert p.equity == pytest.approx(1_047.5)


def test_partial_exit_keeps_proportional_cost_basis(storage):
    p = fund(Portfolio(storage))
    _buy(p, price=1.0, tokens=100.0, fee=1.0)
    realized = _sell(p, price=2.0, tokens=40.0, fee=0.8)

    pos = p.position(TOKEN.address)
    assert pos.quantity == pytest.approx(60.0)
    assert pos.cost_usd == pytest.approx(101.0 * 0.6)
    # proceeds 80 - 0.8 = 79.2 against a 40.4 basis
    assert realized == pytest.approx(79.2 - 40.4)
    assert pos.realized_pnl_usd == pytest.approx(realized)


def test_averaging_up_recomputes_avg_price(storage):
    p = fund(Portfolio(storage))
    _buy(p, price=1.0, tokens=100.0, fee=1.0)
    _buy(p, price=2.0, tokens=100.0, fee=2.0)

    pos = p.position(TOKEN.address)
    assert pos.quantity == 200.0
    assert pos.cost_usd == pytest.approx(303.0)  # 300 notional + 3 in fees
    assert pos.avg_price == pytest.approx(1.515)


def test_marking_updates_equity_and_high_water(storage):
    p = fund(Portfolio(storage))
    _buy(p, price=1.0, tokens=100.0, fee=1.0)
    p.mark(TOKEN.address, 3.0, liquidity_usd=200_000)
    p.mark(TOKEN.address, 2.0)

    pos = p.position(TOKEN.address)
    assert pos.high_price == 3.0
    assert pos.peak_liquidity_usd == 200_000
    assert p.positions_value == pytest.approx(200.0)
    assert p.equity == pytest.approx(1_099.0)
    assert p.unrealized_pnl_usd == pytest.approx(99.0)


def test_failed_fills_change_nothing(storage):
    p = fund(Portfolio(storage))
    order = Order(token=TOKEN, side=Side.BUY, reference_price=1.0, usd_amount=100)
    assert p.apply_fill(Fill(order=order, ok=False, error="boom")) == 0.0
    assert p.cash == 1_000.0
    assert not p.positions


def test_state_survives_a_restart(tmp_path):
    db = str(tmp_path / "state.sqlite3")
    store = Storage(db)
    p = fund(Portfolio(store))
    _buy(p, price=1.0, tokens=100.0, fee=1.0)
    p.mark(TOKEN.address, 1.25)
    store.close()

    reopened = Portfolio(Storage(db))
    assert reopened.cash == pytest.approx(899.0)
    pos = reopened.position(TOKEN.address)
    assert pos is not None and pos.quantity == 100.0
    assert reopened.equity == pytest.approx(899.0 + 125.0)


def test_stats_report_win_rate(storage):
    p = fund(Portfolio(storage))
    _buy(p, price=1.0, tokens=100.0, fee=1.0)
    _sell(p, price=1.5, tokens=100.0, fee=1.0)
    _buy(p, price=1.0, tokens=100.0, fee=1.0)
    _sell(p, price=0.5, tokens=100.0, fee=1.0)

    stats = p.stats()
    assert stats["closed_trades"] == 2
    assert stats["wins"] == 1 and stats["losses"] == 1
    assert stats["win_rate"] == 0.5
    assert stats["total_fees_usd"] == pytest.approx(4.0)


# ---- there is no configured bankroll -------------------------------------------
def test_a_fresh_portfolio_has_nothing_to_spend(storage):
    """No configured starting cash: until the wallet is read, there is nothing.

    Being wrong in this direction is safe - the bot cannot size a trade against
    money it has not seen.
    """
    p = Portfolio(storage)
    assert p.cash == 0.0
    assert p.starting_cash == 0.0
    assert p.equity == 0.0


def test_the_wallet_read_becomes_the_bankroll(storage):
    p = Portfolio(storage)
    p.set_cash(87.40)
    p.set_starting_cash(87.40)

    assert p.cash == pytest.approx(87.40)
    assert p.total_return_pct == pytest.approx(0.0)


def test_a_zero_baseline_does_not_divide_by_zero(storage):
    assert Portfolio(storage).total_return_pct == 0.0
