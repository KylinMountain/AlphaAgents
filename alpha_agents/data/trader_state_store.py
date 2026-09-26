"""Append-only persistence for the continuous Trader Runtime state."""
from __future__ import annotations

import json
import sqlite3

from alpha_agents.trader import TraderState


class TraderStateConflict(RuntimeError):
    """The caller tried to advance a stale TraderState lineage."""


class TraderStateStoreError(ValueError):
    """Malformed persistence identity or snapshot."""


def _identity(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TraderStateStoreError(f"{field} must be an explicit non-empty string")
    return value.strip()


def _connection(conn: sqlite3.Connection | None) -> tuple[sqlite3.Connection, bool]:
    if conn is not None:
        return conn, False
    from alpha_agents.data.memory_store import _get_conn
    return _get_conn(), True


def load_latest(
    *, run_id: str, trader_id: str,
    conn: sqlite3.Connection | None = None,
) -> TraderState | None:
    """Return the newest sealed state for exactly one run/trader identity."""
    run = _identity(run_id, "run_id")
    trader = _identity(trader_id, "trader_id")
    db, _ = _connection(conn)
    row = db.execute(
        "SELECT payload_json FROM trader_state_snapshots "
        "WHERE run_id=? AND trader_id=? ORDER BY version DESC LIMIT 1",
        (run, trader),
    ).fetchone()
    if row is None:
        return None
    payload = json.loads(row["payload_json"] if isinstance(row, sqlite3.Row) else row[0])
    state = TraderState.from_dict(payload)
    if state.trader_id != trader:
        raise TraderStateStoreError("stored TraderState trader identity mismatch")
    return state


def save(
    state: TraderState, *, run_id: str,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """Append one state with compare-and-swap lineage protection.

    Returns True when a new row is appended and False for an idempotent retry of
    the exact already-latest state. A stale writer raises TraderStateConflict.
    """
    if not isinstance(state, TraderState):
        raise TraderStateStoreError("state must be TraderState")
    run = _identity(run_id, "run_id")
    trader = _identity(state.trader_id, "trader_id")
    db, _ = _connection(conn)
    if db.in_transaction:
        raise TraderStateStoreError(
            "TraderState CAS requires a clean transaction boundary")

    try:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT state_hash,version FROM trader_state_snapshots "
            "WHERE run_id=? AND trader_id=? ORDER BY version DESC LIMIT 1",
            (run, trader),
        ).fetchone()
        latest_hash = None if row is None else row[0]
        latest_version = None if row is None else int(row[1])

        if latest_hash == state.state_hash:
            db.commit()
            return False

        if state.parent_state_hash != latest_hash:
            raise TraderStateConflict(
                "TraderState parent hash does not match latest persisted state")
        expected_version = 0 if latest_version is None else latest_version + 1
        if state.version != expected_version:
            raise TraderStateConflict(
                "TraderState version is not the next persisted version")

        payload = json.dumps(
            state.as_dict(), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False)
        db.execute(
            "INSERT INTO trader_state_snapshots "
            "(state_hash,run_id,trader_id,version,parent_state_hash,as_of,payload_json) "
            "VALUES (?,?,?,?,?,?,?)",
            (state.state_hash, run, trader, state.version,
             state.parent_state_hash, state.as_of.isoformat(), payload),
        )
        db.commit()
        return True
    except Exception:
        db.rollback()
        raise


def snapshot_payloads(
    *, run_id: str, trader_id: str,
    conn: sqlite3.Connection | None = None,
) -> list[dict]:
    """Every sealed state for one run/trader, oldest first, as raw payloads.

    For readers that need the whole history rather than the latest state —
    TraderState keeps a bounded tail of decisions, so only the union of the
    append-only snapshots holds all of them.
    """
    run = _identity(run_id, "run_id")
    trader = _identity(trader_id, "trader_id")
    db, _ = _connection(conn)
    rows = db.execute(
        "SELECT payload_json FROM trader_state_snapshots "
        "WHERE run_id=? AND trader_id=? ORDER BY version",
        (run, trader)).fetchall()
    return [json.loads(row["payload_json"] if isinstance(row, sqlite3.Row)
                       else row[0]) for row in rows]
