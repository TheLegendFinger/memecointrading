"""The two-speed loop, the countdown, and stopping by typing STOP.

Coins already held are the ones worth watching closely - a stop loss is only
as good as the last price it saw - but a full market scan costs ~35 requests
and cannot run every few seconds. So the loop runs at two speeds, and this
file pins that they stay separate.
"""

import io
import os
import random

import pytest

from conftest import FakeDexScreener, make_pair
from memebot.console_view import ConsoleView
from memebot.engine import TradingEngine
from memebot.keyboard import LineReader
from memebot.storage import Storage
from tests.fakes import SimulatedExecutor
from test_engine import VirtualClock


@pytest.fixture
def hot_pair():
    return make_pair("BEST", chg_m5=12.0, chg_h1=55.0, vol_m5=60_000, vol_h1=400_000,
                     vol_h24=960_000, buys_m5=90, sells_m5=10, buys_h1=800, sells_h1=200,
                     liquidity=700_000, price=0.01)


def build(config, pairs):
    market = FakeDexScreener(pairs)
    engine = TradingEngine(
        config, storage=Storage(":memory:"), data=market,
        executor=SimulatedExecutor(config, data=market, rng=random.Random(11)),
    )
    return engine, market


# ---- the two speeds --------------------------------------------------------------
def test_a_position_tick_does_not_scan_the_market(config, hot_pair):
    engine, market = build(config, [hot_pair])
    engine.run_cycle()
    scans_after_cycle = market.discover_calls

    engine.run_position_tick()

    assert market.discover_calls == scans_after_cycle, "no discovery on a tick"
    assert engine.ticks == 1


def test_a_position_tick_still_takes_the_exit(config, hot_pair):
    """The whole point: a stop loss must not wait for the next market scan."""
    config.risk.stop_loss_pct = 0.20
    engine, market = build(config, [hot_pair])
    engine.run_cycle()
    assert engine.portfolio.has_position(hot_pair.base.address)

    market.set_price(hot_pair.base.address, 0.005)     # -50%
    report = engine.run_position_tick()

    assert len(report.closed) == 1
    assert "stop loss" in report.closed[0].order.reason


def test_held_coins_are_checked_far_more_often_than_the_market_is_scanned(config, hot_pair):
    config.poll_interval_seconds = 20.0
    config.position_poll_seconds = 5.0
    engine, market = build(config, [hot_pair])
    clock = VirtualClock()

    engine.run(max_cycles=3, sleep=clock.sleep, clock=clock)

    assert engine.cycles == 3
    # 20s between scans, 5s between ticks: three ticks in each gap.
    assert engine.ticks >= 2 * (int(20 / 5) - 1)
    assert market.discover_calls == 3, "discovery only on the slow cadence"


def test_the_position_poll_cannot_be_slower_than_the_scan(config):
    config.poll_interval_seconds = 10.0
    config.position_poll_seconds = 30.0
    with pytest.raises(ValueError, match="position_poll_seconds"):
        config.validate()


# ---- the countdown ---------------------------------------------------------------
def test_the_countdown_falls_towards_the_next_scan(config, hot_pair):
    config.poll_interval_seconds = 20.0
    engine, _ = build(config, [hot_pair])
    seen = []
    clock = VirtualClock()

    engine.run(max_cycles=2, sleep=clock.sleep, clock=clock,
               on_idle=lambda eng: seen.append(eng.seconds_to_next_scan))

    assert seen, "the loop reports its own countdown"
    assert max(seen) <= 20.0
    assert min(seen) < max(seen), "it counts down rather than sitting still"


def test_the_countdown_is_zero_when_nothing_is_running(config):
    engine, _ = build(config, [])
    assert engine.seconds_to_next_scan == 0.0


def test_the_footer_shows_the_countdown_and_the_stop_word(config, hot_pair):
    engine, _ = build(config, [hot_pair])
    engine.run_cycle()
    view = ConsoleView(force=True)

    footer = "\n".join(view.footer(engine))

    assert "next scan in" in footer
    assert "held, re-checked every" in footer
    assert "type STOP then Enter" in footer


def test_the_footer_echoes_what_is_being_typed(config):
    """On Windows the keys are read one at a time, so the console shows nothing
    unless the view echoes it."""
    engine, _ = build(config, [])
    view = ConsoleView(force=True)
    view.typed = "STO"

    footer = "\n".join(view.footer(engine))

    assert "> STO" in footer
    assert "type STOP" not in footer


