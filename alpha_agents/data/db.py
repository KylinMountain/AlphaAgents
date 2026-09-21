import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS concepts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL DEFAULT 'ths',
    -- THS 概念的建立日期 (YYYY-MM-DD)。NULL = 早于已知最早日期，
    -- 一律视为"回放窗口开始前就存在"。见 concept_dates.py。
    created_date TEXT
);

CREATE TABLE IF NOT EXISTS stocks (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    market_cap REAL,
    industry TEXT,
    is_st INTEGER NOT NULL DEFAULT 0,
    is_suspended INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS concept_stocks (
    concept_id INTEGER NOT NULL,
    stock_code TEXT NOT NULL,
    PRIMARY KEY (concept_id, stock_code),
    FOREIGN KEY (concept_id) REFERENCES concepts(id),
    FOREIGN KEY (stock_code) REFERENCES stocks(code)
);

CREATE INDEX IF NOT EXISTS idx_concept_name ON concepts(name);
CREATE INDEX IF NOT EXISTS idx_stock_name ON stocks(name);

CREATE TABLE IF NOT EXISTS watchlist (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    concepts TEXT NOT NULL DEFAULT '[]',
    added_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
"""


def get_connection(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = get_connection(db_path)
    conn.executescript(_SCHEMA)
    _migrate(conn)
    conn.close()


#: ``(table, column, type)`` added after the table first shipped. CREATE TABLE
#: IF NOT EXISTS leaves an existing table alone, so a new column in _SCHEMA
#: never reaches a database that already has the table.
_ADDED_COLUMNS = (
    ("concepts", "created_date", "TEXT"),
)


def _migrate(conn) -> None:
    """Add columns that _SCHEMA gained after the table already existed."""
    for table, column, coltype in _ADDED_COLUMNS:
        have = {r[1] for r in conn.execute(
            "PRAGMA table_info(%s)" % table).fetchall()}
        if column not in have:
            conn.execute("ALTER TABLE %s ADD COLUMN %s %s"
                         % (table, column, coltype))
            logger.info("migrated %s: added column %s", table, column)
    conn.commit()
