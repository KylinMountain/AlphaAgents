"""Isolated, append-only forward observation evidence.

This store is deliberately separate from memory.db and the trading ledger.
It records what was actually available in real time and what an observation-
only decision consumed. Nothing here can place orders, update active policy,
or manufacture a historical registration timestamp.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3


OBSERVATION_ONLY = "observation_only"
CAPTURE_STATUSES = frozenset({
    "ok", "empty", "unknown", "failure", "permission_denied", "timeout",
})
DECISION_STATUSES = frozenset({
    "selected", "abstained", "no_candidate", "parse_error",
    "technical_failure", "permission_denied", "timeout",
})


class ForwardObservationError(ValueError):
    pass


def _dump(value) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False)


def _hash(value) -> str:
    return hashlib.sha256(_dump(value).encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


_CAPTURES = """
CREATE TABLE IF NOT EXISTS forward_captures (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    source_key TEXT NOT NULL,
    status TEXT NOT NULL,
    source_timezone TEXT,
    source_published_at TEXT,
    source_captured_at TEXT,
    available_at TEXT,
    payload_json TEXT,
    payload_hash TEXT NOT NULL,
    error TEXT,
    collector_version TEXT NOT NULL,
    idempotency_hash TEXT NOT NULL UNIQUE,
    supersedes_id INTEGER,
    registered_at TEXT NOT NULL,
    evidence_scope TEXT NOT NULL,
    FOREIGN KEY(supersedes_id) REFERENCES forward_captures(id)
)
"""

_DECISIONS = """
CREATE TABLE IF NOT EXISTS forward_decisions (
    id INTEGER PRIMARY KEY,
    decision_key TEXT NOT NULL,
    status TEXT NOT NULL,
    information_cutoff TEXT NOT NULL,
    code_ref TEXT NOT NULL,
    policy_ref TEXT NOT NULL,
    world_read_set_hash TEXT NOT NULL,
    research_packet_hash TEXT,
    model_config_json TEXT NOT NULL,
    model_config_hash TEXT NOT NULL,
    request_json TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    budget_json TEXT NOT NULL,
    result_json TEXT,
    result_hash TEXT NOT NULL,
    error TEXT,
    idempotency_hash TEXT NOT NULL UNIQUE,
    supersedes_id INTEGER,
    registered_at TEXT NOT NULL,
    evidence_scope TEXT NOT NULL,
    FOREIGN KEY(supersedes_id) REFERENCES forward_decisions(id)
)
"""


def connect(path: Path) -> sqlite3.Connection:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    init_schema(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_CAPTURES)
    conn.execute(_DECISIONS)
    for table in ("forward_captures", "forward_decisions"):
        conn.execute(
            f"CREATE TRIGGER IF NOT EXISTS {table}_no_update "
            f"BEFORE UPDATE ON {table} BEGIN "
            f"SELECT RAISE(ABORT, '{table} is append-only'); END")
        conn.execute(
            f"CREATE TRIGGER IF NOT EXISTS {table}_no_delete "
            f"BEFORE DELETE ON {table} BEGIN "
            f"SELECT RAISE(ABORT, '{table} is append-only'); END")
    conn.commit()


def _latest(conn: sqlite3.Connection, table: str, key_field: str,
            key: str) -> sqlite3.Row | None:
    return conn.execute(
        f"SELECT * FROM {table} WHERE {key_field}=? ORDER BY id DESC LIMIT 1",
        (key,),
    ).fetchone()


def record_capture(
        conn: sqlite3.Connection, *, source: str, source_key: str,
        status: str, collector_version: str,
        payload=None, error: str | None = None,
        source_timezone: str | None = None,
        source_published_at: str | None = None,
        source_captured_at: str | None = None,
        available_at: str | None = None) -> int:
    """Append one real-time source observation; exact retries are idempotent."""
    if status not in CAPTURE_STATUSES:
        raise ForwardObservationError(f"unsupported capture status {status!r}")
    if not str(source or "").strip() or not str(source_key or "").strip():
        raise ForwardObservationError("source and source_key are required")
    if not str(collector_version or "").strip():
        raise ForwardObservationError("collector_version is required")

    payload_json = None if payload is None else _dump(payload)
    payload_hash = _hash({"payload": payload})
    identity = {
        "source": source,
        "source_key": source_key,
        "status": status,
        "source_timezone": source_timezone,
        "source_published_at": source_published_at,
        "source_captured_at": source_captured_at,
        "available_at": available_at,
        "payload_hash": payload_hash,
        "error": error,
        "collector_version": collector_version,
    }
    idem = _hash(identity)
    existing = conn.execute(
        "SELECT id FROM forward_captures WHERE idempotency_hash=?",
        (idem,),
    ).fetchone()
    if existing is not None:
        return int(existing["id"])

    previous = _latest(
        conn, "forward_captures", "source_key", source_key)
    registered = _utc_now()
    with conn:
        cur = conn.execute(
            "INSERT INTO forward_captures "
            "(source,source_key,status,source_timezone,source_published_at,"
            "source_captured_at,available_at,payload_json,payload_hash,error,"
            "collector_version,idempotency_hash,supersedes_id,registered_at,"
            "evidence_scope) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                source, source_key, status, source_timezone,
                source_published_at, source_captured_at, available_at,
                payload_json, payload_hash, error, collector_version, idem,
                int(previous["id"]) if previous is not None else None,
                registered, OBSERVATION_ONLY,
            ),
        )
    return int(cur.lastrowid)


def record_decision(
        conn: sqlite3.Connection, *, decision_key: str, status: str,
        information_cutoff: str, code_ref: str, policy_ref: str,
        world_read_set_hash: str, model_config: dict, request: dict,
        budget: dict, result=None, error: str | None = None,
        research_packet_hash: str | None = None) -> int:
    """Append one observation-only decision with frozen evidence hashes."""
    if status not in DECISION_STATUSES:
        raise ForwardObservationError(f"unsupported decision status {status!r}")
    required = {
        "decision_key": decision_key,
        "information_cutoff": information_cutoff,
        "code_ref": code_ref,
        "policy_ref": policy_ref,
        "world_read_set_hash": world_read_set_hash,
    }
    missing = [key for key, value in required.items()
               if not str(value or "").strip()]
    if missing:
        raise ForwardObservationError(
            "missing decision evidence: " + ", ".join(missing))

    model_json = _dump(model_config)
    request_json = _dump(request)
    budget_json = _dump(budget)
    result_json = None if result is None else _dump(result)
    model_hash = _hash(model_config)
    request_hash = _hash(request)
    result_hash = _hash({"result": result, "error": error})
    identity = {
        **required,
        "status": status,
        "research_packet_hash": research_packet_hash,
        "model_config_hash": model_hash,
        "request_hash": request_hash,
        "budget": budget,
        "result_hash": result_hash,
    }
    idem = _hash(identity)
    existing = conn.execute(
        "SELECT id FROM forward_decisions WHERE idempotency_hash=?",
        (idem,),
    ).fetchone()
    if existing is not None:
        return int(existing["id"])

    previous = _latest(
        conn, "forward_decisions", "decision_key", decision_key)
    registered = _utc_now()
    with conn:
        cur = conn.execute(
            "INSERT INTO forward_decisions "
            "(decision_key,status,information_cutoff,code_ref,policy_ref,"
            "world_read_set_hash,research_packet_hash,model_config_json,"
            "model_config_hash,request_json,request_hash,budget_json,"
            "result_json,result_hash,error,idempotency_hash,supersedes_id,"
            "registered_at,evidence_scope) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                decision_key, status, information_cutoff, code_ref, policy_ref,
                world_read_set_hash, research_packet_hash, model_json,
                model_hash, request_json, request_hash, budget_json,
                result_json, result_hash, error, idem,
                int(previous["id"]) if previous is not None else None,
                registered, OBSERVATION_ONLY,
            ),
        )
    return int(cur.lastrowid)


def capture(conn: sqlite3.Connection, row_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM forward_captures WHERE id=?", (int(row_id),)
    ).fetchone()
    return dict(row) if row else None


def decision(conn: sqlite3.Connection, row_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM forward_decisions WHERE id=?", (int(row_id),)
    ).fetchone()
    return dict(row) if row else None
