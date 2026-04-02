import sqlite3
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS concepts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL DEFAULT 'ths'
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
    conn.close()
