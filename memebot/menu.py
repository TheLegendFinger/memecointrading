"""The numbered menu - the front door for people who would rather not memorise
command line flags.

Everything the CLI can do is reachable by typing a single digit. The header
shows live state (mode, equity, open positions) so the screen answers "how am I
doing" before you pick anything.

The class takes its input and output functions, so the whole thing is testable
without a terminal.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from . import __version__
from .config import BotConfig, load_config
from .ui import (
    BOLD, CYAN, DIM, GREEN, GREY, RED, RESET, WHITE, YELLOW,
    Glyphs, box, clear_screen, money, paint, pct,
)

CONFIG = "config.yaml"
CONFIG_EXAMPLE = "config.example.yaml"
CONFIRM_ENV = "LIVE_TRADING_CONFIRM"
CONFIRM_VALUE = "I_UNDERSTAND_THE_RISK"


@dataclass
class Item:
    key: str
    title: str
    description: str
    action: str
    section: str
    style: str = ""


MENU: List[Item] = [
    Item("1", "Start trading", "REAL money on Solana", "trade", "TRADE", YELLOW),
    Item("2", "Dry run", "decide and log, place no orders", "dry_run", "TRADE", GREEN),
    Item("3", "Close all positions", "sell everything at market", "liquidate", "TRADE", RED),

    Item("4", "Portfolio", "equity, open positions, win rate", "status", "LOOK"),
    Item("5", "Trade history", "recent fills with fees and P&L", "trades", "LOOK"),
    Item("6", "Scan the market", "what the bot sees right now", "scan", "LOOK"),

    Item("7", "Wallet", "create, fund, back up, withdraw", "wallet", "SETUP"),
    Item("8", "Health check", "are the market feeds reachable?", "doctor", "SETUP"),

    Item("0", "Quit", "", "quit", ""),
]


class Menu:
    def __init__(
        self,
        config_path: Optional[str] = None,
        input_fn: Callable[[str], str] = input,
        output: Callable[..., None] = print,
        clear: bool = True,
    ) -> None:
        self.config_path = config_path
        self.input_fn = input_fn
        self.output = output
        self.clear = clear
        self.glyphs = Glyphs()
        self.message: str = ""
        self.message_style: str = ""

    # ---- config ---------------------------------------------------------------
    def _config_file(self) -> Optional[str]:
        if self.config_path:
            return self.config_path
        return CONFIG if Path(CONFIG).exists() else None

    def load(self) -> BotConfig:
        return load_config(self._config_file())

    # ---- rendering ------------------------------------------------------------
    def _state_line(self) -> str:
        """A one-line summary of the book, or a hint when there is nothing yet."""
        try:
            config = self.load()
            from .portfolio import Portfolio
            from .storage import open_storage

            storage = open_storage(config.state_db)
            try:
                portfolio = Portfolio(storage, config.risk.starting_cash_usd)
                stats = portfolio.stats()
            finally:
                storage.close()
        except Exception as exc:  # noqa: BLE001 - the menu must still render
            return paint(f"  (could not read the portfolio: {exc})", GREY)

        if stats["closed_trades"] == 0 and stats["open_positions"] == 0 and not stats["realized_pnl_usd"]:
            return paint("  No trades yet. Press 2 for a dry run, or 1 to trade.", GREY)

        change = stats["total_return_pct"]
        colour = GREEN if change > 0 else (RED if change < 0 else GREY)
        dot = f" {self.glyphs.dot} "
        return (
            "  "
            + paint(money(stats["equity_usd"]), BOLD, WHITE)
            + paint(dot, GREY)
            + paint(f"{money(stats['cash_usd'])} cash", GREY)
            + paint(dot, GREY)
            + paint(f"{stats['open_positions']} open", GREY)
            + paint(dot, GREY)
            + paint(pct(change), colour)
        )

    def render(self) -> None:
        if self.clear:
            clear_screen()

        self.output(paint(box(f"memebot {__version__}", "LIVE", glyphs=self.glyphs), YELLOW))
        self.output("")
        self.output(self._state_line())
        self.output("")

        section = None
        for item in MENU:
            if item.section != section and item.section:
                section = item.section
                self.output(paint(f"  {section}", DIM, GREY))
            key = paint(f"  {item.key:>2}  ", BOLD, item.style or CYAN)
            title = paint(f"{item.title:<22}", item.style or WHITE)
            desc = paint(item.description, GREY)
            self.output(f"{key}{title}{desc}")
            if item.key in ("3", "6", "8"):
                self.output("")

        if self.message:
            self.output("")
            self.output(paint(f"  {self.message}", self.message_style or GREY))
            self.message = ""
            self.message_style = ""

        self.output("")

    def ask(self, prompt: str) -> str:
        try:
            return self.input_fn(paint(f"  {self.glyphs.arrow} {prompt} ", CYAN)).strip()
        except (EOFError, KeyboardInterrupt):
            return "0"

    def confirm(self, prompt: str) -> bool:
        return self.ask(f"{prompt} [y/N]").lower() in ("y", "yes")

    def pause(self) -> None:
        self.ask("Press Enter to go back")

    def notify(self, text: str, style: str = "") -> None:
        self.message = text
        self.message_style = style

    # ---- loop -----------------------------------------------------------------
    def run(self) -> int:
        while True:
            self.render()
            choice = self.ask("Type a number:").lower()
            if choice in ("0", "q", "quit", "exit"):
                self.output(paint("\n  Bye.\n", GREY))
                return 0
            item = next((i for i in MENU if i.key == choice), None)
            if item is None:
                self.notify(f"'{choice}' is not on the menu - pick a number from 0 to 8.", YELLOW)
                continue
            try:
                self.dispatch(item.action)
            except KeyboardInterrupt:
                self.notify("Stopped.", GREY)
            except Exception as exc:  # noqa: BLE001 - never crash out of the menu
                self.notify(f"{type(exc).__name__}: {exc}", RED)

    def dispatch(self, action: str) -> None:
        handler = getattr(self, f"do_{action}", None)
        if handler is None:  # pragma: no cover - MENU and methods are in step
            raise ValueError(f"no handler for {action}")
        handler()

    # ---- actions --------------------------------------------------------------
    def ensure_solana_packages(self) -> bool:
        """Install solders/base58 if they are missing.

        Anything touching a wallet needs these, so every path that does asks
        here first rather than failing with an instruction to run pip yourself.
        """
        try:
            import solders  # noqa: F401

            return True
        except ImportError:
            pass

        self.output(paint("  Installing the Solana packages (one time, ~20s)...", CYAN))
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", "requirements-live.txt",
             "--quiet", "--disable-pip-version-check"],
            check=False,
        )
        if result.returncode != 0:
            self.notify("Could not install the Solana packages (solders, base58, mnemonic).", RED)
            return False
        try:
            import solders  # noqa: F401

            return True
        except ImportError:
            self.notify("Installed, but the Solana packages still will not import.", RED)
            return False

    def _run_engine(self, dry_run: bool = False) -> None:
        from .console_view import ConsoleView
        from .engine import TradingEngine
        from .logging_utils import setup_logging

        config = self.load()
        config.dry_run = dry_run
        view = ConsoleView(output=self.output)

        # With the live display on, log lines would scribble over the frame, so
        # they go to the file only. Without a terminal (piped output, CI) it is
        # the other way round: plain logs, no frames.
        setup_logging(config.log_level, config.log_file, console=not view.active)

        engine = TradingEngine(config, on_cycle=view)
        blocked = engine.preflight()
        if blocked:
            self.notify(blocked, RED)
            return

        self.output("")
        if dry_run:
            self.output(paint("  Dry run: it decides and logs, but places no orders.", GREEN))
        self.output(paint("  First scan takes a few seconds...", GREY))
        self.output(paint("  Ctrl+C stops it and returns to the menu.", GREY))
        try:
            engine.run()
        finally:
            engine.storage.close()
        self.notify("Stopped. Positions are still open - use 3 to close them.", GREY)

    def do_dry_run(self) -> None:
        """Everything the real thing does, except sending orders."""
        if self.clear:
            clear_screen()
        self.output(paint("\n  DRY RUN", BOLD, GREEN))
        self.output(paint("  Live market, real scoring, real sizing - no orders sent.\n", GREY))
        if not self._prepare(require_wallet=False):
            return
        self._run_engine(dry_run=True)

    def do_trade(self) -> None:
        if self.clear:
            clear_screen()
        self.output(paint("\n  START TRADING", BOLD, YELLOW))
        self.output("")
        if not self._prepare(require_wallet=True):
            return

        self.output("")
        self.output(paint("  You are about to trade REAL money.", BOLD, YELLOW))
        self.output(paint("  This bot buys brand-new memecoins. Many go to zero. It can lose", GREY))
        self.output(paint("  everything in the wallet, and it keeps trading until you stop it.", GREY))
        self.output("")
        if self.ask("Type LIVE to start trading for real:") != "LIVE":
            self.notify("Not started.", GREY)
            return

        self._run_engine()

    def _prepare(self, require_wallet: bool) -> bool:
        """Config, packages, interlock and wallet - the things a run needs."""
        if not Path(CONFIG).exists() and Path(CONFIG_EXAMPLE).exists():
            Path(CONFIG).write_text(Path(CONFIG_EXAMPLE).read_text())
            self.output(paint(f"  Created {CONFIG} from the example.", GREEN))

        if not self.ensure_solana_packages():
            return False

        # Arming is not trading: it lets the checks below see the real path, and
        # it lives only in this process, so quitting the menu disarms it.
        os.environ[CONFIRM_ENV] = CONFIRM_VALUE

        if not require_wallet:
            return True

        from argparse import Namespace

        from .cli import NO_WALLET, cmd_wallet

        config = self.load()
        code = cmd_wallet(Namespace(new=False, save=False, words=12, no_phrase=False,
                                    import_phrase=False, phrase=False, withdraw=False,
                                    to=None, amount=None, yes=False), config)
        if code == NO_WALLET:
            self.output("")
            if not self.confirm("No wallet yet. Create one now?"):
                self.notify("Trading needs a wallet.", GREY)
                return False
            cmd_wallet(Namespace(new=True, save=True, words=12, no_phrase=False,
                                 import_phrase=False, phrase=False, withdraw=False,
                                 to=None, amount=None, yes=False), config)
            self.notify("Send SOL to that address, then choose 1 again.", YELLOW)
            self.pause()
            return False
        if code != 0:
            self.output("")
            self.notify("The wallet cannot trade yet - see above.", YELLOW)
            self.pause()
            return False
        return True

    def do_liquidate(self) -> None:
        from .engine import TradingEngine
        from .logging_utils import setup_logging

        config = self.load()
        os.environ[CONFIRM_ENV] = CONFIRM_VALUE
        setup_logging(config.log_level, None)

        engine = TradingEngine(config)
        try:
            count = len(engine.portfolio.positions)
            if count == 0:
                self.notify("No open positions.", GREY)
                return
            if not self.confirm(f"Sell all {count} position(s) at market?"):
                self.notify("Cancelled.", GREY)
                return
            report = engine.liquidate_all()
            self.notify(
                f"Closed {len(report.closed)} position(s). "
                f"Equity {money(engine.portfolio.equity)}.",
                GREEN if not report.errors else YELLOW,
            )
            for error in report.errors:
                self.output(paint(f"  {error}", RED))
        finally:
            engine.storage.close()

    def _run_cli(self, argv: List[str]) -> None:
        """Reuse the CLI's own rendering rather than duplicating it."""
        from .cli import main

        if self.clear:
            clear_screen()
        main(argv)
        self.output("")
        self.pause()

    def do_status(self) -> None:
        self._run_cli(self._with_config(["status"]))

    def do_trades(self) -> None:
        self._run_cli(self._with_config(["trades", "--limit", "25"]))

    def do_scan(self) -> None:
        self.output(paint("\n  Scanning the live market, this takes a few seconds...\n", GREY))
        self._run_cli(self._with_config(["scan", "--limit", "20"]))

    def do_doctor(self) -> None:
        self._run_cli(self._with_config(["doctor"]))

    def _with_config(self, argv: List[str]) -> List[str]:
        config_file = self._config_file()
        return (["--config", config_file] if config_file else []) + argv

    WALLET_ITEMS = [
        ("1", "Show wallet and balance", "wallet_show"),
        ("2", "Create a new wallet", "wallet_create"),
        ("3", "Show seed phrase", "wallet_phrase"),
        ("4", "Restore from a seed phrase", "wallet_import"),
        ("5", "Withdraw SOL", "wallet_withdraw"),
    ]

    def do_wallet(self) -> None:
        """A small submenu - creating, funding, backing up and emptying."""
        if not self.ensure_solana_packages():
            return

        while True:
            if self.clear:
                clear_screen()
            self.output(paint("\n  WALLET", BOLD, CYAN))
            self.output(paint("  The bot trades from this wallet, and only this one.\n", GREY))
            for key, title, _action in self.WALLET_ITEMS:
                self.output(paint(f"   {key}  ", BOLD, CYAN) + paint(title, WHITE))
            self.output(paint("   0  ", BOLD, CYAN) + paint("Back", WHITE))
            self.output("")

            choice = self.ask("Type a number:").lower()
            if choice in ("0", "q", "b", "back", ""):
                return
            entry = next((item for item in self.WALLET_ITEMS if item[0] == choice), None)
            if entry is None:
                continue
            try:
                getattr(self, f"do_{entry[2]}")()
            except Exception as exc:  # noqa: BLE001 - never crash out of the menu
                self.output(paint(f"\n  {type(exc).__name__}: {exc}", RED))
            self.pause()

    def _wallet_cli(self, **flags) -> int:
        from argparse import Namespace

        from .cli import cmd_wallet

        defaults = dict(new=False, save=False, words=12, no_phrase=False,
                        import_phrase=False, phrase=False, withdraw=False,
                        to=None, amount=None, yes=False)
        defaults.update(flags)
        config = self.load()
        return cmd_wallet(Namespace(**defaults), config)

    def do_wallet_show(self) -> None:
        if self.clear:
            clear_screen()
        code = self._wallet_cli()
        if code == 2:
            self.output(paint("\n  Create one with option 2, or restore one with option 4.", GREY))

    def do_wallet_create(self) -> None:
        if self.clear:
            clear_screen()
        from .wallet import ENV_KEY, env_file_has_key

        if env_file_has_key(key=ENV_KEY):
            self.output(paint("\n  A wallet is already configured.", YELLOW))
            self.output(paint("  Creating another does not move any funds out of the old one -", GREY))
            self.output(paint("  back up its seed phrase (option 3) first, or withdraw (option 5).", GREY))
            self.output(paint("  To replace it, remove SOLANA_PRIVATE_KEY and SOLANA_MNEMONIC", GREY))
            self.output(paint("  from the .env file yourself.\n", GREY))
            return

        words = 24 if self.confirm("Use a 24-word phrase instead of 12?") else 12
        self._wallet_cli(new=True, save=True, words=words)

    def do_wallet_phrase(self) -> None:
        if self.clear:
            clear_screen()
        self.output(paint("\n  This shows the words that control the wallet.", YELLOW))
        self.output(paint("  Make sure nobody can see your screen, and that you are not "
                          "sharing it.", GREY))
        if not self.confirm("Show the seed phrase now?"):
            self.output(paint("\n  Not shown.", GREY))
            return
        self._wallet_cli(phrase=True)

    def do_wallet_import(self) -> None:
        if self.clear:
            clear_screen()
        self._wallet_cli(import_phrase=True)

    def do_wallet_withdraw(self) -> None:
        if self.clear:
            clear_screen()
        self.output(paint("\n  WITHDRAW", BOLD, YELLOW))
        try:
            engine_positions = self._open_position_count()
        except Exception:  # noqa: BLE001
            engine_positions = 0
        if engine_positions:
            self.output(paint(
                f"  The bot still holds {engine_positions} position(s). Withdrawing moves SOL",
                YELLOW))
            self.output(paint("  only - close them first (option 3 on the main menu) to get "
                              "that value back into SOL.\n", GREY))
            if not self.confirm("Withdraw anyway?"):
                return
        self._wallet_cli(withdraw=True)

    def _open_position_count(self) -> int:
        from .portfolio import Portfolio
        from .storage import open_storage

        config = self.load()
        storage = open_storage(config.state_db)
        try:
            return len(Portfolio(storage, config.risk.starting_cash_usd).positions)
        finally:
            storage.close()

    def do_quit(self) -> None:  # pragma: no cover - handled in run()
        raise SystemExit(0)


def run_menu(config_path: Optional[str] = None) -> int:
    return Menu(config_path=config_path).run()
