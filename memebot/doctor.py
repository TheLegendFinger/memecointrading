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
from .data import DexScreenerClient, build_dexscreener, discover_candidates
from .data.jupiter import JupiterClient
from .http import HttpError
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
    data = data or build_dexscreener(config.data, max_retries=1, cache_ttl_seconds=0.0)

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

    # ---- GeckoTerminal pool feeds ------------------------------------------
    wants_pools = (config.data.use_trending_pools or config.data.use_top_pools
                   or config.data.use_new_pools)
    if wants_pools:
        def check_pools():
            gecko = getattr(data, "gecko", None)
            if gecko is None:
                return WARN, "no GeckoTerminal client - discovery is by name only"
            trending = gecko.trending_tokens(limit=10)
            if not trending:
                return WARN, (
                    "no trending pools came back - discovery falls back to name "
                    "search, which is narrower"
                )
            return OK, f"{len(trending)} trending token(s); e.g. {trending[0][:8]}..."

        report.run("geckoterminal pools", check_pools)

    # ---- the funnel, end to end -------------------------------------------
    def check_pipeline():
        from .strategy import CandidateFilter, build_strategy

        candidates = discover_candidates(data, config.data)
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
        sources = getattr(data, "last_sources", {}) or {}
        if sources:
            detail += " | found by: " + ", ".join(
                f"{name} {count}" for name, count in
                sorted(sources.items(), key=lambda kv: -kv[1])
            )
        if result.rejections:
            detail += f" | top rejections: {result.summary()}"
        if len(candidates) < 100:
            detail += (
                f" | only {len(candidates)} coins seen - widen data.search_terms"
            )
        if not result.passed:
            return WARN, detail + " | filters are rejecting everything - loosen them"
        if not tradable:
            # Naming the number matters: "lower min_score" without one sent
            # people hunting for a value that would have worked.
            suggestion = max(0.10, round(scores[0] - 0.05, 2))
            return WARN, detail + (
                f" | best score was {scores[0]:.2f} - nothing is running hard enough "
                f"right now. min_score {suggestion:.2f} would have taken it; leaving it "
                "where it is and waiting is the other answer"
            )
        return OK, detail

    report.run("candidate pipeline", check_pipeline)

    # ---- the on-chain safety reader ----------------------------------------
    if config.safety.enabled:
        def check_authorities():
            """The read that decides whether a coin can be bought at all.

            Probed against wrapped SOL, whose authorities are revoked and whose
            state is public - if that comes back wrong, the reader is broken
            rather than the token.
            """
            from .execution import build_executor

            executor = build_executor(config, data=data, jupiter=jupiter)
            rpc = getattr(executor, "rpc", None)
            if rpc is None:
                return WARN, "no RPC - tokens will not be checked on-chain"
            info = rpc.get_mint_account(WSOL_MINT)
            if not info or "mintAuthority" not in info:
                return FAIL, (
                    f"{config.execution.rpc_url} did not return a parsed mint account - "
                    "nothing can be bought, since an unreadable token counts as failed"
                )
            return OK, "mint and freeze authorities readable"

        report.run("chain: token authorities", check_authorities)

        def check_holders():
            """The optional read. Heavy, and free endpoints often refuse it."""
            from .execution import build_executor

            executor = build_executor(config, data=data, jupiter=jupiter)
            rpc = getattr(executor, "rpc", None)
            if rpc is None:
                return WARN, "no RPC"
            try:
                supply = rpc.get_token_supply(WSOL_MINT)
                holders = rpc.get_token_largest_accounts(WSOL_MINT)
            except HttpError as exc:
                blocked = config.safety.require_holder_data
                return (FAIL if blocked else WARN), (
                    f"{exc} | holder concentration will be skipped"
                    + (" AND every coin refused, because safety.require_holder_data "
                       "is on - turn it off, or use an RPC that answers "
                       "getTokenLargestAccounts (Helius, QuickNode, Alchemy)"
                       if blocked else
                       " - the authority checks still run, so this only costs a check")
                )
            if not holders:
                return WARN, "the node answered with no holders; concentration is skipped"
            return OK, f"supply and {len(holders)} largest accounts readable (wSOL {supply:,.0f})"

        report.run("chain: holder concentration", check_holders)

    # ---- what a trade costs -------------------------------------------------
    def check_trade_size():
        """Is the smallest allowed position big enough to survive its own fees?

        Most of the cost of a swap is flat - network fee, priority fee, the
        rent for a new token account - so it does not shrink with the trade.
        Halve the position and the fees stay put; at some size the round trip
        costs more than the move you are trading for.
        """
        e = config.execution
        flat = e.network_fee_usd + e.priority_fee_usd
        smallest = config.risk.min_position_usd
        # In and out: two swaps, each a flat cost plus the venue's percentage.
        round_trip = 2 * flat + 2 * smallest * (e.fee_bps / 10_000.0)
        share = round_trip / smallest if smallest > 0 else float("inf")
        detail = (
            f"smallest position ${smallest:,.2f}; a round trip costs about "
            f"${round_trip:.2f} ({share * 100:.0f}% of it)"
        )
        if share >= 0.5:
            return WARN, detail + (
                f" - it has to gain {share * 100:.0f}% just to break even. "
                "Raise risk.min_position_usd, or lower the priority fee."
            )
        if share >= 0.15:
            return WARN, detail + " - fees are a large share of a trade this size"
        return OK, detail

    report.run("trade size vs fees", check_trade_size)

    # ---- trading readiness -------------------------------------------------
    def check_execution():
        """Is this setup able to trade - wallet, RPC, funds?

        Arming is deliberately not part of it. It is a per-run acknowledgement
        that the menu and the scripts make for you the moment you start
        trading, so failing the health check over it says nothing about the
        setup and hides the three answers that do.
        """
        from .execution import build_executor
        from .execution.live import is_armed

        executor = build_executor(config, data=data, jupiter=jupiter)
        blocked = executor.preflight(require_arming=False)
        if blocked:
            return FAIL, blocked
        detail = executor.describe()
        if not is_armed():
            detail += " | arms itself when you start trading"
        return OK, detail

    report.run("execution", check_execution)
    return report


def format_report(report: Report) -> str:
    width = max(len(c.name) for c in report.checks) if report.checks else 10
    lines = []
    for check in report.checks:
        timing = f"{check.elapsed_ms:6.0f}ms" if check.elapsed_ms else " " * 8
        lines.append(f"  [{check.icon}] {check.name.ljust(width)}  {timing}  {check.detail}")
    return "\n".join(lines)
