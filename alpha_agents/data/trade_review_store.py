"""Facts-first reviews with auditable, bounded interpretation attempts."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

MAX_ATTEMPTS = 3
_FIELDS = {"facts_available_on": "TEXT", "review_status": "TEXT NOT NULL DEFAULT 'pending'",
           "attempt_count": "INTEGER NOT NULL DEFAULT 0", "last_attempt_on": "TEXT",
           "review_available_on": "TEXT"}


def complete(words: dict) -> bool:
    """A verdict alone is not an interpretation."""
    return (isinstance(words.get("verdict"), str) and bool(words["verdict"].strip())
            and any(isinstance(words.get(k), str) and words[k].strip()
                    for k in ("right", "wrong", "next_time")))


def migrate(conn: sqlite3.Connection) -> None:
    """Never fabricate legacy review availability from the trade's close date."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(trade_reviews)")}
    for name, definition in _FIELDS.items():
        if name not in cols:
            conn.execute(f"ALTER TABLE trade_reviews ADD COLUMN {name} {definition}")
    for rid, closed, created, raw in conn.execute(
            "SELECT id,close_date,created_at,lesson_json FROM trade_reviews "
            "WHERE facts_available_on IS NULL").fetchall():
        try:
            stamp = datetime.fromisoformat(created.replace("Z", "+00:00"))
            if stamp.tzinfo:
                stamp = stamp.astimezone(ZoneInfo("Asia/Shanghai"))
            available = max(closed, stamp.date().isoformat())
            words = json.loads(raw or "{}")
            done = isinstance(words, dict) and complete(words)
        except (TypeError, ValueError, AttributeError):
            available, done = "9999-12-31", False
        conn.execute("UPDATE trade_reviews SET facts_available_on=?,review_status=?,"
                     "review_available_on=? WHERE id=?", (available,
                     "complete" if done else "pending", available if done else None, rid))


def save_facts(conn: sqlite3.Connection, facts: dict, trader_id: str, as_of: str) -> bool:
    cur = conn.execute("INSERT OR IGNORE INTO trade_reviews "
                       "(position_id,trader_id,code,close_date,facts_json,lesson_json,facts_available_on) "
                       "VALUES (?,?,?,?,?,'{}',?)", (facts["position_id"], trader_id,
                       facts["code"], facts["close_date"],
                       json.dumps(facts, ensure_ascii=False, allow_nan=False), as_of))
    return cur.rowcount == 1


def claim(conn: sqlite3.Connection, position_id: int, as_of: str) -> int | None:
    """One logical attempt per day; caller commits before any provider call."""
    conn.execute("SAVEPOINT review_claim")
    try:
        row = conn.execute(
            "UPDATE trade_reviews SET attempt_count=attempt_count+1,last_attempt_on=?,"
            "review_status='pending' WHERE position_id=? AND review_status<>'complete' "
            "AND attempt_count<? AND facts_available_on<=? "
            "AND (last_attempt_on IS NULL OR last_attempt_on<?) RETURNING attempt_count",
            (as_of, position_id, MAX_ATTEMPTS, as_of, as_of)).fetchone()
        if row:
            conn.execute("INSERT INTO trade_review_attempts "
                         "(position_id,attempt,attempted_on,status,lesson_json,error) "
                         "VALUES (?,?,?,'started','{}','')", (position_id, row[0], as_of))
    except Exception:
        conn.execute("ROLLBACK TO review_claim")
        raise
    finally:
        conn.execute("RELEASE review_claim")
    return row[0] if row else None


def finish(conn: sqlite3.Connection, position_id: int, attempt: int, words: dict,
           as_of: str, error: str = "", *, completed_on: str | None = None) -> bool:
    """CAS prevents stale callbacks from overwriting a later/completed attempt."""
    available, done = max(as_of, completed_on or as_of), complete(words)
    status, raw = "complete" if done else "failed", json.dumps(words, ensure_ascii=False, allow_nan=False)
    conn.execute("SAVEPOINT review_finish")
    try:
        changed = conn.execute(
            "UPDATE trade_reviews SET lesson_json=?,review_status=?,review_available_on=? "
            "WHERE position_id=? AND attempt_count=? AND last_attempt_on=? AND review_status='pending'",
            (raw, status, available if done else None, position_id, attempt, as_of)).rowcount
        if changed:
            conn.execute("INSERT INTO trade_review_attempts "
                         "(position_id,attempt,attempted_on,status,lesson_json,error) VALUES (?,?,?,?,?,?)",
                         (position_id, attempt, available, status, raw, error))
    except Exception:
        conn.execute("ROLLBACK TO review_finish")
        raise
    finally:
        conn.execute("RELEASE review_finish")
    return bool(changed and done)


def pending_counts(conn: sqlite3.Connection, trader_id: str, as_of: str) -> dict:
    rows = conn.execute("SELECT attempt_count FROM trade_reviews WHERE trader_id=? "
                        "AND close_date<=? AND review_status<>'complete'", (trader_id, as_of)).fetchall()
    return {"trade_review_pending": len(rows),
            "trade_review_exhausted": sum(n >= MAX_ATTEMPTS for (n,) in rows)}
