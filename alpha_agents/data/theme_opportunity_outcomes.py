"""Immutable forward labels for direction-level opportunities.

These labels describe what the whole sector did after a recorded 09:00/close
decision. They are not simulated portfolio P&L and they never infer that a
single member represents its sector.

Membership is resolved from the exact snapshot id/hash frozen in the direction
journal. That prevents today's concept membership from rewriting yesterday's
Dream world.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import statistics

from alpha_agents.config import DATA_DIR
from alpha_agents.data import (
    clock,
    memory_store,
    sector_membership,
    theme_opportunity_journal,
)

LABEL_VERSION = "theme_forward_median_v1"
HORIZONS = (1, 3, 5, 10)
_HISTORY = DATA_DIR / "market_history.db"

_TABLE = """
CREATE TABLE IF NOT EXISTS theme_opportunity_outcomes (
    id INTEGER PRIMARY KEY,
    theme_opportunity_item_id INTEGER NOT NULL,
    theme_opportunity_set_id INTEGER NOT NULL,
    sector_id TEXT NOT NULL,
    label_version TEXT NOT NULL,
    outcome_json TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(theme_opportunity_item_id)
        REFERENCES theme_opportunity_items(id),
    FOREIGN KEY(theme_opportunity_set_id)
        REFERENCES theme_opportunity_sets(id),
    UNIQUE(theme_opportunity_item_id, label_version)
)
"""

_GUARDS = (
    "CREATE TRIGGER IF NOT EXISTS theme_opportunity_outcomes_no_update "
    "BEFORE UPDATE ON theme_opportunity_outcomes BEGIN "
    "SELECT RAISE(ABORT, 'theme_opportunity_outcomes is append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS theme_opportunity_outcomes_no_delete "
    "BEFORE DELETE ON theme_opportunity_outcomes BEGIN "
    "SELECT RAISE(ABORT, 'theme_opportunity_outcomes is append-only'); END",
)


def init_schema(conn: sqlite3.Connection) -> None:
    theme_opportunity_journal.init_schema(conn)
    conn.execute(_TABLE)
    for statement in _GUARDS:
        conn.execute(statement)


def _dump(value) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False)


def _hash(value) -> str:
    return hashlib.sha256(_dump(value).encode("utf-8")).hexdigest()


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    return round(float(statistics.median(values)), 4)


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 4)


def _history_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{_HISTORY}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _market_dates(history_conn: sqlite3.Connection, day: str,
                  count: int) -> list[str]:
    rows = history_conn.execute(
        "SELECT DISTINCT date FROM daily_kline "
        "WHERE date>=? ORDER BY date LIMIT ?",
        (str(day)[:10], int(count)),
    ).fetchall()
    return [str(row["date"]) for row in rows]


def compute(*, sector_id: str, membership_snapshot,
            day: str, phase: str, history_conn: sqlite3.Connection,
            horizons: tuple[int, ...] = HORIZONS) -> dict | None:
    """Return one matured sector label, or None while the market window is young."""
    if phase not in {"open", "close"}:
        raise ValueError(f"unknown phase {phase!r}")
    if not horizons or min(horizons) <= 0:
        raise ValueError("horizons must be positive")
    members = tuple(sorted(set(
        membership_snapshot.members.get(sector_id, ()))))
    if not members:
        raise ValueError(
            f"sector {sector_id!r} is absent from membership snapshot "
            f"{membership_snapshot.snapshot_id!r}")

    max_h = max(horizons)
    dates = _market_dates(history_conn, day, max_h + 1)
    if len(dates) < max_h + 1 or dates[0] != str(day)[:10]:
        return None

    returns: dict[str, list[float]] = {
        str(horizon): [] for horizon in horizons}
    observations = {}
    entry_field = "open" if phase == "open" else "close"

    for code in members:
        entry_row = history_conn.execute(
            f"SELECT {entry_field} entry FROM daily_kline "
            "WHERE code=? AND date=?",
            (code, dates[0]),
        ).fetchone()
        entry = (
            float(entry_row["entry"])
            if entry_row is not None and entry_row["entry"] is not None
            else None
        )
        if entry is not None and entry <= 0:
            entry = None

        close_rows = history_conn.execute(
            "SELECT date, close FROM daily_kline "
            "WHERE code=? AND date BETWEEN ? AND ?",
            (code, dates[0], dates[-1]),
        ).fetchall()
        closes = {
            str(row["date"]): (
                float(row["close"]) if row["close"] is not None else None)
            for row in close_rows
        }
        observations[code] = {
            "entry": entry,
            "target_closes": {
                str(h): closes.get(dates[h]) for h in horizons},
        }
        if entry is None:
            continue
        for horizon in horizons:
            target = closes.get(dates[horizon])
            if target is None:
                continue
            returns[str(horizon)].append(
                (float(target) / entry - 1.0) * 100.0)

    medians = {}
    means = {}
    positive = {}
    ex_top1 = {}
    coverage = {}
    for horizon in horizons:
        key = str(horizon)
        values = returns[key]
        medians[key] = _median(values)
        means[key] = _mean(values)
        positive[key] = (
            round(100.0 * sum(value > 0 for value in values) / len(values), 4)
            if values else None)
        ordered = sorted(values, reverse=True)
        ex_top1[key] = _median(ordered[1:]) if len(ordered) > 1 else None
        coverage[key] = {
            "covered": len(values),
            "members": len(members),
            "ratio": round(len(values) / len(members), 4),
        }

    source = {
        "sector_id": sector_id,
        "membership_snapshot_id": membership_snapshot.snapshot_id,
        "membership_hash": membership_snapshot.content_hash,
        "day": dates[0],
        "phase": phase,
        "target_dates": {str(h): dates[h] for h in horizons},
        "observations": observations,
    }
    return {
        "label_version": LABEL_VERSION,
        "sector_id": sector_id,
        "membership_snapshot_id": membership_snapshot.snapshot_id,
        "membership_hash": membership_snapshot.content_hash,
        "entry_date": dates[0],
        "entry_phase": phase,
        "member_count": len(members),
        "forward_median_return_pct": medians,
        "forward_mean_return_pct": means,
        "positive_member_pct": positive,
        "ex_top1_forward_median_return_pct": ex_top1,
        "coverage": coverage,
        "target_dates": {str(h): dates[h] for h in horizons},
        "matured_through": dates[max_h],
        "source_hash": _hash(source),
    }


def sweep(*, membership_archive,
          conn: sqlite3.Connection | None = None,
          history_conn: sqlite3.Connection | None = None,
          limit: int = 5000) -> dict:
    """Label every fully matured direction once; leave young sets pending."""
    own_history = history_conn is None
    history = history_conn or _history_conn()
    target = conn if conn is not None else memory_store._get_conn()
    init_schema(target)

    rows = target.execute(
        "SELECT i.id item_id, i.theme_opportunity_set_id set_id, "
        "i.sector_id, i.snapshot_json, s.day, s.phase "
        "FROM theme_opportunity_items i "
        "JOIN theme_opportunity_sets s "
        "ON s.id=i.theme_opportunity_set_id "
        "LEFT JOIN theme_opportunity_outcomes o "
        "ON o.theme_opportunity_item_id=i.id AND o.label_version=? "
        "WHERE o.id IS NULL ORDER BY s.day, i.id LIMIT ?",
        (LABEL_VERSION, int(limit)),
    ).fetchall()

    written = pending = 0
    try:
        with target:
            for row in rows:
                frozen = json.loads(row["snapshot_json"])
                snapshot_id = str(
                    frozen.get("membership_snapshot_id") or "").strip()
                membership_hash = str(
                    frozen.get("membership_hash") or "").strip()
                if not snapshot_id or not membership_hash:
                    raise ValueError(
                        "direction journal item lacks frozen membership id/hash")
                membership = sector_membership.by_id(
                    membership_archive, snapshot_id, membership_hash)
                outcome = compute(
                    sector_id=str(row["sector_id"]),
                    membership_snapshot=membership,
                    day=str(row["day"]),
                    phase=str(row["phase"]),
                    history_conn=history,
                )
                if outcome is None:
                    pending += 1
                    continue
                target.execute(
                    "INSERT INTO theme_opportunity_outcomes "
                    "(theme_opportunity_item_id,theme_opportunity_set_id,"
                    "sector_id,label_version,outcome_json,source_hash,created_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (row["item_id"], row["set_id"], row["sector_id"],
                     LABEL_VERSION, _dump(outcome),
                     outcome["source_hash"], clock.today()),
                )
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
        "SELECT COUNT(*) n FROM theme_opportunity_items").fetchone()["n"]
    labeled = conn.execute(
        "SELECT COUNT(*) n FROM theme_opportunity_outcomes "
        "WHERE label_version=?", (LABEL_VERSION,)).fetchone()["n"]
    return {
        "label_version": LABEL_VERSION,
        "items": int(total),
        "labeled": int(labeled),
        "pending": int(total - labeled),
        "coverage": round(labeled / total, 4) if total else None,
    }
