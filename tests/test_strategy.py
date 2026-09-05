import pytest

from memebot.config import StrategyConfig
from memebot.models import Position, Side, Token
from memebot.strategy import build_strategy
from memebot.strategy.momentum import MomentumStrategy
from tests.conftest import make_pair


@pytest.fixture
def strategy() -> MomentumStrategy:
    return MomentumStrategy(StrategyConfig())


def test_build_strategy_by_name():
    assert isinstance(build_strategy("momentum", StrategyConfig()), MomentumStrategy)
    with pytest.raises(ValueError):
        build_strategy("does-not-exist", StrategyConfig())


def test_scores_are_bounded(strategy):
    hot = make_pair(chg_m5=30.0, chg_h1=90.0, vol_h1=900_000, vol_h24=1_000_000,
                    buys_m5=95, sells_m5=5, liquidity=800_000)
    cold = make_pair(chg_m5=-9.0, chg_h1=-30.0, vol_h1=1_000, vol_h24=900_000,
                     buys_m5=5, sells_m5=95, liquidity=26_000)
    assert 0.0 <= strategy.score(cold) < strategy.score(hot) <= 1.0


def test_stronger_momentum_scores_higher(strategy):
    weak = make_pair(chg_m5=1.0, chg_h1=3.0)
    strong = make_pair(chg_m5=10.0, chg_h1=45.0)
    assert strategy.score(strong) > strategy.score(weak)


def test_volume_surge_lifts_the_score(strategy):
    quiet = make_pair(vol_h1=40_000, vol_h24=960_000)   # 1x its hourly average
    surging = make_pair(vol_h1=240_000, vol_h24=960_000)  # 6x
    assert strategy.score(surging) > strategy.score(quiet)


def test_buy_pressure_lifts_the_score(strategy):
    balanced = make_pair(buys_m5=50, sells_m5=50, buys_h1=500, sells_h1=500)
    buying = make_pair(buys_m5=85, sells_m5=15, buys_h1=750, sells_h1=250)
    assert strategy.score(buying) > strategy.score(balanced)


def test_falling_5m_candle_is_penalised(strategy):
    rising = make_pair(chg_m5=5.0)
    falling = make_pair(chg_m5=-5.0)
    assert strategy.score(falling) < 0.5 * strategy.score(rising) + 1e-9


def test_generate_entries_filters_and_ranks(strategy):
    strategy.cfg.min_score = 0.4
    pairs = [
        make_pair("MID", chg_m5=5.0, chg_h1=20.0),
        make_pair("BEST", chg_m5=12.0, chg_h1=55.0, vol_h1=400_000, vol_h24=960_000,
                  buys_m5=90, sells_m5=10, liquidity=700_000),
        make_pair("DEAD", chg_m5=-6.0, chg_h1=-20.0, vol_h1=41_000, vol_h24=980_000,
                  buys_m5=10, sells_m5=90),
    ]
    signals = strategy.generate_entries(pairs)

    assert signals, "expected at least one entry signal"
    assert signals[0].token.symbol == "BEST"
    assert all(s.side is Side.BUY for s in signals)
    assert all(s.score >= 0.4 for s in signals)
    assert [s.score for s in signals] == sorted((s.score for s in signals), reverse=True)
    assert "DEAD" not in [s.token.symbol for s in signals]


def test_min_score_gate_can_reject_everything(strategy):
    strategy.cfg.min_score = 0.99
    assert strategy.generate_entries([make_pair()]) == []


def test_exit_on_momentum_reversal(strategy):
    pos = Position(token=Token("mint-WIF", "WIF"), quantity=100, avg_price=1.0, cost_usd=100.0)
    assert strategy.should_exit(pos, make_pair(chg_m5=3.0)) is None
    reason = strategy.should_exit(pos, make_pair(chg_m5=-15.0))
    assert reason and "momentum reversed" in reason


def test_exit_on_dominant_sell_flow(strategy):
    pos = Position(token=Token("mint-WIF", "WIF"), quantity=100, avg_price=1.0, cost_usd=100.0)
    reason = strategy.should_exit(pos, make_pair(chg_m5=-2.0, buys_m5=5, sells_m5=95))
    assert reason and "sell flow dominant" in reason


def test_no_market_data_is_not_an_exit_signal(strategy):
    pos = Position(token=Token("mint-WIF", "WIF"), quantity=100, avg_price=1.0, cost_usd=100.0)
    assert strategy.should_exit(pos, None) is None
