"""Cash, positions and P&L accounting.

The portfolio is the single source of truth for what the bot owns. It applies
fills, keeps average-cost basis per token, and persists everything to SQLite so
a restart resumes exactly where it left off.
"""

from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional

from .models import Fill, Position, Side, Token
from .storage import Storage

log = logging.getLogger(__name__)

CASH_KEY = "cash_usd"
START_KEY = "starting_cash_usd"
DAY_KEY = "day_start"


class Portfolio:
    def __init__(self, storage: Storage, starting_cash_usd: float = 1000.0, mode: str = "paper") -> None:
        self.storage = storage
        self.mode = mode
        self.starting_cash = float(storage.get_state(START_KEY, starting_cash_usd))
        if storage.get_state(START_KEY) is None:
            storage.set_state(START_KEY, self.starting_cash)
        self.cash: float = float(storage.get_state(CASH_KEY, self.starting_cash))
        self.positions: Dict[str, Position] = storage.load_positions()

    # ---- accessors -------------------------------------------------------------
    def position(self, token_address: str) -> Optional[Position]:
        return self.positions.get(token_address)

    def has_position(self, token_address: str) -> bool:
        return token_address in self.positions

    @property
    def open_positions(self) -> List[Position]:
        return list(self.positions.values())

    @property
    def positions_value(self) -> float:
        return sum(p.market_value for p in self.positions.values())

    @property
    def equity(self) -> float:
        return self.cash + self.positions_value

    @property
    def unrealized_pnl_usd(self) -> float:
        return sum(p.unrealized_pnl_usd for p in self.positions.values())

    @property
    def total_return_pct(self) -> float:
        if self.starting_cash <= 0:
            return 0.0
        return (self.equity - self.starting_cash) / self.starting_cash

    # ---- mutation --------------------------------------------------------------
    def mark(self, token_address: str, price: float, liquidity_usd: float = 0.0) -> None:
        pos = self.positions.get(token_address)
        if pos is None or price <= 0:
            return
        pos.mark(price)
        if liquidity_usd > 0:
            pos.peak_liquidity_usd = max(pos.peak_liquidity_usd, liquidity_usd)
        self.storage.save_position(pos)

    def _persist_cash(self) -> None:
        self.storage.set_state(CASH_KEY, self.cash)

    def set_cash(self, value: float) -> None:
        """Overwrite the cash balance - used in live mode, where the wallet's
        on-chain balance is the truth rather than our own running total."""
        self.cash = max(0.0, float(value))
        self._persist_cash()

    def set_starting_cash(self, value: float) -> None:
        """Re-anchor the return baseline (live mode, first run)."""
        self.starting_cash = float(value)
        self.storage.set_state(START_KEY, self.starting_cash)

    def apply_fill(self, fill: Fill) -> float:
        """Apply a fill, returning the realized P&L it produced (0 for buys)."""
        if not fill.ok:
            return 0.0

        realized = 0.0
        address = fill.token.address

        if fill.side is Side.BUY:
            cost = fill.usd_amount + fill.fee_usd
            self.cash -= cost
            pos = self.positions.get(address)
            if pos is None:
                pos = Position(
                    token=fill.token,
                    quantity=fill.token_amount,
                    avg_price=fill.price,
                    cost_usd=cost,
                    opened_at=fill.ts,
                    pair_address=fill.order.pair.pair_address if fill.order.pair else "",
                    last_price=fill.price,
                    high_price=fill.price,
                    fees_usd=fill.fee_usd,
                    peak_liquidity_usd=fill.order.pair.liquidity_usd if fill.order.pair else 0.0,
                )
                self.positions[address] = pos
            else:
                total_qty = pos.quantity + fill.token_amount
                pos.cost_usd += cost
                pos.fees_usd += fill.fee_usd
                pos.avg_price = (pos.cost_usd / total_qty) if total_qty > 0 else fill.price
                pos.quantity = total_qty
                pos.mark(fill.price)
            self.storage.save_position(pos)

        else:  # SELL
            pos = self.positions.get(address)
            proceeds = fill.usd_amount - fill.fee_usd
            self.cash += proceeds
            if pos is None:
                log.warning("Sell fill for %s with no open position", address)
            else:
                sold = min(fill.token_amount, pos.quantity)
                share = (sold / pos.quantity) if pos.quantity > 0 else 1.0
                cost_basis = pos.cost_usd * share
                realized = proceeds - cost_basis
                pos.quantity -= sold
                pos.cost_usd -= cost_basis
                pos.fees_usd += fill.fee_usd
                pos.realized_pnl_usd += realized
                pos.mark(fill.price)
                if pos.quantity <= 1e-12 or share >= 0.999999:
                    self.positions.pop(address, None)
                    self.storage.delete_position(address)
                else:
                    self.storage.save_position(pos)

        self._persist_cash()
        self.storage.record_fill(fill, realized_pnl=realized, mode=self.mode)
        return realized

    # ---- reporting -------------------------------------------------------------
    def snapshot_equity(self) -> None:
        self.storage.record_equity(self.cash, self.positions_value)

    @staticmethod
    def _utc_day_start(now: Optional[float] = None) -> float:
        """Midnight UTC for the given timestamp. Computed from a single clock
        reading - taking two would make the boundary drift on every call."""
        now = now if now is not None else time.time()
        return now - (now % 86400)

    def day_start_equity(self) -> float:
        """Equity at the start of the current UTC day (for the daily loss cap)."""
        day_start = self._utc_day_start()
        stored_day = self.storage.get_state(DAY_KEY)
        if stored_day != day_start:
            self.storage.set_state(DAY_KEY, day_start)
            self.storage.set_state("day_start_equity", self.equity)
            return self.equity
        value = self.storage.get_state("day_start_equity")
        return float(value) if value is not None else self.equity

    def realized_pnl_today(self) -> float:
        return self.storage.realized_pnl_since(self._utc_day_start())

    def stats(self) -> Dict[str, float]:
        base = self.storage.trade_stats()
        base.update(
            {
                "cash_usd": self.cash,
                "positions_value_usd": self.positions_value,
                "equity_usd": self.equity,
                "starting_cash_usd": self.starting_cash,
                "unrealized_pnl_usd": self.unrealized_pnl_usd,
                "total_return_pct": self.total_return_pct * 100.0,
                "open_positions": len(self.positions),
            }
        )
        return base
