"""The trading loop.

One cycle:
  1. refresh every open position with fresh market data and mark it,
  2. close positions whose risk rules or strategy thesis say to close,
  3. if there is capacity, scan for candidates, filter, score and open positions,
  4. snapshot equity and log a one-line summary.

The loop is intentionally synchronous and single-threaded: it is far easier to
reason about (and to stop safely) than a concurrent design, and DexScreener
rate limits are the real bottleneck anyway.
"""

from __future__ import annotations

import logging
import signal
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .config import BotConfig
from .data.dexscreener import DexScreenerClient
from .data.jupiter import JupiterClient
from .execution import build_executor
from .execution.base import Executor
from .models import Fill, Mode, Order, PairSnapshot, Position, Side, Signal
from .portfolio import Portfolio
from .risk import RiskManager
from .storage import Storage, open_storage
from .strategy import CandidateFilter, build_strategy

log = logging.getLogger(__name__)


@dataclass
class CycleReport:
    """What happened in one pass of the loop - handy for tests and the CLI."""

    scanned: int = 0
    passed_filters: int = 0
    signals: int = 0
    opened: List[Fill] = field(default_factory=list)
    closed: List[Fill] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    skipped: Dict[str, int] = field(default_factory=dict)
    halted_reason: str = ""

    def note_skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


