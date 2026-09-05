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


def test_the_shipped_example_config_is_valid():
    """config.example.yaml is the file users copy - every key must be real."""
    cfg = load_config("config.example.yaml")
    assert cfg.mode == "paper"
    assert cfg.data.use_token_profiles is True
    assert cfg.execution.paper_use_live_quotes is False


def test_state_db_accepts_a_postgres_url(monkeypatch):
    monkeypatch.delenv("POSTGRES_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("MEMEBOT_STATE_DB", raising=False)
    cfg = load_config(None, {"state_db": "postgresql://u:p@host/db"})
    cfg.validate()
    assert cfg.state_db.startswith("postgresql://")


def test_database_url_in_the_environment_wins_over_the_file(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("state_db: data/local.sqlite3\n")
    monkeypatch.delenv("MEMEBOT_STATE_DB", raising=False)
    monkeypatch.delenv("POSTGRES_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgres://cloud/db")
    assert load_config(str(path)).state_db == "postgres://cloud/db"


def test_an_explicit_db_flag_still_wins_over_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://cloud/db")
    cfg = load_config(None, {"state_db": str(tmp_path / "forced.sqlite3")})
    assert cfg.state_db.endswith("forced.sqlite3")


# ---- wallet command exit codes (the PowerShell scripts branch on these) --------
def test_wallet_reports_no_wallet_with_a_distinct_code(monkeypatch, capsys):
    """2 means 'nothing configured' - only then may a script offer to create one."""
    import memebot.config as config_module

    monkeypatch.delenv("SOLANA_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("SOLANA_KEYPAIR_PATH", raising=False)
    # A developer's own .env must not decide the result of this test.
    monkeypatch.setattr(config_module, "load_dotenv", lambda *a, **k: None)

    assert cli.main(["wallet"]) == cli.NO_WALLET
    assert "No wallet configured" in capsys.readouterr().out


def test_wallet_reports_an_unreadable_balance_distinctly(monkeypatch, capsys):
    """3 means 'a wallet exists but cannot trade' - never offer to create another."""
    import memebot.config as config_module
    import memebot.wallet as wallet_module
    from memebot.execution import live as live_module

    monkeypatch.setattr(config_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(wallet_module, "configured_address", lambda: "Wallet1111")

    class Broken:
        def __init__(self, *a, **k):
            pass

        def wallet_summary(self):
            raise RuntimeError("RPC unreachable")

    monkeypatch.setattr(live_module, "LiveExecutor", Broken)

    assert cli.main(["wallet"]) == cli.WALLET_NOT_READY
    output = capsys.readouterr().out
    assert "could not be read" in output
    assert "Do NOT create another wallet" in output


def test_the_wallet_exit_codes_are_distinct():
    """The scripts tell these apart; they must never collide with success."""
    assert cli.NO_WALLET != cli.WALLET_NOT_READY
    assert 0 not in (cli.NO_WALLET, cli.WALLET_NOT_READY)
