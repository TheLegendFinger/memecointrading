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


def _build_engine(config: BotConfig, on_cycle=None) -> TradingEngine:
    return TradingEngine(config, on_cycle=on_cycle)


# --------------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------------
def cmd_run(args: argparse.Namespace, config: BotConfig) -> int:
    from .console_view import ConsoleView
    from .logging_utils import setup_logging

    view = ConsoleView(force=False if args.plain else None)
    setup_logging(config.log_level, config.log_file, console=not view.active)
    engine = _build_engine(config, on_cycle=view if view.active else None)

    if not args.yes:
        try:
            wallet = engine.executor.wallet_address
        except Exception as exc:  # noqa: BLE001 - report, do not trace
            print(f"\nerror: no usable wallet - {exc}", file=sys.stderr)
            return 1
        print("\n*** REAL MONEY ***")
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
        feed_limit=config.data.feed_limit,
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


def _print_new_wallet(address: str, secret: str, phrase: str, saved: bool) -> None:
    """Show a freshly created wallet. The phrase is the part that matters."""
    print("\n  New Solana wallet created.\n")
    print(f"  Address       {address}")
    print(f"  Explorer      https://solscan.io/account/{address}")
    if phrase:
        print("\n  SEED PHRASE (write this down, on paper):\n")
        words = phrase.split()
        for row in range(0, len(words), 4):
            line = "   ".join(f"{i + 1:>2}. {word:<10}" for i, word in
                              enumerate(words[row:row + 4], start=row))
            print(f"    {line}")
    print("\n  " + "!" * 68)
    print("  Anyone with this phrase can take everything in the wallet.")
    print("  Never type it into a website. Never share it. Write it on paper.")
    print("  " + "!" * 68)
    if phrase:
        print("\n  It restores this wallet in Phantom or Solflare:")
        print("  'Import wallet' > paste the phrase > pick the first account.")
    if saved:
        print("\n  Saved to .env, which git ignores. Back the phrase up anyway -")
        print("  losing that folder without a written copy loses the funds.")
    else:
        print("\n  Add these lines to .env to use it:")
        if phrase:
            print(f"    SOLANA_MNEMONIC={phrase}")
        print(f"    SOLANA_PRIVATE_KEY={secret}")
    print(f"\n  Fund it: send SOL to {address}")
    print("  Start with an amount you would be fine losing entirely.\n")


def _create_wallet(args: argparse.Namespace) -> int:
    from .wallet import (
        ENV_KEY, ENV_MNEMONIC_KEY, WalletError, append_to_env, create_keypair,
        create_wallet_with_phrase,
    )

    try:
        if args.no_phrase:
            address, secret = create_keypair()
            phrase = ""
        else:
            address, secret, phrase = create_wallet_with_phrase(words=args.words)
    except WalletError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    saved = False
    if args.save:
        values = {ENV_KEY: secret}
        if phrase:
            values[ENV_MNEMONIC_KEY] = phrase
        try:
            append_to_env(values)
            saved = True
        except WalletError as exc:
            _print_new_wallet(address, secret, phrase, saved=False)
            print(f"  Not saved: {exc}\n")
            return 1

    _print_new_wallet(address, secret, phrase, saved=saved)
    return 0


def _import_wallet(args: argparse.Namespace) -> int:
    from .wallet import (
        ENV_KEY, ENV_MNEMONIC_KEY, WalletError, address_from_mnemonic, append_to_env,
        keypair_from_mnemonic, validate_mnemonic,
    )

    print("\n  Restore a wallet from its 12 or 24 word seed phrase.")
    print("  The words are not shown as you type them anywhere else - this is a")
    print("  local prompt, nothing is sent over the network.\n")
    phrase = " ".join(input("  Phrase: ").split())
    if not phrase:
        print("  Nothing entered.\n")
        return 1

    try:
        if not validate_mnemonic(phrase):
            print("\n  That phrase is not valid - check the words and their order.\n",
                  file=sys.stderr)
            return 1
        address = address_from_mnemonic(phrase)
        secret = str(keypair_from_mnemonic(phrase))
    except WalletError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"\n  That phrase is the wallet {address}")
    try:
        append_to_env({ENV_MNEMONIC_KEY: phrase, ENV_KEY: secret})
    except WalletError as exc:
        print(f"\n  Not saved: {exc}")
        print("  Remove the existing lines from .env first if you mean to replace them.\n")
        return 1
    print("  Saved to .env. The bot will use it from now on.\n")
    return 0


