"""Promotion-grade forward evidence for the frozen Sector-First protocol.

Historical matrix comparisons and RP-09 observation-only rows cannot enter
this store.  A run preregisters an ordered set of future sample IDs, freezes
all implementation identities, accepts exactly that sample, and produces at
most one gate verdict.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import sqlite3
import threading

from alpha_agents.data import memory_store, policy_registry
from alpha_agents.evolution import dream_agent, gene_registry, holdout_gate


EVIDENCE_SCOPE = policy_registry.SCOPE_CANDIDATE
PRODUCER = "sector_rank_price_v1"
EVALUATOR = "sector_forward_portfolio_v1"
EVALUATOR_VERSION = "1"
MINIMUM_SAMPLES = 50


class SectorForwardError(ValueError):
    pass


_LOCK = threading.RLock()


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _hash(value) -> str:
    return hashlib.sha256(_dump(value).encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


_RUNS = """
CREATE TABLE IF NOT EXISTS sector_forward_runs (
    id INTEGER PRIMARY KEY,
    parent_version_id INTEGER NOT NULL,
    candidate_version_id INTEGER NOT NULL,
    registered_at TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    manifest_hash TEXT NOT NULL UNIQUE
)
"""

_ROWS = """
CREATE TABLE IF NOT EXISTS sector_forward_rows (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL,
    sample_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    decision_at TEXT NOT NULL,
    result_json TEXT NOT NULL,
    result_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, sample_id),
    UNIQUE(run_id, ordinal),
    FOREIGN KEY(run_id) REFERENCES sector_forward_runs(id)
)
"""

_SEALS = """
CREATE TABLE IF NOT EXISTS sector_forward_seals (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL UNIQUE,
    sample_count INTEGER NOT NULL,
    sample_ids_json TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    summary_hash TEXT NOT NULL,
    sealed_at TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES sector_forward_runs(id)
)
"""

_CI = """
CREATE TABLE IF NOT EXISTS sector_forward_ci (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL UNIQUE,
    artifact_json TEXT NOT NULL,
    artifact_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES sector_forward_runs(id)
)
"""


def init_schema(conn: sqlite3.Connection) -> None:
    for statement in (_RUNS, _ROWS, _SEALS, _CI):
        conn.execute(statement)
    for table in ("sector_forward_runs", "sector_forward_rows",
                  "sector_forward_seals", "sector_forward_ci"):
        conn.execute(
            f"CREATE TRIGGER IF NOT EXISTS {table}_no_update BEFORE UPDATE "
            f"ON {table} BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END")
        conn.execute(
            f"CREATE TRIGGER IF NOT EXISTS {table}_no_delete BEFORE DELETE "
            f"ON {table} BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END")


def _nonempty_hash(value: str, field: str, *, lengths=(64,)) -> str:
    text = str(value or "").strip()
    if (len(text) not in lengths
            or any(ch not in "0123456789abcdef" for ch in text.lower())):
        sizes = " or ".join(str(length) for length in lengths)
        raise SectorForwardError(f"{field} must be a {sizes}-character hex digest")
    return text.lower()


def open_run(*, parent_version_id: int, candidate_version_id: int,
             sample_ids: list[str], protocol_hash: str, code_hash: str,
             data_hash: str, evaluator_hash: str,
             required_ci_checks: list[str],
             maximum_drawdown_pct: float, maximum_tail_loss_pct: float,
             maximum_concentration_pct: float,
             minimum_mean_delta_pp: float = 0.0,
             minimum_behavior_changes: int = 1,
             minimum_samples: int = MINIMUM_SAMPLES,
             conn: sqlite3.Connection | None = None) -> int:
    """Register one immutable future experiment using the trusted UTC clock."""
    if minimum_samples < MINIMUM_SAMPLES:
        raise SectorForwardError(
            f"minimum_samples cannot be below {MINIMUM_SAMPLES}")
    if len(sample_ids) < minimum_samples or len(set(sample_ids)) != len(sample_ids):
        raise SectorForwardError(
            "sample_ids must be unique and cover the preregistered floor")
    registered = _utc_now()
    registered_day = registered[:10]
    for sample_id in sample_ids:
        if not isinstance(sample_id, str) or len(sample_id) < 12:
            raise SectorForwardError("sample IDs must begin YYYY-MM-DD/")
        try:
            datetime.strptime(sample_id[:10], "%Y-%m-%d")
        except ValueError as exc:
            raise SectorForwardError("sample IDs must begin YYYY-MM-DD/") from exc
        if sample_id[10] != "/" or sample_id[:10] <= registered_day:
            raise SectorForwardError(
                "every preregistered sample must be strictly after registration")

    changed = dream_agent.changed_genes(parent_version_id, candidate_version_id)
    try:
        gene_registry.assert_exact_coverage(
            changed, gene_registry.SELECTION_RANK_GENES,
            actor=f"producer {PRODUCER!r} and evaluator {EVALUATOR!r}")
    except gene_registry.GeneRegistryError as exc:
        raise SectorForwardError(str(exc)) from exc
    candidate = policy_registry.get_version(candidate_version_id)
    if candidate is None:
        raise SectorForwardError(f"No candidate policy #{candidate_version_id}")
    limits = {
        "maximum_drawdown_pct": float(maximum_drawdown_pct),
        "maximum_tail_loss_pct": float(maximum_tail_loss_pct),
        "maximum_concentration_pct": float(maximum_concentration_pct),
    }
    if any(value < 0 for value in limits.values()):
        raise SectorForwardError("risk limits cannot be negative")
    if minimum_behavior_changes <= 0:
        raise SectorForwardError("minimum_behavior_changes must be positive")
    ci_checks = [str(name).strip() for name in required_ci_checks]
    if (not ci_checks or any(not name for name in ci_checks)
            or len(set(ci_checks)) != len(ci_checks)):
        raise SectorForwardError("required_ci_checks must be unique nonempty names")

    manifest = {
        "schema_version": 1,
        "parent_version_id": parent_version_id,
        "candidate_version_id": candidate_version_id,
        "candidate_hash": candidate["content_hash"],
        "registered_at": registered,
        "sample_ids": list(sample_ids),
        "minimum_samples": int(minimum_samples),
        "changed_genes": changed,
        "producer": PRODUCER,
        "producer_genes": sorted(gene_registry.SELECTION_RANK_GENES),
        "evaluator": EVALUATOR,
        "evaluator_version": EVALUATOR_VERSION,
        "evaluator_genes": sorted(gene_registry.SELECTION_RANK_GENES),
        "protocol_hash": _nonempty_hash(protocol_hash, "protocol_hash"),
        "code_hash": _nonempty_hash(code_hash, "code_hash", lengths=(40, 64)),
        "data_hash": _nonempty_hash(data_hash, "data_hash"),
        "evaluator_hash": _nonempty_hash(evaluator_hash, "evaluator_hash"),
        "required_ci_checks": ci_checks,
        "risk_limits": limits,
        "minimum_mean_delta_pp": float(minimum_mean_delta_pp),
        "minimum_behavior_changes": int(minimum_behavior_changes),
        "missing_rule": "planned sample remains missing and blocks sealing",
        "stopping_rule": "seal exactly the first N preregistered sample IDs",
        "evidence_scope": EVIDENCE_SCOPE,
    }
    target = conn if conn is not None else memory_store._get_conn()
    init_schema(target)
    with target:
        cur = target.execute(
            "INSERT INTO sector_forward_runs "
            "(parent_version_id,candidate_version_id,registered_at,"
            "manifest_json,manifest_hash) VALUES (?,?,?,?,?)",
            (parent_version_id, candidate_version_id, registered,
             _dump(manifest), _hash(manifest)))
    return int(cur.lastrowid)


def _run(conn: sqlite3.Connection, run_id: int) -> tuple[dict, dict]:
    row = conn.execute(
        "SELECT * FROM sector_forward_runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        raise SectorForwardError(f"No Sector-First forward run #{run_id}")
    stored = dict(row)
    manifest = json.loads(stored["manifest_json"])
    if _hash(manifest) != stored["manifest_hash"]:
        raise SectorForwardError("forward manifest hash mismatch")
    return stored, manifest


def ingest(*, run_id: int, samples: list[dict],
           conn: sqlite3.Connection | None = None) -> dict:
    """Idempotently store planned results and seal only the exact prefix N."""
    target = conn if conn is not None else memory_store._get_conn()
    init_schema(target)
    with _LOCK, target:
        run, manifest = _run(target, run_id)
        sealed = target.execute(
            "SELECT * FROM sector_forward_seals WHERE run_id=?", (run_id,)
        ).fetchone()
        if sealed is not None:
            return {"run_id": run_id, "new_rows": 0, "sealed": True,
                    "sample_count": int(sealed["sample_count"])}
        plan = list(manifest["sample_ids"])
        floor = int(manifest["minimum_samples"])
        allowed = {sample_id: ordinal for ordinal, sample_id in enumerate(plan)}
        before_changes = target.total_changes
        for sample in samples:
            sample_id = str(sample.get("sample_id") or "")
            if sample_id not in allowed:
                raise SectorForwardError(
                    f"sample {sample_id!r} was not preregistered")
            if allowed[sample_id] >= floor:
                continue
            decision_at = str(sample.get("decision_at") or "")
            try:
                decided = datetime.fromisoformat(decision_at)
                observed = datetime.fromisoformat(_utc_now())
            except ValueError as exc:
                raise SectorForwardError(
                    "decision_at must be an ISO-8601 timestamp") from exc
            if decided.tzinfo is None:
                raise SectorForwardError("decision_at must include a timezone")
            if decision_at[:10] != sample_id[:10]:
                raise SectorForwardError(
                    "decision_at must belong to the preregistered sample day")
            if decision_at <= str(run["registered_at"]):
                raise SectorForwardError("candidate-forward samples cannot be backfilled")
            if decided > observed:
                raise SectorForwardError(
                    "future samples cannot be ingested before decision_at")
            if sample.get("evidence_scope") != EVIDENCE_SCOPE:
                raise SectorForwardError(
                    "historical or observation-only evidence cannot enter this run")
            _assert_sample_identity(sample, run, manifest)
            result = _evaluate_sample(sample)
            result_json = _dump(result)
            result_hash = _hash(result)
            existing = target.execute(
                "SELECT decision_at,result_hash FROM sector_forward_rows "
                "WHERE run_id=? AND sample_id=?", (run_id, sample_id)
            ).fetchone()
            if existing is not None:
                if (existing["decision_at"] != decision_at
                        or existing["result_hash"] != result_hash):
                    raise SectorForwardError(
                        "conflicting replay for an accepted forward sample")
                continue
            target.execute(
                "INSERT OR IGNORE INTO sector_forward_rows "
                "(run_id,sample_id,ordinal,decision_at,result_json,result_hash,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (run_id, sample_id, allowed[sample_id], decision_at,
                 result_json, result_hash, _utc_now()))

        rows = target.execute(
            "SELECT * FROM sector_forward_rows WHERE run_id=? ORDER BY ordinal",
            (run_id,)).fetchall()
        present = {int(row["ordinal"]): row for row in rows}
        ready = all(ordinal in present for ordinal in range(floor))
        if ready:
            ordered = [present[ordinal] for ordinal in range(floor)]
            _seal(target, run_id, manifest, ordered)
        count = target.execute(
            "SELECT COUNT(*) n FROM sector_forward_rows WHERE run_id=?",
            (run_id,)).fetchone()["n"]
        inserted = target.total_changes - before_changes
        # Sealing itself is one insert and is not a newly accepted sample.
        if ready:
            inserted -= 1
        return {"run_id": run_id, "new_rows": inserted,
                "sample_count": int(count), "sealed": ready}


def _seal(conn, run_id: int, manifest: dict, rows) -> None:
    floor = int(manifest["minimum_samples"])
    if len(rows) != floor:
        raise SectorForwardError("seal requires exactly the preregistered floor")
    results = [json.loads(row["result_json"]) for row in rows]
    summary = {
        "run_id": run_id,
        "sample_count": floor,
        "sample_ids": [row["sample_id"] for row in rows],
        "mean_return_delta_pp": round(
            sum(item["return_delta_pp"] for item in results) / floor, 8),
        "behavior_changed_samples": sum(
            1 for item in results if item["behavior_changed"]),
        "maximum_drawdown_pct": max(
            item["maximum_drawdown_pct"] for item in results),
        "maximum_tail_loss_pct": max(item["tail_loss_pct"] for item in results),
        "maximum_concentration_pct": max(
            item["maximum_concentration_pct"] for item in results),
        "evidence_scope": EVIDENCE_SCOPE,
    }
    conn.execute(
        "INSERT INTO sector_forward_seals "
        "(run_id,sample_count,sample_ids_json,summary_json,summary_hash,sealed_at) "
        "VALUES (?,?,?,?,?,?)",
        (run_id, floor, _dump(summary["sample_ids"]), _dump(summary),
         _hash(summary), _utc_now()))


def _finite_series(sample: dict, field: str, *, positive=False) -> list[float]:
    raw = sample.get(field)
    if not isinstance(raw, list) or not raw:
        raise SectorForwardError(f"{field} must be a nonempty list")
    try:
        values = [float(value) for value in raw]
    except (TypeError, ValueError) as exc:
        raise SectorForwardError(f"{field} must contain numbers") from exc
    if any(not math.isfinite(value) for value in values):
        raise SectorForwardError(f"{field} contains a non-finite value")
    if positive and any(value <= 0 for value in values):
        raise SectorForwardError(f"{field} must contain positive values")
    return values


def _assert_sample_identity(sample: dict, run: dict, manifest: dict) -> None:
    expected = {
        "manifest_hash": run["manifest_hash"],
        "candidate_hash": manifest["candidate_hash"],
        "protocol_hash": manifest["protocol_hash"],
        "code_hash": manifest["code_hash"],
        "data_hash": manifest["data_hash"],
        "evaluator_hash": manifest["evaluator_hash"],
    }
    mismatched = [field for field, value in expected.items()
                  if sample.get(field) != value]
    if mismatched:
        raise SectorForwardError(
            "sample identity does not match the frozen manifest: "
            + ", ".join(mismatched))


def _evaluate_sample(sample: dict) -> dict:
    """Compute the registered portfolio metrics from immutable raw paths."""
    parent = _finite_series(sample, "parent_equity", positive=True)
    candidate = _finite_series(sample, "candidate_equity", positive=True)
    concentrations = _finite_series(sample, "candidate_concentration_pct")
    if len(parent) < 2 or len(candidate) < 2:
        raise SectorForwardError("equity paths need an initial and final mark")
    if len(parent) != len(candidate) or len(concentrations) != len(candidate):
        raise SectorForwardError(
            "parent, candidate and concentration paths must align")
    if any(value < 0 or value > 100 for value in concentrations):
        raise SectorForwardError("candidate concentration must be within 0..100")
    parent_return = (parent[-1] / parent[0] - 1.0) * 100.0
    candidate_return = (candidate[-1] / candidate[0] - 1.0) * 100.0
    peak = candidate[0]
    drawdowns = []
    daily_returns = []
    for previous, current in zip(candidate, candidate[1:]):
        peak = max(peak, current)
        drawdowns.append((peak - current) / peak * 100.0)
        daily_returns.append((current / previous - 1.0) * 100.0)
    parent_decision = _nonempty_hash(
        sample.get("parent_decision_hash"), "parent_decision_hash")
    candidate_decision = _nonempty_hash(
        sample.get("candidate_decision_hash"), "candidate_decision_hash")
    return {
        "return_delta_pp": round(candidate_return - parent_return, 8),
        "maximum_drawdown_pct": round(max(drawdowns, default=0.0), 8),
        "tail_loss_pct": round(max(0.0, -min(daily_returns, default=0.0)), 8),
        "maximum_concentration_pct": round(max(concentrations), 8),
        "behavior_changed": parent_decision != candidate_decision,
        "parent_decision_hash": parent_decision,
        "candidate_decision_hash": candidate_decision,
    }


def record_ci(*, run_id: int, commit_hash: str, checks: list[dict],
              manifest_hash: str, candidate_hash: str,
              conn: sqlite3.Connection | None = None) -> int:
    """Bind one complete successful CI artifact to the frozen experiment."""
    target = conn if conn is not None else memory_store._get_conn()
    init_schema(target)
    run, manifest = _run(target, run_id)
    if manifest_hash != run["manifest_hash"]:
        raise SectorForwardError("CI artifact is bound to another manifest")
    if candidate_hash != manifest["candidate_hash"]:
        raise SectorForwardError("CI artifact is bound to another candidate")
    if commit_hash != manifest["code_hash"]:
        raise SectorForwardError("CI artifact is bound to another code revision")
    names = [str(item.get("name") or "").strip() for item in checks]
    required = list(manifest["required_ci_checks"])
    if (sorted(names) != sorted(required)
            or len(set(names)) != len(names)
            or any(item.get("conclusion") != "success" for item in checks)):
        raise SectorForwardError("every preregistered CI check must succeed")
    artifact = {
        "commit_hash": _nonempty_hash(
            commit_hash, "commit_hash", lengths=(40, 64)),
        "manifest_hash": manifest_hash,
        "candidate_hash": candidate_hash,
        "checks": checks,
        "complete": True,
    }
    with target:
        cur = target.execute(
            "INSERT INTO sector_forward_ci "
            "(run_id,artifact_json,artifact_hash,created_at) VALUES (?,?,?,?)",
            (run_id, _dump(artifact), _hash(artifact), _utc_now()))
    return int(cur.lastrowid)


def evaluate(run_id: int, conn: sqlite3.Connection | None = None) -> dict:
    target = conn if conn is not None else memory_store._get_conn()
    init_schema(target)
    run, manifest = _run(target, run_id)
    seal = target.execute(
        "SELECT * FROM sector_forward_seals WHERE run_id=?", (run_id,)
    ).fetchone()
    if seal is None:
        raise SectorForwardError("forward sample is not sealed")
    ci = target.execute(
        "SELECT * FROM sector_forward_ci WHERE run_id=?", (run_id,)
    ).fetchone()
    if ci is None:
        raise SectorForwardError("successful bound CI artifact is required")
    summary = json.loads(seal["summary_json"])
    limits = manifest["risk_limits"]
    breaches = []
    for metric, limit_name in (
            ("maximum_drawdown_pct", "maximum_drawdown_pct"),
            ("maximum_tail_loss_pct", "maximum_tail_loss_pct"),
            ("maximum_concentration_pct", "maximum_concentration_pct")):
        if float(summary[metric]) > float(limits[limit_name]):
            breaches.append(
                f"{metric}={summary[metric]} > {limits[limit_name]}")
    behavior_ok = int(summary["behavior_changed_samples"]) >= int(
        manifest["minimum_behavior_changes"])
    improved = float(summary["mean_return_delta_pp"]) > float(
        manifest["minimum_mean_delta_pp"])
    promote = improved and behavior_ok and not breaches
    reason = (
        "passes preregistered return, behavior, risk and CI gates" if promote
        else "; ".join(breaches or (["no behavior change"] if not behavior_ok
                                    else ["return delta did not improve"])))
    return {
        "policy_version_id": int(run["candidate_version_id"]),
        "n": int(seal["sample_count"]),
        "validation_days": len({sample_id[:10] for sample_id in summary["sample_ids"]}),
        "evidence_scope": EVIDENCE_SCOPE,
        "manifest_id": int(run_id),
        "manifest_hash": run["manifest_hash"],
        "mean_diff": summary["mean_return_delta_pp"],
        "t_stat": None,
        "promote": promote,
        "outcome": "promote" if promote else "reject",
        "abstained": False,
        "reason": reason,
        "gate_kind": EVALUATOR,
        "seal_hash": seal["summary_hash"],
        "ci_artifact_hash": ci["artifact_hash"],
        "risk_breaches": breaches,
        "summary": summary,
    }


def run_gate(run_id: int, conn: sqlite3.Connection | None = None) -> dict:
    """Persist exactly one verdict for the immutable sealed experiment."""
    target = conn if conn is not None else memory_store._get_conn()
    init_schema(target)
    run, _manifest = _run(target, run_id)
    try:
        previous = target.execute(
            "SELECT id FROM gate_decisions WHERE manifest_id=? "
            "AND manifest_hash=? LIMIT 1", (run_id, run["manifest_hash"])
        ).fetchone()
    except sqlite3.OperationalError:
        previous = None
    if previous is not None:
        raise SectorForwardError("sealed forward experiment already has a verdict")
    decision = evaluate(run_id, target)
    gate_id = holdout_gate.record_gate_decision(
        f"sector-forward:{run_id}", decision)
    if gate_id is None:
        raise SectorForwardError("gate verdict could not be persisted")
    return {**decision, "id": int(gate_id)}
