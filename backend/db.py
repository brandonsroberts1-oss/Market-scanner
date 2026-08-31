"""SQLite persistence.

Everything the app produces is saved: paper-trading sessions, every order and
fill, an equity-curve snapshot per mark, saved scans and backtest runs.  That
is the point of the persistence layer - you can come back tomorrow and see not
only what you traded but what the scanner was saying when you traded it.

Uses stdlib sqlite3 with WAL enabled.  A connection is created per operation
(cheap for SQLite) so the async server never shares a connection across tasks.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from .config import settings

_init_lock = threading.Lock()
_initialised = False

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS sessions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    starting_cash REAL NOT NULL,
    cash          REAL NOT NULL,
    status        TEXT NOT NULL DEFAULT 'active',
    created_at    TEXT NOT NULL,
    closed_at     TEXT,
    notes         TEXT
);

CREATE TABLE IF NOT EXISTS positions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    symbol       TEXT NOT NULL,
    asset_type   TEXT NOT NULL,            -- equity | option
    quantity     REAL NOT NULL,            -- negative means short
    avg_price    REAL NOT NULL,
    multiplier   REAL NOT NULL DEFAULT 1,
    underlying   TEXT,
    expiration   TEXT,
    strike       REAL,
    kind         TEXT,                     -- call | put
    group_id     TEXT,                     -- ties the legs of one spread together
    strategy     TEXT,
    opened_at    TEXT NOT NULL,
    UNIQUE(session_id, symbol)
);

CREATE TABLE IF NOT EXISTS orders (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    group_id      TEXT,
    symbol        TEXT NOT NULL,
    asset_type    TEXT NOT NULL,
    side          TEXT NOT NULL,           -- buy | sell
    quantity      REAL NOT NULL,
    order_type    TEXT NOT NULL DEFAULT 'market',
    limit_price   REAL,
    status        TEXT NOT NULL,           -- filled | rejected | cancelled
    fill_price    REAL,
    commission    REAL NOT NULL DEFAULT 0,
    realized_pnl  REAL NOT NULL DEFAULT 0,
    strategy      TEXT,
    note          TEXT,
    created_at    TEXT NOT NULL,
    filled_at     TEXT
);

CREATE TABLE IF NOT EXISTS snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    taken_at        TEXT NOT NULL,
    cash            REAL NOT NULL,
    positions_value REAL NOT NULL,
    total_equity    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS saved_scans (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL,
    label        TEXT,
    params       TEXT NOT NULL,
    result       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS backtests (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL,
    label        TEXT,
    params       TEXT NOT NULL,
    result       TEXT NOT NULL
);

-- Last-known-good market data. Only ever written from a successful live
-- fetch, so what is served from here is real data that has aged, never
-- anything invented.
CREATE TABLE IF NOT EXISTS market_cache (
    key        TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    symbol     TEXT,
    payload    TEXT NOT NULL,
    source     TEXT,
    fetched_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cache_kind    ON market_cache(kind, symbol);
CREATE INDEX IF NOT EXISTS idx_orders_session  ON orders(session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_positions_sess  ON positions(session_id);
CREATE INDEX IF NOT EXISTS idx_snapshots_sess  ON snapshots(session_id, taken_at);
"""


def db_path() -> Path:
    path = Path(settings.db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def init_db(force: bool = False) -> None:
    global _initialised
    with _init_lock:
        if _initialised and not force:
            return
        with sqlite3.connect(db_path()) as conn:
            conn.executescript(SCHEMA)
        _initialised = True


@contextmanager
def connect():
    """Yield a row-dict connection, committing on success."""
    init_db()
    conn = sqlite3.connect(db_path(), timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def rows_to_dicts(rows) -> list[dict]:
    return [dict(r) for r in rows]


def dumps(value) -> str:
    return json.dumps(value, default=str)


def loads(value: str):
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None
