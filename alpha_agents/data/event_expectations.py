"""Point-in-time event calendar and expectation snapshots.

The same event can be known long before it happens, and the market's
expectation can change many times before realization. Therefore neither the
calendar nor the consensus is stored as one mutable row.

Everything is append-only:
- event_calendar_snapshots: what event was scheduled, as known then;
- event_expectation_snapshots: consensus / market-implied state, as known then;
- event_realizations: what actually happened, only after announcement.

Readers always ask "as of T". A later correction or revised consensus therefore
cannot leak backward into replay.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

from alpha_agents.config import DATA_DIR

DB_PATH = DATA_DIR / "market_snapshots.db"


class EventExpectationError(ValueError):
    """An event snapshot is malformed or unavailable."""


_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS event_calendar_snapshots (
        id INTEGER PRIMARY KEY,
        event_key TEXT NOT NULL,
        event_type TEXT NOT NULL,
        scope TEXT NOT NULL,
        subject TEXT NOT NULL,
        scheduled_at TEXT NOT NULL,
        captured_at TEXT NOT NULL,
        source TEXT NOT NULL,
        metadata_json TEXT NOT NULL,
        content_hash TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS event_expectation_snapshots (
        id INTEGER PRIMARY KEY,
        event_key TEXT NOT NULL,
        captured_at TEXT NOT NULL,
        consensus_json TEXT,
        market_implied_json TEXT,
        source TEXT NOT NULL,
        content_hash TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS event_realizations (
        id INTEGER PRIMARY KEY,
        event_key TEXT NOT NULL,
        announced_at TEXT NOT NULL,
        actual_json TEXT NOT NULL,
        source TEXT NOT NULL,
        content_hash TEXT NOT NULL
    )
    """,
)

_GUARDS = (
    ("event_calendar_snapshots", "calendar"),
    ("event_expectation_snapshots", "expectation"),
    ("event_realizations", "realization"),
)


def _dump(value) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False)


def _hash(payload: dict) -> str:
    return hashlib.sha256(_dump(payload).encode("utf-8")).hexdigest()


def connect(*, readonly: bool = True) -> sqlite3.Connection:
    if readonly:
        if not DB_PATH.exists():
            raise EventExpectationError(f"event store does not exist: {DB_PATH}")
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=10)
    else:
        conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    for sql in _SCHEMA:
        conn.execute(sql)
    # Content hashes include event_key/source/timestamps, so these indexes make
    # provider ingestion safely re-runnable without collapsing distinct
    # revisions of the same event.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_event_calendar_content "
        "ON event_calendar_snapshots(content_hash)")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_event_expectation_content "
        "ON event_expectation_snapshots(content_hash)")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_event_realization_content "
        "ON event_realizations(content_hash)")
    for table, label in _GUARDS:
        conn.execute(
            f"CREATE TRIGGER IF NOT EXISTS {table}_no_update "
            f"BEFORE UPDATE ON {table} BEGIN "
            f"SELECT RAISE(ABORT, '{label} snapshots are append-only'); END")
        conn.execute(
            f"CREATE TRIGGER IF NOT EXISTS {table}_no_delete "
            f"BEFORE DELETE ON {table} BEGIN "
            f"SELECT RAISE(ABORT, '{label} snapshots are append-only'); END")


def record_event(*, event_key: str, event_type: str, scope: str, subject: str,
                 scheduled_at: str, captured_at: str, source: str,
                 metadata: dict | None = None,
                 conn: sqlite3.Connection | None = None) -> int:
    payload = {
        "event_key": event_key, "event_type": event_type, "scope": scope,
        "subject": subject, "scheduled_at": scheduled_at,
        "captured_at": captured_at, "source": source,
        "metadata": metadata or {},
    }
    target = conn or connect(readonly=False)
    close = conn is None
    try:
        init_schema(target)
        with target:
            digest = _hash(payload)
            target.execute(
                "INSERT OR IGNORE INTO event_calendar_snapshots "
                "(event_key,event_type,scope,subject,scheduled_at,captured_at,"
                " source,metadata_json,content_hash) VALUES (?,?,?,?,?,?,?,?,?)",
                (event_key, event_type, scope, subject, scheduled_at,
                 captured_at, source, _dump(metadata or {}), digest))
            row = target.execute(
                "SELECT id FROM event_calendar_snapshots WHERE content_hash=?",
                (digest,)).fetchone()
            return int(row["id"])
    finally:
        if close:
            target.close()


