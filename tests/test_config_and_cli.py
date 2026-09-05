import json

import pytest

from memebot import cli
from memebot.config import BotConfig, load_config
from memebot.storage import Storage


# ---- config --------------------------------------------------------------------
def test_defaults_are_paper_mode():
    cfg = BotConfig()
    cfg.validate()
    assert cfg.mode == "paper" and not cfg.is_live


def test_yaml_file_overrides_defaults(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "mode: paper\n"
        "poll_interval_seconds: 45\n"
        "risk:\n"
        "  starting_cash_usd: 5000\n"
        "  max_open_positions: 3\n"
        "filters:\n"
        "  min_liquidity_usd: 100000\n"
    )
    cfg = load_config(str(path))
    assert cfg.poll_interval_seconds == 45
    assert cfg.risk.starting_cash_usd == 5000
    assert cfg.risk.max_open_positions == 3
    assert cfg.filters.min_liquidity_usd == 100000
    assert cfg.strategy.name == "momentum"  # untouched default


def test_json_config_is_supported(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"risk": {"starting_cash_usd": 250}}))
    assert load_config(str(path)).risk.starting_cash_usd == 250


def test_unknown_keys_are_rejected(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("risk:\n  not_a_real_setting: 1\n")
    with pytest.raises(ValueError, match="Unknown config key: risk.not_a_real_setting"):
        load_config(str(path))


def test_missing_file_is_an_error():
    with pytest.raises(FileNotFoundError):
        load_config("/nope/does-not-exist.yaml")


def test_env_overrides_the_file(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("risk:\n  starting_cash_usd: 100\n")
    monkeypatch.setenv("MEMEBOT_STARTING_CASH", "777")
    monkeypatch.setenv("MEMEBOT_MAX_POSITIONS", "9")
    cfg = load_config(str(path))
    assert cfg.risk.starting_cash_usd == 777
    assert cfg.risk.max_open_positions == 9


def test_explicit_overrides_beat_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMEBOT_MODE", "live")
    cfg = load_config(None, {"mode": "paper"})
    assert cfg.mode == "paper"


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda c: setattr(c, "mode", "turbo"), "mode must be"),
        (lambda c: setattr(c.risk, "position_size_pct", 0), "position_size_pct"),
        (lambda c: setattr(c.risk, "max_open_positions", 0), "max_open_positions"),
        (lambda c: setattr(c.risk, "stop_loss_pct", 1.5), "stop_loss_pct"),
        (lambda c: setattr(c.execution, "slippage_bps", -10), "slippage_bps"),
        (lambda c: setattr(c, "poll_interval_seconds", 0.1), "poll_interval_seconds"),
    ],
)
def test_validation_catches_bad_settings(mutate, message):
    cfg = BotConfig()
    mutate(cfg)
    with pytest.raises(ValueError, match=message):
        cfg.validate()


def test_min_position_cannot_exceed_max():
    cfg = BotConfig()
    cfg.risk.min_position_usd = 500
    cfg.risk.max_position_usd = 100
    with pytest.raises(ValueError, match="min_position_usd"):
        cfg.validate()


# ---- CLI -----------------------------------------------------------------------
def test_config_command_prints_effective_settings(capsys):
    assert cli.main(["config"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["mode"] == "paper"
    assert printed["risk"]["starting_cash_usd"] == 1000.0


def test_status_command_on_a_fresh_database(tmp_path, capsys):
    db = str(tmp_path / "s.sqlite3")
    assert cli.main(["--db", db, "status"]) == 0
    out = capsys.readouterr().out
    assert "equity" in out and "$1,000.00" in out


def test_status_json_output(tmp_path, capsys):
    db = str(tmp_path / "s.sqlite3")
    assert cli.main(["--db", db, "status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["equity_usd"] == 1000.0
    assert payload["open_positions"] == 0


def test_bad_config_path_exits_with_code_two(capsys):
    assert cli.main(["--config", "/nope.yaml", "status"]) == 2
    assert "config error" in capsys.readouterr().err


def test_reset_refuses_in_live_mode(tmp_path, capsys):
    db = str(tmp_path / "s.sqlite3")
    assert cli.main(["--db", db, "--mode", "live", "reset", "-y"]) == 1
    assert "Refusing to reset" in capsys.readouterr().err


def test_reset_wipes_paper_state(tmp_path, capsys):
    db = str(tmp_path / "s.sqlite3")
    store = Storage(db)
    store.set_state("cash_usd", 12.34)
    store.close()

    assert cli.main(["--db", db, "reset", "-y"]) == 0
    assert Storage(db).get_state("cash_usd") is None


def test_trades_command_is_empty_on_a_new_database(tmp_path, capsys):
    db = str(tmp_path / "s.sqlite3")
    assert cli.main(["--db", db, "trades"]) == 0
    assert "nothing to show" in capsys.readouterr().out


def test_cli_flags_reach_the_engine_config(monkeypatch, capsys):
    """`--dry-run` and `--mode` must survive parsing into the live BotConfig."""
    seen = {}

    class StubEngine:
        def __init__(self, config):
            seen["config"] = config

        def preflight(self):
            return None

        def run_cycle(self):
            from memebot.engine import CycleReport

            return CycleReport(scanned=3, passed_filters=1, signals=1)

    monkeypatch.setattr(cli, "_build_engine", StubEngine)
    assert cli.main(["--dry-run", "--mode", "paper", "once"]) == 0

    assert seen["config"].dry_run is True
    assert seen["config"].mode == "paper"
    assert "scanned=3" in capsys.readouterr().out


def test_once_reports_a_blocked_executor(monkeypatch, capsys):
    class StubEngine:
        def __init__(self, config):
            pass

        def preflight(self):
            return "live trading is not armed"

    monkeypatch.setattr(cli, "_build_engine", StubEngine)
    assert cli.main(["once"]) == 1
    assert "not armed" in capsys.readouterr().err
