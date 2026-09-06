"""Persistence.

The same schema and queries run on two backends:

  * **SQLite** - the default, a single file next to the bot. Perfect when the
    bot runs on your own machine.
  * **Postgres** - for deployments where the filesystem is ephemeral (Vercel,
    Fly, containers) or where a local bot and a hosted dashboard need to read
    the same book.

Which one you get is decided by `state_db`: anything starting with
`postgres://` or `postgresql://` opens Postgres, everything else is a SQLite
path. Only the dialect differs - the queries below are written once.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from typing import Any, Dict, List, Optional

from .models import Fill, Position, Token

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------------
# dialects
# --------------------------------------------------------------------------------
class _Dialect:
    """The handful of things SQL engines disagree about."""

    name = "sqlite"
    placeholder = "?"
    serial_pk = "INTEGER PRIMARY KEY AUTOINCREMENT"
    real = "REAL"
    text = "TEXT"
    integer = "INTEGER"
    needs_commit = True

    def adapt(self, sql: str) -> str:
        return sql


class _PostgresDialect(_Dialect):
    name = "postgres"
    placeholder = "%s"
    serial_pk = "BIGSERIAL PRIMARY KEY"
    real = "DOUBLE PRECISION"
    text = "TEXT"
    integer = "BIGINT"
    needs_commit = False  # we connect with autocommit

    def adapt(self, sql: str) -> str:
        # None of our SQL contains a literal '?', so a plain swap is safe.
        return sql.replace("?", "%s")


def is_postgres_dsn(target: str) -> bool:
    return str(target).startswith(("postgres://", "postgresql://"))


# --------------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------------
class Storage:
    """Trades, positions, key/value state and the equity curve.

    `target` is either a SQLite path (`data/memebot.sqlite3`, `:memory:`) or a
    Postgres connection URL.
    """

    def __init__(self, target: str = "data/memebot.sqlite3", connection: Any = None) -> None:
        self.target = target
        self.postgres = is_postgres_dsn(target)
        self.dialect: _Dialect = _PostgresDialect() if self.postgres else _Dialect()

        if connection is not None:
            self.conn = connection
        elif self.postgres:
            self.conn = self._connect_postgres(target)
        else:
            self.conn = self._connect_sqlite(target)

        self._create_schema()

    # ---- connections -----------------------------------------------------------
    @staticmethod
    def _connect_sqlite(path: str):
        if path != ":memory:":
            directory = os.path.dirname(path)
            if directory:
                os.makedirs(directory, exist_ok=True)
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _connect_postgres(dsn: str):
        try:
            import psycopg  # type: ignore
            from psycopg.rows import dict_row  # type: ignore
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "Postgres state needs the 'psycopg' package: pip install 'psycopg[binary]'"
            ) from exc
        # Neon and friends hand out URLs with extra query args; psycopg handles them.
        return psycopg.connect(dsn, autocommit=True, row_factory=dict_row)

    # ---- plumbing --------------------------------------------------------------
    def execute(self, sql: str, params: tuple = ()):  # noqa: D401 - thin wrapper
        return self.conn.execute(self.dialect.adapt(sql), params)

    def _commit(self) -> None:
        if self.dialect.needs_commit:
            self.conn.commit()

    def _fetchone(self, sql: str, params: tuple = ()) -> Optional[Dict[str, Any]]:
        row = self.execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    def _fetchall(self, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
        return [dict(row) for row in self.execute(sql, params).fetchall()]

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:  # pragma: no cover - already closed
            pass

    # ---- schema ----------------------------------------------------------------
    def _schema_statements(self) -> List[str]:
        d = self.dialect
        return [
            f"""CREATE TABLE IF NOT EXISTS trades (
                    id            {d.serial_pk},
                    ts            {d.real} NOT NULL,
                    client_id     {d.text},
                    side          {d.text} NOT NULL,
                    token_address {d.text} NOT NULL,
                    symbol        {d.text},
                    price         {d.real} NOT NULL,
                    token_amount  {d.real} NOT NULL,
                    usd_amount    {d.real} NOT NULL,
                    fee_usd       {d.real} NOT NULL DEFAULT 0,
                    slippage_bps  {d.real} NOT NULL DEFAULT 0,
                    realized_pnl  {d.real} NOT NULL DEFAULT 0,
                    tx_signature  {d.text},
                    reason        {d.text},
                    mode          {d.text}
                )""",
            "CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts)",
            "CREATE INDEX IF NOT EXISTS idx_trades_token ON trades(token_address)",
            f"""CREATE TABLE IF NOT EXISTS positions (
                    token_address      {d.text} PRIMARY KEY,
                    symbol             {d.text},
                    name               {d.text},
                    decimals           {d.integer} DEFAULT 9,
                    pair_address       {d.text},
                    quantity           {d.real} NOT NULL,
                    avg_price          {d.real} NOT NULL,
                    cost_usd           {d.real} NOT NULL,
                    opened_at          {d.real} NOT NULL,
                    last_price         {d.real} NOT NULL DEFAULT 0,
                    high_price         {d.real} NOT NULL DEFAULT 0,
                    fees_usd           {d.real} NOT NULL DEFAULT 0,
                    realized_pnl_usd   {d.real} NOT NULL DEFAULT 0,
                    peak_liquidity_usd {d.real} NOT NULL DEFAULT 0,
                    last_seen_at       {d.real} NOT NULL DEFAULT 0
                )""",
            f"""CREATE TABLE IF NOT EXISTS state (
                    key   {d.text} PRIMARY KEY,
                    value {d.text} NOT NULL
                )""",
            f"""CREATE TABLE IF NOT EXISTS events (
                    id      {d.serial_pk},
                    ts      {d.real} NOT NULL,
                    kind    {d.text} NOT NULL,
                    level   {d.text} NOT NULL DEFAULT 'info',
                    symbol  {d.text},
                    address {d.text},
                    message {d.text} NOT NULL,
                    detail  {d.text}
                )""",
            "CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts)",
            f"""CREATE TABLE IF NOT EXISTS price_samples (
                    address {d.text} NOT NULL,
                    ts      {d.real} NOT NULL,
                    price   {d.real} NOT NULL,
                    PRIMARY KEY (address, ts)
                )""",
            "CREATE INDEX IF NOT EXISTS idx_samples_addr_ts ON price_samples(address, ts)",
            f"""CREATE TABLE IF NOT EXISTS trade_outcomes (
                    token_address {d.text} NOT NULL,
                    opened_at     {d.real} NOT NULL,
                    closed_at     {d.real} NOT NULL DEFAULT 0,
                    symbol        {d.text},
                    score         {d.real} NOT NULL DEFAULT 0,
                    source        {d.text},
                    features      {d.text} NOT NULL DEFAULT '{{}}',
                    cost_usd      {d.real} NOT NULL DEFAULT 0,
                    return_pct    {d.real} NOT NULL DEFAULT 0,
                    exit_reason   {d.text},
                    PRIMARY KEY (token_address, opened_at)
                )""",
            "CREATE INDEX IF NOT EXISTS idx_outcomes_closed ON trade_outcomes(closed_at)",
            f"""CREATE TABLE IF NOT EXISTS equity (
                    ts        {d.real} PRIMARY KEY,
                    cash      {d.real} NOT NULL,
                    positions {d.real} NOT NULL,
                    equity    {d.real} NOT NULL
                )""",
        ]

    def _create_schema(self) -> None:
        for statement in self._schema_statements():
            self.execute(statement)
        self._commit()
        self._migrate()

    # The stored state's shape, bumped when old values stop being meaningful.
    STATE_VERSION = 2

    def _migrate(self) -> None:
        """Bring an older database's state up to date.

        Version 2: the bankroll became the wallet's on-chain balance. Databases
        written before that hold a cash figure seeded from a config setting that
        no longer exists - a number the bot never actually had. Showing it as
        equity would be a lie, so it goes; the next cycle reads the real one.
        """
        try:
            version = int(self.get_state("state_version", 0) or 0)
        except (TypeError, ValueError):
            version = 0
        if version >= self.STATE_VERSION:
            return

        if version < 2:
            for key in ("cash_usd", "starting_cash_usd", "day_start_equity"):
                self.execute("DELETE FROM state WHERE key = ?", (key,))
            # The equity curve recorded that same imaginary bankroll.
            self.execute("DELETE FROM equity")
            self._commit()
            log.debug("Cleared a pre-wallet bankroll from %s", self.target)

        self.set_state("state_version", self.STATE_VERSION)

    # ---- key/value state -------------------------------------------------------
    def get_state(self, key: str, default: Any = None) -> Any:
        row = self._fetchone("SELECT value FROM state WHERE key = ?", (key,))
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            return default

    def set_state(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO state(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )
        self._commit()

    # ---- positions -------------------------------------------------------------
    def save_position(self, position: Position) -> None:
        self.execute(
            """INSERT INTO positions(token_address, symbol, name, decimals, pair_address,
                    quantity, avg_price, cost_usd, opened_at, last_price, high_price,
                    fees_usd, realized_pnl_usd, peak_liquidity_usd, last_seen_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(token_address) DO UPDATE SET
                    symbol=excluded.symbol, name=excluded.name, decimals=excluded.decimals,
                    pair_address=excluded.pair_address, quantity=excluded.quantity,
                    avg_price=excluded.avg_price, cost_usd=excluded.cost_usd,
                    opened_at=excluded.opened_at, last_price=excluded.last_price,
                    high_price=excluded.high_price, fees_usd=excluded.fees_usd,
                    realized_pnl_usd=excluded.realized_pnl_usd,
                    peak_liquidity_usd=excluded.peak_liquidity_usd,
                    last_seen_at=excluded.last_seen_at""",
            (
                position.token.address,
                position.token.symbol,
                position.token.name,
                position.token.decimals,
                position.pair_address,
                position.quantity,
                position.avg_price,
                position.cost_usd,
                position.opened_at,
                position.last_price,
                position.high_price,
                position.fees_usd,
                position.realized_pnl_usd,
                position.peak_liquidity_usd,
                position.last_seen_at,
            ),
        )
        self._commit()

    def delete_position(self, token_address: str) -> None:
        self.execute("DELETE FROM positions WHERE token_address = ?", (token_address,))
        self._commit()

    def load_positions(self) -> Dict[str, Position]:
        out: Dict[str, Position] = {}
        for row in self._fetchall("SELECT * FROM positions"):
            out[row["token_address"]] = Position(
                token=Token(
                    address=row["token_address"],
                    symbol=row["symbol"] or "",
                    name=row["name"] or "",
                    decimals=int(row["decimals"] or 9),
                ),
                quantity=float(row["quantity"]),
                avg_price=float(row["avg_price"]),
                cost_usd=float(row["cost_usd"]),
                opened_at=float(row["opened_at"]),
                pair_address=row["pair_address"] or "",
                last_price=float(row["last_price"]),
                high_price=float(row["high_price"]),
                fees_usd=float(row["fees_usd"]),
                realized_pnl_usd=float(row["realized_pnl_usd"]),
                peak_liquidity_usd=float(row["peak_liquidity_usd"]),
                last_seen_at=float(row["last_seen_at"]),
            )
        return out

    # ---- trades ----------------------------------------------------------------
    def record_fill(self, fill: Fill, realized_pnl: float = 0.0, mode: str = "live") -> None:
        row = fill.as_row()
        self.execute(
            """INSERT INTO trades(ts, client_id, side, token_address, symbol, price,
                    token_amount, usd_amount, fee_usd, slippage_bps, realized_pnl,
                    tx_signature, reason, mode)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                row["ts"], row["client_id"], row["side"], row["token_address"], row["symbol"],
                row["price"], row["token_amount"], row["usd_amount"], row["fee_usd"],
                row["slippage_bps"], realized_pnl, row["tx_signature"], row["reason"], mode,
            ),
        )
        self._commit()

    def list_trades(self, limit: int = 50, since: Optional[float] = None) -> List[Dict[str, Any]]:
        if since is not None:
            return self._fetchall(
                "SELECT * FROM trades WHERE ts >= ? ORDER BY ts DESC LIMIT ?", (since, limit)
            )
        return self._fetchall("SELECT * FROM trades ORDER BY ts DESC LIMIT ?", (limit,))

    def realized_pnl_since(self, since: float) -> float:
        row = self._fetchone(
            "SELECT COALESCE(SUM(realized_pnl), 0) AS pnl FROM trades WHERE ts >= ?", (since,)
        )
        return float((row or {}).get("pnl") or 0.0)

    def last_exit_time(self, token_address: str) -> float:
        row = self._fetchone(
            "SELECT MAX(ts) AS ts FROM trades WHERE token_address = ? AND side = 'sell'",
            (token_address,),
        )
        return float((row or {}).get("ts") or 0.0)

    def trade_stats(self) -> Dict[str, Any]:
        totals = self._fetchone(
            """SELECT COUNT(*) AS n,
                      COALESCE(SUM(realized_pnl), 0) AS pnl
               FROM trades WHERE side = 'sell'"""
        ) or {}
        wins_row = self._fetchone(
            "SELECT COUNT(*) AS n FROM trades WHERE side = 'sell' AND realized_pnl > 0"
        ) or {}
        fees_row = self._fetchone("SELECT COALESCE(SUM(fee_usd), 0) AS f FROM trades") or {}

        closed = int(totals.get("n") or 0)
        wins = int(wins_row.get("n") or 0)
        return {
            "closed_trades": closed,
            "wins": wins,
            "losses": closed - wins,
            "win_rate": (wins / closed) if closed else 0.0,
            "realized_pnl_usd": float(totals.get("pnl") or 0.0),
            "total_fees_usd": float(fees_row.get("f") or 0.0),
        }

    # ---- trade outcomes (what the learner reads) -------------------------------
    def record_entry(self, token_address: str, opened_at: float, symbol: str,
                     score: float, source: str, features: Dict[str, Any],
                     cost_usd: float) -> None:
        """What the bot knew at the moment it bought, before the result exists.

        Written at entry rather than reconstructed at exit: by the time a
        position closes the market has moved and the numbers that led to the
        buy are gone.
        """
        self.execute("DELETE FROM trade_outcomes WHERE token_address = ? AND opened_at = ?",
                     (token_address, opened_at))
        self.execute(
            """INSERT INTO trade_outcomes(token_address, opened_at, closed_at, symbol,
                                          score, source, features, cost_usd)
               VALUES(?,?,?,?,?,?,?,?)""",
            (token_address, opened_at, 0.0, symbol, score, source,
             json.dumps(features, sort_keys=True), cost_usd),
        )
        self._commit()

    def record_exit(self, token_address: str, closed_at: float, return_pct: float,
                    exit_reason: str) -> bool:
        """Close out the most recent open entry for a token. False if none."""
        row = self._fetchone(
            """SELECT opened_at FROM trade_outcomes
               WHERE token_address = ? AND closed_at = 0
               ORDER BY opened_at DESC LIMIT 1""",
            (token_address,),
        )
        if row is None:
            return False
        self.execute(
            """UPDATE trade_outcomes SET closed_at = ?, return_pct = ?, exit_reason = ?
               WHERE token_address = ? AND opened_at = ?""",
            (closed_at, return_pct, exit_reason, token_address, row["opened_at"]),
        )
        self._commit()
        return True

    def closed_outcomes(self, limit: int = 500) -> List[Dict[str, Any]]:
        """Finished trades, newest first, with the features they were bought on."""
        rows = self._fetchall(
            """SELECT * FROM trade_outcomes WHERE closed_at > 0
               ORDER BY closed_at DESC LIMIT ?""",
            (int(limit),),
        )
        out = []
        for row in rows:
            record = dict(row)
            try:
                record["features"] = json.loads(record.get("features") or "{}")
            except (json.JSONDecodeError, TypeError):
                record["features"] = {}
            out.append(record)
        return out

    # ---- events ----------------------------------------------------------------
    def record_event(
        self,
        kind: str,
        message: str,
        symbol: str = "",
        address: str = "",
        level: str = "info",
        detail: str = "",
        ts: Optional[float] = None,
    ) -> None:
        """Append one line to the activity feed the live view reads."""
        self.execute(
            """INSERT INTO events(ts, kind, level, symbol, address, message, detail)
               VALUES(?,?,?,?,?,?,?)""",
            (ts if ts is not None else time.time(), kind, level, symbol, address, message, detail),
        )
        self._commit()

    def list_events(self, limit: int = 200, since_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """Newest first, or everything after `since_id` for incremental polling."""
        if since_id is not None:
            return self._fetchall(
                "SELECT * FROM events WHERE id > ? ORDER BY id ASC LIMIT ?", (since_id, limit)
            )
        return self._fetchall("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))

    def prune_events(self, keep: int = 2000) -> None:
        """Keep the feed bounded - it is a rolling log, not an archive."""
        row = self._fetchone(
            "SELECT id FROM events ORDER BY id DESC LIMIT 1 OFFSET ?", (keep,)
        )
        if row:
            self.execute("DELETE FROM events WHERE id <= ?", (row["id"],))
            self._commit()

    # ---- price samples ---------------------------------------------------------
    def record_price_sample(self, address: str, price: float, ts: Optional[float] = None) -> None:
        """One observed price. Enough of these make a candle chart even when no
        external OHLC source is reachable."""
        if price <= 0:
            return
        self.execute(
            """INSERT INTO price_samples(address, ts, price) VALUES(?,?,?)
               ON CONFLICT(address, ts) DO UPDATE SET price = excluded.price""",
            (address, ts if ts is not None else time.time(), price),
        )
        self._commit()

    def price_samples(self, address: str, since: float = 0.0, limit: int = 5000) -> List[Dict[str, Any]]:
        return self._fetchall(
            "SELECT ts, price FROM price_samples WHERE address = ? AND ts >= ? "
            "ORDER BY ts ASC LIMIT ?",
            (address, since, limit),
        )

    def sampled_addresses(self, since: float = 0.0) -> List[str]:
        rows = self._fetchall(
            "SELECT DISTINCT address FROM price_samples WHERE ts >= ?", (since,)
        )
        return [row["address"] for row in rows]

    def prune_price_samples(self, older_than: float) -> None:
        self.execute("DELETE FROM price_samples WHERE ts < ?", (older_than,))
        self._commit()

    # ---- equity curve ----------------------------------------------------------
    def record_equity(self, cash: float, positions_value: float, ts: Optional[float] = None) -> None:
        ts = ts if ts is not None else time.time()
        self.execute(
            """INSERT INTO equity(ts, cash, positions, equity) VALUES(?,?,?,?)
               ON CONFLICT(ts) DO UPDATE SET
                    cash = excluded.cash,
                    positions = excluded.positions,
                    equity = excluded.equity""",
            (ts, cash, positions_value, cash + positions_value),
        )
        self._commit()

    def equity_curve(self, limit: int = 500) -> List[Dict[str, Any]]:
        return self._fetchall("SELECT * FROM equity ORDER BY ts DESC LIMIT ?", (limit,))

    def peak_equity(self) -> float:
        row = self._fetchone("SELECT MAX(equity) AS e FROM equity")
        return float((row or {}).get("e") or 0.0)

    def equity_at_or_before(self, ts: float) -> Optional[float]:
        row = self._fetchone(
            "SELECT equity FROM equity WHERE ts <= ? ORDER BY ts DESC LIMIT 1", (ts,)
        )
        return float(row["equity"]) if row else None

    def reset(self) -> None:
        for table in ("trades", "positions", "state", "equity", "events", "price_samples",
                      "trade_outcomes"):
            self.execute(f"DELETE FROM {table}")
        self._commit()


def open_storage(target: str = "data/memebot.sqlite3") -> Storage:
    """Open whichever backend `target` describes."""
    return Storage(target)


def resolve_state_target(configured: str) -> str:
    """Pick the state location, preferring an explicitly provided database URL.

    Hosted Postgres providers inject their own environment variable, so a
    deployment works with no config edit at all:

        MEMEBOT_STATE_DB > MEMEBOT_DATABASE_URL > POSTGRES_URL >
        DATABASE_URL > the configured value
    """
    for key in ("MEMEBOT_STATE_DB", "MEMEBOT_DATABASE_URL", "POSTGRES_URL", "DATABASE_URL"):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    return configured
