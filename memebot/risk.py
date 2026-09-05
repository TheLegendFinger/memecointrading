"""Risk management: what we are allowed to buy, how big, and when to get out.

Nothing reaches the executor without passing through here. The risk manager is
deliberately paranoid - memecoin pairs go to zero routinely, so every position
carries a stop, a trailing stop, a time limit and a liquidity guard.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from .config import RiskConfig
from .models import PairSnapshot, Position
from .portfolio import Portfolio

log = logging.getLogger(__name__)


@dataclass
class ExitDecision:
    should_exit: bool
    reason: str = ""
    fraction: float = 1.0  # portion of the position to close

    def __bool__(self) -> bool:  # pragma: no cover - trivial
        return self.should_exit


NO_EXIT = ExitDecision(False)


class RiskManager:
    def __init__(self, config: RiskConfig) -> None:
        self.cfg = config
        self._last_loss_ts: float = 0.0
        self._halted_reason: str = ""

    # ---- entry gating ----------------------------------------------------------
    def can_open_position(self, portfolio: Portfolio, now: Optional[float] = None) -> Tuple[bool, str]:
        """Portfolio-level checks that apply before any specific candidate."""
        now = now if now is not None else time.time()
        cfg = self.cfg

        if len(portfolio.positions) >= cfg.max_open_positions:
            return False, f"max_open_positions ({cfg.max_open_positions}) reached"

        halt, reason = self.should_halt(portfolio)
        if halt:
            return False, reason

        if self._last_loss_ts and cfg.cooldown_minutes_after_loss > 0:
            elapsed_min = (now - self._last_loss_ts) / 60.0
            if elapsed_min < cfg.cooldown_minutes_after_loss:
                remaining = cfg.cooldown_minutes_after_loss - elapsed_min
                return False, f"cooling down after a loss ({remaining:.1f}m left)"

        investable = self.investable_cash(portfolio)
        if investable < cfg.min_position_usd:
            return False, f"insufficient free cash (${investable:,.2f} < ${cfg.min_position_usd:,.2f})"

        return True, ""

    def should_halt(self, portfolio: Portfolio) -> Tuple[bool, str]:
        """Circuit breakers: daily loss cap and peak-to-trough drawdown."""
        cfg = self.cfg

        if cfg.max_daily_loss_pct > 0:
            day_start_equity = portfolio.day_start_equity()
            if day_start_equity > 0:
                change = (portfolio.equity - day_start_equity) / day_start_equity
                if change <= -cfg.max_daily_loss_pct:
                    return True, (
                        f"daily loss limit hit ({change * 100:.1f}% <= "
                        f"-{cfg.max_daily_loss_pct * 100:.1f}%)"
                    )

        if cfg.max_drawdown_pct > 0:
            peak = max(portfolio.storage.peak_equity(), portfolio.starting_cash)
            if peak > 0:
                drawdown = (peak - portfolio.equity) / peak
                if drawdown >= cfg.max_drawdown_pct:
                    return True, (
                        f"max drawdown hit ({drawdown * 100:.1f}% >= "
                        f"{cfg.max_drawdown_pct * 100:.1f}%)"
                    )

        return False, ""

    def can_enter_token(
        self,
        portfolio: Portfolio,
        token_address: str,
        now: Optional[float] = None,
    ) -> Tuple[bool, str]:
        """Per-token checks: already held, or still inside the re-entry cooldown."""
        now = now if now is not None else time.time()
        if portfolio.has_position(token_address):
            return False, "already holding this token"
        cooldown = self.cfg.reentry_cooldown_minutes
        if cooldown > 0:
            last_exit = portfolio.storage.last_exit_time(token_address)
            if last_exit and (now - last_exit) / 60.0 < cooldown:
                remaining = cooldown - (now - last_exit) / 60.0
                return False, f"re-entry cooldown ({remaining:.1f}m left)"
        return True, ""

    # ---- sizing ----------------------------------------------------------------
    def investable_cash(self, portfolio: Portfolio) -> float:
        reserve = portfolio.equity * self.cfg.cash_reserve_pct
        return max(0.0, portfolio.cash - reserve)

    def position_size_usd(self, portfolio: Portfolio, pair: Optional[PairSnapshot] = None) -> float:
        """How many dollars to put into one new position.

        Bounded by: the configured equity fraction, the min/max clamps, the free
        cash after reserve, and a hard cap relative to the pool's liquidity so we
        never become the price.
        """
        cfg = self.cfg
        size = portfolio.equity * cfg.position_size_pct
        size = min(size, cfg.max_position_usd)
        size = min(size, self.investable_cash(portfolio))

        if pair is not None and pair.liquidity_usd > 0 and cfg.max_position_pct_of_liquidity > 0:
            size = min(size, pair.liquidity_usd * cfg.max_position_pct_of_liquidity)

        if size < cfg.min_position_usd:
            return 0.0
        return round(size, 2)

    # ---- exits -----------------------------------------------------------------
    def evaluate_exit(
        self,
        position: Position,
        pair: Optional[PairSnapshot] = None,
        now: Optional[float] = None,
    ) -> ExitDecision:
        """Check every protective rule against a live position.

        Order matters: capital-preserving exits are evaluated before profit
        taking, so a pair that is simultaneously rugging and up 60% still exits
        for the right reason.
        """
        now = now if now is not None else time.time()
        cfg = self.cfg
        price = position.last_price or position.avg_price
        if price <= 0 or position.quantity <= 0:
            return NO_EXIT

        pnl_pct = position.unrealized_pnl_pct

        # 1. Liquidity draining out of the pool - get out at any price.
        if pair is not None and position.peak_liquidity_usd > 0 and cfg.liquidity_drain_pct > 0:
            drop = (position.peak_liquidity_usd - pair.liquidity_usd) / position.peak_liquidity_usd
            if drop >= cfg.liquidity_drain_pct:
                return ExitDecision(True, f"liquidity drained {drop * 100:.0f}% from peak")

        # 2. The pair stopped reporting prices - treat as a dead market.
        if cfg.stale_price_exit_minutes > 0:
            stale_min = (now - position.last_seen_at) / 60.0
            if stale_min >= cfg.stale_price_exit_minutes:
                return ExitDecision(True, f"no market data for {stale_min:.0f}m")

        # 3. Hard stop loss.
        if cfg.stop_loss_pct > 0 and pnl_pct <= -cfg.stop_loss_pct:
            return ExitDecision(True, f"stop loss {pnl_pct * 100:.1f}%")

        # 4. Trailing stop, armed only once the position has run far enough.
        if cfg.trailing_stop_pct > 0 and position.high_price > 0:
            high_gain = (position.high_price - position.avg_price) / position.avg_price
            if high_gain >= cfg.trailing_arm_profit_pct:
                if position.drawdown_from_high >= cfg.trailing_stop_pct:
                    return ExitDecision(
                        True,
                        f"trailing stop ({position.drawdown_from_high * 100:.1f}% off high, "
                        f"pnl {pnl_pct * 100:+.1f}%)",
                    )

        # 5. Take profit.
        if cfg.take_profit_pct > 0 and pnl_pct >= cfg.take_profit_pct:
            return ExitDecision(True, f"take profit {pnl_pct * 100:+.1f}%")

        # 6. Time stop - memecoin edge decays fast.
        if cfg.max_hold_minutes > 0 and position.age_minutes >= cfg.max_hold_minutes:
            return ExitDecision(True, f"max hold {position.age_minutes:.0f}m (pnl {pnl_pct * 100:+.1f}%)")

        return NO_EXIT

    # ---- bookkeeping -----------------------------------------------------------
    def record_close(self, realized_pnl: float, now: Optional[float] = None) -> None:
        if realized_pnl < 0:
            self._last_loss_ts = now if now is not None else time.time()

    def state(self) -> Dict[str, float]:
        return {"last_loss_ts": self._last_loss_ts}
