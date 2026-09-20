"""Measured data capabilities for one historical replay window.

A date range is not an information set. The same Trader can have daily bars in
2020, no intraday snapshots until much later, and a concept label that is only
known in its current form. This module makes those differences data instead of
a caveat a reader has to remember.

Coverage means "trading days in this requested window on which this source has
at least one row". It does not claim the row set is complete for every symbol.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

_SOURCE_CAPABILITIES = {
    "daily_price": ("market_history.db", "daily_kline", "date"),
    "news": ("market_snapshots.db", "news_items", "published_at"),
    "market_regime": (
        "market_snapshots.db", "market_breadth_snapshots", "captured_at"),
    "theme_state": (
        "market_snapshots.db", "limit_pool_snapshots", "captured_at"),
    "intraday_shape": (
        "market_snapshots.db", "all_quote_snapshots", "captured_at"),
    "fund_flow": (
        "market_snapshots.db", "stock_fund_flow_daily", "date"),
    "event_expectations": (
        "market_snapshots.db", "event_expectation_snapshots", "captured_at"),
}


def _observed_days(path: Path, table: str, field: str,
                   start: str, end: str) -> tuple[set[str], str]:
    if not path.exists():
        return set(), "missing_file"
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return set(), "unreadable"
    try:
        found = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,)).fetchone()
        if not found:
            return set(), "missing_table"
        # table and field come only from the constant map above.
        rows = conn.execute(
            f"SELECT DISTINCT substr({field},1,10) d FROM {table} "
            f"WHERE substr({field},1,10) BETWEEN ? AND ?",
            (start, end)).fetchall()
        return {str(row[0]) for row in rows if row[0]}, "available"
    except sqlite3.Error:
        return set(), "unreadable"
    finally:
        conn.close()


def build(window: list[str], data_dir: Path) -> dict:
    """Measure source-day coverage over exactly the replay's trading sessions."""
    if not window:
        return {"window_days": 0, "capabilities": {}}

    days = list(dict.fromkeys(str(day)[:10] for day in window))
    wanted = set(days)
    start, end = min(days), max(days)
    capabilities = {}

    for name, (filename, table, field) in _SOURCE_CAPABILITIES.items():
        observed, source_status = _observed_days(
            data_dir / filename, table, field, start, end)
        observed &= wanted
        n = len(observed)
        status = source_status
        if source_status == "available" and not observed:
            status = "no_rows_in_window"
        capabilities[name] = {
            "status": status,
            "point_in_time": True,
            "source": f"{filename}:{table}.{field}",
            "observed_days": n,
            "window_days": len(days),
            "coverage_pct": round(n / len(days) * 100, 1),
            "first_observed": min(observed) if observed else None,
            "last_observed": max(observed) if observed else None,
        }

    # The repository has no dated membership table. This is not 0% coverage:
    # zero would mean "we looked and saw no memberships". The historical fact
    # is unavailable, while the current label is deliberately shown in replay.
    capabilities["concept_membership"] = {
        "status": "current_only",
        "point_in_time": False,
        "source": "stocks.db:concept_stocks (no as-of column)",
        "observed_days": None,
        "window_days": len(days),
        "coverage_pct": None,
        "first_observed": None,
        "last_observed": None,
    }
    capabilities["trader_memory"] = {
        "status": "run_local",
        "point_in_time": True,
        "source": "replay memory.db",
        "observed_days": None,
        "window_days": len(days),
        "coverage_pct": None,
        "first_observed": days[0],
        "last_observed": days[-1],
    }

    return {
        "window": {"start": days[0], "end": days[-1]},
        "window_days": len(days),
        "coverage_semantics": (
            "percent of requested trading days with at least one source row; "
            "not per-symbol completeness"),
        "capabilities": capabilities,
    }


def summary_lines(matrix: dict) -> list[str]:
    """Human-readable lines for the walk-forward summary."""
    lines = ["", "— Replay Capability Matrix —"]
    for name, item in matrix.get("capabilities", {}).items():
        coverage = item.get("coverage_pct")
        if coverage is None:
            value = item["status"]
        else:
            value = (
                f"{coverage:>5.1f}% "
                f"({item['observed_days']}/{item['window_days']} days)")
        pit = "PIT" if item.get("point_in_time") else "非 PIT"
        lines.append(f"  {name:<20} {value:<22} {pit}")
    return lines
