import sqlite3
from pathlib import Path

import pytest

from alpha_agents.data.db import init_db, get_connection


@pytest.fixture
def tmp_db(tmp_path):
    db_path = tmp_path / "test_stocks.db"
    init_db(db_path)
    return db_path


def test_init_db_creates_tables(tmp_db):
    conn = get_connection(tmp_db)
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = [row[0] for row in cursor.fetchall()]
    conn.close()
    assert "concepts" in tables
    assert "stocks" in tables
    assert "concept_stocks" in tables


def test_init_db_concepts_schema(tmp_db):
    conn = get_connection(tmp_db)
    cursor = conn.execute("PRAGMA table_info(concepts)")
    columns = {row[1] for row in cursor.fetchall()}
    conn.close()
    assert columns == {"id", "name", "source", "created_date"}


def test_init_db_stocks_schema(tmp_db):
    conn = get_connection(tmp_db)
    cursor = conn.execute("PRAGMA table_info(stocks)")
    columns = {row[1] for row in cursor.fetchall()}
    conn.close()
    assert columns == {"code", "name", "market_cap", "industry", "is_st", "is_suspended"}


def test_init_db_concept_stocks_schema(tmp_db):
    conn = get_connection(tmp_db)
    cursor = conn.execute("PRAGMA table_info(concept_stocks)")
    columns = {row[1] for row in cursor.fetchall()}
    conn.close()
    assert columns == {"concept_id", "stock_code"}


def test_init_db_idempotent(tmp_db):
    """Calling init_db twice should not error."""
    init_db(tmp_db)
    conn = get_connection(tmp_db)
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = [row[0] for row in cursor.fetchall()]
    conn.close()
    assert "concepts" in tables


def test_migrate_adds_created_date_to_an_existing_concepts_table(tmp_path):
    """A database created before the column gains it on the next init_db.

    CREATE TABLE IF NOT EXISTS leaves an existing table alone, so without the
    migration a deployed database would never see a column added to _SCHEMA —
    and ``concept_dates`` would silently have nowhere to write.
    """
    import sqlite3
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        "CREATE TABLE concepts (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "name TEXT NOT NULL UNIQUE, source TEXT NOT NULL DEFAULT 'ths');"
        "INSERT INTO concepts (name) VALUES ('人工智能');"
    )
    conn.commit()
    conn.close()

    init_db(db_path)

    conn = get_connection(db_path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(concepts)")}
    rows = conn.execute("SELECT name, created_date FROM concepts").fetchall()
    conn.close()
    assert "created_date" in columns
    assert [tuple(r) for r in rows] == [("人工智能", None)]
