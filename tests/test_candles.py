"""Candle tests - the chart is only as honest as these."""

import pytest

from memebot.data.candles import (
    Candle, GeckoTerminalClient, TIMEFRAMES, bucket_seconds, candles_from_samples,
)
from memebot.http import HttpError
from tests.test_data import FakeHttp


# ---- building candles from our own samples -------------------------------------
def test_samples_are_bucketed_into_ohlc():
    # Three samples inside one 5m bucket, rising then falling.
    samples = [
        {"ts": 0, "price": 1.0},
        {"ts": 60, "price": 1.5},
        {"ts": 120, "price": 1.2},
    ]
    candles = candles_from_samples(samples, "5m")

    assert len(candles) == 1
    candle = candles[0]
    assert (candle.open, candle.high, candle.low, candle.close) == (1.0, 1.5, 1.0, 1.2)


def test_samples_split_across_buckets():
    samples = [{"ts": 0, "price": 1.0}, {"ts": 400, "price": 2.0}]
    candles = candles_from_samples(samples, "5m")

    assert [c.ts for c in candles] == [0.0, 300.0]
    assert candles[1].open == candles[1].close == 2.0


def test_a_lone_sample_makes_a_flat_candle():
    """One observation is one price - not a fabricated range."""
    candle = candles_from_samples([{"ts": 10, "price": 3.0}])[0]
    assert candle.open == candle.high == candle.low == candle.close == 3.0


def test_candles_come_out_in_time_order():
    samples = [{"ts": 900, "price": 3.0}, {"ts": 0, "price": 1.0}, {"ts": 300, "price": 2.0}]
    candles = candles_from_samples(samples, "5m")
    assert [c.ts for c in candles] == sorted(c.ts for c in candles)


def test_junk_samples_are_skipped_not_fatal():
    samples = [
        {"ts": 0, "price": 1.0},
        {"ts": "nonsense", "price": 2.0},
        {"ts": 60, "price": 0.0},      # a zero price is not a price
        {"ts": 120, "price": None},
        {"nope": 1},
        {"ts": 180, "price": 1.4},
    ]
    candles = candles_from_samples(samples, "5m")
    assert len(candles) == 1
    assert (candles[0].low, candles[0].high) == (1.0, 1.4)


def test_the_limit_keeps_the_most_recent_candles():
    samples = [{"ts": i * 300, "price": float(i + 1)} for i in range(50)]
    candles = candles_from_samples(samples, "5m", limit=10)
    assert len(candles) == 10
    assert candles[-1].close == 50.0


@pytest.mark.parametrize("timeframe, seconds", [(k, v[2]) for k, v in TIMEFRAMES.items()])
def test_every_timeframe_has_a_bucket_size(timeframe, seconds):
    assert bucket_seconds(timeframe) == seconds


def test_an_unknown_timeframe_falls_back_to_five_minutes():
    assert bucket_seconds("banana") == 300


def test_no_samples_means_no_candles():
    assert candles_from_samples([]) == []


# ---- GeckoTerminal -------------------------------------------------------------
def gecko_payload(rows):
    return {"data": {"attributes": {"ohlcv_list": rows}}}


def test_ohlcv_is_parsed_and_ordered_oldest_first():
    # The API returns newest first; the chart needs the opposite.
    http = FakeHttp({"/ohlcv/": gecko_payload([
        [1_700_000_600, 2.0, 2.5, 1.9, 2.4, 5000],
        [1_700_000_300, 1.0, 1.8, 0.9, 1.7, 4000],
    ])})
    candles = GeckoTerminalClient(http=http).ohlcv("pool-1", "5m")

    assert [c.ts for c in candles] == [1_700_000_300, 1_700_000_600]
    assert candles[0].open == 1.0 and candles[0].close == 1.7
    assert candles[1].volume == 5000


def test_the_timeframe_maps_to_unit_and_aggregate():
    http = FakeHttp({"/ohlcv/": gecko_payload([])})
    GeckoTerminalClient(http=http).ohlcv("pool-1", "15m")

    path, params = http.calls[-1]
    assert path.endswith("/ohlcv/minute")
    assert params["aggregate"] == 15


