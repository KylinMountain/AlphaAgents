"""Quarantined learning proposals and measurements, never active knowledge.

This module owns its schema independently of memory_store migrations. There is
no approval or promotion API: evidence collection is not permission to trade.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date

from alpha_agents.data import memory_store


def init_schema(conn: sqlite3.Connection) -> None:
    """Initialize only the quarantine tables on the supplied connection."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS learning_candidates ("
        "id INTEGER PRIMARY KEY, "
        "fingerprint TEXT NOT NULL UNIQUE, "
        "entity_type TEXT NOT NULL CHECK(entity_type IN ('principle', 'playbook')), "
        "operation TEXT NOT NULL CHECK(operation IN ('create', 'reinforce', 'update', 'retire')), "
        "target_id INTEGER, source TEXT NOT NULL, source_date TEXT NOT NULL, "
        "payload_json TEXT NOT NULL, "
        "status TEXT NOT NULL DEFAULT 'candidate' CHECK(status = 'candidate'), "
        "created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS learning_observations ("
        "id INTEGER PRIMARY KEY, "
        "entity_type TEXT NOT NULL CHECK(entity_type IN ('principle', 'playbook')), "
        "target_id INTEGER NOT NULL, source TEXT NOT NULL, source_date TEXT NOT NULL, "
        "payload_json TEXT NOT NULL, "
        "created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
        "UNIQUE(entity_type, target_id, source, source_date, payload_json))"
    )


def _validate_envelope(entity_type: str, source: str, source_date: str, payload: dict) -> None:
    """Validate provenance, not the truth or trading merit of a proposal."""
    if entity_type not in ("principle", "playbook"):
        raise ValueError("Unsupported learning entity type")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("Learning provenance requires a nonempty source")
    if not isinstance(source_date, str) or date.fromisoformat(source_date).isoformat() != source_date:
        raise ValueError("Learning provenance requires a YYYY-MM-DD source_date")
    if not isinstance(payload, dict):
        raise ValueError("Learning payload must be an object")


def save_candidate(*, entity_type: str, operation: str, source: str,
                   source_date: str, payload: dict, target_id: int | None = None) -> int:
    """Persist a proposal; exact retries return the original candidate ID.

    The original payload and provenance are retained. Changed evidence produces
    a separate candidate, not an overwrite. Storage errors propagate to callers.
    """
    _validate_envelope(entity_type, source, source_date, payload)
    allowed = ("create", "reinforce") if entity_type == "principle" else ("create", "update", "retire")
    if operation not in allowed:
        raise ValueError("Unsupported candidate operation for this entity type")
    if operation == "create":
        if target_id is not None:
            raise ValueError("Create candidates cannot target existing knowledge")
    elif type(target_id) is not int or target_id <= 0:
        raise ValueError("Existing-knowledge candidates require a positive integer target_id")
    payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
    identity = json.dumps([entity_type, operation, target_id, source, source_date, payload_json])
    fingerprint = hashlib.sha256(identity.encode('utf-8')).hexdigest()
    with memory_store._write_lock:
        conn = memory_store._get_conn()
        with conn:
            init_schema(conn)
            conn.execute(
                "INSERT INTO learning_candidates "
                "(fingerprint, entity_type, operation, target_id, source, source_date, payload_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(fingerprint) DO NOTHING",
                (fingerprint, entity_type, operation, target_id, source, source_date, payload_json),
            )
            row = conn.execute(
                "SELECT id FROM learning_candidates WHERE fingerprint = ?", (fingerprint,),
            ).fetchone()
            return row['id']


def record_observation(*, entity_type: str, target_id: int, source: str,
                       source_date: str, payload: dict) -> None:
    """Keep measurements separate from scores/annotations injected as rules."""
    _validate_envelope(entity_type, source, source_date, payload)
    if type(target_id) is not int or target_id <= 0:
        raise ValueError("Observations require a positive integer target_id")
    payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
    with memory_store._write_lock:
        conn = memory_store._get_conn()
        with conn:
            init_schema(conn)
            conn.execute(
                "INSERT INTO learning_observations "
                "(entity_type, target_id, source, source_date, payload_json) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(entity_type, target_id, source, source_date, payload_json) DO NOTHING",
                (entity_type, target_id, source, source_date, payload_json),
            )
