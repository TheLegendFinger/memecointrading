"""Terminal live-display tests."""

import random

import pytest

from memebot.config import BotConfig
from memebot.console_view import ConsoleView, sparkline
from memebot.engine import TradingEngine
from tests.fakes import SimulatedExecutor
from memebot.storage import Storage
from tests.conftest import FakeDexScreener, make_pair


@pytest.fixture
def traded(config):
    """An engine that has actually bought something."""
    hot = make_pair("BEST", chg_m5=12.0, chg_h1=55.0, vol_h1=400_000, vol_h24=960_000,
                    buys_m5=90, sells_m5=10, liquidity=700_000)
    market = FakeDexScreener([hot])
    engine = TradingEngine(config, storage=Storage(":memory:"), data=market,
                           executor=SimulatedExecutor(config, data=market, rng=random.Random(3)))
    report = engine.run_cycle()
    return engine, report


# ---- sparklines ----------------------------------------------------------------
def test_sparkline_tracks_direction():
    assert sparkline([1, 2, 3, 4])[0] < sparkline([1, 2, 3, 4])[-1]
    assert sparkline([4, 3, 2, 1])[0] > sparkline([4, 3, 2, 1])[-1]


def test_a_flat_price_is_a_flat_line_not_noise():
    line = sparkline([5, 5, 5, 5])
    assert len(set(line)) == 1


def test_sparkline_ignores_junk_and_empties():
    assert sparkline([]) == ""
    assert sparkline([0, 0]) == ""
    assert len(sparkline([1, 0, 2, None, 3])) == 3


def test_sparkline_is_capped_to_its_width():
    assert len(sparkline(list(range(1, 100)), width=10)) == 10


# ---- the frame -----------------------------------------------------------------
def test_the_frame_shows_what_it_holds_and_what_it_did(traded):
    engine, report = traded
    frame = ConsoleView(force=True).frame(engine, report)

    assert "HOLDING" in frame and "BEST" in frame
    assert "ACTIVITY" in frame
    assert "Bought BEST" in frame
    assert "type STOP then Enter to stop" in frame
    assert "next scan in" in frame


def test_the_frame_shows_the_money(traded):
    engine, report = traded
    frame = ConsoleView(force=True).frame(engine, report)
    assert "cash" in frame and "open" in frame and "W/" in frame


def test_the_frame_lists_what_it_is_watching(traded):
    engine, report = traded
    frame = ConsoleView(force=True).frame(engine, report)
    assert "WATCHING" in frame
    assert "liquidity" in frame


def test_an_empty_book_says_so(config):
    market = FakeDexScreener([])
    engine = TradingEngine(config, storage=Storage(":memory:"), data=market,
                           executor=SimulatedExecutor(config, data=market, rng=random.Random(1)))
    frame = ConsoleView(force=True).frame(engine, engine.run_cycle())
    assert "nothing right now" in frame


def test_the_display_says_real_funds(config):
    market = FakeDexScreener([])
    engine = TradingEngine(config, storage=Storage(":memory:"), data=market,
                           executor=SimulatedExecutor(config, data=market, rng=random.Random(1)))
    frame = ConsoleView(force=True).frame(engine, None)
    assert "LIVE" in frame and "real funds" in frame


def test_the_frame_is_pure_ascii_when_the_console_cannot_do_better(traded, monkeypatch):
    """A cp1252 console must not crash on a box-drawing character."""
    import memebot.ui as ui

    monkeypatch.setattr(ui, "_UNICODE_OK", False)
    monkeypatch.setattr(ui, "_ENABLED", False)
    engine, report = traded
    frame = ConsoleView(force=True).frame(engine, report)

    frame.encode("ascii")  # raises if anything slipped through
    assert "+---" in frame, "the ASCII box should be drawn"


def test_a_storage_failure_does_not_break_the_display(traded):
    engine, report = traded
    engine.storage.close()  # the display must survive whatever it finds
    frame = ConsoleView(force=True).frame(engine, report)
    assert "memebot" in frame


# ---- wiring --------------------------------------------------------------------
def test_the_view_is_inactive_when_output_is_piped():
    """Redrawn frames in a log file or CI are noise, not a display."""
    assert ConsoleView(force=False).active is False
    lines = []
    ConsoleView(force=False, output=lines.append).render(None, None)
    assert lines == []


def test_the_engine_calls_the_view_after_every_cycle(config):
    calls = []
    market = FakeDexScreener([])
    engine = TradingEngine(config, storage=Storage(":memory:"), data=market,
                           executor=SimulatedExecutor(config, data=market, rng=random.Random(1)),
                           on_cycle=lambda e, r: calls.append(r))
    engine.run_cycle()
    engine.run_cycle()
    assert len(calls) == 2


def test_a_broken_display_never_stops_the_bot(config):
    def explode(engine, report):
        raise RuntimeError("render failed")

    market = FakeDexScreener([])
    engine = TradingEngine(config, storage=Storage(":memory:"), data=market,
                           executor=SimulatedExecutor(config, data=market, rng=random.Random(1)),
                           on_cycle=explode)
    report = engine.run_cycle()  # must not raise
    assert report.scanned == 0


def test_reconfiguring_logging_does_not_leak_the_log_file(tmp_path):
    """The menu reconfigures logging every run; handles must not pile up."""
    import logging

    from memebot.logging_utils import setup_logging

    log_file = str(tmp_path / "memebot.log")
    opened = []
    for _ in range(5):
        setup_logging("INFO", log_file, console=False)
        opened.append([h for h in logging.getLogger().handlers
                       if isinstance(h, logging.FileHandler)])

    assert all(len(handlers) == 1 for handlers in opened), "one file handler at a time"
    # A closed FileHandler drops its stream, so either state means "not leaked".
    assert all(h.stream is None or h.stream.closed
               for batch in opened[:-1] for h in batch), \
        "the previous run's file handle should be closed"
    setup_logging("INFO", None)
