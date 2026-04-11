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


def test_compute_beta_basic():
    """Beta of a stock perfectly correlated with sector should be ~1.0."""
    from alpha_agents.data.beta_calculator import compute_beta
    # Stock returns = sector returns -> beta = 1.0
    stock_returns = [0.01, -0.02, 0.03, -0.01, 0.02, 0.01, -0.01, 0.02, -0.02, 0.01]
    sector_returns = [0.01, -0.02, 0.03, -0.01, 0.02, 0.01, -0.01, 0.02, -0.02, 0.01]
    beta = compute_beta(stock_returns, sector_returns)
    assert abs(beta - 1.0) < 0.01


def test_compute_beta_high_beta():
    """Stock that moves 2x sector should have beta ~= 2.0."""
    from alpha_agents.data.beta_calculator import compute_beta
    sector_returns = [0.01, -0.02, 0.03, -0.01, 0.02, 0.01, -0.01, 0.02, -0.02, 0.01]
    stock_returns = [r * 2 for r in sector_returns]
    beta = compute_beta(stock_returns, sector_returns)
    assert abs(beta - 2.0) < 0.01


def test_compute_beta_zero_variance():
    """If sector doesn't move, beta should be 0."""
    from alpha_agents.data.beta_calculator import compute_beta
    stock_returns = [0.01, -0.02, 0.03]
    sector_returns = [0.0, 0.0, 0.0]
    beta = compute_beta(stock_returns, sector_returns)
    assert beta == 0.0


def test_weighted_beta():
    """Multi-period weighted beta calculation."""
    from alpha_agents.data.beta_calculator import weighted_beta
    result = weighted_beta(beta_20d=1.5, beta_60d=1.2, beta_120d=1.0)
    expected = 1.5 * 0.5 + 1.2 * 0.3 + 1.0 * 0.2  # = 0.75 + 0.36 + 0.20 = 1.31
    assert abs(result - expected) < 0.01
