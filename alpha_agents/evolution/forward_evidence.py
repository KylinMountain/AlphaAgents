"""Isolated append-only forward evidence store.

RP-09 deliberately stops before candidate promotion.  The store can capture
raw source observations and frozen research decisions, but it has no API that
moves an active policy, mutates the production book or rewrites prior evidence.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any


SCHEMA_VERSION = 1

RESULT_STATES = frozenset({
    "success",
    "empty",
    "unknown",
    "abstained",
    "parse_error",
    "permission_denied",
    "timeout",
    "technical_error",
})

_SECRET_KEYS = (
    "authorization", "api_key", "apikey", "token", "secret",
    "password", "passwd", "cookie", "set-cookie",
)


class ForwardEvidenceError(ValueError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _dump(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(_dump(value).encode("utf-8")).hexdigest()


def _secret_key(key: str) -> bool:
    lowered = str(key).lower().replace("-", "_")
    return any(
        secret.replace("-", "_") in lowered for secret in _SECRET_KEYS)


def redact(value: Any) -> Any:
    """Remove credentials recursively while retaining evidence shape."""
    if isinstance(value, dict):
        return {
            str(key): ("[REDACTED]" if _secret_key(str(key)) else redact(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return [redact(item) for item in value]
    return value


class ForwardEvidenceStore:
    """One explicit SQLite file, independent of the production data directory."""

    def __init__(self, path: str | Path, *, enabled: bool = True):
        self.path = Path(path)
        self.enabled = bool(enabled)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def close(self) -> None:
        self.conn.close()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS forward_source_versions (
                id INTEGER PRIMARY KEY,
                source TEXT NOT NULL,
                source_key TEXT NOT NULL,
                source_published_at TEXT,
                source_captured_at TEXT,
                source_available_at TEXT,
                original_timezone TEXT,
                collected_at_utc TEXT NOT NULL,
                collector_version TEXT NOT NULL,
                result_state TEXT NOT NULL,
                payload_json TEXT,
                payload_hash TEXT,
                error_class TEXT,
                error_message TEXT,
                observation_only INTEGER NOT NULL DEFAULT 1,
                UNIQUE(source, source_key, payload_hash, result_state,
                       error_class, error_message)
            );

            CREATE TABLE IF NOT EXISTS forward_decisions (
                id INTEGER PRIMARY KEY,
                decision_key TEXT NOT NULL,
                registered_at_utc TEXT NOT NULL,
                opened_on TEXT,
                code_ref TEXT NOT NULL,
                policy_ref TEXT NOT NULL,
                world_hash TEXT NOT NULL,
                packet_hash TEXT,
                model_config_json TEXT NOT NULL,
                request_json TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                budget_json TEXT NOT NULL,
                result_state TEXT NOT NULL,
                result_json TEXT,
                result_hash TEXT,
                error_class TEXT,
                error_message TEXT,
                observation_only INTEGER NOT NULL DEFAULT 1,
                UNIQUE(decision_key, request_hash)
            );

            CREATE TRIGGER IF NOT EXISTS forward_source_no_update
            BEFORE UPDATE ON forward_source_versions
            BEGIN
              SELECT RAISE(ABORT, 'forward source evidence is append-only');
            END;

            CREATE TRIGGER IF NOT EXISTS forward_source_no_delete
            BEFORE DELETE ON forward_source_versions
            BEGIN
              SELECT RAISE(ABORT, 'forward source evidence is append-only');
            END;

            CREATE TRIGGER IF NOT EXISTS forward_decision_no_update
            BEFORE UPDATE ON forward_decisions
            BEGIN
              SELECT RAISE(ABORT, 'forward decision evidence is append-only');
            END;

            CREATE TRIGGER IF NOT EXISTS forward_decision_no_delete
            BEFORE DELETE ON forward_decisions
            BEGIN
              SELECT RAISE(ABORT, 'forward decision evidence is append-only');
            END;
            """
        )
        self.conn.commit()

    @staticmethod
    def _validate_state(result_state: str) -> str:
        state = str(result_state or "").strip()
        if state not in RESULT_STATES:
            raise ForwardEvidenceError(
                "result_state must be one of: " +
                ", ".join(sorted(RESULT_STATES)))
        return state

    def capture_source(
            self, *, source: str, source_key: str,
            collector_version: str, result_state: str,
            payload: Any | None = None,
            source_published_at: str | None = None,
            source_captured_at: str | None = None,
            source_available_at: str | None = None,
            original_timezone: str | None = None,
            error_class: str | None = None,
            error_message: str | None = None) -> dict | None:
        """Append one source vintage; exact retries are idempotent."""
        if not self.enabled:
            return None
        state = self._validate_state(result_state)
        if not str(source).strip() or not str(source_key).strip():
            raise ForwardEvidenceError("source and source_key are required")
        if not str(collector_version).strip():
            raise ForwardEvidenceError("collector_version is required")

        safe_payload = redact(payload) if payload is not None else None
        payload_json = (
            _dump(safe_payload) if safe_payload is not None else None)
        payload_hash = (
            hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
            if payload_json is not None else None)

        values = (
            str(source), str(source_key), source_published_at,
            source_captured_at, source_available_at, original_timezone,
            _utc_now(), str(collector_version), state, payload_json,
            payload_hash, error_class, error_message,
        )
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO forward_source_versions "
                "(source,source_key,source_published_at,source_captured_at,"
                "source_available_at,original_timezone,collected_at_utc,"
                "collector_version,result_state,payload_json,payload_hash,"
                "error_class,error_message,observation_only) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
                values,
            )
        row = self.conn.execute(
            "SELECT * FROM forward_source_versions "
            "WHERE source=? AND source_key=? "
            "AND payload_hash IS ? AND result_state=? "
            "AND error_class IS ? AND error_message IS ? "
            "ORDER BY id LIMIT 1",
            (str(source), str(source_key), payload_hash, state,
             error_class, error_message),
        ).fetchone()
        return dict(row) if row else None

    def record_decision(
            self, *, decision_key: str, opened_on: str | None,
            code_ref: str, policy_ref: str, world_hash: str,
            packet_hash: str | None, model_config: dict,
            request: dict, budget: dict, result_state: str,
            result: Any | None = None,
            error_class: str | None = None,
            error_message: str | None = None) -> dict | None:
        """Freeze a forward research decision; registered time is server-owned."""
        if not self.enabled:
            return None
        state = self._validate_state(result_state)
        required = {
            "decision_key": decision_key,
            "code_ref": code_ref,
            "policy_ref": policy_ref,
            "world_hash": world_hash,
        }
        missing = [key for key, value in required.items()
                   if not str(value or "").strip()]
        if missing:
            raise ForwardEvidenceError(
                "missing decision identity: " + ", ".join(missing))

        safe_request = redact(request)
        safe_result = redact(result) if result is not None else None
        request_json = _dump(safe_request)
        request_hash = hashlib.sha256(
            request_json.encode("utf-8")).hexdigest()
        result_json = (
            _dump(safe_result) if safe_result is not None else None)
        result_hash = (
            hashlib.sha256(result_json.encode("utf-8")).hexdigest()
            if result_json is not None else None)

        values = (
            str(decision_key), _utc_now(), opened_on,
            str(code_ref), str(policy_ref), str(world_hash), packet_hash,
            _dump(redact(model_config)), request_json, request_hash,
            _dump(redact(budget)), state, result_json, result_hash,
            error_class, error_message,
        )
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO forward_decisions "
                "(decision_key,registered_at_utc,opened_on,code_ref,policy_ref,"
                "world_hash,packet_hash,model_config_json,request_json,"
                "request_hash,budget_json,result_state,result_json,result_hash,"
                "error_class,error_message,observation_only) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
                values,
            )
        row = self.conn.execute(
            "SELECT * FROM forward_decisions "
            "WHERE decision_key=? AND request_hash=?",
            (str(decision_key), request_hash),
        ).fetchone()
        return dict(row) if row else None
