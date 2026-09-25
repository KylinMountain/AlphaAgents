"""Trader Runtime review records and quarantined lesson candidates.

Review is allowed to interpret a decision. It is not allowed to create an
active trading rule. Every candidate retains the decision/timeframe/evidence
scope that produced it so a daily replay experience cannot silently become an
intraday rule.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3

from alpha_agents.data import memory_store

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trader_decision_reviews (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL,
    trader_id TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    review_date TEXT NOT NULL,
    action TEXT NOT NULL,
    code TEXT,
    decision_quality TEXT NOT NULL,
    execution_quality TEXT NOT NULL,
    outcome_quality TEXT NOT NULL,
    reason TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(run_id,trader_id,decision_id)
);
CREATE TABLE IF NOT EXISTS trader_lesson_candidates (
    id INTEGER PRIMARY KEY,
    fingerprint TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL,
    trader_id TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    source_date TEXT NOT NULL,
    claim TEXT NOT NULL,
    action TEXT NOT NULL,
    applicable_context TEXT NOT NULL,
    evidence_timeframe TEXT NOT NULL,
    decision_horizon TEXT NOT NULL,
    evidence_scope TEXT NOT NULL,
    support_count INTEGER NOT NULL DEFAULT 1,
    counterexample_count INTEGER NOT NULL DEFAULT 0,
    confidence REAL NOT NULL DEFAULT 0.5,
    evidence_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_trader_decision_reviews_day
  ON trader_decision_reviews(run_id,trader_id,review_date);
CREATE INDEX IF NOT EXISTS idx_trader_lesson_candidates_day
  ON trader_lesson_candidates(run_id,trader_id,source_date);
CREATE TRIGGER IF NOT EXISTS trader_decision_reviews_no_update
BEFORE UPDATE ON trader_decision_reviews BEGIN
  SELECT RAISE(ABORT,'trader_decision_reviews is append-only');
END;
CREATE TRIGGER IF NOT EXISTS trader_decision_reviews_no_delete
BEFORE DELETE ON trader_decision_reviews BEGIN
  SELECT RAISE(ABORT,'trader_decision_reviews is append-only');
END;
CREATE TRIGGER IF NOT EXISTS trader_lesson_candidates_no_update
BEFORE UPDATE ON trader_lesson_candidates BEGIN
  SELECT RAISE(ABORT,'trader_lesson_candidates is append-only');
END;
CREATE TRIGGER IF NOT EXISTS trader_lesson_candidates_no_delete
BEFORE DELETE ON trader_lesson_candidates BEGIN
  SELECT RAISE(ABORT,'trader_lesson_candidates is append-only');
END;
"""


def init_schema(conn: sqlite3.Connection | None = None) -> sqlite3.Connection:
    db = conn or memory_store._get_conn()
    db.executescript(_SCHEMA)
    return db


def _text(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def save_decision_review(
    *, run_id: str, trader_id: str, decision: dict, review_date: str,
    decision_quality: str, execution_quality: str, outcome_quality: str,
    reason: str, payload: dict, conn: sqlite3.Connection | None = None,
) -> bool:
    """Append one interpretation; an exact decision is reviewed only once."""
    db = init_schema(conn)
    decision_id = _text(decision.get("decision_id"), "decision_id")
    values = (
        _text(run_id, "run_id"), _text(trader_id, "trader_id"), decision_id,
        _text(review_date, "review_date"), _text(decision.get("action"), "action"),
        decision.get("code"), _text(decision_quality, "decision_quality"),
        _text(execution_quality, "execution_quality"),
        _text(outcome_quality, "outcome_quality"), _text(reason, "reason"),
        json.dumps(payload or {}, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":"), allow_nan=False),
    )
    cur = db.execute(
        "INSERT OR IGNORE INTO trader_decision_reviews "
        "(run_id,trader_id,decision_id,review_date,action,code,"
        "decision_quality,execution_quality,outcome_quality,reason,payload_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)", values)
    if conn is None:
        db.commit()
    return bool(cur.rowcount)