def _show_phrase(args: argparse.Namespace) -> int:
    from .wallet import configured_address, configured_mnemonic

    phrase = configured_mnemonic()
    if not phrase:
        print("\n  This wallet has no seed phrase.")
        print("  It was created from a raw private key, or imported as one.")
        print("  Back up SOLANA_PRIVATE_KEY from .env instead.\n")
        return 1

    print(f"\n  Seed phrase for {configured_address()}:\n")
    words = phrase.split()
    for row in range(0, len(words), 4):
        print("    " + "   ".join(f"{i + 1:>2}. {word:<10}" for i, word in
                                  enumerate(words[row:row + 4], start=row)))
    print("\n  Anyone with these words owns the wallet. Paper only.\n")
    return 0


def _withdraw(args: argparse.Namespace, config: BotConfig) -> int:
    from .execution.live import SolanaRpc, _load_keypair
    from .wallet import LAMPORTS_PER_SOL, WalletError, is_valid_address, withdraw_sol

    destination = (args.to or "").strip()
    if not destination:
        destination = input("\n  Send to which Solana address? ").strip()
    if not destination:
        print("  No destination given.\n")
        return 1

    try:
        if not is_valid_address(destination):
            print(f"\n  {destination!r} is not a valid Solana address.\n", file=sys.stderr)
            return 1
        keypair = _load_keypair()
    except Exception as exc:  # noqa: BLE001 - report, never trace
        print(f"error: {exc}", file=sys.stderr)
        return 1

    rpc = SolanaRpc(config.execution.rpc_url, timeout=config.data.request_timeout)
    try:
        balance = rpc.get_balance_lamports(str(keypair.pubkey())) / LAMPORTS_PER_SOL
    except Exception as exc:  # noqa: BLE001
        print(f"error: could not read the wallet balance: {exc}", file=sys.stderr)
        return 1

    amount = (args.amount or "").strip().lower()
    if not amount:
        print(f"\n  Wallet holds {balance:.6f} SOL")
        amount = input("  How much SOL to send? ('all' for everything) ").strip().lower()

    lamports = None
    if amount not in ("all", "max", ""):
        try:
            lamports = int(round(float(amount) * LAMPORTS_PER_SOL))
        except ValueError:
            print(f"\n  {amount!r} is not a number.\n", file=sys.stderr)
            return 1

    sending = "everything (less the fee)" if lamports is None else f"{lamports / LAMPORTS_PER_SOL:.6f} SOL"
    print(f"\n  Sending {sending}")
    print(f"  From    {keypair.pubkey()}")
    print(f"  To      {destination}")
    print("\n  This moves SOL only. Any memecoins the bot still holds stay put -")
    print("  close positions first if you want the whole balance out.")
    if not args.yes:
        if input("\n  Type SEND to confirm: ").strip() != "SEND":
            print("  Cancelled.\n")
            return 1

    try:
        result = withdraw_sol(rpc, keypair, destination, lamports,
                              confirm_timeout=config.execution.confirm_timeout_seconds)
    except WalletError as exc:
        print(f"\n  {exc}\n", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"\n  Withdrawal failed: {exc}\n", file=sys.stderr)
        return 1

    if result["confirmed"]:
        print(f"\n  Sent {result['sol']:.6f} SOL.")
        print(f"  {result['explorer']}\n")
        return 0
    print(f"\n  Not confirmed: {result['error']}")
    print(f"  {result['explorer']}\n", file=sys.stderr)
    return 1


def cmd_wallet(args: argparse.Namespace, config: BotConfig) -> int:
    """Create, inspect, back up, restore or empty the bot's wallet."""
    from .wallet import WalletError, configured_address, configured_mnemonic

    if getattr(args, "new", False):
        return _create_wallet(args)
    if getattr(args, "import_phrase", False):
        return _import_wallet(args)
    if getattr(args, "phrase", False):
        return _show_phrase(args)
    if getattr(args, "withdraw", False):
        return _withdraw(args, config)

    # ---- show the configured wallet ----
    try:
        address = configured_address()
    except WalletError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if not address:
        print("\nNo wallet configured.")
        print("  Create one : python -m memebot wallet --new --save")
        print("  Restore one: python -m memebot wallet --import")
        print("  Or set SOLANA_PRIVATE_KEY / SOLANA_MNEMONIC in .env\n")
        return NO_WALLET

    not_ready = False
    print(f"\n  Address   {address}")
    print(f"  Explorer  https://solscan.io/account/{address}")
    if configured_mnemonic():
        print("  Backup    seed phrase available (menu: Show seed phrase)")
    else:
        print("  Backup    private key only - this wallet has no seed phrase")

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
          + ("" if armed else " (starting the bot arms it for that run only)"))
    print()
    return WALLET_NOT_READY if not_ready else 0


