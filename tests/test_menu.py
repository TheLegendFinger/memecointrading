"""Menu tests.

The menu is the front door: a crash there strands someone who does not know
the command line. So every action is checked for existence, every failure mode
is checked for being caught, and the loop is checked for always terminating.
"""

import pytest

from memebot.menu import MENU, Menu


class Driver:
    """Feeds scripted keystrokes and collects what was printed."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.lines = []
        self.prompts = []

    def input_fn(self, prompt=""):
        self.prompts.append(prompt)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)

    def output(self, *args, **kwargs):
        self.lines.append(" ".join(str(a) for a in args))

    @property
    def text(self):
        return "\n".join(self.lines)


def menu_for(*answers, **kwargs):
    driver = Driver(*answers)
    return Menu(input_fn=driver.input_fn, output=driver.output, clear=False, **kwargs), driver


# ---- rendering -----------------------------------------------------------------
def test_every_item_is_shown_with_its_number_and_purpose():
    menu, driver = menu_for("0")
    menu.run()

    for item in MENU:
        assert item.key in driver.text
        assert item.title in driver.text
        if item.description:
            assert item.description in driver.text


def test_the_wordmark_is_shown():
    menu, driver = menu_for("0")
    menu.run()
    from memebot.ui import wordmark

    for line in wordmark():
        assert line.rstrip() in driver.text


def test_the_wordmark_falls_back_to_ascii_on_a_limited_console(monkeypatch):
    """A console that cannot encode box drawing must not crash on the title."""
    import memebot.ui as ui

    monkeypatch.setattr(ui, "_UNICODE_OK", False)
    monkeypatch.setattr(ui, "_ENABLED", False)
    menu, driver = menu_for("0")
    menu.run()
    driver.text.encode("ascii")


def test_the_sections_are_labelled():
    menu, driver = menu_for("0")
    menu.run()
    for section in ("TRADE", "LOOK", "SETUP"):
        assert section in driver.text


def test_the_header_shows_the_portfolio(tmp_path, monkeypatch):
    """With a real book, the state line summarises it."""
    import random

    from memebot.config import BotConfig
    from memebot.engine import TradingEngine
    from tests.fakes import SimulatedExecutor
    from memebot.storage import Storage
    from tests.conftest import FakeDexScreener, make_pair

    db = str(tmp_path / "menu.sqlite3")
    monkeypatch.setenv("MEMEBOT_STATE_DB", db)

    config = BotConfig()
    config.state_db = db
    hot = make_pair("BEST", chg_m5=12.0, chg_h1=55.0, vol_h1=400_000, vol_h24=960_000,
                    buys_m5=90, sells_m5=10, liquidity=700_000)
    market = FakeDexScreener([hot])
    engine = TradingEngine(config, storage=Storage(db), data=market,
                           executor=SimulatedExecutor(config, data=market, rng=random.Random(1)))
    engine.run_cycle()
    engine.storage.close()

    menu, driver = menu_for("0")
    menu.run()
    assert "1 open" in driver.text
    assert "cash" in driver.text


def test_a_broken_state_file_still_renders_the_menu(monkeypatch):
    """The header must never be the thing that stops you using the menu."""
    monkeypatch.setenv("MEMEBOT_STATE_DB", "postgres://nobody@nowhere/db")
    menu, driver = menu_for("0")
    menu.run()
    assert "could not read the portfolio" in driver.text
    assert "Start trading" in driver.text


# ---- input handling ------------------------------------------------------------
@pytest.mark.parametrize("answer", ["0", "q", "quit", "exit", "Q"])
def test_quit_words(answer):
    menu, driver = menu_for(answer)
    assert menu.run() == 0
    assert "Bye." in driver.text


def test_an_unknown_choice_explains_itself_and_carries_on():
    menu, driver = menu_for("42", "0")
    menu.run()
    assert "not on the menu" in driver.text
    assert driver.text.count("Start trading") == 2, "the menu should redraw"


def test_empty_input_is_not_fatal():
    menu, driver = menu_for("", "0")
    menu.run()
    assert "not on the menu" in driver.text


def test_end_of_input_quits_rather_than_looping_forever():
    """Piping into the menu must terminate, not spin."""
    menu, _driver = menu_for()  # no answers at all -> EOFError on first read
    assert menu.run() == 0


# ---- dispatch ------------------------------------------------------------------
def test_every_menu_item_has_a_handler():
    menu, _ = menu_for("0")
    for item in MENU:
        assert hasattr(menu, f"do_{item.action}"), f"missing handler for {item.key} ({item.title})"


@pytest.mark.parametrize("key, expected", [
    ("4", ["status"]),
    ("5", ["trades", "--limit", "25"]),
    ("6", ["scan", "--limit", "20"]),
    ("8", ["doctor"]),
])
def test_read_only_actions_call_the_cli(monkeypatch, key, expected):
    calls = []
    monkeypatch.setattr("memebot.cli.main", lambda argv: calls.append(argv) or 0)

    menu, _driver = menu_for(key, "", "0")
    menu.run()

    assert calls, f"choice {key} ran nothing"
    assert calls[0][-len(expected):] == expected


def test_an_action_that_raises_is_reported_not_fatal(monkeypatch):
    def explode(self):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(Menu, "do_status", explode)
    menu, driver = menu_for("4", "0")

    assert menu.run() == 0
    assert "RuntimeError: kaboom" in driver.text


def test_ctrl_c_inside_an_action_returns_to_the_menu(monkeypatch):
    def interrupted(self):
        raise KeyboardInterrupt

    monkeypatch.setattr(Menu, "do_trade", interrupted)
    menu, driver = menu_for("1", "0")

    assert menu.run() == 0
    assert "Stopped." in driver.text


# ---- trading actions -----------------------------------------------------------
def test_a_dry_run_places_no_orders(monkeypatch):
    seen = {}
    monkeypatch.setattr(Menu, "_prepare", lambda self, require_wallet: True)
    monkeypatch.setattr(Menu, "_run_engine", lambda self, dry_run=False: seen.update(dry=dry_run))

    menu, _driver = menu_for("2", "0")
    menu.run()
    assert seen == {"dry": True}


def test_a_dry_run_does_not_need_a_wallet(monkeypatch):
    """Nothing is sent, so there is nothing to sign with."""
    asked = {}
    monkeypatch.setattr(Menu, "_prepare",
                        lambda self, require_wallet: asked.update(wallet=require_wallet) or True)
    monkeypatch.setattr(Menu, "_run_engine", lambda self, dry_run=False: None)

    menu, _driver = menu_for("2", "0")
    menu.run()
    assert asked == {"wallet": False}


def test_trading_needs_the_word_LIVE(monkeypatch):
    """Anything other than exactly LIVE must not start real trading."""
    started = []
    monkeypatch.setattr(Menu, "_prepare", lambda self, require_wallet: True)
    monkeypatch.setattr(Menu, "_run_engine", lambda self, dry_run=False: started.append(True))

    # Lower case, a vague yes, or just pressing Enter must never be enough.
    for answer in ("live", "yes", "y", "ok", ""):
        menu, driver = menu_for("1", answer, "0")
        menu.run()
        assert not started, f"{answer!r} must not start real trading"
        assert "Not started." in driver.text


def test_surrounding_whitespace_does_not_defeat_the_confirmation(monkeypatch):
    """Typing the word with a stray space still counts - they typed the word."""
    started = []
    monkeypatch.setattr(Menu, "_prepare", lambda self, require_wallet: True)
    monkeypatch.setattr(Menu, "_run_engine", lambda self, dry_run=False: started.append(True))

    menu, _driver = menu_for("1", "  LIVE  ", "0")
    menu.run()
    assert started == [True]


def test_trading_starts_when_confirmed(monkeypatch):
    started = []
    monkeypatch.setattr(Menu, "_prepare", lambda self, require_wallet: True)
    monkeypatch.setattr(Menu, "_run_engine", lambda self, dry_run=False: started.append(dry_run))

    menu, _driver = menu_for("1", "LIVE", "0")
    menu.run()
    assert started == [False], "the real thing, not a dry run"


def test_trading_stops_when_the_wallet_is_not_ready(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    started = []
    monkeypatch.setattr(Menu, "ensure_solana_packages", lambda self: True)
    monkeypatch.setattr(Menu, "_run_engine", lambda self, dry_run=False: started.append(dry_run))
    monkeypatch.setattr("memebot.cli.cmd_wallet", lambda args, config: 3)  # exists, unfunded

    menu, driver = menu_for("1", "", "0")
    menu.run()
    assert not started
    assert "cannot trade yet" in driver.text


def test_trading_offers_to_create_a_missing_wallet(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Menu, "ensure_solana_packages", lambda self: True)
    created = []

    def fake_wallet(args, config):
        if args.new:
            created.append(True)
            return 0
        return 2  # nothing configured

    monkeypatch.setattr("memebot.cli.cmd_wallet", fake_wallet)
    started = []
    monkeypatch.setattr(Menu, "_run_engine", lambda self, dry_run=False: started.append(dry_run))

    menu, driver = menu_for("1", "y", "", "0")
    menu.run()

    assert created == [True]
    assert not started, "a freshly created wallet has no funds yet"
    assert "Send SOL to that address" in driver.text


def test_liquidate_says_so_when_there_is_nothing_to_close(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMEBOT_STATE_DB", str(tmp_path / "empty.sqlite3"))
    menu, driver = menu_for("3", "0")
    menu.run()
    assert "No open positions" in driver.text


# ---- the live terminal display -------------------------------------------------
def test_the_menu_has_no_browser_dashboard():
    """Trading is watched in the terminal now, not in a browser tab."""
    assert all(item.action != "dashboard" for item in MENU)
    assert not hasattr(Menu, "do_dashboard")


def test_running_attaches_the_live_display(monkeypatch, tmp_path):
    """The engine must be given the view, or nothing would redraw."""
    monkeypatch.setenv("MEMEBOT_STATE_DB", str(tmp_path / "view.sqlite3"))
    captured = {}

    class StubEngine:
        def __init__(self, config, on_cycle=None):
            captured["on_cycle"] = on_cycle
            self.storage = type("S", (), {"close": lambda self: None})()

        def preflight(self):
            return None

        def run(self):
            captured["ran"] = True

    monkeypatch.setattr("memebot.engine.TradingEngine", StubEngine)
    monkeypatch.setattr(Menu, "_prepare", lambda self, require_wallet: True)

    menu, _driver = menu_for("1", "LIVE", "0")
    menu.run()

    assert captured.get("ran") is True
    from memebot.console_view import ConsoleView

    assert isinstance(captured["on_cycle"], ConsoleView)


def test_logging_goes_to_the_file_while_the_display_is_up(monkeypatch, tmp_path):
    """Log lines would scribble over the frame the view redraws."""
    monkeypatch.setenv("MEMEBOT_STATE_DB", str(tmp_path / "view.sqlite3"))
    calls = {}

    class StubEngine:
        def __init__(self, config, on_cycle=None):
            self.storage = type("S", (), {"close": lambda self: None})()

        def preflight(self):
            return None

        def run(self):
            pass

    monkeypatch.setattr("memebot.engine.TradingEngine", StubEngine)
    monkeypatch.setattr(Menu, "_prepare", lambda self, require_wallet: True)
    monkeypatch.setattr("memebot.logging_utils.setup_logging",
                        lambda level, log_file=None, console=True: calls.update(console=console))

    menu, _driver = menu_for("1", "LIVE", "0")
    menu.run()

    # The test harness is not a terminal, so the view is inactive and ordinary
    # logging stays on - which is exactly the piped-output behaviour.
    assert calls["console"] is True


# ---- the wallet submenu --------------------------------------------------------
def test_the_wallet_submenu_offers_every_operation(monkeypatch):
    monkeypatch.setattr(Menu, "ensure_solana_packages", lambda self: True)
    menu, driver = menu_for("7", "0", "0")
    menu.run()

    for expected in ("Show wallet", "Create a new wallet", "Show seed phrase",
                     "Restore from a seed phrase", "Withdraw SOL"):
        assert expected in driver.text


def test_the_wallet_submenu_installs_the_solana_packages_first(monkeypatch):
    """The reported bug: creating a wallet failed telling you to run pip yourself."""
    calls = []
    monkeypatch.setattr(Menu, "ensure_solana_packages",
                        lambda self: calls.append(True) or True)
    menu, _driver = menu_for("7", "0", "0")
    menu.run()
    assert calls, "the wallet menu must ensure its dependencies"


def test_the_wallet_submenu_stops_when_packages_cannot_be_installed(monkeypatch):
    monkeypatch.setattr(Menu, "ensure_solana_packages", lambda self: False)
    shown = []
    monkeypatch.setattr(Menu, "_wallet_cli", lambda self, **kw: shown.append(kw))

    menu, _driver = menu_for("7", "0")
    menu.run()
    assert shown == [], "nothing should run without the packages"


@pytest.mark.parametrize("key, expected", [
    ("1", {}),                      # plain show: no flags
    ("3", {"phrase": True}),
    ("4", {"import_phrase": True}),
])
def test_wallet_options_call_the_right_command(monkeypatch, key, expected):
    monkeypatch.setattr(Menu, "ensure_solana_packages", lambda self: True)
    calls = []
    monkeypatch.setattr(Menu, "_wallet_cli", lambda self, **kw: calls.append(kw) or 0)
    # option 3 asks for confirmation before revealing the phrase
    answers = ["7", key] + (["y"] if key == "3" else []) + ["", "0", "0"]

    menu, _driver = menu_for(*answers)
    menu.run()

    assert calls, f"option {key} ran nothing"
    assert calls[0] == expected


def test_the_seed_phrase_is_not_shown_without_confirmation(monkeypatch):
    monkeypatch.setattr(Menu, "ensure_solana_packages", lambda self: True)
    calls = []
    monkeypatch.setattr(Menu, "_wallet_cli", lambda self, **kw: calls.append(kw) or 0)

    menu, driver = menu_for("7", "3", "n", "", "0", "0")
    menu.run()

    assert calls == [], "declining must not print the phrase"
    assert "Not shown" in driver.text


def test_creating_a_second_wallet_warns_instead_of_replacing(monkeypatch, tmp_path):
    """Overwriting a funded wallet's key would strand the funds."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("SOLANA_PRIVATE_KEY=already-here\n")
    monkeypatch.setattr(Menu, "ensure_solana_packages", lambda self: True)
    calls = []
    monkeypatch.setattr(Menu, "_wallet_cli", lambda self, **kw: calls.append(kw) or 0)

    menu, driver = menu_for("7", "2", "", "0", "0")
    menu.run()

    assert calls == [], "it must not create over an existing wallet"
    assert "already configured" in driver.text
    assert "back up its seed phrase" in driver.text


def test_withdrawing_warns_while_positions_are_open(monkeypatch):
    monkeypatch.setattr(Menu, "ensure_solana_packages", lambda self: True)
    monkeypatch.setattr(Menu, "_open_position_count", lambda self: 2)
    calls = []
    monkeypatch.setattr(Menu, "_wallet_cli", lambda self, **kw: calls.append(kw) or 0)

    menu, driver = menu_for("7", "5", "n", "", "0", "0")
    menu.run()

    assert "still holds 2 position" in driver.text
    assert calls == [], "declining the warning must not withdraw"
