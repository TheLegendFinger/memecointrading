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
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from . import __version__
from .config import BotConfig, load_config
from .models import Mode
from .ui import (
    BOLD, CYAN, DIM, GREEN, GREY, RED, RESET, WHITE, YELLOW,
    Glyphs, box, clear_screen, money, paint, pct,
)

PAPER_CONFIG = "config.yaml"
LIVE_CONFIG = "config.live.yaml"
LIVE_CONFIG_EXAMPLE = "config.live.example.yaml"
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
    Item("1", "Paper trade", "practice on the real market, no real money",
         "paper", "TRADE", GREEN),
    Item("2", "Live trade", "REAL money on Solana", "live", "TRADE", YELLOW),
    Item("3", "Close all positions", "sell everything at market", "liquidate", "TRADE", RED),

    Item("4", "Portfolio", "equity, open positions, win rate", "status", "LOOK"),
    Item("5", "Trade history", "recent fills with fees and P&L", "trades", "LOOK"),
    Item("6", "Scan the market", "what the bot sees right now", "scan", "LOOK"),
    Item("7", "Dashboard", "the web view, in your browser", "dashboard", "LOOK"),

    Item("8", "Wallet", "address, balance, or create a burner", "wallet", "SETUP"),
    Item("9", "Health check", "are the market feeds reachable?", "doctor", "SETUP"),

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
    def _config_file(self, live: bool = False) -> Optional[str]:
        if self.config_path:
            return self.config_path
        candidate = LIVE_CONFIG if live else PAPER_CONFIG
        return candidate if Path(candidate).exists() else None

    def load(self, live: bool = False) -> BotConfig:
        overrides = {"mode": Mode.LIVE.value} if live else {}
        return load_config(self._config_file(live), overrides)

    # ---- rendering ------------------------------------------------------------
    def _state_line(self) -> str:
        """A one-line summary of the book, or a hint when there is nothing yet."""
        try:
            config = self.load()
            from .portfolio import Portfolio
            from .storage import open_storage

            storage = open_storage(config.state_db)
            try:
                portfolio = Portfolio(storage, config.risk.starting_cash_usd, mode=config.mode)
                stats = portfolio.stats()
            finally:
                storage.close()
        except Exception as exc:  # noqa: BLE001 - the menu must still render
            return paint(f"  (could not read the portfolio: {exc})", GREY)

        if stats["closed_trades"] == 0 and stats["open_positions"] == 0 and not stats["realized_pnl_usd"]:
            return paint("  No trades yet. Press 1 to start paper trading.", GREY)

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

        live_file = Path(LIVE_CONFIG).exists()
        badge = "LIVE CONFIGURED" if live_file else "PAPER"
        badge_colour = YELLOW if live_file else GREY

        self.output(paint(box(f"memebot {__version__}", badge, glyphs=self.glyphs),
                          CYAN if not live_file else YELLOW))
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
            if item.key in ("3", "7", "9"):
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
                self.notify(f"'{choice}' is not on the menu - pick a number from 0 to 9.", YELLOW)
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
    def _run_engine(self, live: bool) -> None:
        from .engine import TradingEngine
        from .logging_utils import setup_logging

        config = self.load(live=live)
        setup_logging(config.log_level, config.log_file)
        engine = TradingEngine(config)
        blocked = engine.preflight()
        if blocked:
            self.notify(blocked, RED)
            return

        self.output("")
        self.output(paint(f"  Running in {config.mode} mode. "
                          "Ctrl+C stops it and returns to the menu.", GREY))
        self.output("")
        try:
            engine.run()
        finally:
            engine.storage.close()
        self.notify("Stopped. Positions are still open - use 3 to close them.", GREY)

    def do_paper(self) -> None:
        if self.clear:
            clear_screen()
        self.output(paint("\n  PAPER TRADING", BOLD, GREEN))
        self.output(paint("  Live market data, simulated fills. No money moves.\n", GREY))
        self._run_engine(live=False)

    def do_live(self) -> None:
        if self.clear:
            clear_screen()
        self.output(paint("\n  LIVE TRADING", BOLD, YELLOW))
        self.output("")

        # 1. A live config, separate from the paper one.
        if not Path(LIVE_CONFIG).exists():
            if not Path(LIVE_CONFIG_EXAMPLE).exists():
                self.notify(f"{LIVE_CONFIG_EXAMPLE} is missing from this folder.", RED)
                return
            Path(LIVE_CONFIG).write_text(Path(LIVE_CONFIG_EXAMPLE).read_text())
            self.output(paint(f"  Created {LIVE_CONFIG} with small-wallet settings.", GREEN))

        # 2. The Solana packages.
        try:
            import solders  # noqa: F401
        except ImportError:
            self.output(paint("  Installing the Solana packages...", CYAN))
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "-r", "requirements-live.txt",
                 "--quiet", "--disable-pip-version-check"],
                check=False,
            )
            if result.returncode != 0:
                self.notify("Could not install the Solana packages (solders, base58).", RED)
                return

        # 3. Arming is not trading: it lets the checks below see the real live
        #    path, and it lives only in this process.
        os.environ[CONFIRM_ENV] = CONFIRM_VALUE

        # 4. The wallet has to exist and be able to pay for a swap.
        from .cli import NO_WALLET, cmd_wallet
        from argparse import Namespace

        config = self.load(live=True)
        code = cmd_wallet(Namespace(new=False, save=False), config)
        if code == NO_WALLET:
            self.output("")
            if not self.confirm("No wallet yet. Create a burner wallet now?"):
                self.notify("Live trading needs a wallet.", GREY)
                return
            cmd_wallet(Namespace(new=True, save=True), config)
            self.notify("Send SOL to that address, then choose 2 again.", YELLOW)
            self.pause()
            return
        if code != 0:
            self.output("")
            self.notify("The wallet cannot trade yet - see above.", YELLOW)
            self.pause()
            return

        # 5. The last gate.
        self.output("")
        self.output(paint("  You are about to trade REAL money.", BOLD, YELLOW))
        self.output(paint("  This bot buys brand-new memecoins. Many go to zero. It can lose", GREY))
        self.output(paint("  everything in the wallet, and it keeps trading until you stop it.", GREY))
        self.output("")
        if self.ask("Type LIVE to start trading for real:") != "LIVE":
            self.notify("Not started.", GREY)
            return

        self._run_engine(live=True)

    def do_liquidate(self) -> None:
        from .engine import TradingEngine
        from .logging_utils import setup_logging

        live = Path(LIVE_CONFIG).exists() and self.confirm("Close LIVE positions? (no = paper)")
        config = self.load(live=live)
        if live:
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

    def do_wallet(self) -> None:
        from argparse import Namespace

        from .cli import cmd_wallet

        if self.clear:
            clear_screen()
        config = self.load(live=Path(LIVE_CONFIG).exists())
        code = cmd_wallet(Namespace(new=False, save=False), config)
        if code == 2 and self.confirm("Create a new burner wallet now?"):
            cmd_wallet(Namespace(new=True, save=True), config)
        self.output("")
        self.pause()

    def do_dashboard(self) -> None:
        config = self.load()
        script = Path(__file__).resolve().parent.parent / "scripts" / "dev_server.py"
        if not script.exists():
            self.notify("scripts/dev_server.py is missing from this folder.", RED)
            return

        process = subprocess.Popen(
            [sys.executable, str(script), "--port", "8000", "--db", config.state_db],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            time.sleep(1.5)
            if process.poll() is not None:
                self.notify("The dashboard server exited immediately - is port 8000 in use?", RED)
                return
            webbrowser.open("http://localhost:8000")
            self.output("")
            self.output(paint("  Dashboard running at http://localhost:8000", GREEN))
            self.ask("Press Enter to stop it and go back")
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover - stubborn child
                process.kill()
        self.notify("Dashboard stopped.", GREY)

    def do_quit(self) -> None:  # pragma: no cover - handled in run()
        raise SystemExit(0)


def run_menu(config_path: Optional[str] = None) -> int:
    return Menu(config_path=config_path).run()