def save_lesson_candidate(
    *, run_id: str, trader_id: str, source_type: str, source_ref: str,
    source_date: str, claim: str, action: str, applicable_context: str,
    evidence_timeframe: str, decision_horizon: str, evidence_scope: str,
    support_count: int = 1, counterexample_count: int = 0,
    confidence: float = 0.5, evidence: dict | None = None,
    conn: sqlite3.Connection | None = None,
) -> int:
    """Quarantine one lesson hypothesis. Nothing here activates behaviour."""
    run_id = _text(run_id, "run_id")
    trader_id = _text(trader_id, "trader_id")
    source_type = _text(source_type, "source_type")
    source_ref = _text(source_ref, "source_ref")
    source_date = _text(source_date, "source_date")
    claim = _text(claim, "claim")
    action = _text(action, "action")
    applicable_context = _text(applicable_context, "applicable_context")
    evidence_timeframe = _text(evidence_timeframe, "evidence_timeframe")
    decision_horizon = _text(decision_horizon, "decision_horizon")
    evidence_scope = _text(evidence_scope, "evidence_scope")
    if type(support_count) is not int or support_count < 0:
        raise ValueError("support_count must be a non-negative integer")
    if type(counterexample_count) is not int or counterexample_count < 0:
        raise ValueError("counterexample_count must be a non-negative integer")
    confidence = float(confidence)
    if not 0 <= confidence <= 1:
        raise ValueError("confidence must be in [0,1]")
    evidence_json = json.dumps(
        evidence or {}, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False)
    identity = json.dumps([
        run_id, trader_id, source_type, source_ref, source_date, claim, action,
        applicable_context, evidence_timeframe, decision_horizon, evidence_scope,
        support_count, counterexample_count, confidence, evidence_json,
    ], ensure_ascii=False, separators=(",", ":"))
    fingerprint = hashlib.sha256(identity.encode("utf-8")).hexdigest()

    db = init_schema(conn)
    db.execute(
        "INSERT OR IGNORE INTO trader_lesson_candidates "
        "(fingerprint,run_id,trader_id,source_type,source_ref,source_date,"
        "claim,action,applicable_context,evidence_timeframe,decision_horizon,"
        "evidence_scope,support_count,counterexample_count,confidence,evidence_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (fingerprint, run_id, trader_id, source_type, source_ref, source_date,
         claim, action, applicable_context, evidence_timeframe, decision_horizon,
         evidence_scope, support_count, counterexample_count, confidence,
         evidence_json))
    row = db.execute(
        "SELECT id FROM trader_lesson_candidates WHERE fingerprint=?",
        (fingerprint,)).fetchone()
    if conn is None:
        db.commit()
    return int(row["id"] if hasattr(row, "keys") else row[0])


def decision_reviews(
    *, run_id: str, trader_id: str, up_to: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[dict]:
    db = init_schema(conn)
    sql = (
        "SELECT * FROM trader_decision_reviews WHERE run_id=? AND trader_id=?")
    args: list[object] = [run_id, trader_id]
    if up_to is not None:
        sql += " AND review_date<=?"
        args.append(up_to)
    sql += " ORDER BY review_date,id"
    return [dict(row) for row in db.execute(sql, tuple(args)).fetchall()]


def lesson_candidates(
    *, run_id: str, trader_id: str, up_to: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[dict]:
    db = init_schema(conn)
    sql = (
        "SELECT * FROM trader_lesson_candidates WHERE run_id=? AND trader_id=?")
    args: list[object] = [run_id, trader_id]
    if up_to is not None:
        sql += " AND source_date<=?"
        args.append(up_to)
    sql += " ORDER BY source_date,id"
    return [dict(row) for row in db.execute(sql, tuple(args)).fetchall()]