def test_a_failing_api_returns_no_candles_rather_than_raising():
    http = FakeHttp({"/ohlcv/": HttpError("429 Too Many Requests", 429)})
    assert GeckoTerminalClient(http=http).ohlcv("pool-1") == []


def test_malformed_rows_are_skipped():
    http = FakeHttp({"/ohlcv/": gecko_payload([
        [1_700_000_300, 1.0, 1.8, 0.9, 1.7, 4000],
        "not a row",
        [1_700_000_600, "x", "y", "z", "w"],
        [1_700_000_900],
        [1_700_001_200, 1.0, 1.0, 1.0, 0.0],   # a zero close is not a price
    ])})
    candles = GeckoTerminalClient(http=http).ohlcv("pool-1")
    assert len(candles) == 1


def test_results_are_cached_within_the_ttl():
    http = FakeHttp({"/ohlcv/": gecko_payload([[1, 1.0, 1.0, 1.0, 1.0, 0]])})
    client = GeckoTerminalClient(http=http, cache_ttl_seconds=60)
    client.ohlcv("pool-1")
    client.ohlcv("pool-1")
    assert len(http.calls) == 1, "the free tier is rate limited; do not spend calls twice"


def test_an_empty_pool_address_makes_no_request():
    http = FakeHttp({})
    assert GeckoTerminalClient(http=http).ohlcv("") == []
    assert http.calls == []


def test_candle_serialises_for_the_api():
    payload = Candle(ts=1.0, open=2.0, high=3.0, low=1.5, close=2.5, volume=10.0).as_dict()
    assert payload == {"ts": 1.0, "open": 2.0, "high": 3.0, "low": 1.5, "close": 2.5, "volume": 10.0}


# ---- fitting the bucket to the data --------------------------------------------
def test_median_spacing_is_the_polling_interval():
    from memebot.data.candles import median_spacing

    samples = [{"ts": i * 30} for i in range(10)]
    assert median_spacing(samples) == 30.0
    assert median_spacing([]) == 0.0
    assert median_spacing([{"ts": 5}]) == 0.0


def test_a_slow_poller_widens_the_bucket():
    """One sample per 5m bucket would draw flat dashes, not candles."""
    from memebot.data.candles import fit_bucket

    slow = [{"ts": i * 300, "price": 1.0} for i in range(96)]
    fitted = fit_bucket(slow, "5m")

    assert fitted >= 900, "a 5-minute poll needs buckets several times wider"
    candles = candles_from_samples(slow, "5m", seconds=fitted)
    assert len(candles) < len(slow)


def test_a_freshly_started_bot_narrows_the_bucket():
    """Thirty seconds of history in 5m buckets is one candle - useless."""
    from memebot.data.candles import fit_bucket

    fresh = [{"ts": i * 2, "price": 1.0 + (i % 5) * 0.02} for i in range(16)]
    fitted = fit_bucket(fresh, "5m")

    assert fitted < 300, "a chart should not be a single candle while it warms up"
    assert len(candles_from_samples(fresh, "5m", seconds=fitted)) >= 4


def test_the_normal_case_is_left_alone():
    """30s polling over a few hours is exactly what 5m candles are for."""
    from memebot.data.candles import fit_bucket, label_for_seconds

    normal = [{"ts": i * 30, "price": 1.0} for i in range(360)]
    fitted = fit_bucket(normal, "5m")
    assert label_for_seconds(fitted) == "5m"


def test_bucket_fitting_never_returns_zero():
    from memebot.data.candles import fit_bucket

    for samples in ([], [{"ts": 1, "price": 1.0}], [{"ts": 1}, {"ts": 1}]):
        assert fit_bucket(samples, "5m") > 0


def test_labels_describe_the_bucket_honestly():
    from memebot.data.candles import label_for_seconds

    assert label_for_seconds(300) == "5m"
    assert label_for_seconds(900) == "15m"
    assert label_for_seconds(3600) == "1h"
    assert label_for_seconds(269) == "5m"      # close enough to say 5m
    assert label_for_seconds(1800) == "30m"    # no standard label; say what it is
