"""The live trading display, in the terminal.

Redrawn after every cycle while the bot runs: what it holds and how those
positions are doing, a sparkline of each one's recent price, what it is
watching, and a rolling feed of what it just did.

Everything here degrades: no colour without ANSI, no block characters on a
console whose code page cannot encode them, and no redraw at all when output
is piped somewhere (a log file or CI), where scrolling frames would be noise.
"""

from __future__ import annotations

import sys
import time
from typing import Any, Dict, List, Optional

from .ui import (
    BOLD, CYAN, DIM, GREEN, GREY, RED, WHITE, YELLOW,
    Glyphs, box, clear_screen, enable_ansi, money, paint, pct, unicode_ok,
)

BLOCKS = "▁▂▃▄▅▆▇█"
ASCII_BLOCKS = "._-=+*#@"

KIND_MARKS = {
    "buy": ("BUY ", GREEN),
    "sell": ("SELL", RED),
    "error": ("FAIL", RED),
    "halt": ("HALT", YELLOW),
    "start": ("RUN ", CYAN),
    "stop": ("STOP", GREY),
    "dry_run": ("DRY ", GREY),
    "cycle": ("    ", GREY),
}


def sparkline(values: List[float], width: int = 12) -> str:
    """A tiny price history. Flat input draws a flat line, not noise."""
    ramp = BLOCKS if unicode_ok() else ASCII_BLOCKS
    points = [v for v in values if v and v > 0][-width:]
    if not points:
        return ""
    low, high = min(points), max(points)
    if high <= low:
        return ramp[len(ramp) // 2] * len(points)
    span = high - low
    return "".join(ramp[min(len(ramp) - 1, int((v - low) / span * (len(ramp) - 1)))]
                   for v in points)


def _price(value: float) -> str:
    if not value:
        return "-"
    return f"{value:.8f}" if value < 0.01 else f"{value:,.4f}"


class ConsoleView:
    """Renders the bot's state. Attach it to TradingEngine(on_cycle=...)."""

    def __init__(self, output=print, force: Optional[bool] = None, width: int = 74) -> None:
        self.output = output
        self.width = width
        self.glyphs = Glyphs()
        # Piped output gets ordinary log lines instead of redrawn frames.
        self.active = force if force is not None else sys.stdout.isatty()

    # ---- pieces ---------------------------------------------------------------
    def header(self, engine) -> List[str]:
        mode = engine.config.mode.upper()
        dot = self.glyphs.dot
        badge = f"{mode} {dot} cycle {engine.cycles} {dot} {time.strftime('%H:%M:%S')}"
        colour = YELLOW if engine.config.is_live else CYAN
        return [paint(box("memebot", badge, width=self.width, glyphs=self.glyphs), colour)]

    def summary(self, engine) -> List[str]:
        portfolio = engine.portfolio
        try:
            stats = portfolio.stats()
        except Exception as exc:  # noqa: BLE001 - show what we can, never crash
            return ["", paint(f"  (portfolio unavailable: {exc})", GREY), ""]
        change = stats["total_return_pct"]
        colour = GREEN if change > 0 else (RED if change < 0 else GREY)
        dot = paint(f" {self.glyphs.dot} ", GREY)
        parts = [
            paint(money(stats["equity_usd"]), BOLD, WHITE),
            paint(f"{money(stats['cash_usd'])} cash", GREY),
            paint(pct(change), colour),
            paint(f"{stats['open_positions']} open", GREY),
            paint(f"{stats['wins']}W/{stats['losses']}L", GREY),
        ]
        return ["", "  " + dot.join(parts), ""]

    def holdings(self, engine) -> List[str]:
        try:
            positions = engine.portfolio.open_positions
        except Exception:  # noqa: BLE001
            positions = []
        if not positions:
            return [paint("  HOLDING", DIM, GREY),
                    paint("   nothing right now", GREY), ""]

        lines = [paint("  HOLDING", DIM, GREY)]
        for position in sorted(positions, key=lambda p: p.opened_at, reverse=True):
            pnl = position.unrealized_pnl_pct * 100.0
            colour = GREEN if pnl > 0 else (RED if pnl < 0 else GREY)
            arrow = (self.glyphs.up if pnl > 0 else self.glyphs.down) if pnl else " "
            try:
                samples = engine.storage.price_samples(position.token.address,
                                                       since=time.time() - 6 * 3600)
                spark = sparkline([s["price"] for s in samples])
            except Exception:  # noqa: BLE001 - the display is not the job
                spark = ""
            symbol = (position.token.symbol or position.token.address[:6])[:10]
            lines.append(
                "   " + paint(f"{symbol:<10}", WHITE)
                + paint(f"{_price(position.last_price):>13}", GREY)
                + paint(f"  {arrow}{pnl:>6.1f}%", colour)
                + paint(f"{money(position.market_value):>10}", GREY)
                + paint(f"  avg {_price(position.avg_price):<12}", GREY)
                + paint(f"{position.age_minutes:>5.0f}m  ", GREY)
                + paint(spark, colour)
            )
        return lines + [""]

    def watching(self, report) -> List[str]:
        if not report or not report.top_candidates:
            return []
        lines = [paint("  WATCHING", DIM, GREY)
                 + paint("            score      5m       1h   liquidity", DIM, GREY)]
        for score, pair in report.top_candidates[:4]:
            change_m5, change_h1 = pair.change("m5"), pair.change("h1")
            lines.append(
                "   " + paint(f"{(pair.base.symbol or pair.base.address[:6])[:10]:<10}", WHITE)
                + paint(f"{score:>15.2f}", GREY)
                + paint(f"{change_m5:>8.1f}%", GREEN if change_m5 > 0 else RED)
                + paint(f"{change_h1:>8.1f}%", GREEN if change_h1 > 0 else RED)
                + paint(f"{'$' + format(int(pair.liquidity_usd), ','):>12}", GREY)
            )
        return lines + [""]

    def activity(self, engine, limit: int = 6) -> List[str]:
        try:
            events = engine.storage.list_events(limit=limit * 3)
        except Exception:  # noqa: BLE001
            return []
        shown = [e for e in events if e["kind"] != "cycle"][:limit]
        if not shown:
            shown = events[:limit]
        if not shown:
            return []

        lines = [paint("  ACTIVITY", DIM, GREY)]
        for event in shown:
            mark, colour = KIND_MARKS.get(event["kind"], ("    ", GREY))
            if event["kind"] == "sell" and event["level"] == "win":
                colour = GREEN
            stamp = time.strftime("%H:%M", time.localtime(event["ts"]))
            message = event["message"]
            if len(message) > 48:
                message = message[:47] + "…" if unicode_ok() else message[:47] + "."
            lines.append("   " + paint(stamp, GREY) + "  " + paint(mark, colour)
                         + "  " + paint(message, WHITE if event["kind"] in ("buy", "sell") else GREY))
            if event["detail"] and event["kind"] in ("buy", "sell", "error", "halt"):
                detail = event["detail"]
                lines.append(paint("           " + (detail[:56]), GREY))
        return lines + [""]

    def footer(self, engine) -> List[str]:
        interval = engine.config.poll_interval_seconds
        note = f"  next scan in ~{interval:.0f}s  {self.glyphs.dot}  Ctrl+C to stop"
        if engine.config.is_live:
            note += f"  {self.glyphs.dot}  LIVE - real funds"
        return [paint(note, GREY)]

    # ---- the frame ------------------------------------------------------------
    def frame(self, engine, report=None) -> str:
        lines: List[str] = []
        lines += self.header(engine)
        lines += self.summary(engine)
        lines += self.holdings(engine)
        lines += self.watching(report)
        lines += self.activity(engine)
        lines += self.footer(engine)
        return "\n".join(lines)

    def render(self, engine, report=None) -> None:
        if not self.active:
            return
        clear_screen()
        self.output(self.frame(engine, report))

    def __call__(self, engine, report=None) -> None:
        self.render(engine, report)