class TradingEngine:
    def __init__(
        self,
        config: BotConfig,
        storage: Optional[Storage] = None,
        data: Optional[DexScreenerClient] = None,
        executor: Optional[Executor] = None,
        portfolio: Optional[Portfolio] = None,
        jupiter: Optional[JupiterClient] = None,
    ) -> None:
        self.config = config
        self.storage = storage or open_storage(config.state_db)

        self.data = data or DexScreenerClient(
            base_url=config.data.dexscreener_base_url,
            chain=config.data.chain,
            timeout=config.data.request_timeout,
            max_retries=config.data.max_retries,
            backoff_seconds=config.data.backoff_seconds,
            rate_limit_per_minute=config.data.rate_limit_per_minute,
            cache_ttl_seconds=config.data.cache_ttl_seconds,
        )
        self.jupiter = jupiter or JupiterClient(
            quote_url=config.data.jupiter_quote_url,
            price_url=config.data.jupiter_price_url,
            timeout=config.data.request_timeout,
            max_retries=config.data.max_retries,
            backoff_seconds=config.data.backoff_seconds,
            rate_limit_per_minute=config.data.jupiter_rate_limit_per_minute,
        )
        self.executor = executor or build_executor(config, data=self.data, jupiter=self.jupiter)
        self.portfolio = portfolio or Portfolio(
            self.storage, config.risk.starting_cash_usd, mode=config.mode
        )
        self.risk = RiskManager(config.risk)
        self.filter = CandidateFilter(config.filters)
        self.strategy = build_strategy(config.strategy.name, config.strategy)

        self._stop = False
        self.cycles = 0

    # ---- lifecycle -------------------------------------------------------------
    def install_signal_handlers(self) -> None:
        def handler(signum, _frame):  # pragma: no cover - signal path
            log.warning("Received signal %s - finishing this cycle then stopping", signum)
            self._stop = True

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):  # pragma: no cover - non-main thread
                pass

    def stop(self) -> None:
        self._stop = True

    def preflight(self) -> Optional[str]:
        return self.executor.preflight()

    # ---- position maintenance --------------------------------------------------
    def _refresh_positions(self) -> Dict[str, PairSnapshot]:
        """Pull fresh pair data for everything we hold, and mark the book."""
        snapshots: Dict[str, PairSnapshot] = {}
        for position in self.portfolio.open_positions:
            pair: Optional[PairSnapshot] = None
            try:
                pair = self.data.best_pair(position.token.address)
            except Exception as exc:  # pragma: no cover - network dependent
                log.warning("Could not refresh %s: %s", position.token, exc)
            if pair is None or pair.price_usd <= 0:
                # Fall back to the executor's own pricing (Jupiter in live mode).
                price = self.executor.price_for(position.token.address)
                if price > 0:
                    self.portfolio.mark(position.token.address, price)
                continue
            snapshots[position.token.address] = pair
            self.portfolio.mark(position.token.address, pair.price_usd, pair.liquidity_usd)
        return snapshots

    def _close_position(self, position: Position, reason: str, report: CycleReport) -> None:
        price = position.last_price or position.avg_price
        order = Order(
            token=position.token,
            side=Side.SELL,
            reference_price=price,
            token_amount=position.quantity,
            slippage_bps=self.config.execution.slippage_bps,
            reason=reason,
        )
        if self.config.dry_run:
            log.info("[dry-run] would SELL %s - %s", position.token, reason)
            report.note_skip("dry_run")
            return

        fill = self.executor.execute(order)
        if not fill.ok:
            log.warning("SELL %s failed: %s", position.token, fill.error)
            report.errors.append(f"sell {position.token.symbol}: {fill.error}")
            return

        realized = self.portfolio.apply_fill(fill)
        self.risk.record_close(realized)
        report.closed.append(fill)
        log.info(
            "SOLD %s %.4f @ %.8g -> $%.2f | pnl %+.2f | %s | %s",
            position.token.symbol or position.token.address[:6],
            fill.token_amount, fill.price, fill.usd_amount - fill.fee_usd,
            realized, reason, fill.tx_signature,
        )

    def manage_positions(self, report: CycleReport) -> None:
        snapshots = self._refresh_positions()
        for position in list(self.portfolio.open_positions):
            pair = snapshots.get(position.token.address)

            decision = self.risk.evaluate_exit(position, pair)
            if decision.should_exit:
                self._close_position(position, decision.reason, report)
                continue

            thesis_broken = self.strategy.should_exit(position, pair)
            if thesis_broken:
                self._close_position(position, thesis_broken, report)

    # ---- entries ---------------------------------------------------------------
    def _open_position(self, signal_obj: Signal, report: CycleReport) -> None:
        pair = signal_obj.pair
        size_usd = self.risk.position_size_usd(self.portfolio, pair)
        if size_usd <= 0:
            report.note_skip("size below minimum")
            return

        order = Order(
            token=signal_obj.token,
            side=Side.BUY,
            reference_price=signal_obj.price or (pair.price_usd if pair else 0.0),
            usd_amount=size_usd,
            slippage_bps=self.config.execution.slippage_bps,
            reason=signal_obj.reason,
            pair=pair,
        )
        if self.config.dry_run:
            log.info("[dry-run] would BUY %s for $%.2f - %s", signal_obj.token, size_usd, signal_obj.reason)
            report.note_skip("dry_run")
            return

        fill = self.executor.execute(order)
        if not fill.ok:
            log.warning("BUY %s failed: %s", signal_obj.token, fill.error)
            report.errors.append(f"buy {signal_obj.token.symbol}: {fill.error}")
            return

        self.portfolio.apply_fill(fill)
        report.opened.append(fill)
        log.info(
            "BOUGHT %s %.4f @ %.8g for $%.2f (fee $%.2f, slip %.0fbps) | %s | %s",
            signal_obj.token.symbol or signal_obj.token.address[:6],
            fill.token_amount, fill.price, fill.usd_amount, fill.fee_usd,
            fill.slippage_bps, signal_obj.reason, fill.tx_signature,
        )

    def scan_for_entries(self, report: CycleReport) -> None:
        allowed, reason = self.risk.can_open_position(self.portfolio)
        if not allowed:
            report.note_skip(reason)
            log.debug("Not opening positions: %s", reason)
            return

        try:
            candidates = self.data.discover(
                self.config.data.search_terms,
                use_boosted_feed=self.config.data.use_boosted_feed,
                use_token_profiles=self.config.data.use_token_profiles,
                max_candidates=self.config.data.max_candidates,
            )
        except Exception as exc:  # pragma: no cover - network dependent
            log.error("Discovery failed: %s", exc)
            report.errors.append(f"discovery: {exc}")
            return

        report.scanned = len(candidates)
        filtered = self.filter.apply(candidates)
        report.passed_filters = len(filtered.passed)
        log.debug("Scanned %d pairs, %d passed (%s)", report.scanned, report.passed_filters, filtered.summary())

        signals = self.strategy.generate_entries(filtered.passed)
        report.signals = len(signals)

        opened = 0
        for signal_obj in signals:
            if opened >= self.config.strategy.max_new_positions_per_cycle:
                break
            allowed, reason = self.risk.can_open_position(self.portfolio)
            if not allowed:
                report.note_skip(reason)
                break
            allowed, reason = self.risk.can_enter_token(self.portfolio, signal_obj.token.address)
            if not allowed:
                report.note_skip(reason)
                continue
            before = len(report.opened)
            self._open_position(signal_obj, report)
            if len(report.opened) > before:
                opened += 1

    # ---- one cycle -------------------------------------------------------------
    def run_cycle(self) -> CycleReport:
        report = CycleReport()
        self.cycles += 1

        self.manage_positions(report)

        halted, reason = self.risk.should_halt(self.portfolio)
        if halted:
            report.halted_reason = reason
            log.warning("Trading halted: %s", reason)
        else:
            self.scan_for_entries(report)

        self.portfolio.snapshot_equity()
        log.info(
            "cycle %d | equity $%.2f (cash $%.2f, %d open) | scanned %d -> %d passed -> %d signals | "
            "+%d/-%d trades | return %+.2f%%",
            self.cycles, self.portfolio.equity, self.portfolio.cash, len(self.portfolio.positions),
            report.scanned, report.passed_filters, report.signals,
            len(report.opened), len(report.closed), self.portfolio.total_return_pct * 100.0,
        )
        return report

    # ---- main loop -------------------------------------------------------------
    def run(self, max_cycles: Optional[int] = None, sleep=time.sleep) -> None:
        blocked = self.preflight()
        if blocked:
            raise RuntimeError(f"Executor is not ready: {blocked}")

        log.info(
            "Starting memebot | mode=%s | %s | equity $%.2f | strategy=%s",
            self.config.mode, self.executor.describe(), self.portfolio.equity, self.strategy.name,
        )
        if self.config.mode == Mode.LIVE.value:
            log.warning("LIVE MODE - real funds are at risk on every order")

        self.install_signal_handlers()
        while not self._stop:
            started = time.monotonic()
            try:
                self.run_cycle()
            except Exception as exc:  # pragma: no cover - defensive
                log.exception("Cycle failed: %s", exc)
            if max_cycles is not None and self.cycles >= max_cycles:
                break
            if self._stop:
                break
            elapsed = time.monotonic() - started
            sleep(max(0.0, self.config.poll_interval_seconds - elapsed))

        log.info(
            "Stopped after %d cycles | equity $%.2f | realized pnl $%.2f",
            self.cycles, self.portfolio.equity, self.storage.trade_stats()["realized_pnl_usd"],
        )

    def liquidate_all(self, reason: str = "manual liquidation") -> CycleReport:
        """Close every open position at the current market price."""
        report = CycleReport()
        self._refresh_positions()
        for position in list(self.portfolio.open_positions):
            self._close_position(position, reason, report)
        self.portfolio.snapshot_equity()
        return report
