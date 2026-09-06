"""Learning a tilt from closed trades - and, mostly, refusing to.

Thirty memecoin trades is a violent sample. The dangerous failure here is not
"it learns nothing", it is "it learns a lucky streak and bets the wallet on
it", so most of these tests are about the guardrails holding.
"""

import time

import pytest

from memebot.config import BotConfig, LearningConfig
from memebot.learning import TradeLearner, entry_features
from memebot.models import PairSnapshot, Token
from memebot.storage import Storage


def make_pair(symbol="DOG", liquidity=100_000.0, m5=2.0, h1=10.0, source="search",
              age_minutes=300.0):
    return PairSnapshot(
        chain_id="solana", dex_id="raydium", pair_address=f"pool-{symbol}",
        base=Token(address=f"mint-{symbol}", symbol=symbol, name=symbol),
        quote=Token(address="So11111111111111111111111111111111111111112", symbol="SOL"),
        price_usd=0.001, liquidity_usd=liquidity,
        volume={"h1": 50_000.0, "h24": 900_000.0},
        price_change={"m5": m5, "h1": h1},
        txns={"h1": {"buys": 600, "sells": 400}},
        pair_created_at=time.time() - age_minutes * 60,
        source=source,
    )


@pytest.fixture
def storage():
    store = Storage(":memory:")
    yield store
    store.close()


def log_trade(storage, features, return_pct, opened_at, symbol="DOG"):
    token = f"mint-{symbol}-{opened_at}"
    storage.record_entry(token_address=token, opened_at=opened_at, symbol=symbol,
                         score=0.6, source=features.get("source", ""),
                         features=features, cost_usd=20.0)
    storage.record_exit(token, opened_at + 600, return_pct, "take profit")


def fill_history(storage, count, features, return_pct, start=1000.0):
    for i in range(count):
        log_trade(storage, features, return_pct, start + i)


# ---- recording -------------------------------------------------------------------
def test_an_entry_is_recorded_with_the_shape_it_was_bought_on(storage):
    features = entry_features(make_pair(liquidity=200_000.0, m5=5.0), "trending")
    storage.record_entry("mint-1", 100.0, "DOG", 0.71, "trending", features, 20.0)

    assert storage.closed_outcomes() == [], "not finished, so not learned from yet"

    assert storage.record_exit("mint-1", 700.0, 0.25, "take profit") is True
    row = storage.closed_outcomes()[0]
    assert row["return_pct"] == 0.25
    assert row["features"]["liquidity"] == "liq150-500k"
    assert row["features"]["m5"] == "m5 3-8%"
    assert row["features"]["source"] == "found by trending"


def test_an_exit_with_no_matching_entry_is_not_invented(storage):
    assert storage.record_exit("mint-unknown", 1.0, 0.5, "stop") is False


def test_only_the_open_entry_is_closed(storage):
    """The same coin can be traded twice; the second exit must not rewrite the
    first trade's result."""
    storage.record_entry("mint-1", 100.0, "DOG", 0.6, "search", {}, 10.0)
    storage.record_exit("mint-1", 200.0, 0.10, "take profit")
    storage.record_entry("mint-1", 300.0, "DOG", 0.6, "search", {}, 10.0)
    storage.record_exit("mint-1", 400.0, -0.20, "stop loss")

    returns = sorted(r["return_pct"] for r in storage.closed_outcomes())
    assert returns == [-0.20, 0.10]


# ---- the guardrails --------------------------------------------------------------
def test_nothing_is_applied_before_the_minimum_number_of_trades(storage):
    good = entry_features(make_pair(), "trending")
    fill_history(storage, 10, good, 0.40)          # a perfect record
    learner = TradeLearner(storage, LearningConfig(min_trades=30))

    score, why = learner.adjust(0.60, make_pair(), "trending")

    assert score == 0.60 and why == ""


def test_a_lucky_streak_barely_moves_anything(storage):
    """Five wins in a bucket is not evidence, and must not read as one."""
    winners = entry_features(make_pair(source="trending"), "trending")
    losers = entry_features(make_pair(source="search"), "search")
    fill_history(storage, 5, winners, 0.50)
    fill_history(storage, 30, losers, -0.05, start=5000.0)

    learner = TradeLearner(storage, LearningConfig(min_trades=10, min_bucket_trades=4))
    score, _ = learner.adjust(0.60, make_pair(source="trending"), "trending")

    raw_edge = 0.50 - (5 * 0.50 + 30 * -0.05) / 35
    assert 0 < score - 0.60 < raw_edge / 2, "the edge must be shrunk, hard"


def test_a_bucket_with_almost_no_data_is_ignored_entirely(storage):
    winners = entry_features(make_pair(source="trending"), "trending")
    fill_history(storage, 2, winners, 0.90)
    fill_history(storage, 40, entry_features(make_pair(source="search"), "search"),
                 0.0, start=5000.0)

    learner = TradeLearner(storage, LearningConfig(min_trades=10, min_bucket_trades=4))
    score, why = learner.adjust(0.60, make_pair(source="trending"), "trending")

    assert score == 0.60 and why == ""


