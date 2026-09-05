"""How the candidate universe is built.

The bug behind this file: discovery was name-driven. DexScreener's search
endpoint matches text, so asking it for twenty fashionable words returns
whatever is *called* those words - and when one ticker catches, a dozen
near-identical copycats all match at once and fill the scan. A user saw
nothing but coins with "stonk" in the name.

Two answers, both tested here: feeds that rank by trading activity rather than
by name, and a cap on how many coins from one ticker family can get through.
"""

import pytest

from memebot.data.dexscreener import DexScreenerClient, symbol_family
from memebot.data.geckoterminal import GeckoTerminalClient, merge_addresses
from memebot.http import HttpError

from test_data import FakeHttp


def raw_pair(address, symbol, liquidity=100_000.0, vol_h1=50_000.0):
    return {
        "chainId": "solana",
        "dexId": "raydium",
        "pairAddress": f"pair-{address}",
        "baseToken": {"address": address, "symbol": symbol, "name": symbol},
        "quoteToken": {"address": "So11111111111111111111111111111111111111112",
                       "symbol": "SOL", "name": "Wrapped SOL"},
        "priceUsd": "0.001",
        "liquidity": {"usd": liquidity},
        "volume": {"h1": vol_h1, "h24": vol_h1 * 24},
        "priceChange": {"m5": 1.0, "h1": 5.0},
        "txns": {"h1": {"buys": 100, "sells": 80}},
    }


def pool_row(token_address):
    """One row of a GeckoTerminal JSON:API pool listing."""
    return {
        "id": f"solana_pool-{token_address}",
        "type": "pool",
        "attributes": {"name": f"{token_address} / SOL"},
        "relationships": {"base_token": {"data": {"id": f"solana_{token_address}",
                                                  "type": "token"}}},
    }


# ---- GeckoTerminal ---------------------------------------------------------------
def test_trending_pools_yield_base_token_addresses():
    http = FakeHttp({"/networks/solana/trending_pools": {
        "data": [pool_row("mint-A"), pool_row("mint-B")]
    }})
    assert GeckoTerminalClient(http=http).trending_tokens(limit=10) == ["mint-A", "mint-B"]


def test_the_network_prefix_is_stripped_from_ids():
    """GeckoTerminal ids are "solana_<address>"; a mint with the prefix left on
    would resolve to nothing at DexScreener and vanish silently."""
    http = FakeHttp({"/pools": {"data": [pool_row("mint-A")]}})
    assert GeckoTerminalClient(http=http).top_volume_tokens(limit=5) == ["mint-A"]


def test_paging_stops_once_a_page_repeats_itself():
    http = FakeHttp({"/trending_pools": {"data": [pool_row("mint-A")]}})
    gecko = GeckoTerminalClient(http=http)
    assert gecko.trending_tokens(limit=50) == ["mint-A"]
    assert len(http.calls) == 2, "one page, then one that adds nothing"


def test_a_dead_feed_costs_breadth_and_nothing_else():
    http = FakeHttp({"/": HttpError("upstream is down", 502)})
    gecko = GeckoTerminalClient(http=http)
    assert gecko.trending_tokens() == []
    assert gecko.new_pool_tokens() == []
    assert gecko.reachable() is False


def test_junk_in_a_feed_is_skipped_not_fatal():
    http = FakeHttp({"/trending_pools": {"data": [
        "not a dict", {"id": "solana_pool-x"}, {"relationships": {}}, pool_row("mint-A"),
    ]}})
    assert GeckoTerminalClient(http=http).trending_tokens() == ["mint-A"]


def test_feeds_are_interleaved_so_one_cannot_crowd_out_the_others():
    merged = merge_addresses(["a1", "a2", "a3"], ["b1"], ["c1", "c2"])
    assert merged == ["a1", "b1", "c1", "a2", "c2", "a3"]


def test_interleaving_drops_duplicates_keeping_the_first_feed_that_had_it():
    assert merge_addresses(["a", "b"], ["a", "c"]) == ["a", "b", "c"]


# ---- ticker families -------------------------------------------------------------
@pytest.mark.parametrize("symbol, expected", [
    ("STONK", "stonk"),
    ("STONKS", "stonk"),
    ("$STONK", "stonk"),
    ("stonk2", "stonk"),
    ("BONK", "bonk"),
])
def test_copycat_tickers_land_in_one_family(symbol, expected):
    pair = DexScreenerClient(http=FakeHttp())._pairs_for_chain([raw_pair("m", symbol)])[0]
    assert symbol_family(pair) == expected