def record_expectation(*, event_key: str, captured_at: str, source: str,
                       consensus: dict | None = None,
                       market_implied: dict | None = None,
                       conn: sqlite3.Connection | None = None) -> int:
    payload = {
        "event_key": event_key, "captured_at": captured_at, "source": source,
        "consensus": consensus, "market_implied": market_implied,
    }
    target = conn or connect(readonly=False)
    close = conn is None
    try:
        init_schema(target)
        with target:
            digest = _hash(payload)
            target.execute(
                "INSERT OR IGNORE INTO event_expectation_snapshots "
                "(event_key,captured_at,consensus_json,market_implied_json,"
                " source,content_hash) VALUES (?,?,?,?,?,?)",
                (event_key, captured_at,
                 _dump(consensus) if consensus is not None else None,
                 _dump(market_implied) if market_implied is not None else None,
                 source, digest))
            row = target.execute(
                "SELECT id FROM event_expectation_snapshots WHERE content_hash=?",
                (digest,)).fetchone()
            return int(row["id"])
    finally:
        if close:
            target.close()


def record_realization(*, event_key: str, announced_at: str, actual: dict,
                       source: str,
                       conn: sqlite3.Connection | None = None) -> int:
    payload = {
        "event_key": event_key, "announced_at": announced_at,
        "actual": actual, "source": source,
    }
    target = conn or connect(readonly=False)
    close = conn is None
    try:
        init_schema(target)
        with target:
            digest = _hash(payload)
            target.execute(
                "INSERT OR IGNORE INTO event_realizations "
                "(event_key,announced_at,actual_json,source,content_hash) "
                "VALUES (?,?,?,?,?)",
                (event_key, announced_at, _dump(actual), source, digest))
            row = target.execute(
                "SELECT id FROM event_realizations WHERE content_hash=?",
                (digest,)).fetchone()
            return int(row["id"])
    finally:
        if close:
            target.close()


def _json(value):
    return json.loads(value) if value else None


def context(*, as_of: str, subject: str | None = None,
            scope: str | None = None, days_ahead: int = 14,
            conn: sqlite3.Connection | None = None) -> list[dict]:
    """Return latest event and expectation facts knowable at as_of.

    Upcoming events are restricted to days_ahead. Realization is attached only
    when its announcement timestamp is already <= as_of.
    """
    if days_ahead < 0:
        raise EventExpectationError("days_ahead cannot be negative")
    target = conn or connect(readonly=True)
    close = conn is None
    try:
        where = ["c.captured_at <= ?",
                 "c.scheduled_at <= datetime(?, ? || ' days')"]
        params: list = [as_of, as_of, f"+{days_ahead}"]
        if subject is not None:
            where.append("c.subject = ?")
            params.append(subject)
        if scope is not None:
            where.append("c.scope = ?")
            params.append(scope)

        rows = target.execute(
            "SELECT c.* FROM event_calendar_snapshots c "
            "JOIN (SELECT event_key, MAX(captured_at) captured_at "
            "      FROM event_calendar_snapshots WHERE captured_at <= ? "
            "      GROUP BY event_key) latest "
            "ON latest.event_key=c.event_key "
            "AND latest.captured_at=c.captured_at "
            "WHERE " + " AND ".join(where) +
            " ORDER BY c.scheduled_at, c.event_key",
            [as_of, *params]).fetchall()

        out = []
        for row in rows:
            event = dict(row)
            exp = target.execute(
                "SELECT * FROM event_expectation_snapshots "
                "WHERE event_key=? AND captured_at<=? "
                "ORDER BY captured_at DESC, id DESC LIMIT 1",
                (event["event_key"], as_of)).fetchone()
            actual = target.execute(
                "SELECT * FROM event_realizations "
                "WHERE event_key=? AND announced_at<=? "
                "ORDER BY announced_at DESC, id DESC LIMIT 1",
                (event["event_key"], as_of)).fetchone()
            out.append({
                "event_key": event["event_key"],
                "event_type": event["event_type"],
                "scope": event["scope"],
                "subject": event["subject"],
                "scheduled_at": event["scheduled_at"],
                "calendar_captured_at": event["captured_at"],
                "metadata": _json(event["metadata_json"]) or {},
                "expectation": ({
                    "captured_at": exp["captured_at"],
                    "consensus": _json(exp["consensus_json"]),
                    "market_implied": _json(exp["market_implied_json"]),
                    "source": exp["source"],
                } if exp else None),
                "realization": ({
                    "announced_at": actual["announced_at"],
                    "actual": _json(actual["actual_json"]),
                    "source": actual["source"],
                } if actual else None),
            })
        return out
    except sqlite3.Error as exc:
        raise EventExpectationError(
            f"event expectation store is unavailable: {exc}") from exc
    finally:
        if close:
            target.close()
