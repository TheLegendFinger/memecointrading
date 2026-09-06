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
    def discover(self, search_terms: Iterable[str], max_candidates: int = 400,
                 **feeds) -> List[PairSnapshot]:
        self.discover_calls += 1
        self.discover_feeds = feeds
        return list(self.pairs.values())[:max_candidates]

    def pairs_for_tokens(self, addresses: Iterable[str]) -> List[PairSnapshot]:
        """The batch lookup the engine uses to re-price open positions."""
        return [self.pairs[a] for a in addresses if a in self.pairs]

    def best_pair(self, token_address: str) -> Optional[PairSnapshot]:
        return self.pairs.get(token_address)

    def price_usd(self, token_address: str) -> float:
        pair = self.pairs.get(token_address)
        return pair.price_usd if pair else 0.0

    def pairs_for_token(self, token_address: str) -> List[PairSnapshot]:
        pair = self.pairs.get(token_address)
        return [pair] if pair else []


def fund(portfolio, usd: float = 1_000.0):
    """Give a portfolio a balance, the way the wallet sync does in production."""
    portfolio.set_cash(usd)
    portfolio.set_starting_cash(usd)
    return portfolio


@pytest.fixture(autouse=True)
def isolate_wallet_env(monkeypatch):
    """Keep the suite independent of whoever is running it.

    A developer with a real .env (a funded wallet, an armed interlock) must get
    the same results as CI, so wallet variables are cleared and the .env reader
    is stubbed out for every test. Tests that need these set them themselves -
    their own monkeypatch runs after this one.
    """
    for key in ("SOLANA_PRIVATE_KEY", "SOLANA_MNEMONIC", "SOLANA_KEYPAIR_PATH",
                "LIVE_TRADING_CONFIRM", "JUPITER_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("memebot.config.load_dotenv", lambda *args, **kwargs: None)


@pytest.fixture
def storage() -> Storage:
    store = Storage(":memory:")
    yield store
    store.close()


@pytest.fixture
def config() -> BotConfig:
    cfg = BotConfig()
    cfg.state_db = ":memory:"
    return cfg


@pytest.fixture
def rng() -> random.Random:
    return random.Random(1234)