# ---- stopping --------------------------------------------------------------------
def test_typing_stop_ends_the_loop(config, hot_pair):
    engine, _ = build(config, [hot_pair])
    clock = VirtualClock()

    def idle(eng):
        if eng.cycles >= 1:
            eng.request_stop("Stopped by STOP. Open positions were left open.")

    engine.run(sleep=clock.sleep, clock=clock, on_idle=idle)

    assert engine.cycles == 1
    assert "Stopped by STOP" in engine.stop_reason


def test_stopping_leaves_open_positions_alone(config, hot_pair):
    """Stopping is not selling: it stops trading and nothing else."""
    engine, _ = build(config, [hot_pair])
    clock = VirtualClock()
    engine.run(sleep=clock.sleep, clock=clock, on_idle=lambda eng: eng.request_stop("bye"))

    assert engine.portfolio.open_positions, "still held after the stop"


def test_a_failing_display_never_stops_the_bot(config, hot_pair):
    engine, _ = build(config, [hot_pair])
    clock = VirtualClock()

    def idle(eng):
        if eng.cycles >= 2:
            eng.request_stop("done")
        raise RuntimeError("the terminal exploded")

    engine.run(sleep=clock.sleep, clock=clock, on_idle=idle)
    assert engine.cycles == 2


# ---- reading the keyboard without blocking ---------------------------------------
class FakeTty:
    """A real file descriptor (so select works) that claims to be a terminal."""

    def __init__(self):
        read_fd, self.write_fd = os.pipe()
        self.stream = os.fdopen(read_fd, "r")

    def isatty(self):
        return True

    def fileno(self):
        return self.stream.fileno()

    def readline(self):
        return self.stream.readline()

    def type(self, text):
        os.write(self.write_fd, text.encode())

    def close(self):
        os.close(self.write_fd)


def test_nothing_typed_reads_as_nothing():
    tty = FakeTty()
    try:
        reader = LineReader(tty)
        assert reader.available is True
        assert reader.poll() is None, "and it does not block waiting for a line"
    finally:
        tty.close()


def test_a_typed_line_comes_back_whole():
    tty = FakeTty()
    try:
        reader = LineReader(tty)
        tty.type("STOP\n")
        assert reader.poll() == "STOP"
        assert reader.poll() is None
    finally:
        tty.close()


def test_a_pipe_is_not_read_at_all():
    """Under cron or a log pipe there is no keyboard; the signal handler covers it."""
    reader = LineReader(io.StringIO("STOP\n"))
    assert reader.available is False
    assert reader.poll() is None


def test_the_stop_words_are_matched_loosely():
    from memebot.cli import STOP_WORDS

    for typed in ("stop", "STOP", "  Stop  ", "quit", "exit"):
        assert typed.strip().lower() in STOP_WORDS


# ---- the watchlist between scans -------------------------------------------------
def test_the_watchlist_survives_a_position_tick(config, hot_pair):
    """The bug: WATCHING vanished every few seconds, because a position tick
    carries no candidates and the panel rendered from the report alone."""
    engine, _ = build(config, [hot_pair])
    view = ConsoleView(force=True)
    scan = engine.run_cycle()
    assert "WATCHING" in view.frame(engine, scan)

    tick = engine.run_position_tick()

    assert "WATCHING" in view.frame(engine, tick)
    assert "BEST" in view.frame(engine, tick)


def test_the_watchlist_survives_a_plain_redraw(config, hot_pair):
    """The countdown redraws once a second with no report at all."""
    engine, _ = build(config, [hot_pair])
    view = ConsoleView(force=True)
    engine.run_cycle()

    assert "WATCHING" in view.frame(engine, None)


def test_the_watchlist_says_how_stale_it_is(config, hot_pair):
    """Between scans it is last-known data, and should not pretend otherwise."""
    engine, _ = build(config, [hot_pair])
    view = ConsoleView(force=True)
    clock = VirtualClock()
    frames = []

    engine.run(max_cycles=2, sleep=clock.sleep, clock=clock,
               on_idle=lambda eng: frames.append(view.frame(eng, None)))

    assert any("as of" in f for f in frames)


def test_nothing_is_watched_before_the_first_scan(config):
    engine, _ = build(config, [])
    assert ConsoleView(force=True).watching(engine, None) == []
