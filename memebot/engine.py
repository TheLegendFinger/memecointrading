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
from typing import Callable, Dict, List, Optional

from .config import BotConfig
from .data.dexscreener import DexScreenerClient
from .data.jupiter import JupiterClient
from .execution import build_executor
from .execution.base import Executor
from .models import Fill, Order, PairSnapshot, Position, Side, Signal
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
    # The best-scoring candidates this cycle, for the live display: (score, pair).
    top_candidates: List[tuple] = field(default_factory=list)

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
        on_cycle: Optional[Callable[["TradingEngine", CycleReport], None]] = None,
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
        self.portfolio = portfolio or Portfolio(self.storage)
        self.risk = RiskManager(config.risk)
        self.filter = CandidateFilter(config.filters)
        self.strategy = build_strategy(config.strategy.name, config.strategy)

        self._stop = False
        self.cycles = 0
        # Called after every cycle. The console live view uses this to redraw;
        # a failure in it must never stop the bot trading.
        self.on_cycle = on_cycle

    # ---- activity feed ---------------------------------------------------------
    def emit(self, kind: str, message: str, symbol: str = "", address: str = "",
             level: str = "info", detail: str = "") -> None:
        """Record one line for the live view's action feed.

        Never let the feed break trading: a storage hiccup here is logged and
        swallowed rather than aborting a cycle mid-order.
        """
        try:
            self.storage.record_event(kind, message, symbol=symbol, address=address,
                                      level=level, detail=detail)
        except Exception as exc:  # noqa: BLE001 - the feed is cosmetic, trading is not
            log.debug("Could not record event: %s", exc)

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

    # ---- live balance ----------------------------------------------------------
    def sync_live_balance(self) -> Optional[float]:
        """Replace our bookkeeping cash with the wallet's actual balance.

        A wallet changes underneath us: you might send SOL in or out, a swap can
        land after we gave up waiting, fees accrue. Reading the chain at the top
        of every cycle keeps position sizing honest - the bot can never size a
        trade against money that is not there.
        """
        cash = self.executor.available_cash_usd()
        if cash is None:
            log.warning("Could not read the wallet balance; keeping the last known cash figure")
            return None

        previous = self.portfolio.cash
        self.portfolio.set_cash(cash)

        # First cycle: anchor the return baseline to what the wallet actually
        # held. Nothing is configured; this is where the number comes from.
        if not self.storage.get_state("live_baseline_set"):
            self.portfolio.set_starting_cash(self.portfolio.equity)
            self.storage.set_state("live_baseline_set", True)
            log.info("Live baseline set at $%.2f", self.portfolio.equity)
        elif abs(cash - previous) > max(1.0, previous * 0.02):
            log.info("Wallet cash moved $%.2f -> $%.2f since the last cycle", previous, cash)
        return cash

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
            # Every observation is a chartable point, so the live view has a
            # price history even where no external OHLC source is reachable.
            try:
                self.storage.record_price_sample(position.token.address, pair.price_usd)
            except Exception as exc:  # noqa: BLE001
                log.debug("Could not record a price sample: %s", exc)
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
        fill = self.executor.execute(order)
        if not fill.ok:
            log.warning("SELL %s failed: %s", position.token, fill.error)
            self.emit("error", f"Sell {position.token.symbol} failed", level="error",
                      symbol=position.token.symbol, address=position.token.address,
                      detail=fill.error)
            report.errors.append(f"sell {position.token.symbol}: {fill.error}")
            return

        realized = self.portfolio.apply_fill(fill)
        self.risk.record_close(realized)
        report.closed.append(fill)
        self.emit(
            "sell",
            f"Sold {position.token.symbol} for ${fill.usd_amount - fill.fee_usd:,.2f} "
            f"({realized:+,.2f})",
            symbol=position.token.symbol, address=position.token.address,
            level="win" if realized > 0 else "loss",
            detail=reason,
        )
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
        fill = self.executor.execute(order)
        if not fill.ok:
            log.warning("BUY %s failed: %s", signal_obj.token, fill.error)
            self.emit("error", f"Buy {signal_obj.token.symbol} failed", level="error",
                      symbol=signal_obj.token.symbol, address=signal_obj.token.address,
                      detail=fill.error)
            report.errors.append(f"buy {signal_obj.token.symbol}: {fill.error}")
            return

        self.portfolio.apply_fill(fill)
        report.opened.append(fill)
        self.emit(
            "buy",
            f"Bought {signal_obj.token.symbol} for ${fill.usd_amount:,.2f} @ {fill.price:.8g}",
            symbol=signal_obj.token.symbol, address=signal_obj.token.address,
            detail=f"score {signal_obj.score:.2f} | slip {fill.slippage_bps:.0f}bps | "
                   f"fee ${fill.fee_usd:.2f}",
        )
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
                feed_limit=self.config.data.feed_limit,
            )
        except Exception as exc:  # pragma: no cover - network dependent
            log.error("Discovery failed: %s", exc)
            report.errors.append(f"discovery: {exc}")
            return

        report.scanned = len(candidates)
        for candidate in candidates[:40]:
            try:
                self.storage.record_price_sample(candidate.base.address, candidate.price_usd)
            except Exception as exc:  # noqa: BLE001
                log.debug("Could not record a price sample: %s", exc)
        filtered = self.filter.apply(candidates)
        report.passed_filters = len(filtered.passed)
        log.debug("Scanned %d pairs, %d passed (%s)", report.scanned, report.passed_filters, filtered.summary())

        signals = self.strategy.generate_entries(filtered.passed)
        report.signals = len(signals)
        report.top_candidates = sorted(
            ((self.strategy.score(pair), pair) for pair in filtered.passed),
            key=lambda item: item[0], reverse=True,
        )[:5]

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

        self.sync_live_balance()
        self.manage_positions(report)

        halted, reason = self.risk.should_halt(self.portfolio)
        if halted:
            report.halted_reason = reason
            log.warning("Trading halted: %s", reason)
            self.emit("halt", "Trading halted", level="error", detail=reason)
        else:
            self.scan_for_entries(report)

        self.portfolio.snapshot_equity()
        # The dashboard's "last cycle" heartbeat. Written here rather than only
        # by the serverless endpoint, so a bot running on your own machine keeps
        # it current too.
        try:
            self.storage.set_state("last_cycle_at", time.time())
            self.storage.set_state("cycles_run", int(self.storage.get_state("cycles_run", 0) or 0) + 1)
        except Exception as exc:  # noqa: BLE001
            log.debug("Could not update the heartbeat: %s", exc)

        # A feed of "nothing happened" is not worth reading. Cycles are recorded
        # when something actually occurred, plus an occasional heartbeat so you
        # can see it is still alive.
        eventful = bool(report.opened or report.closed or report.errors or report.halted_reason)
        if eventful or self.cycles % 10 == 1:
            self.emit(
                "cycle",
                f"Cycle {self.cycles}: scanned {report.scanned}, {report.passed_filters} passed, "
                f"{report.signals} signal(s)",
                detail=f"equity ${self.portfolio.equity:,.2f} | "
                       f"{len(self.portfolio.positions)} open | "
                       f"{len(report.opened)} bought, {len(report.closed)} sold",
            )
        if self.cycles % 20 == 0:
            try:
                self.storage.prune_events()
                self.storage.prune_price_samples(time.time() - 7 * 86400)
            except Exception as exc:  # noqa: BLE001
                log.debug("Could not prune the feed: %s", exc)
        log.info(
            "cycle %d | equity $%.2f (cash $%.2f, %d open) | scanned %d -> %d passed -> %d signals | "
            "+%d/-%d trades | return %+.2f%%",
            self.cycles, self.portfolio.equity, self.portfolio.cash, len(self.portfolio.positions),
            report.scanned, report.passed_filters, report.signals,
            len(report.opened), len(report.closed), self.portfolio.total_return_pct * 100.0,
        )

        if self.on_cycle is not None:
            try:
                self.on_cycle(self, report)
            except Exception as exc:  # noqa: BLE001 - the display is not the job
                log.debug("Cycle observer failed: %s", exc)
        return report

    # ---- main loop -------------------------------------------------------------
    def run(self, max_cycles: Optional[int] = None, sleep=time.sleep) -> None:
        blocked = self.preflight()
        if blocked:
            raise RuntimeError(f"Executor is not ready: {blocked}")

        log.info(
            "Starting memebot | %s | equity $%.2f | strategy=%s",
            self.executor.describe(), self.portfolio.equity, self.strategy.name,
        )
        self.emit("start", "Started trading", level="warn",
                  detail=f"equity ${self.portfolio.equity:,.2f} | {self.executor.describe()}")
        log.warning("Real funds are at risk on every order")

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
        self.emit("stop", f"Stopped after {self.cycles} cycle(s)",
                  detail=f"equity ${self.portfolio.equity:,.2f}")

    def liquidate_all(self, reason: str = "manual liquidation") -> CycleReport:
        """Close every open position at the current market price."""
        report = CycleReport()
        self._refresh_positions()
        for position in list(self.portfolio.open_positions):
            self._close_position(position, reason, report)
        self.portfolio.snapshot_equity()
        return report
