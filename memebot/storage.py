"""SQLite persistence so the bot can be stopped and resumed without losing state."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any, Dict, List, Optional

from .models import Fill, Position, Token

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL    NOT NULL,
    client_id     TEXT,
    side          TEXT    NOT NULL,
    token_address TEXT    NOT NULL,
    symbol        TEXT,
    price         REAL    NOT NULL,
    token_amount  REAL    NOT NULL,
    usd_amount    REAL    NOT NULL,
    fee_usd       REAL    NOT NULL DEFAULT 0,
    slippage_bps  REAL    NOT NULL DEFAULT 0,
    realized_pnl  REAL    NOT NULL DEFAULT 0,
    tx_signature  TEXT,
    reason        TEXT,
    mode          TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts);
CREATE INDEX IF NOT EXISTS idx_trades_token ON trades(token_address);

CREATE TABLE IF NOT EXISTS positions (
    token_address       TEXT PRIMARY KEY,
    symbol              TEXT,
    name                TEXT,
    decimals            INTEGER DEFAULT 9,
    pair_address        TEXT,
    quantity            REAL NOT NULL,
    avg_price           REAL NOT NULL,
    cost_usd            REAL NOT NULL,
    opened_at           REAL NOT NULL,
    last_price          REAL NOT NULL DEFAULT 0,
    high_price          REAL NOT NULL DEFAULT 0,
    fees_usd            REAL NOT NULL DEFAULT 0,
    realized_pnl_usd    REAL NOT NULL DEFAULT 0,
    peak_liquidity_usd  REAL NOT NULL DEFAULT 0,
    last_seen_at        REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS equity (
    ts        REAL PRIMARY KEY,
    cash      REAL NOT NULL,
    positions REAL NOT NULL,
    equity    REAL NOT NULL
);
"""


class Storage:
    """Thin SQLite wrapper. Use `:memory:` for tests."""

    def __init__(self, path: str = "data/memebot.sqlite3") -> None:
        self.path = path
        if path != ":memory:":
            directory = os.path.dirname(path)
            if directory:
                os.makedirs(directory, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ---- key/value state -------------------------------------------------------
    def get_state(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            return default

    def set_state(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT INTO state(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )
        self.conn.commit()

    # ---- positions -------------------------------------------------------------
    def save_position(self, position: Position) -> None:
        self.conn.execute(
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
        self.conn.commit()

    def delete_position(self, token_address: str) -> None:
        self.conn.execute("DELETE FROM positions WHERE token_address = ?", (token_address,))
        self.conn.commit()

    def load_positions(self) -> Dict[str, Position]:
        rows = self.conn.execute("SELECT * FROM positions").fetchall()
        out: Dict[str, Position] = {}
        for row in rows:
            out[row["token_address"]] = Position(
                token=Token(
                    address=row["token_address"],
                    symbol=row["symbol"] or "",
                    name=row["name"] or "",
                    decimals=row["decimals"] or 9,
                ),
                quantity=row["quantity"],
                avg_price=row["avg_price"],
                cost_usd=row["cost_usd"],
                opened_at=row["opened_at"],
                pair_address=row["pair_address"] or "",
                last_price=row["last_price"],
                high_price=row["high_price"],
                fees_usd=row["fees_usd"],
                realized_pnl_usd=row["realized_pnl_usd"],
                peak_liquidity_usd=row["peak_liquidity_usd"],
                last_seen_at=row["last_seen_at"],
            )
        return out

    # ---- trades ----------------------------------------------------------------
    def record_fill(self, fill: Fill, realized_pnl: float = 0.0, mode: str = "paper") -> None:
        row = fill.as_row()
        self.conn.execute(
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
        self.conn.commit()

    def list_trades(self, limit: int = 50, since: Optional[float] = None) -> List[sqlite3.Row]:
        if since is not None:
            return self.conn.execute(
                "SELECT * FROM trades WHERE ts >= ? ORDER BY ts DESC LIMIT ?", (since, limit)
            ).fetchall()
        return self.conn.execute("SELECT * FROM trades ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()

    def realized_pnl_since(self, since: float) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(realized_pnl), 0) AS pnl FROM trades WHERE ts >= ?", (since,)
        ).fetchone()
        return float(row["pnl"] or 0.0)

    def last_exit_time(self, token_address: str) -> float:
        row = self.conn.execute(
            "SELECT MAX(ts) AS ts FROM trades WHERE token_address = ? AND side = 'sell'",
            (token_address,),
        ).fetchone()
        return float(row["ts"] or 0.0)

    def trade_stats(self) -> Dict[str, Any]:
        row = self.conn.execute(
            """SELECT COUNT(*) AS n,
                      COALESCE(SUM(realized_pnl), 0) AS pnl,
                      COALESCE(SUM(fee_usd), 0) AS fees
               FROM trades WHERE side = 'sell'"""
        ).fetchone()
        wins = self.conn.execute(
            "SELECT COUNT(*) AS n FROM trades WHERE side = 'sell' AND realized_pnl > 0"
        ).fetchone()["n"]
        total_fees = self.conn.execute("SELECT COALESCE(SUM(fee_usd), 0) AS f FROM trades").fetchone()["f"]
        closed = int(row["n"] or 0)
        return {
            "closed_trades": closed,
            "wins": int(wins or 0),
            "losses": closed - int(wins or 0),
            "win_rate": (wins / closed) if closed else 0.0,
            "realized_pnl_usd": float(row["pnl"] or 0.0),
            "total_fees_usd": float(total_fees or 0.0),
        }

    # ---- equity curve ----------------------------------------------------------
    def record_equity(self, cash: float, positions_value: float, ts: Optional[float] = None) -> None:
        ts = ts if ts is not None else time.time()
        self.conn.execute(
            "INSERT OR REPLACE INTO equity(ts, cash, positions, equity) VALUES(?,?,?,?)",
            (ts, cash, positions_value, cash + positions_value),
        )
        self.conn.commit()

    def equity_curve(self, limit: int = 500) -> List[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM equity ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()

    def peak_equity(self) -> float:
        row = self.conn.execute("SELECT MAX(equity) AS e FROM equity").fetchone()
        return float(row["e"] or 0.0)

    def equity_at_or_before(self, ts: float) -> Optional[float]:
        row = self.conn.execute(
            "SELECT equity FROM equity WHERE ts <= ? ORDER BY ts DESC LIMIT 1", (ts,)
        ).fetchone()
        return float(row["equity"]) if row else None

    def reset(self) -> None:
        self.conn.executescript(
            "DELETE FROM trades; DELETE FROM positions; DELETE FROM state; DELETE FROM equity;"
        )
        self.conn.commit()
