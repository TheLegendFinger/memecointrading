"""What the momentum score actually comes out at.

An earlier version normalised every component against once-a-week extremes, so
a textbook entry - up 3.5% on the 5m, up 18% on the hour, three times its own
average volume, two buyers for every seller - scored 0.39 against a min_score
of 0.62. Nothing could ever clear the bar, and the health check just said
"lower min_score to trade" without saying to what.

These archetypes pin the scale so that reading is possible again. They are
descriptions of shapes, not predictions: the claim is only that a coin which
is clearly running scores higher than one that is drifting, by a margin worth
putting a threshold in.
"""

import time

import pytest

from memebot.config import StrategyConfig
from memebot.models import PairSnapshot, Token
from memebot.strategy.momentum import MomentumStrategy


def make_pair(m5, h1, h24, vol_h1, vol_h24, buys_m5, sells_m5, buys_h1, sells_h1, liq):
    return PairSnapshot(
        chain_id="solana",
        dex_id="raydium",
        pair_address="pair",
        base=Token(address="base", symbol="X"),
        quote=Token(address="quote", symbol="SOL"),
        price_usd=1.0,
        liquidity_usd=liq,
        volume={"m5": vol_h1 / 12, "h1": vol_h1, "h24": vol_h24},
        price_change={"m5": m5, "h1": h1, "h24": h24},
        txns={"m5": {"buys": buys_m5, "sells": sells_m5},
              "h1": {"buys": buys_h1, "sells": sells_h1}},
        pair_created_at=time.time() - 6 * 3600,
    )


# name: (pair, lowest acceptable score, highest acceptable score)
ARCHETYPES = {
    # Up 40% on the hour, volume 4x its own average, buyers 3:1.
    "ripping": (make_pair(6.0, 40, 120, 400_000, 1_500_000, 90, 30, 900, 400, 300_000), 0.85, 1.0),
    # The textbook entry this strategy exists to find.
    "strong": (make_pair(3.5, 18, 45, 180_000, 1_400_000, 60, 30, 700, 450, 200_000), 0.60, 0.80),
    # Moving up, hotter than usual, more buyers than sellers.
    "decent": (make_pair(2.5, 12, 30, 130_000, 1_300_000, 50, 32, 650, 470, 150_000), 0.38, 0.58),
    "warming up": (make_pair(1.8, 8, 15, 90_000, 1_200_000, 40, 30, 600, 500, 120_000), 0.22, 0.40),
    "drifting up": (make_pair(0.6, 2.5, 5, 40_000, 900_000, 25, 24, 500, 480, 80_000), 0.0, 0.15),
    "flat": (make_pair(0.1, 0.4, 1, 30_000, 800_000, 20, 20, 500, 500, 60_000), 0.0, 0.10),
    "fading": (make_pair(-1.5, 4, 20, 50_000, 1_000_000, 15, 30, 500, 480, 90_000), 0.0, 0.10),
    "dumping": (make_pair(-6.0, -20, -35, 120_000, 2_000_000, 10, 60, 300, 900, 100_000), 0.0, 0.05),
}

ORDER = ["ripping", "strong", "decent", "warming up", "drifting up", "flat", "fading", "dumping"]


@pytest.fixture
def strategy():
    return MomentumStrategy(StrategyConfig())


@pytest.mark.parametrize("name", list(ARCHETYPES))
def test_each_archetype_scores_in_its_band(strategy, name):
    pair, low, high = ARCHETYPES[name]
    score = strategy.score(pair)
    assert low <= score <= high, f"{name} scored {score:.3f}, expected {low}-{high}"


def test_the_ranking_is_the_one_a_human_would_give(strategy):
    scores = [strategy.score(ARCHETYPES[name][0]) for name in ORDER]
    assert scores == sorted(scores, reverse=True), dict(zip(ORDER, scores))


def test_the_default_threshold_admits_a_textbook_entry(strategy):
    """The bug: a default min_score nothing could reach meant it never traded."""
    cfg = StrategyConfig()
    assert strategy.score(ARCHETYPES["strong"][0]) >= cfg.min_score
    assert strategy.score(ARCHETYPES["warming up"][0]) < cfg.min_score, "still picky"


def test_a_coin_going_down_is_never_a_buy(strategy):
    for name in ("fading", "dumping"):
        assert strategy.score(ARCHETYPES[name][0]) < 0.10
