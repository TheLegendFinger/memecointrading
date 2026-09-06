"""Storage tests: SQLite behaviour, and the Postgres dialect via a stub driver.

The Postgres tests use a recording fake connection rather than a live server.
They verify the dialect - placeholders, DDL types, no SQLite-only syntax - and
that every query still binds the parameters it should. They are not a
substitute for running against a real Postgres, which the deployment docs cover.
"""

import pytest

from memebot.models import Fill, Order, Position, Side, Token
from memebot.storage import Storage, is_postgres_dsn, open_storage, resolve_state_target

TOKEN = Token("mint-wif", "WIF", "dogwifhat", 6)


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class FakePostgresConnection:
    """Records every statement; replays canned rows for SELECTs.

    `rows` are returned for the queries under test. The schema migration runs
    first and reads the state version, which must not be fed those rows.
    """

    def __init__(self, rows=None):
        self.statements = []
        self.rows = rows or []
        self.closed = False
        self.commits = 0
        self.ready = False        # flipped once construction is done

    def execute(self, sql, params=()):
        self.statements.append((sql, params))
        if not sql.strip().upper().startswith("SELECT"):
            return FakeCursor([])
        return FakeCursor(self.rows if self.ready else [])

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


def pg_storage(rows=None):
    conn = FakePostgresConnection(rows)
    store = Storage("postgresql://user:pw@host/db", connection=conn)
    conn.ready = True
    return store, conn


# ---- backend selection ---------------------------------------------------------
@pytest.mark.parametrize("dsn, expected", [
    ("postgres://u:p@h/db", True),
    ("postgresql://u:p@h/db", True),
    ("data/memebot.sqlite3", False),
    (":memory:", False),
    ("C:\\Users\\me\\memebot.sqlite3", False),
])
def test_dsn_detection(dsn, expected):
    assert is_postgres_dsn(dsn) is expected


def test_open_storage_returns_sqlite_for_a_path():
    store = open_storage(":memory:")
    assert not store.postgres
    assert store.dialect.name == "sqlite"
    store.close()


def test_resolve_state_target_prefers_env_urls(monkeypatch):
    for key in ("MEMEBOT_STATE_DB", "MEMEBOT_DATABASE_URL", "POSTGRES_URL", "DATABASE_URL"):
        monkeypatch.delenv(key, raising=False)
    assert resolve_state_target("data/x.sqlite3") == "data/x.sqlite3"

    monkeypatch.setenv("DATABASE_URL", "postgres://from-database-url/db")
    assert resolve_state_target("data/x.sqlite3") == "postgres://from-database-url/db"

    monkeypatch.setenv("POSTGRES_URL", "postgres://from-postgres-url/db")
    assert resolve_state_target("data/x.sqlite3") == "postgres://from-postgres-url/db"

    monkeypatch.setenv("MEMEBOT_STATE_DB", "postgres://explicit/db")
    assert resolve_state_target("data/x.sqlite3") == "postgres://explicit/db"


def test_blank_env_values_are_ignored(monkeypatch):
    monkeypatch.setenv("POSTGRES_URL", "   ")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("MEMEBOT_STATE_DB", raising=False)
    monkeypatch.delenv("MEMEBOT_DATABASE_URL", raising=False)
    assert resolve_state_target("data/x.sqlite3") == "data/x.sqlite3"


# ---- SQLite round trips --------------------------------------------------------
def test_state_round_trip(storage):
    assert storage.get_state("missing", "fallback") == "fallback"
    storage.set_state("cash_usd", 123.45)
    assert storage.get_state("cash_usd") == 123.45
    storage.set_state("cash_usd", 99.0)  # upsert, not a duplicate row
    assert storage.get_state("cash_usd") == 99.0


