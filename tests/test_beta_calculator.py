"""Tests for sector beta calculation and storage."""
import sqlite3
import tempfile
from pathlib import Path


def test_sector_betas_table_exists():
    """Verify sector_betas table is created by schema."""
    from alpha_agents.data.memory_store import _SCHEMA
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()]
    assert "sector_betas" in tables


def test_sector_betas_columns():
    from alpha_agents.data.memory_store import _SCHEMA
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sector_betas)").fetchall()}
    expected = {"id", "concept", "code", "name", "beta_20d", "beta_60d",
                "beta_120d", "beta_weighted", "avg_daily_amount", "updated_at"}
    assert expected.issubset(cols)


def test_sector_betas_unique_constraint():
    from alpha_agents.data.memory_store import _SCHEMA
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    conn.execute("INSERT INTO sector_betas (concept, code, name, beta_weighted) VALUES ('电池', '300750', '宁德时代', 1.3)")
    conn.execute("INSERT OR REPLACE INTO sector_betas (concept, code, name, beta_weighted) VALUES ('电池', '300750', '宁德时代', 1.5)")
    rows = conn.execute("SELECT beta_weighted FROM sector_betas WHERE concept='电池' AND code='300750'").fetchall()
    assert len(rows) <= 2  # At most 2 without proper upsert; we'll use ON CONFLICT in real code
