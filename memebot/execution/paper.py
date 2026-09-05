"""Paper execution: realistic simulated fills, no funds at risk.

The point of paper mode is not "assume you get the mid price". Memecoin swaps
lose real money to price impact, aggregator/pool fees, priority fees and
outright failed transactions, so the simulator models all four:

  * price impact from a constant-product curve using the pool's own liquidity
    (or, optionally, a real Jupiter route quote),
  * a base slippage allowance for latency and competing flow,
  * pool fee (bps) + network fee + priority fee in USD,
  * a small probability that the transaction simply fails, and
  * rejection when the modelled slippage exceeds the order's tolerance -
    exactly what an on-chain swap does when the minimum-out check trips.
"""

from __future__ import annotations

import logging
import random
import uuid
from typing import Optional

from ..config import BotConfig
from ..models import Fill, Order, Side
from .base import Executor

log = logging.getLogger(__name__)


class PaperExecutor(Executor):
    mode = "paper"

    def __init__(self, config: BotConfig, data=None, jupiter=None, rng: Optional[random.Random] = None) -> None:
        self.config = config
        self.cfg = config.execution
        self.data = data
        self.jupiter = jupiter
        seed = getattr(self.cfg, "paper_random_seed", None)
        self.rng = rng or (random.Random(seed) if seed is not None else random.Random())

    # ---- fill model ------------------------------------------------------------
    def _impact_fraction(self, order: Order) -> float:
        """Fractional price impact of this order size against the pool.

        For a constant-product pool holding `liquidity_usd` in total value, one
        side of the pool is worth roughly liquidity/2, and trading `size` against
        it moves the average execution price by about size / (liquidity / 2).
        """
        notional = order.usd_amount if order.side is Side.BUY else order.token_amount * order.reference_price
        pair = order.pair
        if pair is None or pair.liquidity_usd <= 0:
            # No depth information: assume a thin pool and charge 1%.
            return 0.01
        side_depth = pair.liquidity_usd / 2.0
        if side_depth <= 0:
            return 0.01
        return min(0.5, notional / side_depth)

    def _quoted_impact(self, order: Order) -> Optional[float]:
        """Price impact from a live Jupiter route, if enabled and reachable."""
        if not getattr(self.cfg, "paper_use_live_quotes", False) or self.jupiter is None:
            return None
        try:
            quote_mint = self.cfg.quote_mint
            if order.side is Side.BUY:
                # Quote currency in, token out. Convert USD to quote units.
                quote_price = self.jupiter.price(quote_mint)
                if quote_price <= 0:
                    return None
                amount = self.jupiter.to_base_units(quote_mint, order.usd_amount / quote_price)
                quote = self.jupiter.quote(quote_mint, order.token.address, amount, self.cfg.slippage_bps)
            else:
                amount = self.jupiter.to_base_units(order.token.address, order.token_amount)
                quote = self.jupiter.quote(order.token.address, quote_mint, amount, self.cfg.slippage_bps)
        except Exception as exc:  # pragma: no cover - network dependent
            log.debug("Live quote for paper fill failed: %s", exc)
            return None
        if quote is None:
            return None
        return max(0.0, quote.price_impact_pct / 100.0)

    def _slippage_fraction(self, order: Order) -> float:
        impact = self._quoted_impact(order)
        if impact is None:
            impact = self._impact_fraction(order)
        base = self.cfg.paper_base_slippage_bps / 10_000.0
        # A little randomness so backtest-style runs are not suspiciously smooth.
        jitter = base * self.rng.uniform(-0.35, 0.75)
        return max(0.0, base + jitter + impact)

    def _fees(self, gross_usd: float) -> float:
        return (
            gross_usd * (self.cfg.fee_bps / 10_000.0)
            + self.cfg.network_fee_usd
            + self.cfg.priority_fee_usd
        )

    # ---- Executor API ----------------------------------------------------------
    def execute(self, order: Order) -> Fill:
        if order.reference_price <= 0:
            return Fill(order=order, ok=False, error="no reference price")
        if order.side is Side.BUY and order.usd_amount <= 0:
            return Fill(order=order, ok=False, error="buy order has no size")
        if order.side is Side.SELL and order.token_amount <= 0:
            return Fill(order=order, ok=False, error="sell order has no size")

        if self.cfg.paper_failure_rate > 0 and self.rng.random() < self.cfg.paper_failure_rate:
            return Fill(order=order, ok=False, error="transaction failed (simulated)")

        slip = self._slippage_fraction(order)
        tolerance = order.slippage_bps / 10_000.0
        if slip > tolerance:
            return Fill(
                order=order,
                ok=False,
                slippage_bps=slip * 10_000.0,
                error=(
                    f"slippage {slip * 10_000:.0f}bps exceeds tolerance "
                    f"{order.slippage_bps}bps (simulated revert)"
                ),
            )

        if order.side is Side.BUY:
            price = order.reference_price * (1.0 + slip)
            gross = order.usd_amount
            token_amount = gross / price
        else:
            price = order.reference_price * (1.0 - slip)
            token_amount = order.token_amount
            gross = token_amount * price

        return Fill(
            order=order,
            ok=True,
            price=price,
            token_amount=token_amount,
            usd_amount=gross,
            fee_usd=self._fees(gross),
            slippage_bps=slip * 10_000.0,
            tx_signature=f"paper-{uuid.uuid4().hex[:16]}",
        )

    def price_for(self, token_address: str) -> float:
        if self.data is not None:
            try:
                return self.data.price_usd(token_address)
            except Exception as exc:  # pragma: no cover - network dependent
                log.debug("Price lookup failed for %s: %s", token_address, exc)
        return 0.0

    def describe(self) -> str:
        return (
            f"paper (fee {self.cfg.fee_bps}bps, base slip {self.cfg.paper_base_slippage_bps}bps, "
            f"fees ${self.cfg.network_fee_usd + self.cfg.priority_fee_usd:.2f}/swap)"
        )
