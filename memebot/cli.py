"""Command line interface.

    python -m memebot run --config config.yaml
    python -m memebot scan --limit 15
    python -m memebot status
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import __version__
from .config import BotConfig, load_config
from .engine import TradingEngine
from .logging_utils import setup_logging
from .models import Mode
from .storage import Storage, open_storage

# `wallet` exit codes - scripts branch on these, so they are part of the API.
NO_WALLET = 2          # nothing configured; offering to create one is right
WALLET_NOT_READY = 3   # a wallet exists but cannot trade yet (unfunded, or
                       # the RPC is unreachable). Never offer to create another:
                       # the existing one may hold funds.


# --------------------------------------------------------------------------------
# small formatting helpers
# --------------------------------------------------------------------------------
def _money(value: float) -> str:
    return f"${value:,.2f}"


def _pct(value: float) -> str:
    return f"{value:+.2f}%"


def _ts(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _table(rows: List[List[str]], headers: List[str]) -> str:
    if not rows:
        return "(nothing to show)"
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    sep = "  ".join("-" * widths[i] for i in range(len(headers)))
    body = "\n".join("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)) for row in rows)
    return f"{line}\n{sep}\n{body}"


def _build_engine(config: BotConfig) -> TradingEngine:
    return TradingEngine(config)


# --------------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------------
def cmd_run(args: argparse.Namespace, config: BotConfig) -> int:
    engine = _build_engine(config)

    if config.mode == Mode.LIVE.value and not args.yes:
        wallet = getattr(engine.executor, "wallet_address", "<unknown>")
        print("\n*** LIVE TRADING ***")
        print(f"  wallet : {wallet}")
        print(f"  rpc    : {config.execution.rpc_url}")
        print(f"  size   : up to {_money(config.risk.max_position_usd)} per position, "
              f"{config.risk.max_open_positions} concurrent")
        answer = input("Type 'trade' to start placing real orders: ").strip().lower()
        if answer != "trade":
            print("Aborted.")
            return 1

    try:
        engine.run(max_cycles=args.cycles)
    except KeyboardInterrupt:  # pragma: no cover - interactive
        print("\nInterrupted.")
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_once(args: argparse.Namespace, config: BotConfig) -> int:
    engine = _build_engine(config)
    blocked = engine.preflight()
    if blocked:
        print(f"error: {blocked}", file=sys.stderr)
        return 1
    report = engine.run_cycle()
    print(
        f"scanned={report.scanned} passed={report.passed_filters} signals={report.signals} "
        f"opened={len(report.opened)} closed={len(report.closed)}"
    )
    for reason, count in sorted(report.skipped.items(), key=lambda kv: -kv[1]):
        print(f"  skipped: {reason} x{count}")
    for error in report.errors:
        print(f"  error: {error}")
    return 0


def cmd_scan(args: argparse.Namespace, config: BotConfig) -> int:
    """Show what the bot sees right now, without trading."""
    engine = _build_engine(config)
    candidates = engine.data.discover(
        config.data.search_terms,
        use_boosted_feed=config.data.use_boosted_feed,
        use_token_profiles=config.data.use_token_profiles,
        max_candidates=config.data.max_candidates,
    )
    result = engine.filter.apply(candidates)
    scored = sorted(
        ((engine.strategy.score(p), p) for p in result.passed), key=lambda t: t[0], reverse=True
    )

    print(f"\nScanned {len(candidates)} pairs; {len(result.passed)} passed filters.")
    if result.rejections:
        print(f"Rejections: {result.summary()}")

    rows = []
    for score, pair in scored[: args.limit]:
        rows.append([
            pair.base.symbol[:12] or pair.base.address[:6],
            f"{score:.3f}",
            f"{pair.price_usd:.8g}",
            f"{pair.change('m5'):+.1f}%",
            f"{pair.change('h1'):+.1f}%",
            f"${pair.liquidity_usd:,.0f}",
            f"${pair.vol('h1'):,.0f}",
            f"{pair.buy_ratio('h1') * 100:.0f}%",
            f"{pair.age_minutes / 60:.1f}h" if pair.age_minutes < 1e9 else "?",
        ])
    print()
    print(_table(rows, ["SYMBOL", "SCORE", "PRICE", "5M", "1H", "LIQ", "VOL1H", "BUY%", "AGE"]))
    tradable = [s for s, _ in scored if s >= config.strategy.min_score]
    print(f"\n{len(tradable)} candidate(s) at or above the {config.strategy.min_score:.2f} entry threshold.")
    return 0


def cmd_menu(args: argparse.Namespace, config: BotConfig) -> int:
    """The numbered menu - everything reachable by typing a digit."""
    from .menu import Menu

    return Menu(config_path=args.config).run()


def cmd_wallet(args: argparse.Namespace, config: BotConfig) -> int:
    """Create a wallet, or show the configured one and its balance."""
    from .wallet import (
        WalletError, address_from_secret, append_to_env, configured_address, create_keypair,
    )

    if args.new:
        try:
            address, secret = create_keypair()
        except WalletError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

        print("\n  New Solana wallet created.\n")
        print(f"  Address     {address}")
        print(f"  Private key {secret}")
        print("\n  " + "!" * 68)
        print("  This private key is shown ONCE and controls every token in the wallet.")
        print("  Save it somewhere safe. Anyone who has it can drain the wallet.")
        print("  " + "!" * 68)

        if args.save:
            try:
                append_to_env({"SOLANA_PRIVATE_KEY": secret})
            except WalletError as exc:
                print(f"\n  Not saved: {exc}")
                return 1
            print("\n  Saved to .env (which is gitignored - keep it that way).")
        else:
            print("\n  To use it, add this line to your .env file:")
            print(f"    SOLANA_PRIVATE_KEY={secret}")
            print("  ...or re-run with --save to have it written for you.")

        print(f"\n  Then fund it: send SOL to {address}")
        print("  Start with a small amount you are willing to lose entirely.\n")
        return 0

    # ---- show the configured wallet ----
    try:
        address = configured_address()
    except WalletError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if not address:
        print("\nNo wallet configured.")
        print("  Create one : python -m memebot wallet --new --save")
        print("  Or set SOLANA_PRIVATE_KEY (base58) / SOLANA_KEYPAIR_PATH in .env\n")
        return NO_WALLET

    not_ready = False
    print(f"\n  Address   {address}")
    print(f"  Explorer  https://solscan.io/account/{address}")

    from .execution.live import LiveExecutor

    executor = LiveExecutor(config)
    try:
        summary = executor.wallet_summary()
    except Exception as exc:  # noqa: BLE001 - a balance lookup failure is informational
        print(f"\n  Balance   could not be read: {exc}")
        print("\n  The wallet key is fine - the network is the problem. Check your")
        print("  internet connection, or set MEMEBOT_RPC_URL in .env to a private RPC.")
        print("  Do NOT create another wallet; this one may already hold funds.\n")
        return WALLET_NOT_READY

    sol = summary["sol_balance"]
    print(f"  RPC       {summary['rpc_url']}")
    print(f"\n  SOL       {sol:.6f}" + (f"  ({_money(summary['sol_value_usd'])})"
                                        if summary.get("sol_value_usd") else ""))
    if "quote_balance" in summary:
        print(f"  Quote     {summary['quote_balance']:,.4f} of {summary['quote_mint'][:6]}...")

    available = summary.get("available_cash_usd")
    if available is not None:
        print(f"  Tradable  {_money(available)}  "
              f"(after a {summary['fee_reserve_sol']:.3f} SOL fee reserve)")
        size = min(config.risk.max_position_usd, available * config.risk.position_size_pct)
        if available <= 0:
            print("\n  Not enough SOL to trade. Send some to the address above.")
            not_ready = True
        elif size < config.risk.min_position_usd:
            print(f"\n  Too small to trade: a position would be {_money(size)}, below the "
                  f"{_money(config.risk.min_position_usd)} minimum.")
            print("  Add more SOL, or lower risk.min_position_usd in your config.")
            not_ready = True
        else:
            print(f"\n  At the current settings each position would be about {_money(size)}, "
                  f"up to {config.risk.max_open_positions} at once.")

    armed = summary.get("armed")
    print(f"\n  Live trading {'ARMED' if armed else 'not armed'}"
          + ("" if armed else " (scripts\\live.ps1 arms it for its own window)"))
    print()
    return WALLET_NOT_READY if not_ready else 0


def cmd_doctor(args: argparse.Namespace, config: BotConfig) -> int:
    """Check every dependency and say whether the bot can actually trade."""
    from .doctor import FAIL, format_report, run_checks

    print(f"\nmemebot {__version__} health check ({config.mode} mode)\n")
    report = run_checks(config, deep=not args.quick)
    print(format_report(report))

    if report.failures:
        print(f"\n{len(report.failures)} check(s) FAILED. The bot will run but see an empty market.")
        print("Common causes: no internet, a corporate/VPN proxy, or an API endpoint that moved")
        print("(override data.dexscreener_base_url / data.jupiter_price_url in your config).")
        return 1
    if report.warnings:
        print(f"\nAll dependencies reachable, with {len(report.warnings)} warning(s) above.")
        return 0
    print("\nAll checks passed - the bot is seeing the live market.")
    return 0


def cmd_status(args: argparse.Namespace, config: BotConfig) -> int:
    storage = open_storage(config.state_db)
    from .portfolio import Portfolio

    portfolio = Portfolio(storage, config.risk.starting_cash_usd, mode=config.mode)
    stats = portfolio.stats()

    if args.json:
        print(json.dumps(stats, indent=2, default=str))
        return 0

    print(f"\nmemebot {__version__} | mode={config.mode} | db={config.state_db}")
    print("-" * 62)
    print(f"  equity            {_money(stats['equity_usd'])}")
    print(f"  cash              {_money(stats['cash_usd'])}")
    print(f"  positions value   {_money(stats['positions_value_usd'])} ({stats['open_positions']} open)")
    print(f"  starting cash     {_money(stats['starting_cash_usd'])}")
    print(f"  total return      {_pct(stats['total_return_pct'])}")
    print(f"  realized pnl      {_money(stats['realized_pnl_usd'])}")
    print(f"  unrealized pnl    {_money(stats['unrealized_pnl_usd'])}")
    print(f"  fees paid         {_money(stats['total_fees_usd'])}")
    print(f"  closed trades     {stats['closed_trades']} "
          f"({stats['wins']}W/{stats['losses']}L, {stats['win_rate'] * 100:.0f}% win rate)")

    if portfolio.positions:
        print("\nOpen positions:")
        rows = []
        for pos in portfolio.open_positions:
            rows.append([
                pos.token.symbol[:12] or pos.token.address[:6],
                f"{pos.quantity:,.4f}",
                f"{pos.avg_price:.8g}",
                f"{pos.last_price:.8g}",
                _money(pos.market_value),
                f"{pos.unrealized_pnl_pct * 100:+.1f}%",
                f"{pos.age_minutes:.0f}m",
            ])
        print(_table(rows, ["SYMBOL", "QTY", "AVG", "LAST", "VALUE", "PNL", "AGE"]))
    storage.close()
    return 0


def cmd_trades(args: argparse.Namespace, config: BotConfig) -> int:
    storage = open_storage(config.state_db)
    trades = storage.list_trades(limit=args.limit)
    if args.json:
        print(json.dumps([dict(t) for t in trades], indent=2, default=str))
        storage.close()
        return 0
    rows = []
    for t in trades:
        rows.append([
            _ts(t["ts"]),
            t["side"].upper(),
            (t["symbol"] or t["token_address"][:6])[:12],
            f"{t['price']:.8g}",
            f"{t['token_amount']:,.4f}",
            _money(t["usd_amount"]),
            _money(t["fee_usd"]),
            _money(t["realized_pnl"]) if t["side"] == "sell" else "-",
            (t["reason"] or "")[:44],
        ])
    print(_table(rows, ["TIME (UTC)", "SIDE", "SYMBOL", "PRICE", "QTY", "USD", "FEE", "PNL", "REASON"]))
    storage.close()
    return 0


def cmd_liquidate(args: argparse.Namespace, config: BotConfig) -> int:
    engine = _build_engine(config)
    if not engine.portfolio.positions:
        print("No open positions.")
        return 0
    if not args.yes:
        answer = input(f"Close all {len(engine.portfolio.positions)} position(s)? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Aborted.")
            return 1
    report = engine.liquidate_all()
    print(f"Closed {len(report.closed)} position(s). Equity now {_money(engine.portfolio.equity)}.")
    for error in report.errors:
        print(f"  error: {error}")
    return 0


def cmd_reset(args: argparse.Namespace, config: BotConfig) -> int:
    if config.mode == Mode.LIVE.value:
        print("Refusing to reset state while mode=live (your on-chain balances would not match).",
              file=sys.stderr)
        return 1
    if not args.yes:
        answer = input(f"Wipe all paper state in {config.state_db}? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Aborted.")
            return 1
    storage = open_storage(config.state_db)
    storage.reset()
    storage.close()
    print(f"State reset. Starting cash is {_money(config.risk.starting_cash_usd)}.")
    return 0


def cmd_config(args: argparse.Namespace, config: BotConfig) -> int:
    print(json.dumps(config.to_dict(), indent=2, default=str))
    return 0


# --------------------------------------------------------------------------------
# argument parsing
# --------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memebot",
        description="A Solana memecoin trading bot. Paper trading by default.",
    )
    parser.add_argument("--version", action="version", version=f"memebot {__version__}")
    parser.add_argument("-c", "--config", help="path to a YAML or JSON config file")
    parser.add_argument("--mode", choices=[Mode.PAPER.value, Mode.LIVE.value],
                        help="override the trading mode")
    parser.add_argument("--db", help="override the state database path")
    parser.add_argument("--log-level", help="DEBUG, INFO, WARNING, ERROR")
    parser.add_argument("--dry-run", action="store_true",
                        help="evaluate and log decisions without sending any order")

    sub = parser.add_subparsers(dest="command", required=False)

    menu = sub.add_parser("menu", help="the numbered menu (default when run with no command)")
    menu.set_defaults(func=cmd_menu)

    run = sub.add_parser("run", help="run the trading loop")
    run.add_argument("--cycles", type=int, default=None, help="stop after N cycles")
    run.add_argument("--interval", type=float, default=None, help="seconds between cycles")
    run.add_argument("-y", "--yes", action="store_true", help="skip the live-trading confirmation")
    run.set_defaults(func=cmd_run)

    once = sub.add_parser("once", help="run a single cycle and exit")
    once.set_defaults(func=cmd_once)

    scan = sub.add_parser("scan", help="show scored candidates without trading")
    scan.add_argument("--limit", type=int, default=20)
    scan.set_defaults(func=cmd_scan)

    wallet = sub.add_parser("wallet", help="create or inspect the live trading wallet")
    wallet.add_argument("--new", action="store_true", help="generate a new wallet")
    wallet.add_argument("--save", action="store_true",
                        help="with --new, write the key to .env (never overwrites)")
    wallet.set_defaults(func=cmd_wallet)

    doctor = sub.add_parser("doctor", help="check API connectivity and configuration")
    doctor.add_argument("--quick", action="store_true",
                        help="skip the Jupiter routing probe")
    doctor.set_defaults(func=cmd_doctor)

    status = sub.add_parser("status", help="portfolio summary")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)

    trades = sub.add_parser("trades", help="recent trade history")
    trades.add_argument("--limit", type=int, default=25)
    trades.add_argument("--json", action="store_true")
    trades.set_defaults(func=cmd_trades)

    liquidate = sub.add_parser("liquidate", help="close every open position now")
    liquidate.add_argument("-y", "--yes", action="store_true")
    liquidate.set_defaults(func=cmd_liquidate)

    reset = sub.add_parser("reset", help="wipe paper trading state")
    reset.add_argument("-y", "--yes", action="store_true")
    reset.set_defaults(func=cmd_reset)

    show = sub.add_parser("config", help="print the effective configuration")
    show.set_defaults(func=cmd_config)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # `python -m memebot` with no command opens the menu, so newcomers never
    # have to know a single flag.
    if args.command is None:
        args.command = "menu"
        args.func = cmd_menu

    overrides: Dict[str, Any] = {
        "mode": args.mode,
        "state_db": args.db,
        "log_level": args.log_level,
        "dry_run": True if args.dry_run else None,
    }
    if getattr(args, "interval", None):
        overrides["poll_interval_seconds"] = args.interval

    try:
        config = load_config(args.config, overrides)
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    setup_logging(config.log_level, config.log_file if args.command in ("run", "once") else None)
    return int(args.func(args, config) or 0)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
