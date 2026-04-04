import json
from unittest.mock import patch, MagicMock

from alpha_agents.tools.watchlist import (
    get_watchlist_fn, add_to_watchlist, remove_from_watchlist, list_watchlist,
)


def _mock_conn(rows=None):
    """Create a mock DB connection."""
    conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = rows or []
    mock_cursor.rowcount = 1
    conn.execute.return_value = mock_cursor
    return conn


@patch("alpha_agents.tools.watchlist.get_connection")
def test_get_watchlist(mock_get_conn):
    mock_get_conn.return_value = _mock_conn([
        {"code": "600519", "name": "贵州茅台", "concepts": '["白酒","消费"]', "added_at": "2026-01-01"},
    ])
    result = json.loads(get_watchlist_fn())
    assert len(result["stocks"]) == 1
    assert result["stocks"][0]["code"] == "600519"
    assert "白酒" in result["stocks"][0]["concepts"]


@patch("alpha_agents.tools.watchlist.get_connection")
def test_get_watchlist_empty(mock_get_conn):
    mock_get_conn.return_value = _mock_conn([])
    result = json.loads(get_watchlist_fn())
    assert len(result["stocks"]) == 0


@patch("alpha_agents.tools.watchlist.get_connection")
def test_add_to_watchlist(mock_get_conn):
    mock_get_conn.return_value = _mock_conn()
    result = add_to_watchlist("600519", "贵州茅台", ["白酒"])
    assert result["ok"] is True
    assert result["code"] == "600519"


@patch("alpha_agents.tools.watchlist.get_connection")
def test_remove_from_watchlist(mock_get_conn):
    mock_get_conn.return_value = _mock_conn()
    result = remove_from_watchlist("600519")
    assert result["ok"] is True


@patch("alpha_agents.tools.watchlist.get_connection")
def test_list_watchlist(mock_get_conn):
    mock_get_conn.return_value = _mock_conn([
        {"code": "600519", "name": "贵州茅台", "concepts": '["白酒"]', "added_at": "2026-01-01"},
        {"code": "000858", "name": "五粮液", "concepts": '["白酒"]', "added_at": "2026-01-02"},
    ])
    result = list_watchlist()
    assert len(result) == 2
    assert result[1]["name"] == "五粮液"
