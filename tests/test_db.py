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
    assert columns == {"id", "name", "source"}


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
