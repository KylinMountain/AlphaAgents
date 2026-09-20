"""Immutable forward market labels for every offered opportunity.

These are not simulated fills or P&L. They put every name in one opportunity
set on the same price convention so ranking and selection can be evaluated
without pretending the account actually traded every branch.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

from alpha_agents.config import DATA_DIR
from alpha_agents.data import clock, memory_store, opportunity_journal

LABEL_VERSION = "forward_close_v1"
HORIZONS = (1, 3, 5)
_HISTORY = DATA_DIR / "market_history.db"

_TABLE = """
CREATE TABLE IF NOT EXISTS opportunity_outcomes (
    id INTEGER PRIMARY KEY,
    opportunity_item_id INTEGER NOT NULL,
    opportunity_set_id INTEGER NOT NULL,
    code TEXT NOT NULL,
    label_version TEXT NOT NULL,
    outcome_json TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(opportunity_item_id) REFERENCES opportunity_items(id),
    FOREIGN KEY(opportunity_set_id) REFERENCES opportunity_sets(id),
    UNIQUE(opportunity_item_id, label_version)
)
"""

_GUARDS = (
    "CREATE TRIGGER IF NOT EXISTS opportunity_outcomes_no_update "
    "BEFORE UPDATE ON opportunity_outcomes BEGIN "
    "SELECT RAISE(ABORT, 'opportunity_outcomes is append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS opportunity_outcomes_no_delete "
    "BEFORE DELETE ON opportunity_outcomes BEGIN "
    "SELECT RAISE(ABORT, 'opportunity_outcomes is append-only'); END",
)


def init_schema(conn: sqlite3.Connection) -> None:
    opportunity_journal.init_schema(conn)
    conn.execute(_TABLE)
    for statement in _GUARDS:
        conn.execute(statement)


def _dump(value) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False)


def _hash(value) -> str:
    return hashlib.sha256(_dump(value).encode("utf-8")).hexdigest()


def _history_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{_HISTORY}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def compute(*, code: str, day: str, phase: str,
            history_conn: sqlite3.Connection,
            horizons: tuple[int, ...] = HORIZONS) -> dict | None:
    """Return a fully matured market outcome, or None when not ripe."""
    if phase not in {"open", "close"}:
        raise ValueError(f"unknown phase {phase!r}")
    if not horizons or min(horizons) <= 0:
        raise ValueError("horizons must be positive")
    max_h = max(horizons)
    rows = history_conn.execute(
        "SELECT date, open, high, low, close FROM daily_kline "
        "WHERE code=? AND date>=? ORDER BY date LIMIT ?",
        (code, day, max_h + 1)).fetchall()
    if len(rows) < max_h + 1 or str(rows[0]["date"]) != str(day)[:10]:
        return None

    entry = rows[0]["open"] if phase == "open" else rows[0]["close"]
    if entry is None or float(entry) <= 0:
        return None
    entry = float(entry)

    returns = {}
    target_dates = {}
    for horizon in horizons:
        target = rows[horizon]["close"]
        if target is None:
            return None
        returns[str(horizon)] = round((float(target) / entry - 1) * 100, 4)
        target_dates[str(horizon)] = str(rows[horizon]["date"])

    future = rows[1:max_h + 1]
    highs = [float(r["high"]) for r in future if r["high"] is not None]
    lows = [float(r["low"]) for r in future if r["low"] is not None]
    if not highs or not lows:
        return None

    source = {
        "code": code,
        "day": str(day)[:10],
        "phase": phase,
        "entry_price": entry,
        "bars": [dict(r) for r in rows],
    }
    return {
        "label_version": LABEL_VERSION,
        "entry_date": str(rows[0]["date"]),
        "entry_phase": phase,
        "entry_price": entry,
        "forward_close_return_pct": returns,
        "target_dates": target_dates,
        "mfe_5d_pct": round((max(highs) / entry - 1) * 100, 4),
        "mae_5d_pct": round((min(lows) / entry - 1) * 100, 4),
        "matured_through": str(rows[max_h]["date"]),
        "source_hash": _hash(source),
    }


def sweep(*, conn: sqlite3.Connection | None = None,
          history_conn: sqlite3.Connection | None = None,
          limit: int = 5000) -> dict:
    """Label every fully matured opportunity once; leave young rows pending."""
    own_history = history_conn is None
    history = history_conn or _history_conn()
    target = conn if conn is not None else memory_store._get_conn()
    init_schema(target)

    rows = target.execute(
        "SELECT i.id item_id, i.opportunity_set_id, i.code, "
        "s.day, s.phase FROM opportunity_items i "
        "JOIN opportunity_sets s ON s.id=i.opportunity_set_id "
        "LEFT JOIN opportunity_outcomes o "
        "ON o.opportunity_item_id=i.id AND o.label_version=? "
        "WHERE o.id IS NULL ORDER BY s.day, i.id LIMIT ?",
        (LABEL_VERSION, int(limit))).fetchall()

    written = pending = 0
    try:
        with target:
            for row in rows:
                outcome = compute(
                    code=row["code"], day=row["day"], phase=row["phase"],
                    history_conn=history)
                if outcome is None:
                    pending += 1
                    continue
                target.execute(
                    "INSERT INTO opportunity_outcomes "
                    "(opportunity_item_id, opportunity_set_id, code, "
                    " label_version, outcome_json, source_hash, created_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (row["item_id"], row["opportunity_set_id"], row["code"],
                     LABEL_VERSION, _dump(outcome),
                     outcome["source_hash"], clock.today()))
                written += 1
    finally:
        if own_history:
            history.close()

    return {
        "label_version": LABEL_VERSION,
        "eligible_unlabeled": len(rows),
        "written": written,
        "pending": pending,
    }


def coverage(conn: sqlite3.Connection | None = None) -> dict:
    conn = conn if conn is not None else memory_store._get_conn()
    init_schema(conn)
    total = conn.execute(
        "SELECT COUNT(*) n FROM opportunity_items").fetchone()["n"]
    labeled = conn.execute(
        "SELECT COUNT(*) n FROM opportunity_outcomes "
        "WHERE label_version=?", (LABEL_VERSION,)).fetchone()["n"]
    return {
        "label_version": LABEL_VERSION,
        "items": int(total),
        "labeled": int(labeled),
        "pending": int(total - labeled),
        "coverage": (round(labeled / total, 4) if total else None),
    }