def cmd_doctor(args: argparse.Namespace, config: BotConfig) -> int:
    """Check every dependency and say whether the bot can actually trade."""
    from .doctor import FAIL, format_report, run_checks

    print(f"\nmemebot {__version__} health check\n")
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

    portfolio = Portfolio(storage)
    stats = portfolio.stats()

    if args.json:
        print(json.dumps(stats, indent=2, default=str))
        return 0

    print(f"\nmemebot {__version__} | db={config.state_db}")
    print("-" * 62)
    print(f"  equity            {_money(stats['equity_usd'])}")
    print(f"  cash              {_money(stats['cash_usd'])}")
    print(f"  positions value   {_money(stats['positions_value_usd'])} ({stats['open_positions']} open)")
    baseline = stats["starting_cash_usd"]
    print(f"  baseline          {_money(baseline)}"
          + ("" if baseline else "  (set on the first cycle from the wallet)"))
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
    print("\n  This wipes the bot's record of its own trades and positions.")
    print("  It does NOT move any funds - the wallet and whatever it holds are")
    print("  untouched, so the bot will no longer know about tokens it bought.")
    print("  Close positions first if you want a clean slate.\n")
    if not args.yes:
        answer = input(f"Wipe the trade history in {config.state_db}? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Aborted.")
            return 1
    storage = open_storage(config.state_db)
    storage.reset()
    storage.close()
    print("State reset. The wallet balance is read fresh on the next cycle.")
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
    parser.add_argument("--db", help="override the state database path")
    parser.add_argument("--log-level", help="DEBUG, INFO, WARNING, ERROR")

    sub = parser.add_subparsers(dest="command", required=False)

    menu = sub.add_parser("menu", help="the numbered menu (default when run with no command)")
    menu.set_defaults(func=cmd_menu)

    run = sub.add_parser("run", help="run the trading loop")
    run.add_argument("--cycles", type=int, default=None, help="stop after N cycles")
    run.add_argument("--interval", type=float, default=None, help="seconds between cycles")
    run.add_argument("-y", "--yes", action="store_true", help="skip the live-trading confirmation")
    run.add_argument("--plain", action="store_true",
                     help="plain log lines instead of the live display")
    run.set_defaults(func=cmd_run)

    once = sub.add_parser("once", help="run a single cycle and exit")
    once.set_defaults(func=cmd_once)

    scan = sub.add_parser("scan", help="show scored candidates without trading")
    scan.add_argument("--limit", type=int, default=20)
    scan.set_defaults(func=cmd_scan)

    wallet = sub.add_parser("wallet", help="create, inspect, back up or empty the wallet")
    wallet.add_argument("--new", action="store_true", help="generate a new wallet")
    wallet.add_argument("--words", type=int, default=12, choices=[12, 24],
                        help="seed phrase length for --new (default 12)")
    wallet.add_argument("--no-phrase", action="store_true",
                        help="with --new, a raw key with no seed phrase")
    wallet.add_argument("--save", action="store_true",
                        help="with --new, write it to .env (never overwrites)")
    wallet.add_argument("--import", dest="import_phrase", action="store_true",
                        help="restore a wallet from its seed phrase")
    wallet.add_argument("--phrase", action="store_true", help="show the seed phrase")
    wallet.add_argument("--withdraw", action="store_true", help="send SOL out of the wallet")
    wallet.add_argument("--to", help="with --withdraw, the destination address")
    wallet.add_argument("--amount", help="with --withdraw, SOL to send, or 'all'")
    wallet.add_argument("-y", "--yes", action="store_true",
                        help="with --withdraw, skip the confirmation")
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
        "state_db": args.db,
        "log_level": args.log_level,
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