def test_a_symbolless_token_falls_back_to_its_address():
    pair = DexScreenerClient(http=FakeHttp())._pairs_for_chain([raw_pair("MintAddr123", "")])[0]
    assert symbol_family(pair) == "mintaddr"


# ---- discovery end to end --------------------------------------------------------
def test_one_ticker_family_cannot_fill_the_scan():
    """The reported symptom, in one test."""
    stonks = [raw_pair(f"mint-{i}", "STONK", vol_h1=900_000 - i) for i in range(8)]
    others = [raw_pair("mint-dog", "DOG", vol_h1=10_000),
              raw_pair("mint-cat", "CAT", vol_h1=9_000)]
    http = FakeHttp({"/latest/dex/search": {"pairs": stonks + others}})

    candidates = DexScreenerClient(http=http).discover(
        ["anything"], use_boosted_feed=False, use_token_profiles=False, max_per_symbol=2
    )

    symbols = [c.base.symbol for c in candidates]
    assert symbols.count("STONK") == 2, symbols
    assert {"DOG", "CAT"} <= set(symbols), "the rest of the market survives"


def test_the_cap_keeps_the_busiest_of_a_family():
    quiet = raw_pair("mint-quiet", "STONK", vol_h1=1_000)
    busy = raw_pair("mint-busy", "STONK", vol_h1=900_000)
    http = FakeHttp({"/latest/dex/search": {"pairs": [quiet, busy]}})

    candidates = DexScreenerClient(http=http).discover(
        ["x"], use_boosted_feed=False, use_token_profiles=False, max_per_symbol=1
    )
    assert [c.base.address for c in candidates] == ["mint-busy"]


def test_pool_feeds_widen_the_universe_beyond_the_search_terms():
    http = FakeHttp({
        "/latest/dex/search": {"pairs": [raw_pair("mint-named", "MOON")]},
        "/tokens/v1/solana/": {"pairs": [raw_pair("mint-trending", "QUIET", vol_h1=80_000)]},
    })
    gecko_http = FakeHttp({"/trending_pools": {"data": [pool_row("mint-trending")]},
                           "/pools": {"data": []}})
    client = DexScreenerClient(http=http, gecko=GeckoTerminalClient(http=gecko_http))

    candidates = client.discover(
        ["moon"], use_boosted_feed=False, use_token_profiles=False,
        use_trending_pools=True, max_per_symbol=0,
    )

    found = {c.base.symbol: c.source for c in candidates}
    assert found == {"QUIET": "trending", "MOON": "search"}
    assert client.last_sources == {"trending": 1, "search": 1}


def test_pool_feeds_are_skipped_when_no_client_was_given():
    """A client handed a transport must not quietly reach a second API."""
    http = FakeHttp({"/latest/dex/search": {"pairs": [raw_pair("mint-a", "A")]}})
    client = DexScreenerClient(http=http)

    candidates = client.discover(["a"], use_boosted_feed=False, use_token_profiles=False,
                                 use_trending_pools=True, use_top_pools=True)

    assert [c.base.symbol for c in candidates] == ["A"]
    assert all("geckoterminal" not in path for path, _ in http.calls)


def test_the_deepest_pool_wins_but_keeps_the_feed_that_found_it_first():
    thin = raw_pair("mint-a", "A", liquidity=10_000)
    deep = raw_pair("mint-a", "A", liquidity=900_000)
    http = FakeHttp({
        "/latest/dex/search": {"pairs": [thin]},
        "/tokens/v1/solana/": {"pairs": [deep]},
    })
    gecko_http = FakeHttp({"/trending_pools": {"data": [pool_row("mint-other")]},
                           "/pools": {"data": []}})
    client = DexScreenerClient(http=http, gecko=GeckoTerminalClient(http=gecko_http))

    candidates = client.discover(["a"], use_boosted_feed=False, use_token_profiles=False,
                                 use_trending_pools=True)
    assert len(candidates) == 1
    assert candidates[0].liquidity_usd == 900_000
    assert candidates[0].source == "search"
