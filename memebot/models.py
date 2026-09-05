"""Core domain objects shared by data, strategy, risk and execution layers."""

from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional

# Well known Solana mints.
WSOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"

# Windows exposed by DexScreener, shortest first.
WINDOWS = ("m5", "h1", "h6", "h24")


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class Mode(str, Enum):
    PAPER = "paper"
    LIVE = "live"


@dataclass(frozen=True)
class Token:
    """A tradable SPL token."""

    address: str
    symbol: str = ""
    name: str = ""
    decimals: int = 9

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.symbol or self.address[:6]}({self.address[:4]}..{self.address[-4:]})"


@dataclass
class PairSnapshot:
    """A point-in-time view of one DEX pair, normalised from DexScreener."""

    chain_id: str
    dex_id: str
    pair_address: str
    base: Token
    quote: Token
    price_usd: float
    price_native: float = 0.0
    liquidity_usd: float = 0.0
    fdv: float = 0.0
    market_cap: float = 0.0
    volume: Dict[str, float] = field(default_factory=dict)
    price_change: Dict[str, float] = field(default_factory=dict)
    txns: Dict[str, Dict[str, int]] = field(default_factory=dict)
    pair_created_at: float = 0.0  # epoch seconds
    url: str = ""
    fetched_at: float = field(default_factory=time.time)

    # ---- convenience accessors -------------------------------------------------
    def vol(self, window: str) -> float:
        return float(self.volume.get(window, 0.0) or 0.0)

    def change(self, window: str) -> float:
        """Price change in percent over `window` (e.g. 12.5 == +12.5%)."""
        return float(self.price_change.get(window, 0.0) or 0.0)

    def buys(self, window: str) -> int:
        return int((self.txns.get(window) or {}).get("buys", 0) or 0)

    def sells(self, window: str) -> int:
        return int((self.txns.get(window) or {}).get("sells", 0) or 0)

    def trades(self, window: str) -> int:
        return self.buys(window) + self.sells(window)

    def buy_ratio(self, window: str) -> float:
        """Share of trades in `window` that were buys; 0.5 when there is no data."""
        total = self.trades(window)
        if total <= 0:
            return 0.5
        return self.buys(window) / total

    @property
    def age_minutes(self) -> float:
        if not self.pair_created_at:
            return math.inf
        return max(0.0, (time.time() - self.pair_created_at) / 60.0)

    @property
    def is_stale(self) -> bool:
        return self.price_usd <= 0.0


@dataclass
class Signal:
    """A strategy's intent to trade, before risk sizing."""

    token: Token
    side: Side
    score: float
    reason: str
    pair: Optional[PairSnapshot] = None
    price: float = 0.0
    fraction: float = 1.0  # for sells: portion of the position to close

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.side.value.upper()} {self.token} score={self.score:.3f} ({self.reason})"


@dataclass
class Order:
    """A sized, ready-to-execute instruction."""

    token: Token
    side: Side
    reference_price: float
    usd_amount: float = 0.0  # buys: how much quote currency to spend
    token_amount: float = 0.0  # sells: how many base tokens to sell
    slippage_bps: int = 100
    reason: str = ""
    pair: Optional[PairSnapshot] = None
    client_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def __str__(self) -> str:  # pragma: no cover - trivial
        size = f"${self.usd_amount:,.2f}" if self.side is Side.BUY else f"{self.token_amount:,.4f} tokens"
        return f"{self.side.value.upper()} {size} of {self.token} @~{self.reference_price:.8g}"


@dataclass
class Fill:
    """The result of executing an order (paper or live)."""

    order: Order
    ok: bool
    price: float = 0.0  # realised price per token in USD
    token_amount: float = 0.0  # base tokens received (buy) or sold (sell)
    usd_amount: float = 0.0  # gross USD value at the realised price
    fee_usd: float = 0.0  # swap + network + priority fees
    slippage_bps: float = 0.0  # realised slippage vs reference price
    tx_signature: str = ""
    error: str = ""
    ts: float = field(default_factory=time.time)

    @property
    def side(self) -> Side:
        return self.order.side

    @property
    def token(self) -> Token:
        return self.order.token

    @property
    def cash_delta(self) -> float:
        """Signed change to the cash balance, fees included."""
        if not self.ok:
            return 0.0
        if self.side is Side.BUY:
            return -(self.usd_amount + self.fee_usd)
        return self.usd_amount - self.fee_usd

    def as_row(self) -> Dict[str, Any]:
        return {
            "ts": self.ts,
            "client_id": self.order.client_id,
            "side": self.side.value,
            "token_address": self.token.address,
            "symbol": self.token.symbol,
            "price": self.price,
            "token_amount": self.token_amount,
            "usd_amount": self.usd_amount,
            "fee_usd": self.fee_usd,
            "slippage_bps": self.slippage_bps,
            "tx_signature": self.tx_signature,
            "reason": self.order.reason,
        }


@dataclass
class Position:
    """An open long position in one token."""

    token: Token
    quantity: float
    avg_price: float
    cost_usd: float  # cash spent to build the position, fees included
    opened_at: float = field(default_factory=time.time)
    pair_address: str = ""
    last_price: float = 0.0
    high_price: float = 0.0
    fees_usd: float = 0.0
    realized_pnl_usd: float = 0.0
    peak_liquidity_usd: float = 0.0
    last_seen_at: float = field(default_factory=time.time)

    def mark(self, price: float) -> None:
        if price <= 0:
            return
        self.last_price = price
        self.high_price = max(self.high_price, price)
        self.last_seen_at = time.time()

    @property
    def market_value(self) -> float:
        return self.quantity * (self.last_price or self.avg_price)

    @property
    def unrealized_pnl_usd(self) -> float:
        return self.market_value - self.cost_usd

    @property
    def unrealized_pnl_pct(self) -> float:
        if self.cost_usd <= 0:
            return 0.0
        return self.unrealized_pnl_usd / self.cost_usd

    @property
    def drawdown_from_high(self) -> float:
        """Fractional drop from the highest price seen since entry (0.0 .. 1.0)."""
        if self.high_price <= 0:
            return 0.0
        return max(0.0, (self.high_price - (self.last_price or self.high_price)) / self.high_price)

    @property
    def age_minutes(self) -> float:
        return max(0.0, (time.time() - self.opened_at) / 60.0)
