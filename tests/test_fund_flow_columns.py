"""The two places that read ``stock_fund_flow_daily`` must use its real columns.

Both were querying a ``date`` column the table has never had. Nothing failed
loudly: ``trader_tools._fund_flow_series`` caught ``sqlite3.Error`` and handed
the agent the string "资金流表不可用: no such column: date" as if the table
were missing, and ``replay_capabilities`` caught the same error and reported
the source as unreadable — which a replay report prints as 0% coverage, the
same as a source with no rows in the window.

These tests build the table from the shipped schema, so a future rename breaks
them here rather than silently downgrading a tool and a report.
"""

import sqlite3
from pathlib import Path

import pytest

from alpha_agents.data.snapshot_schema import _SCHEMA
from alpha_agents.evolution import replay_capabilities


def _snapshots_db(tmp_path: Path, rows: list[tuple]) -> Path:
    path = tmp_path / "market_snapshots.db"
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.executemany(
        "INSERT INTO stock_fund_flow_daily "
        "(trade_date, ts_code, code, net_amount, net_amount_rate) "
        "VALUES (?, ?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()
    return path


_ROWS = [
    ("20260105", "600519.SH", "600519", 12345.0, 1.5),
    ("20260106", "600519.SH", "600519", -6789.0, -0.8),
    ("20260107", "600519.SH", "600519", 500.0, None),
]


def test_fund_flow_series_reads_real_columns(tmp_path, monkeypatch):
    """The tool returns rows, not "no such column"."""
    from alpha_agents.tools import trader_tools
    path = _snapshots_db(tmp_path, _ROWS)
    monkeypatch.setattr(trader_tools, "_SNAPSHOTS", path)

    out = trader_tools._fund_flow_series("600519", "2026-01-07")

    assert out["available"] is True, out.get("reason")
    assert [r["date"] for r in out["recent"]] == [
        "2026-01-07", "2026-01-06", "2026-01-05"]
    # 万元 → 亿元
    assert out["recent"][1]["main_net_yi"] == pytest.approx(-0.6789)
    assert out["recent"][2]["main_net_pct"] == pytest.approx(1.5)


def test_fund_flow_series_honours_the_iso_cut_day(tmp_path, monkeypatch):
    """``cut_day`` arrives ISO; the table keys on YYYYMMDD.

    Without the conversion the string compare is wrong in both directions —
    '2026-01-06' <= '20260107' is false for every row, so a working table
    would read as empty.
    """
    from alpha_agents.tools import trader_tools
    path = _snapshots_db(tmp_path, _ROWS)
    monkeypatch.setattr(trader_tools, "_SNAPSHOTS", path)

    out = trader_tools._fund_flow_series("600519", "2026-01-06")

    assert [r["date"] for r in out["recent"]] == ["2026-01-06", "2026-01-05"]


def test_fund_flow_series_says_no_rows_rather_than_no_column(tmp_path, monkeypatch):
    """An empty table is reported as absent data, in the caller's own terms."""
    from alpha_agents.tools import trader_tools
    path = _snapshots_db(tmp_path, [])
    monkeypatch.setattr(trader_tools, "_SNAPSHOTS", path)

    out = trader_tools._fund_flow_series("600519", "2026-01-07")

    assert out["available"] is False
    assert "没有资金流记录" in out["reason"]
    assert "no such column" not in out["reason"]


def test_capability_probe_counts_fund_flow_days(tmp_path):
    """The probe sees a YYYYMMDD key as ISO trading days."""
    _snapshots_db(tmp_path, _ROWS)
    (tmp_path / "market_history.db").touch()

    out = replay_capabilities.build(
        ["2026-01-05", "2026-01-06", "2026-01-07"], tmp_path)
    flow = out["capabilities"]["fund_flow"]

    assert flow["status"] == "available"
    assert flow["observed_days"] == 3
    assert flow["coverage_pct"] == 100.0
    assert flow["first_observed"] == "2026-01-05"


def test_capability_probe_names_a_schema_error(tmp_path, monkeypatch):
    """A bad column in the map reads as "unreadable: ...", never as no rows.

    The two are different problems — one is our bug, the other is the window —
    and a report that renders both as 0% cannot tell a reader which it hit.
    """
    _snapshots_db(tmp_path, _ROWS)
    monkeypatch.setitem(
        replay_capabilities._SOURCE_CAPABILITIES, "fund_flow",
        ("market_snapshots.db", "stock_fund_flow_daily", "no_such_column"))

    out = replay_capabilities.build(["2026-01-05"], tmp_path)
    flow = out["capabilities"]["fund_flow"]

    assert flow["status"].startswith("unreadable:")
    assert "no_such_column" in flow["status"]
