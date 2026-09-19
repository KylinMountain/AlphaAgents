"""Read-only source discovery for sector-first experiments.

This probe does not decide that a dataset is historically usable merely
because a table exists. It reports schema, time fields and coverage so a human
can grade the source before strict replay consumes it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from alpha_agents.config import DATA_DIR


_TIME_FIELDS = {
    "as_of", "captured_at", "effective_at", "effective_date", "date",
    "trade_date", "start_date", "end_date", "created_at", "updated_at",
}
_MEMBERSHIP_WORDS = ("concept", "industry", "sector", "theme")


def _tables(path: Path) -> list[str]:
    if not path.exists():
        return []
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    try:
        return [
            str(row[0]) for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' ORDER BY name").fetchall()
        ]
    finally:
        conn.close()


def _columns(path: Path, table: str) -> list[str]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    try:
        return [
            str(row[1]) for row in conn.execute(
                f"PRAGMA table_info('{table}')").fetchall()
        ]
    finally:
        conn.close()


def probe_membership_sources(data_dir: Path = DATA_DIR) -> list[dict]:
    path = data_dir / "stocks.db"
    if not path.exists():
        return [{
            "provider": "local_sqlite",
            "database": "stocks.db",
            "available": False,
            "status": "missing_file",
        }]

    out = []
    for table in _tables(path):
        if not any(word in table.lower() for word in _MEMBERSHIP_WORDS):
            continue
        columns = _columns(path, table)
        time_fields = [
            name for name in columns if name.lower() in _TIME_FIELDS
        ]
        grade = "U" if time_fields else "C"
        note = (
            "time-like fields exist; row semantics still need verification"
            if time_fields else
            "no as-of/effective field found; treat as current-only")
        out.append({
            "provider": "local_sqlite",
            "database": "stocks.db",
            "dataset": table,
            "available": True,
            "columns": columns,
            "time_fields": time_fields,
            "point_in_time_grade": grade,
            "strict_replay_eligible": False,
            "note": note,
        })
    return out


def _coverage(path: Path, table: str,
              candidates: tuple[str, ...]) -> dict:
    if not path.exists():
        return {"status": "missing_file"}
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,)).fetchone()
        if not exists:
            return {"status": "missing_table"}
        columns = {
            str(row[1]) for row in conn.execute(
                f"PRAGMA table_info('{table}')").fetchall()
        }
        field = next((name for name in candidates if name in columns), None)
        if field is None:
            return {
                "status": "ungraded",
                "columns": sorted(columns),
                "reason": "no recognized date/time field",
            }
        row = conn.execute(
            f"SELECT MIN({field}),MAX({field}),COUNT(DISTINCT {field}) "
            f"FROM {table}").fetchone()
        return {
            "status": "available",
            "time_field": field,
            "first": row[0],
            "last": row[1],
            "distinct_times": int(row[2] or 0),
            "columns": sorted(columns),
        }
    except sqlite3.Error as exc:
        return {"status": "unreadable", "error": str(exc)}
    finally:
        conn.close()


def probe_all(data_dir: Path = DATA_DIR) -> dict:
    return {
        "membership_sources": probe_membership_sources(data_dir),
        "daily_price": _coverage(
            data_dir / "market_history.db", "daily_kline", ("date",)),
        "fund_flow": _coverage(
            data_dir / "market_snapshots.db",
            "stock_fund_flow_daily",
            ("trade_date", "date", "captured_at")),
        "event_expectations": _coverage(
            data_dir / "market_snapshots.db",
            "event_expectation_snapshots",
            ("captured_at",)),
        "semantics": {
            "A": "verified historical vintages/effective membership",
            "B": "historical observations but revision semantics incomplete",
            "C": "current/latest snapshot only",
            "U": "unknown until semantics are verified",
        },
    }
