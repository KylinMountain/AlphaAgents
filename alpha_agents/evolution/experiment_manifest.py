"""Immutable contract for one shadow experiment.

A version id says which policy was named. This manifest says what the
experiment actually promised to vary, observe and measure before evidence
arrived. Keeping this storage separate from shadow.py is intentional:
forecast production and experiment governance are different responsibilities.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

EVALUATOR = "paired_brier_v1"
METRIC = "brier"
STOPPING_RULE = "one_verdict_at_or_after_minimum_paired_samples"
HORIZON_RULE = "champion_declared_per_code"

_TABLE = """
CREATE TABLE IF NOT EXISTS shadow_manifests (
    id INTEGER PRIMARY KEY,
    policy_version_id INTEGER NOT NULL,
    reference_version_id INTEGER,
    producer TEXT NOT NULL,
    report_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
)
"""

_GUARDS = (
    "CREATE TRIGGER IF NOT EXISTS shadow_manifests_no_update "
    "BEFORE UPDATE ON shadow_manifests BEGIN "
    "SELECT RAISE(ABORT, 'shadow_manifests is append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS shadow_manifests_no_delete "
    "BEFORE DELETE ON shadow_manifests BEGIN "
    "SELECT RAISE(ABORT, 'shadow_manifests is append-only'); END",
)


class ManifestError(ValueError):
    """The frozen experiment contract is absent or inconsistent."""


def init_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_TABLE)
    for guard in _GUARDS:
        conn.execute(guard)


def content_hash(payload: dict) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def build(*, policy_version_id: int, reference_version_id: int | None,
          producer: str, producer_kind: str, report_type: str,
          changed_genes: list[str], observed_genes: list[str],
          minimum_samples: int, brier_tolerance: float,
          opened_at: str) -> dict:
    return {
        "schema_version": 1,
        "policy_version_id": policy_version_id,
        "reference_version_id": reference_version_id,
        "producer": producer,
        "producer_kind": producer_kind,
        "report_type": report_type,
        "changed_genes": list(changed_genes),
        "observed_genes": sorted(observed_genes),
        "evaluator": EVALUATOR,
        "metric": METRIC,
        "minimum_samples": int(minimum_samples),
        "brier_tolerance": float(brier_tolerance),
        "stopping_rule": STOPPING_RULE,
        "horizon_rule": HORIZON_RULE,
        "opened_at": opened_at,
    }


def write(conn: sqlite3.Connection, payload: dict) -> int:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)
    cursor = conn.execute(
        "INSERT INTO shadow_manifests "
        "(policy_version_id, reference_version_id, producer, report_type, "
        " payload_json, content_hash) VALUES (?, ?, ?, ?, ?, ?)",
        (payload["policy_version_id"], payload["reference_version_id"],
         payload["producer"], payload["report_type"], blob,
         content_hash(payload)))
    return int(cursor.lastrowid)


def for_run(conn: sqlite3.Connection, run_id: int) -> dict | None:
    row = conn.execute(
        "SELECT m.* FROM shadow_runs r "
        "JOIN shadow_manifests m ON m.id = r.manifest_id "
        "WHERE r.id = ?", (run_id,)).fetchone()
    if row is None:
        return None
    stored = dict(row)
    try:
        payload = json.loads(stored["payload_json"])
    except (TypeError, ValueError) as exc:
        raise ManifestError(
            f"Shadow run #{run_id} has an unreadable experiment manifest: "
            f"{exc}") from exc
    if content_hash(payload) != stored["content_hash"]:
        raise ManifestError(
            f"Shadow run #{run_id} has a manifest whose content hash no "
            "longer matches. The experiment question changed after opening.")
    return {**payload, "id": stored["id"],
            "content_hash": stored["content_hash"]}


def seal(conn: sqlite3.Connection, *, run_id: int, gate_decision_id: int,
         reason: str, sealed_at: str) -> None:
    row = conn.execute(
        "SELECT sealed_at FROM shadow_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise ManifestError(f"No shadow run #{run_id} to seal.")
    if row["sealed_at"]:
        raise ManifestError(
            f"Shadow run #{run_id} was already sealed at {row['sealed_at']}. "
            "A second look needs a new manifest.")
    conn.execute(
        "UPDATE shadow_runs SET status='closed', sealed_at=?, "
        "gate_decision_id=?, closed_at=COALESCE(closed_at, ?), "
        "reason=reason || ' | sealed: ' || ? WHERE id=?",
        (sealed_at, gate_decision_id, sealed_at, reason, run_id))
