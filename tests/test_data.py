"""Data-layer tests. No network: a fake HttpClient replays canned payloads."""

import time

import pytest

from memebot.data.dexscreener import DexScreenerClient, parse_pair
from memebot.data.jupiter import JupiterClient
from memebot.data.jupiter import JupiterQuote
from memebot.http import HttpError, RateLimiter
from memebot.models import USDC_MINT, WSOL_MINT


class FakeHttp:
    """Minimal stand-in for HttpClient; routes are matched by substring."""

    def __init__(self, routes=None):
        self.routes = routes or {}
        self.calls = []

    def _resolve(self, path, params):
        self.calls.append((path, params))
        for fragment, response in self.routes.items():
            if fragment in path:
                if isinstance(response, Exception):
                    raise response
                return response(params) if callable(response) else response
        raise HttpError(f"no route for {path}", 404)

    def get(self, path, params=None, **_kw):
        return self._resolve(path, params)

    def post(self, path, json_body=None, **_kw):
        return self._resolve(path, json_body)


def raw_pair(symbol="WIF", address="mint-wif", price="0.0125", liquidity=250_000.0, chain="solana"):
    return {
        "chainId": chain,
        "dexId": "raydium",
        "pairAddress": f"pair-{symbol}",
        "baseToken": {"address": address, "symbol": symbol, "name": f"{symbol} coin"},
        "quoteToken": {"address": WSOL_MINT, "symbol": "SOL", "name": "Wrapped SOL"},
        "priceUsd": price,
        "priceNative": "0.00008",
        "liquidity": {"usd": liquidity, "base": 1, "quote": 2},
        "fdv": 9_000_000,
        "marketCap": 8_500_000,
        "volume": {"m5": 5_000, "h1": 90_000, "h6": 300_000, "h24": 800_000},
        "priceChange": {"m5": 3.2, "h1": 18.4, "h6": 40.0, "h24": 65.0},
        "txns": {"m5": {"buys": 40, "sells": 20}, "h1": {"buys": 500, "sells": 300}},
        "pairCreatedAt": int((time.time() - 7200) * 1000),
        "url": "https://dexscreener.com/solana/pair-WIF",
    }


# ---- parsing -------------------------------------------------------------------
def test_parse_pair_normalises_types():
    snap = parse_pair(raw_pair())
    assert snap.base.symbol == "WIF"
    assert snap.price_usd == 0.0125          # string in the payload
    assert snap.liquidity_usd == 250_000.0
    assert snap.vol("h1") == 90_000
    assert snap.change("m5") == 3.2
    assert snap.buys("h1") == 500
    assert 119 < snap.age_minutes < 121      # milliseconds -> seconds


def test_parse_pair_tolerates_missing_fields():
    snap = parse_pair({"baseToken": {"address": "abc"}, "chainId": "solana"})
    assert snap.price_usd == 0.0
    assert snap.liquidity_usd == 0.0
    assert snap.buy_ratio("h1") == 0.5
    assert snap.is_stale


def test_parse_pair_rejects_junk():
    assert parse_pair({}) is None
    assert parse_pair("not a dict") is None


# ---- DexScreener client --------------------------------------------------------
def test_search_filters_to_the_configured_chain():
    http = FakeHttp({"/latest/dex/search": {"pairs": [raw_pair(), raw_pair("ETHCOIN", "0xabc", chain="ethereum")]}})
    client = DexScreenerClient(http=http)
    results = client.search("WIF")
    assert [p.base.symbol for p in results] == ["WIF"]


def test_search_results_are_cached_within_the_ttl():
    http = FakeHttp({"/latest/dex/search": {"pairs": [raw_pair()]}})
    client = DexScreenerClient(http=http, cache_ttl_seconds=60)
    client.search("WIF")
    client.search("WIF")
    assert len(http.calls) == 1


def test_api_failures_degrade_to_empty_results():
    client = DexScreenerClient(http=FakeHttp({"/latest/dex/search": HttpError("boom", 500)}))
    assert client.search("WIF") == []
    assert client.price_usd("mint-wif") == 0.0


def test_best_pair_picks_the_deepest_pool():
    http = FakeHttp({"/token-pairs/v1": [
        raw_pair("WIF", "mint-wif", liquidity=50_000.0),
        raw_pair("WIF", "mint-wif", liquidity=900_000.0),
    ]})
    client = DexScreenerClient(http=http)
    assert client.best_pair("mint-wif").liquidity_usd == 900_000.0


def test_discover_dedupes_by_token_keeping_the_deepest_pool():
    http = FakeHttp({
        "/latest/dex/search": {"pairs": [
            raw_pair("WIF", "mint-wif", liquidity=100_000.0),
            raw_pair("WIF", "mint-wif", liquidity=400_000.0),
            raw_pair("BONK", "mint-bonk"),
        ]},
        "/token-boosts": [],
    })
    client = DexScreenerClient(http=http)
    found = client.discover(["WIF"], use_boosted_feed=False)
    assert len(found) == 2
    wif = next(p for p in found if p.base.symbol == "WIF")
    assert wif.liquidity_usd == 400_000.0