def test_position_round_trip_preserves_every_field(storage):
    pos = Position(token=TOKEN, quantity=1234.5, avg_price=0.01, cost_usd=12.345,
                   pair_address="pair-wif", last_price=0.02, high_price=0.03,
                   fees_usd=0.5, realized_pnl_usd=1.5, peak_liquidity_usd=250_000.0)
    storage.save_position(pos)

    loaded = storage.load_positions()["mint-wif"]
    assert loaded.token == TOKEN
    assert loaded.quantity == 1234.5
    assert loaded.high_price == 0.03
    assert loaded.peak_liquidity_usd == 250_000.0

    pos.quantity = 500.0
    storage.save_position(pos)
    assert len(storage.load_positions()) == 1
    assert storage.load_positions()["mint-wif"].quantity == 500.0

    storage.delete_position("mint-wif")
    assert storage.load_positions() == {}


def test_equity_curve_upserts_on_the_same_timestamp(storage):
    storage.record_equity(900.0, 100.0, ts=1000.0)
    storage.record_equity(800.0, 300.0, ts=1000.0)
    curve = storage.equity_curve()
    assert len(curve) == 1
    assert curve[0]["equity"] == 1100.0
    assert storage.peak_equity() == 1100.0


def test_equity_at_or_before(storage):
    storage.record_equity(1000.0, 0.0, ts=100.0)
    storage.record_equity(1100.0, 0.0, ts=200.0)
    assert storage.equity_at_or_before(150.0) == 1000.0
    assert storage.equity_at_or_before(50.0) is None


def test_reset_clears_every_table(storage):
    order = Order(token=TOKEN, side=Side.BUY, reference_price=1.0, usd_amount=10)
    storage.record_fill(Fill(order=order, ok=True, price=1.0, token_amount=10, usd_amount=10))
    storage.set_state("cash_usd", 1.0)
    storage.record_equity(1.0, 0.0)
    storage.save_position(Position(token=TOKEN, quantity=1, avg_price=1, cost_usd=1))

    storage.reset()
    assert storage.list_trades() == []
    assert storage.load_positions() == {}
    assert storage.get_state("cash_usd") is None
    assert storage.equity_curve() == []


# ---- Postgres dialect ----------------------------------------------------------
def test_postgres_schema_uses_portable_types():
    _store, conn = pg_storage()
    ddl = " ".join(sql for sql, _ in conn.statements)

    assert "BIGSERIAL PRIMARY KEY" in ddl
    assert "DOUBLE PRECISION" in ddl
    assert "AUTOINCREMENT" not in ddl, "SQLite-only syntax leaked into the Postgres schema"
    assert "INSERT OR REPLACE" not in ddl
    tables = ["trades", "positions", "state", "events", "price_samples",
              "trade_outcomes", "equity"]
    for table in tables:
        assert f"CREATE TABLE IF NOT EXISTS {table}" in ddl
    assert ddl.count("CREATE TABLE IF NOT EXISTS") == len(tables)


def test_postgres_queries_use_percent_placeholders():
    store, conn = pg_storage()
    store.set_state("cash_usd", 10.0)

    sql, params = conn.statements[-1]
    assert "%s" in sql and "?" not in sql
    assert params[0] == "cash_usd"


def test_postgres_writes_do_not_call_commit():
    """We connect with autocommit; an explicit commit would be a bug."""
    store, conn = pg_storage()
    store.set_state("k", 1)
    store.record_equity(1.0, 2.0, ts=5.0)
    assert conn.commits == 0


def test_sqlite_writes_do_commit(storage):
    assert storage.dialect.needs_commit is True


def test_postgres_position_insert_binds_all_columns():
    store, conn = pg_storage()
    store.save_position(Position(token=TOKEN, quantity=10.0, avg_price=1.0, cost_usd=10.0))

    sql, params = conn.statements[-1]
    assert sql.count("%s") == 15
    assert len(params) == 15
    assert params[0] == "mint-wif"
    assert "ON CONFLICT(token_address) DO UPDATE" in sql


