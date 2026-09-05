"""Shared fixtures: a fake market that never touches the network."""

from __future__ import annotations

import random
import time
from typing import Dict, Iterable, List, Optional

import pytest

from memebot.config import BotConfig
from memebot.models import PairSnapshot, Token
from memebot.storage import Storage


def make_pair(
    symbol: str = "WIF",
    address: Optional[str] = None,
    price: float = 0.01,
    liquidity: float = 250_000.0,
    vol_m5: float = 12_000.0,
    vol_h1: float = 140_000.0,
    vol_h24: float = 900_000.0,
    chg_m5: float = 6.0,
    chg_h1: float = 25.0,
    chg_h24: float = 40.0,
    buys_m5: int = 70,
    sells_m5: int = 30,
    buys_h1: int = 600,
    sells_h1: int = 400,
    fdv: float = 8_000_000.0,
    age_minutes: float = 240.0,
    quote_mint: str = "So11111111111111111111111111111111111111112",
    dex_id: str = "raydium",
) -> PairSnapshot:
    """Build a snapshot that passes the default filters unless tweaked."""
    return PairSnapshot(
        chain_id="solana",
        dex_id=dex_id,
        pair_address=f"pair-{symbol}",
        base=Token(address=address or f"mint-{symbol}", symbol=symbol, name=f"{symbol} coin"),
        quote=Token(address=quote_mint, symbol="SOL"),
        price_usd=price,
        price_native=price / 150.0,
        liquidity_usd=liquidity,
        fdv=fdv,
        market_cap=fdv,
        volume={"m5": vol_m5, "h1": vol_h1, "h6": vol_h1 * 5, "h24": vol_h24},
        price_change={"m5": chg_m5, "h1": chg_h1, "h6": chg_h1 * 1.2, "h24": chg_h24},
        txns={
            "m5": {"buys": buys_m5, "sells": sells_m5},
            "h1": {"buys": buys_h1, "sells": sells_h1},
            "h24": {"buys": buys_h1 * 20, "sells": sells_h1 * 20},
        },
        pair_created_at=time.time() - age_minutes * 60,
        url=f"https://dexscreener.com/solana/pair-{symbol}",
    )


class FakeDexScreener:
    """Stands in for DexScreenerClient with a scriptable universe."""

    def __init__(self, pairs: Optional[List[PairSnapshot]] = None) -> None:
        self.pairs: Dict[str, PairSnapshot] = {}
        self.discover_calls = 0
        for pair in pairs or []:
            self.add(pair)

    def add(self, pair: PairSnapshot) -> PairSnapshot:
        self.pairs[pair.base.address] = pair
        return pair

    def set_price(self, token_address: str, price: float, liquidity: Optional[float] = None) -> None:
        pair = self.pairs[token_address]
        pair.price_usd = price
        if liquidity is not None:
            pair.liquidity_usd = liquidity
        pair.fetched_at = time.time()

    def remove(self, token_address: str) -> None:
        self.pairs.pop(token_address, None)

    # DexScreenerClient API surface used by the engine
    def discover(self, search_terms: Iterable[str], use_boosted_feed: bool = True,
                 max_candidates: int = 120) -> List[PairSnapshot]:
        self.discover_calls += 1
        return list(self.pairs.values())[:max_candidates]

    def best_pair(self, token_address: str) -> Optional[PairSnapshot]:
        return self.pairs.get(token_address)

    def price_usd(self, token_address: str) -> float:
        pair = self.pairs.get(token_address)
        return pair.price_usd if pair else 0.0

    def pairs_for_token(self, token_address: str) -> List[PairSnapshot]:
        pair = self.pairs.get(token_address)
        return [pair] if pair else []


@pytest.fixture
def storage() -> Storage:
    store = Storage(":memory:")
    yield store
    store.close()


@pytest.fixture
def config() -> BotConfig:
    cfg = BotConfig()
    cfg.state_db = ":memory:"
    cfg.risk.starting_cash_usd = 1_000.0
    # Deterministic, frictionless-by-default execution for tests that only care
    # about the surrounding logic; individual tests re-enable the friction.
    cfg.execution.paper_failure_rate = 0.0
    cfg.execution.paper_random_seed = 7
    return cfg


@pytest.fixture
def rng() -> random.Random:
    return random.Random(1234)
