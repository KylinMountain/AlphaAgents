"""Tests for daily_snapshots and virtual_portfolio tables."""
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch


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


# ── Portfolio CRUD Tests ────────────────────────────────────


def _make_test_conn():
    """Create an in-memory DB with schema for testing."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    from alpha_agents.data.memory_store import _SCHEMA
    conn.executescript(_SCHEMA)
    return conn


def test_open_position():
    from alpha_agents.data.portfolio import open_position, get_open_positions

    with patch("alpha_agents.data.portfolio._get_conn") as mock:
        mock.return_value = _make_test_conn()
        pos_id = open_position(
            code="300475", name="香农芯创", theme="芯片概念",
            open_date="2026-04-09", open_price=147.11,
            stop_loss=135.0, target_price=165.0,
            source="morning", reason="机构看多",
        )
        assert pos_id > 0
        positions = get_open_positions()
        assert len(positions) == 1
        assert positions[0]["code"] == "300475"
        assert positions[0]["status"] == "open"


def test_no_duplicate_open_position():
    from alpha_agents.data.portfolio import open_position, get_open_positions

    with patch("alpha_agents.data.portfolio._get_conn") as mock:
        mock.return_value = _make_test_conn()
        open_position(code="300475", name="香农芯创", theme="芯片",
                      open_date="2026-04-09", open_price=147.0,
                      source="morning", reason="test")
        # Second open for same code should be skipped
        pos_id = open_position(code="300475", name="香农芯创", theme="芯片",
                               open_date="2026-04-09", open_price=150.0,
                               source="intraday", reason="test2")
        assert pos_id is None
        assert len(get_open_positions()) == 1


def test_close_position():
    from alpha_agents.data.portfolio import open_position, close_position, get_open_positions

    with patch("alpha_agents.data.portfolio._get_conn") as mock:
        mock.return_value = _make_test_conn()
        pos_id = open_position(code="300475", name="香农芯创", theme="芯片",
                               open_date="2026-04-08", open_price=140.0,
                               source="morning", reason="test")
        close_position(pos_id, close_price=130.0, close_reason="止损触发")
        positions = get_open_positions()
        assert len(positions) == 0


def test_check_positions_stop_loss():
    """check_positions moved to position_monitor; both names need the conn."""
    from alpha_agents.data.portfolio import open_position, check_positions

    conn = _make_test_conn()
    with patch("alpha_agents.data.portfolio._get_conn") as mock, \
         patch("alpha_agents.data.position_monitor._get_conn") as mock_m:
        mock.return_value = conn
        mock_m.return_value = conn
        open_position(code="300475", name="香农芯创", theme="芯片",
                      open_date="2026-04-08", open_price=147.0,
                      stop_loss=135.0, source="morning", reason="test")
        # Simulate price below stop loss
        alerts = check_positions(
            realtime_prices={"300475": 130.0},
            today="2026-04-09",
        )
        assert len(alerts) == 1
        assert alerts[0]["type"] == "stopped"
        assert alerts[0]["code"] == "300475"


def test_check_positions_t1_skip():
    """T+1: positions opened today should NOT be checked."""
    from alpha_agents.data.portfolio import open_position, check_positions

    conn = _make_test_conn()

    with patch("alpha_agents.data.portfolio._get_conn") as mock, \
         patch("alpha_agents.data.position_monitor._get_conn") as mock_m:
        mock.return_value = conn
        mock_m.return_value = conn
        open_position(code="300475", name="香农芯创", theme="芯片",
                      open_date="2026-04-09", open_price=147.0,
                      stop_loss=135.0, source="morning", reason="test")
        # Same day — should skip even if price is below stop
        alerts = check_positions(
            realtime_prices={"300475": 100.0},
            today="2026-04-09",
        )
        assert len(alerts) == 0


def test_get_portfolio_stats():
    from alpha_agents.data.portfolio import open_position, close_position, get_portfolio_stats

    # Writes go through portfolio, the read goes through portfolio_report
    # since the split — both bind _get_conn from memory_store, so both
    # names need the same test connection.
    conn = _make_test_conn()
    with patch("alpha_agents.data.portfolio._get_conn") as mock, \
         patch("alpha_agents.data.portfolio_report._get_conn") as mock_r:
        mock.return_value = conn
        mock_r.return_value = conn
        id1 = open_position(code="300475", name="A", theme="芯片",
                            open_date="2026-04-07", open_price=100.0,
                            source="morning", reason="t")
        close_position(id1, close_price=110.0, close_reason="止盈触发")
        id2 = open_position(code="000586", name="B", theme="通信",
                            open_date="2026-04-07", open_price=100.0,
                            source="morning", reason="t")
        close_position(id2, close_price=93.0, close_reason="止损触发")

        stats = get_portfolio_stats(days=7)
        assert stats["total_closed"] == 2
        assert stats["wins"] == 1
        assert stats["losses"] == 1
        assert stats["win_rate"] == 50.0