def test_discover_pulls_the_boosted_feed():
    http = FakeHttp({
        "/latest/dex/search": {"pairs": []},
        "/token-boosts": [
            {"chainId": "solana", "tokenAddress": "mint-boosted"},
            {"chainId": "ethereum", "tokenAddress": "0xnope"},
        ],
        "/token-pairs/v1": [raw_pair("BOOST", "mint-boosted")],
    })
    client = DexScreenerClient(http=http)
    found = client.discover([], use_boosted_feed=True)
    assert [p.base.symbol for p in found] == ["BOOST"]


def test_discover_respects_max_candidates():
    pairs = [raw_pair(f"C{i}", f"mint-{i}") for i in range(50)]
    http = FakeHttp({"/latest/dex/search": {"pairs": pairs}, "/token-boosts": []})
    client = DexScreenerClient(http=http)
    assert len(client.discover(["x"], use_boosted_feed=False, max_candidates=10)) == 10


# ---- Jupiter client ------------------------------------------------------------
def test_prices_are_parsed():
    http = FakeHttp({"price": {"data": {WSOL_MINT: {"id": WSOL_MINT, "price": "152.31"}}}})
    client = JupiterClient(http=http)
    assert client.price(WSOL_MINT) == pytest.approx(152.31)


def test_price_failures_return_nothing():
    client = JupiterClient(http=FakeHttp({"price": HttpError("rate limited", 429)}))
    assert client.prices([WSOL_MINT]) == {}


def test_quote_is_normalised():
    http = FakeHttp({"/quote": {
        "inputMint": WSOL_MINT, "inAmount": "1000000000",
        "outputMint": "mint-wif", "outAmount": "12345678",
        "otherAmountThreshold": "12000000", "priceImpactPct": "0.0042",
        "slippageBps": 150,
        "routePlan": [{"swapInfo": {"label": "Raydium"}}, {"swapInfo": {"label": "Orca"}}],
    }})
    client = JupiterClient(http=http)
    quote = client.quote(WSOL_MINT, "mint-wif", 1_000_000_000, 150)

    assert quote.out_amount == 12_345_678
    assert quote.price_impact_pct == pytest.approx(0.42)  # percent, not fraction
    assert quote.route_labels == ["Raydium", "Orca"]
    assert quote.hops == 2
    assert quote.raw["outAmount"] == "12345678"


def test_quote_returns_none_when_there_is_no_route():
    client = JupiterClient(http=FakeHttp({"/quote": {}}))
    assert client.quote(WSOL_MINT, "mint-wif", 1_000, 150) is None
    assert client.quote(WSOL_MINT, "mint-wif", 0, 150) is None


def test_base_unit_conversion_uses_known_decimals():
    client = JupiterClient(http=FakeHttp({}))
    assert client.to_base_units(WSOL_MINT, 1.5) == 1_500_000_000
    assert client.to_base_units(USDC_MINT, 10.0) == 10_000_000
    assert client.from_base_units(USDC_MINT, 2_500_000) == pytest.approx(2.5)


def test_unknown_decimals_are_fetched_and_cached():
    http = FakeHttp({"tokens.jup.ag": {"decimals": 6}})
    client = JupiterClient(http=http)
    assert client.decimals("mint-wif") == 6
    assert client.decimals("mint-wif") == 6
    assert len(http.calls) == 1


def test_unknown_decimals_fall_back_to_the_default():
    client = JupiterClient(http=FakeHttp({"tokens.jup.ag": HttpError("404", 404)}))
    assert client.decimals("mint-wif", default=9) == 9


def test_swap_transaction_posts_the_quote_verbatim():
    http = FakeHttp({"/swap": {"swapTransaction": "BASE64TX", "lastValidBlockHeight": 42}})
    client = JupiterClient(http=http)
    q = JupiterQuote(WSOL_MINT, "mint-wif", 1, 2, 1, 0.1, 150, [], {"raw": True})
    result = client.swap_transaction(q, "MyWallet1111")
    assert result["swapTransaction"] == "BASE64TX"
    sent = http.calls[-1][1]
    assert sent["quoteResponse"] == {"raw": True}
    assert sent["userPublicKey"] == "MyWallet1111"


# ---- rate limiter --------------------------------------------------------------
def test_rate_limiter_allows_calls_within_the_budget():
    limiter = RateLimiter(max_per_minute=2)
    slept = []
    limiter.acquire(sleep=slept.append)
    limiter.acquire(sleep=slept.append)
    assert slept == []


def test_rate_limiter_waits_once_the_budget_is_spent():
    limiter = RateLimiter(max_per_minute=2)
    limiter.acquire()
    limiter.acquire()

    waits = []

    def fake_sleep(seconds):
        waits.append(seconds)
        limiter._calls.popleft()  # pretend the window rolled forward

    limiter.acquire(sleep=fake_sleep)
    assert len(waits) == 1
    assert 0 < waits[0] <= 60.0
