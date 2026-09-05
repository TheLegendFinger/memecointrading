"""Connectivity and configuration checks.

The bot depends on third-party APIs that occasionally move or rate limit. This
module pings each one, reports what came back, and says plainly whether the bot
can trade right now - so a quiet cycle ("scanned 0 pairs") can be diagnosed as
a network problem rather than a boring market.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional

from .config import BotConfig
from .data.dexscreener import DexScreenerClient
from .data.jupiter import JupiterClient
from .models import USDC_MINT, WSOL_MINT
from .storage import is_postgres_dsn, open_storage

OK = "ok"
WARN = "warn"
FAIL = "fail"


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    elapsed_ms: float = 0.0

    @property
    def icon(self) -> str:
        return {OK: "PASS", WARN: "WARN", FAIL: "FAIL"}.get(self.status, "?")


@dataclass
class Report:
    checks: List[Check] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "", elapsed_ms: float = 0.0) -> Check:
        check = Check(name, status, detail, elapsed_ms)
        self.checks.append(check)
        return check

    def run(self, name: str, fn: Callable[[], Any]) -> Check:
        """Run a probe, timing it and turning exceptions into FAIL rows."""
        started = time.monotonic()
        try:
            status, detail = fn()
        except Exception as exc:  # noqa: BLE001 - a failed probe is a result, not a crash
            status, detail = FAIL, f"{type(exc).__name__}: {exc}"
        return self.add(name, status, detail, (time.monotonic() - started) * 1000.0)

    @property
    def failures(self) -> List[Check]:
        return [c for c in self.checks if c.status == FAIL]

    @property
    def warnings(self) -> List[Check]:
        return [c for c in self.checks if c.status == WARN]

    @property
    def healthy(self) -> bool:
        return not self.failures


def _describe_state(config: BotConfig) -> str:
    if is_postgres_dsn(config.state_db):
        # Never print credentials.
        tail = config.state_db.split("@")[-1]
        return f"postgres ({tail})"
    return f"sqlite ({config.state_db})"


def run_checks(
    config: BotConfig,
    deep: bool = False,
    data: Optional[DexScreenerClient] = None,
    jupiter: Optional[JupiterClient] = None,
) -> Report:
    """Probe every dependency the configured mode needs.

    `deep` additionally runs a real Jupiter route quote, which is the closest
    thing to a dry run of order execution. The clients can be injected, which
    is what the tests do.
    """
    report = Report()
    # The clients log their own warnings; here the report is the output, so
    # suppress the duplicates for the duration of the checks.
    noisy = [logging.getLogger("memebot.data.dexscreener"), logging.getLogger("memebot.data.jupiter")]
    previous = [(logger, logger.level) for logger in noisy]
    for logger in noisy:
        logger.setLevel(logging.ERROR)
    try:
        return _run_checks(config, deep, report, data, jupiter)
    finally:
        for logger, level in previous:
            logger.setLevel(level)


def _run_checks(
    config: BotConfig,
    deep: bool,
    report: Report,
    data: Optional[DexScreenerClient] = None,
    jupiter: Optional[JupiterClient] = None,
) -> Report:

    # ---- configuration -----------------------------------------------------
    report.add(
        "config",
        OK,
        f"strategy={config.strategy.name} min_score={config.strategy.min_score} "
        f"state={_describe_state(config)}",
    )

    # ---- state store -------------------------------------------------------
    def check_state():
        store = open_storage(config.state_db)
        try:
            positions = len(store.load_positions())
            trades = store.trade_stats()["closed_trades"]
            return OK, f"reachable, {positions} open position(s), {trades} closed trade(s)"
        finally:
            store.close()

    report.run("state store", check_state)

    # ---- DexScreener -------------------------------------------------------
    data = data or DexScreenerClient(
        base_url=config.data.dexscreener_base_url,
        chain=config.data.chain,
        timeout=config.data.request_timeout,
        max_retries=1,
        rate_limit_per_minute=config.data.rate_limit_per_minute,
        cache_ttl_seconds=0.0,
    )

    def check_search():
        # Go through the raw HTTP client so a transport error surfaces here
        # rather than being swallowed into an empty result.
        payload = data.http.get("/latest/dex/search", params={"q": "SOL"})
        raw = (payload or {}).get("pairs") or []
        pairs = data.search("SOL")
        if not raw:
            return FAIL, "endpoint answered but returned no pairs - it may have moved"
        if not pairs:
            return WARN, f"{len(raw)} pairs returned, none on chain '{config.data.chain}'"
        sample = pairs[0]
        return OK, f"{len(pairs)} {config.data.chain} pairs; e.g. {sample.base.symbol} at ${sample.price_usd:.8g}"

    report.run("dexscreener search", check_search)

    def check_boosts():
        payload = data.http.get("/token-boosts/top/v1")
        addresses = data.boosted_tokens(limit=10)
        if not isinstance(payload, list):
            return WARN, "boost feed returned an unexpected shape"
        if not addresses:
            return WARN, f"boost feed has {len(payload)} entries, none on {config.data.chain}"
        return OK, f"{len(addresses)} boosted token(s)"

    report.run("dexscreener boosts", check_boosts)

    def check_token_pairs():
        data.http.get(f"/token-pairs/v1/{config.data.chain}/{USDC_MINT}")
        pair = data.best_pair(USDC_MINT)
        if pair is None:
            return FAIL, "token-pairs answered but no usable USDC pool was parsed"
        return OK, f"deepest USDC pool has ${pair.liquidity_usd:,.0f} liquidity"

    report.run("dexscreener token pairs", check_token_pairs)

    # ---- Jupiter -----------------------------------------------------------
    jupiter = jupiter or JupiterClient(
        quote_url=config.data.jupiter_quote_url,
        price_url=config.data.jupiter_price_url,
        timeout=config.data.request_timeout,
        max_retries=1,
        rate_limit_per_minute=config.data.jupiter_rate_limit_per_minute,
    )

    def check_price():
        # Ask the client, not the configured URL directly: it walks the known
        # endpoints when one has been retired, and reporting a 404 the bot
        # itself recovers from would be a false alarm.
        configured = jupiter.price_url
        price = jupiter.price(WSOL_MINT)

        if price > 0:
            detail = f"SOL = ${price:,.2f}"
            if jupiter.price_url != configured:
                detail += (f" (via {jupiter.price_url} - {configured} is retired; "
                           "update data.jupiter_price_url in your config or delete the line)")
            if jupiter.api_key:
                detail += " (api key in use)"
            return OK, detail

        # Nothing worked. Probe directly so the real transport error surfaces.
        jupiter.http.get(configured, params={"ids": WSOL_MINT})
        return FAIL, f"{configured} answered but no SOL price was parsed - the shape changed"

    report.run("jupiter price", check_price)

    if deep:
        def check_quote():
            quote = jupiter.quote(WSOL_MINT, USDC_MINT, 100_000_000, 100)  # 0.1 SOL
            if quote is None:
                return FAIL, "no route for 0.1 SOL -> USDC"
            out = quote.out_amount / 1e6
            route = " > ".join(quote.route_labels) or "direct"
            return OK, f"0.1 SOL routes to {out:,.2f} USDC via {route} (impact {quote.price_impact_pct:.3f}%)"

        report.run("jupiter routing", check_quote)

    # ---- the funnel, end to end -------------------------------------------
    def check_pipeline():
        from .strategy import CandidateFilter, build_strategy

        candidates = data.discover(
            config.data.search_terms,
            use_boosted_feed=config.data.use_boosted_feed,
            use_token_profiles=config.data.use_token_profiles,
            max_candidates=config.data.max_candidates,
        )
        if not candidates:
            return FAIL, "discovery returned no pairs at all"

        result = CandidateFilter(config.filters).apply(candidates)
        strategy = build_strategy(config.strategy.name, config.strategy)
        scores = sorted((strategy.score(p) for p in result.passed), reverse=True)
        tradable = [s for s in scores if s >= config.strategy.min_score]

        detail = (
            f"{len(candidates)} scanned -> {len(result.passed)} passed filters -> "
            f"{len(tradable)} above min_score {config.strategy.min_score:.2f}"
        )
        if result.rejections:
            detail += f" | top rejections: {result.summary()}"
        if not result.passed:
            return WARN, detail + " | filters are rejecting everything - loosen them"
        if not tradable:
            return WARN, detail + f" | best score was {scores[0]:.2f}; lower min_score to trade"
        return OK, detail

    report.run("candidate pipeline", check_pipeline)

    # ---- trading readiness -------------------------------------------------
    def check_execution():
        from .execution import build_executor

        executor = build_executor(config, data=data, jupiter=jupiter)
        blocked = executor.preflight()
        if blocked:
            return FAIL, blocked
        return OK, executor.describe()

    report.run("execution", check_execution)
    return report


def format_report(report: Report) -> str:
    width = max(len(c.name) for c in report.checks) if report.checks else 10
    lines = []
    for check in report.checks:
        timing = f"{check.elapsed_ms:6.0f}ms" if check.elapsed_ms else " " * 8
        lines.append(f"  [{check.icon}] {check.name.ljust(width)}  {timing}  {check.detail}")
    return "\n".join(lines)
