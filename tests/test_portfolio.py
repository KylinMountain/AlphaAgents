"""Tests for daily_snapshots and virtual_portfolio tables."""
import sqlite3
import tempfile
from pathlib import Path


def _init_db(path: Path) -> sqlite3.Connection:
    """Initialize a test database with the memory_store schema."""
    import alpha_agents.data.memory_store as ms
    original = ms.MEMORY_DB_PATH
    ms.MEMORY_DB_PATH = path
    # Force new connection
    if hasattr(ms._local, "conn"):
        del ms._local.conn
    conn = ms._get_conn()
    ms.MEMORY_DB_PATH = original
    return conn


def test_daily_snapshots_table_exists():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "test_memory.db"
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        # Execute the schema from memory_store
        from alpha_agents.data.memory_store import _SCHEMA
        conn.executescript(_SCHEMA)
        # Verify table exists
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        assert "daily_snapshots" in tables


def test_virtual_portfolio_table_exists():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "test_memory.db"
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        from alpha_agents.data.memory_store import _SCHEMA
        conn.executescript(_SCHEMA)
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        assert "virtual_portfolio" in tables


def test_virtual_portfolio_columns():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "test_memory.db"
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        from alpha_agents.data.memory_store import _SCHEMA
        conn.executescript(_SCHEMA)
        row = conn.execute("PRAGMA table_info(virtual_portfolio)").fetchall()
        col_names = {r[1] for r in row}
        expected = {
            "id", "code", "name", "theme", "open_date", "open_price",
            "stop_loss", "target_price", "status", "close_date", "close_price",
            "holding_days", "return_pct", "peak_return_pct", "max_drawdown_pct",
            "source", "reason", "close_reason", "created_at",
        }
        assert expected.issubset(col_names)