def test_postgres_reads_are_decoded_into_domain_objects():
    row = {
        "token_address": "mint-wif", "symbol": "WIF", "name": "dogwifhat", "decimals": 6,
        "pair_address": "pair-wif", "quantity": 100.0, "avg_price": 0.01, "cost_usd": 1.0,
        "opened_at": 1700000000.0, "last_price": 0.02, "high_price": 0.03, "fees_usd": 0.1,
        "realized_pnl_usd": 0.2, "peak_liquidity_usd": 1000.0, "last_seen_at": 1700000001.0,
    }
    store, _conn = pg_storage(rows=[row])
    positions = store.load_positions()
    assert positions["mint-wif"].token.symbol == "WIF"
    assert positions["mint-wif"].quantity == 100.0


def test_postgres_trade_stats_tolerate_null_aggregates():
    store, _conn = pg_storage(rows=[{"n": 0, "pnl": None, "f": None}])
    stats = store.trade_stats()
    assert stats["closed_trades"] == 0
    assert stats["realized_pnl_usd"] == 0.0
    assert stats["win_rate"] == 0.0


def test_missing_psycopg_is_reported_clearly(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name.startswith("psycopg"):
            raise ImportError("no psycopg here")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    with pytest.raises(RuntimeError, match="psycopg"):
        Storage("postgres://user:pw@host/db")


# ---- migrating away an imaginary bankroll --------------------------------------
def test_a_pre_wallet_bankroll_is_cleared_on_open(tmp_path):
    """An older database holds cash seeded from a config setting that is gone.

    Showing that as equity would report money the bot never had, so it is
    dropped and the next cycle reads the real balance from the chain.
    """
    db = str(tmp_path / "old.sqlite3")
    old = Storage(db)
    old.execute("DELETE FROM state WHERE key = 'state_version'")
    old.set_state("cash_usd", 100.0)
    old.set_state("starting_cash_usd", 100.0)
    old.set_state("day_start_equity", 100.0)
    old.record_equity(100.0, 0.0, ts=1.0)
    old._commit()
    old.close()

    reopened = Storage(db)
    try:
        assert reopened.get_state("cash_usd") is None
        assert reopened.get_state("starting_cash_usd") is None
        assert reopened.get_state("day_start_equity") is None
        assert reopened.equity_curve() == [], "the curve recorded the same fiction"
        assert reopened.get_state("state_version") == Storage.STATE_VERSION
    finally:
        reopened.close()


def test_a_real_balance_survives_reopening(tmp_path):
    """Once migrated, a wallet-derived balance must persist across restarts."""
    db = str(tmp_path / "current.sqlite3")
    store = Storage(db)
    store.set_state("cash_usd", 87.40)
    store.set_state("starting_cash_usd", 87.40)
    store.close()

    reopened = Storage(db)
    try:
        assert reopened.get_state("cash_usd") == 87.40
        assert reopened.get_state("starting_cash_usd") == 87.40
    finally:
        reopened.close()


def test_the_migration_leaves_trades_and_positions_alone(tmp_path):
    """It clears an invented bankroll, not the record of what actually happened."""
    from memebot.models import Fill, Order, Side, Token

    db = str(tmp_path / "old.sqlite3")
    old = Storage(db)
    old.execute("DELETE FROM state WHERE key = 'state_version'")
    old.set_state("cash_usd", 100.0)
    token = Token("mint-x", "X")
    old.record_fill(Fill(order=Order(token=token, side=Side.BUY, reference_price=1.0,
                                     usd_amount=10.0),
                         ok=True, price=1.0, token_amount=10, usd_amount=10.0))
    old.save_position(Position(token=token, quantity=10, avg_price=1.0, cost_usd=10.0))
    old.close()

    reopened = Storage(db)
    try:
        assert reopened.get_state("cash_usd") is None
        assert len(reopened.list_trades()) == 1
        assert len(reopened.load_positions()) == 1
    finally:
        reopened.close()


def test_the_migration_runs_once(tmp_path):
    db = str(tmp_path / "once.sqlite3")
    Storage(db).close()

    store = Storage(db)
    try:
        store.set_state("cash_usd", 50.0)
    finally:
        store.close()

    reopened = Storage(db)
    try:
        assert reopened.get_state("cash_usd") == 50.0, "a migrated database is not re-migrated"
    finally:
        reopened.close()
