"""Tests for daily data archiving."""
import json
from unittest.mock import patch


def test_save_and_get_snapshot():
    """Test that save_snapshot writes and get_snapshot reads correctly."""
    from alpha_agents.data.daily_archive import save_snapshot, get_snapshot

    with patch("alpha_agents.data.daily_archive._get_conn") as mock_conn:
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        from alpha_agents.data.memory_store import _SCHEMA
        conn.executescript(_SCHEMA)
        mock_conn.return_value = conn

        # Save
        data = {"gainers": [{"concept": "芯片", "change_pct": 3.5}]}
        save_snapshot("2026-04-09", "concept_fund_flow", data)

        # Read back
        result = get_snapshot("2026-04-09", "concept_fund_flow")
        assert result is not None
        assert result["gainers"][0]["concept"] == "芯片"


def test_save_snapshot_upserts():
    """Test that saving the same date+type overwrites."""
    from alpha_agents.data.daily_archive import save_snapshot, get_snapshot

    with patch("alpha_agents.data.daily_archive._get_conn") as mock_conn:
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        from alpha_agents.data.memory_store import _SCHEMA
        conn.executescript(_SCHEMA)
        mock_conn.return_value = conn

        save_snapshot("2026-04-09", "lhb", {"count": 10})
        save_snapshot("2026-04-09", "lhb", {"count": 20})

        result = get_snapshot("2026-04-09", "lhb")
        assert result["count"] == 20


def test_run_daily_archive_calls_all_sources(monkeypatch):
    """Test that run_daily_archive attempts to archive all data types."""
    from alpha_agents.data import daily_archive
    archived = []

    def fake_save(date, data_type, data):
        archived.append(data_type)

    monkeypatch.setattr(daily_archive, "save_snapshot", fake_save)
    monkeypatch.setattr(daily_archive, "_tushare_archive", lambda today: 0)

    # Mock current legacy JSON-blob data source functions to return valid JSON
    for fn_name in [
        "get_concept_ranking_fn", "get_sector_ranking_fn",
        "get_anomaly_stocks_fn", "get_market_breadth_fn", "get_block_trade_fn",
    ]:
        monkeypatch.setattr(
            daily_archive, fn_name,
            lambda *a, **kw: json.dumps({"data": []}),
        )

    daily_archive.run_daily_archive()

    expected_types = {
        "concept_fund_flow", "industry_fund_flow", "limit_up_pool",
        "market_breadth", "block_trade",
    }
    assert expected_types.issubset(set(archived))
