import pytest

from memebot.config import FilterConfig
from memebot.models import USDC_MINT
from memebot.strategy.filters import CandidateFilter
from tests.conftest import make_pair


@pytest.fixture
def filt() -> CandidateFilter:
    return CandidateFilter(FilterConfig())


def test_a_healthy_pair_passes(filt):
    assert filt.check(make_pair()) is None


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        ({"liquidity": 5_000.0}, "liquidity too low"),
        ({"liquidity": 20_000_000.0}, "liquidity too high"),
        ({"vol_h1": 1_000.0}, "1h volume too low"),
        ({"vol_h24": 10_000.0}, "24h volume too low"),
        ({"age_minutes": 3.0}, "pair too new"),
        ({"age_minutes": 60 * 24 * 400}, "pair too old"),
        ({"fdv": 900_000_000.0}, "fdv too high"),
        ({"buys_h1": 5, "sells_h1": 5}, "too few trades"),
        ({"buys_h1": 200, "sells_h1": 800}, "sell pressure"),
        ({"chg_h1": 900.0}, "already parabolic (1h)"),
        ({"chg_h24": 5000.0}, "already parabolic (24h)"),
        ({"price": 0.0}, "no price"),
    ],
)
def test_unsafe_pairs_are_rejected(filt, kwargs, expected):
    assert filt.check(make_pair(**kwargs)) == expected


def test_liquidity_to_fdv_ratio_guard(filt):
    # $30k of liquidity behind a $40m valuation: nobody is getting out.
    pair = make_pair(liquidity=30_000.0, fdv=40_000_000.0)
    assert filt.check(pair) == "liquidity/fdv too thin"


def test_quote_token_whitelist():
    cfg = FilterConfig(allowed_quote_mints=[USDC_MINT])
    assert CandidateFilter(cfg).check(make_pair()) == "unsupported quote token"


def test_dex_whitelist():
    cfg = FilterConfig(allowed_dex_ids=["orca"])
    assert CandidateFilter(cfg).check(make_pair(dex_id="raydium")) == "unsupported dex"


def test_blacklists():
    by_mint = FilterConfig(blacklist_mints=["mint-WIF"])
    assert CandidateFilter(by_mint).check(make_pair("WIF")) == "blacklisted mint"

    by_symbol = FilterConfig(blacklist_symbols=["wif"])
    assert CandidateFilter(by_symbol).check(make_pair("WIF")) == "blacklisted symbol"


def test_apply_partitions_and_counts(filt):
    pairs = [
        make_pair("GOOD"),
        make_pair("THIN", liquidity=1_000.0),
        make_pair("QUIET", vol_h1=10.0),
        make_pair("ALSOTHIN", liquidity=900.0),
    ]
    result = filt.apply(pairs)

    assert [p.base.symbol for p in result.passed] == ["GOOD"]
    assert result.rejections["liquidity too low"] == 2
    assert result.detail["mint-QUIET"] == "1h volume too low"
    assert "liquidity too low=2" in result.summary()
