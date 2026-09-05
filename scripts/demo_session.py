#!/usr/bin/env python3
"""Run a paper session against a simulated market - no network required.

    python scripts/demo_session.py [cycles]

The market here is synthetic: prices follow a random walk with the occasional
pump and the occasional rug, so you can watch the full pipeline (discovery ->
filters -> scoring -> sizing -> fills -> stops and take-profits) end to end
before pointing the bot at the real DexScreener feed.
"""

from __future__ import annotations

import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memebot.config import BotConfig
from memebot.engine import TradingEngine
from memebot.execution.paper import PaperExecutor
from memebot.logging_utils import setup_logging
from memebot.models import PairSnapshot, Token
from memebot.storage import Storage

SYMBOLS = ["WIF", "BONK", "POPCAT", "MEW", "GIGA", "PNUT", "MOODENG", "FWOG"]


class SimulatedMarket:
    """A DexScreenerClient-shaped fake with prices that actually move."""

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.pairs = {}
        for symbol in SYMBOLS:
            self._create(symbol)

    def _create(self, symbol: str) -> None:
        price = self.rng.uniform(0.0004, 0.05)
        self.pairs[f"mint-{symbol}"] = PairSnapshot(
            chain_id="solana",
            dex_id=self.rng.choice(["raydium", "orca", "meteora"]),
            pair_address=f"pair-{symbol}",
            base=Token(f"mint-{symbol}", symbol, f"{symbol} coin", 6),
            quote=Token("So11111111111111111111111111111111111111112", "SOL"),
            price_usd=price,
            liquidity_usd=self.rng.uniform(40_000, 900_000),
            fdv=self.rng.uniform(2_000_000, 30_000_000),
            market_cap=self.rng.uniform(2_000_000, 30_000_000),
            volume={"m5": 0.0, "h1": 0.0, "h24": 0.0},
            price_change={"m5": 0.0, "h1": 0.0, "h24": 0.0},
            txns={"m5": {"buys": 0, "sells": 0}, "h1": {"buys": 0, "sells": 0}},
            pair_created_at=time.time() - self.rng.uniform(2, 200) * 3600,
        )
        self.tick(f"mint-{symbol}")

    def tick(self, address: str) -> None:
        pair = self.pairs[address]
        roll = self.rng.random()
        if roll < 0.10:                      # pump
            move = self.rng.uniform(0.08, 0.35)
        elif roll < 0.16:                    # rug
            move = -self.rng.uniform(0.25, 0.60)
            pair.liquidity_usd *= self.rng.uniform(0.2, 0.6)
        else:                                # drift
            move = self.rng.gauss(0.002, 0.05)

        pair.price_usd = max(1e-9, pair.price_usd * (1 + move))
        pair.price_change = {
            "m5": move * 100,
            "h1": move * 100 * self.rng.uniform(1.5, 4.0),
            "h24": move * 100 * self.rng.uniform(2.0, 8.0),
        }
        hourly = self.rng.uniform(30_000, 500_000) * (1 + abs(move) * 6)
        pair.volume = {"m5": hourly / 12, "h1": hourly, "h24": hourly * self.rng.uniform(8, 20)}
        buys = int(self.rng.uniform(30, 120) * (1.6 if move > 0 else 0.6))
        sells = int(self.rng.uniform(30, 120) * (0.6 if move > 0 else 1.6))
        pair.txns = {
            "m5": {"buys": buys, "sells": sells},
            "h1": {"buys": buys * 12, "sells": sells * 12},
        }
        pair.fetched_at = time.time()

    def advance(self) -> None:
        for address in list(self.pairs):
            self.tick(address)

    # ---- DexScreenerClient API ----
    def discover(self, search_terms, use_boosted_feed=True, use_token_profiles=True,
                 max_candidates=120):
        return list(self.pairs.values())[:max_candidates]

    def best_pair(self, token_address):
        return self.pairs.get(token_address)

    def price_usd(self, token_address):
        pair = self.pairs.get(token_address)
        return pair.price_usd if pair else 0.0


def main(argv) -> int:
    cycles = int(argv[1]) if len(argv) > 1 else 20
    seed = int(argv[2]) if len(argv) > 2 else 20260904

    setup_logging("INFO")
    rng = random.Random(seed)

    config = BotConfig()
    config.state_db = ":memory:"
    config.risk.starting_cash_usd = 1_000.0
    config.risk.max_open_positions = 4
    config.risk.reentry_cooldown_minutes = 0.0
    config.risk.cooldown_minutes_after_loss = 0.0
    config.execution.paper_random_seed = seed
    config.validate()

    market = SimulatedMarket(rng)
    storage = Storage(":memory:")
    executor = PaperExecutor(config, data=market, rng=random.Random(seed))
    bot = TradingEngine(config, storage=storage, data=market, executor=executor)

    print(f"\nSimulating {cycles} cycles over {len(SYMBOLS)} synthetic pairs "
          f"(seed {seed}, ${config.risk.starting_cash_usd:,.0f} bankroll)\n")

    for _ in range(cycles):
        market.advance()
        bot.run_cycle()

    stats = bot.portfolio.stats()
    print("\n" + "=" * 64)
    print(f"  equity          ${stats['equity_usd']:,.2f}  "
          f"({stats['total_return_pct']:+.2f}% on ${stats['starting_cash_usd']:,.0f})")
    print(f"  cash            ${stats['cash_usd']:,.2f}")
    print(f"  open positions  {stats['open_positions']} "
          f"(${stats['positions_value_usd']:,.2f})")
    print(f"  closed trades   {stats['closed_trades']} "
          f"({stats['wins']}W/{stats['losses']}L, {stats['win_rate'] * 100:.0f}% win rate)")
    print(f"  realized pnl    ${stats['realized_pnl_usd']:,.2f}")
    print(f"  fees paid       ${stats['total_fees_usd']:,.2f}")
    print("=" * 64)
    print("\nThis is a synthetic market - the returns mean nothing about live "
          "performance.\nIt exists to show the machinery working end to end.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