def test_the_tilt_is_hard_capped(storage):
    """However lopsided the history, this tilts the strategy; it never replaces it."""
    fill_history(storage, 60, entry_features(make_pair(source="trending"), "trending"), 5.00)
    fill_history(storage, 60, entry_features(make_pair(source="search"), "search"),
                 -0.50, start=5000.0)
    learner = TradeLearner(storage, LearningConfig(min_trades=10, max_adjustment=0.10))

    best, _ = learner.adjust(0.60, make_pair(source="trending"), "trending")
    worst, _ = learner.adjust(0.60, make_pair(source="search"), "search")

    assert best == pytest.approx(0.70), "+500% a trade still only buys the cap"
    assert worst == pytest.approx(0.50)


def test_a_history_with_nothing_to_compare_tilts_nothing(storage):
    """Every trade identical and profitable says which entries are better than
    which - nothing. An edge is relative or it is not an edge."""
    fill_history(storage, 60, entry_features(make_pair(source="trending"), "trending"), 5.00)
    learner = TradeLearner(storage, LearningConfig(min_trades=10))

    assert learner.adjust(0.60, make_pair(source="trending"), "trending") == (0.60, "")


def test_a_bad_bucket_tilts_downwards(storage):
    thin = entry_features(make_pair(liquidity=10_000.0), "search")
    deep = entry_features(make_pair(liquidity=600_000.0), "search")
    fill_history(storage, 30, thin, -0.30)
    fill_history(storage, 30, deep, 0.30, start=5000.0)

    learner = TradeLearner(storage, LearningConfig(min_trades=10))
    worse, why = learner.adjust(0.60, make_pair(liquidity=10_000.0), "search")
    better, _ = learner.adjust(0.60, make_pair(liquidity=600_000.0), "search")

    assert worse < 0.60 < better
    assert "learned" in why


def test_learning_can_be_switched_off(storage):
    fill_history(storage, 60, entry_features(make_pair(), "trending"), 0.50)
    learner = TradeLearner(storage, LearningConfig(enabled=False, min_trades=1))

    assert learner.adjust(0.60, make_pair(), "trending") == (0.60, "")


def test_the_score_stays_inside_zero_and_one(storage):
    fill_history(storage, 60, entry_features(make_pair()), 5.0)
    learner = TradeLearner(storage, LearningConfig(min_trades=10))
    assert learner.adjust(0.98, make_pair())[0] <= 1.0


def test_a_broken_database_never_stops_trading(storage):
    class Broken:
        def closed_outcomes(self, limit=500):
            raise RuntimeError("the disk is gone")

    learner = TradeLearner(Broken(), LearningConfig(min_trades=1))
    assert learner.adjust(0.60, make_pair()) == (0.60, "")


# ---- the report ------------------------------------------------------------------
def test_the_report_shows_the_evidence_not_just_the_verdict(storage):
    fill_history(storage, 20, entry_features(make_pair(source="trending"), "trending"), 0.20)
    fill_history(storage, 20, entry_features(make_pair(source="search"), "search"),
                 -0.10, start=5000.0)

    report = TradeLearner(storage, LearningConfig(min_trades=10)).report()

    assert report.trades == 40
    assert report.active is True
    assert report.win_rate == pytest.approx(0.5)
    by_bucket = {b.bucket: b for b in report.buckets}
    assert by_bucket["found by trending"].trades == 20
    assert by_bucket["found by trending"].mean_return == pytest.approx(0.20)
    assert by_bucket["found by trending"].adjustment > 0
    assert by_bucket["found by search"].adjustment < 0


def test_the_report_says_how_far_off_it_is_when_still_recording(storage):
    fill_history(storage, 3, entry_features(make_pair()), 0.1)
    report = TradeLearner(storage, LearningConfig(min_trades=30)).report()
    assert report.trades == 3 and report.active is False


# ---- end to end through the engine -----------------------------------------------
def test_a_closed_trade_lands_in_the_learning_history(config):
    """The whole loop: buy, record the shape, sell, record the result."""
    from test_engine import build_engine, make_pair as engine_pair

    hot = engine_pair("BEST", chg_m5=12.0, chg_h1=55.0, vol_m5=60_000, vol_h1=400_000,
                      vol_h24=960_000, buys_m5=90, sells_m5=10, buys_h1=800, sells_h1=200,
                      liquidity=700_000, price=0.01)
    # A fixed seed: the simulated executor jitters slippage, and a random revert
    # would make this test flake rather than fail.
    engine, _ = build_engine(config, [hot], seed=11)
    engine.run_cycle()
    assert engine.portfolio.open_positions, "a position to close"

    engine.liquidate_all()

    closed = engine.storage.closed_outcomes()
    assert len(closed) == 1
    assert closed[0]["symbol"] == "BEST"
    assert closed[0]["features"], "the shape it was bought on was kept"
    assert closed[0]["exit_reason"]
