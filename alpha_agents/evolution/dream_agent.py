"""Dream Agent: cheap historical screening before forward shadow.

This is deliberately narrower than a full-market simulator. The agent replays
frozen historical observations through frozen policy variants and a registered,
non-LLM evaluator. A Dream result is development evidence only: it may retire a
candidate or let it survive to forward shadow, but it can never promote a policy.

The first evaluator is confidence_brier_v1. It re-maps the confidence label
recorded by the champion and compares mean Brier score on the exact same rows.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass

from alpha_agents.data import clock, memory_store, policy_registry, scoring
from alpha_agents.evolution.dream_world import DreamWorld

EVIDENCE_SCOPE = "historical_dream_only"
SURVIVOR = "survivor"
RETIRE = "retire"
INSUFFICIENT = "insufficient"


class DreamError(ValueError):
    """A dream experiment is not causally interpretable."""


@dataclass(frozen=True)
class DreamEvaluator:
    name: str
    metric: str
    observed_genes: frozenset[str]


CONFIDENCE_BRIER = DreamEvaluator(
    name="confidence_brier_v1",
    metric="mean_brier_delta",
    observed_genes=frozenset({"decision.confidence_priors"}),
)

EVALUATORS = {CONFIDENCE_BRIER.name: CONFIDENCE_BRIER}


_TABLE = """
CREATE TABLE IF NOT EXISTS dream_experiments (
    id INTEGER PRIMARY KEY,
    parent_version_id INTEGER NOT NULL,
    variant_version_id INTEGER NOT NULL,
    evaluator TEXT NOT NULL,
    world_hash TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    manifest_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    result_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""

_GUARDS = (
    "CREATE TRIGGER IF NOT EXISTS dream_experiments_no_update "
    "BEFORE UPDATE ON dream_experiments BEGIN "
    "SELECT RAISE(ABORT, 'dream_experiments is append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS dream_experiments_no_delete "
    "BEFORE DELETE ON dream_experiments BEGIN "
    "SELECT RAISE(ABORT, 'dream_experiments is append-only'); END",
)


def init_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_TABLE)
    for statement in _GUARDS:
        conn.execute(statement)


def _hash(payload: dict) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _flatten(value, prefix: str = "") -> dict[str, object]:
    if not isinstance(value, dict):
        return {prefix: value}
    out: dict[str, object] = {}
    for key in sorted(value):
        child = f"{prefix}.{key}" if prefix else str(key)
        out.update(_flatten(value[key], child))
    return out


def changed_genes(parent_version_id: int, variant_version_id: int) -> list[str]:
    parent = policy_registry.get_version(parent_version_id)
    variant = policy_registry.get_version(variant_version_id)
    if parent is None:
        raise DreamError(f"No parent policy version #{parent_version_id}")
    if variant is None:
        raise DreamError(f"No variant policy version #{variant_version_id}")
    explicit_parent = variant.get("parent_id")
    if explicit_parent is not None and int(explicit_parent) != parent_version_id:
        raise DreamError(
            f"variant #{variant_version_id} declares parent #{explicit_parent}, "
            f"not #{parent_version_id}")

    before = _flatten(policy_registry.sources_of(parent_version_id) or {})
    after = _flatten(policy_registry.sources_of(variant_version_id) or {})
    missing = object()
    changed = sorted(
        key for key in (set(before) | set(after))
        if before.get(key, missing) != after.get(key, missing)
    )
    if not changed:
        raise DreamError(
            f"variant #{variant_version_id} changes no frozen policy gene "
            f"relative to parent #{parent_version_id}")
    return changed


def _covered(gene: str, scopes: frozenset[str]) -> bool:
    return any(gene == scope or gene.startswith(scope + ".")
               for scope in scopes)


def _manifest(*, world: DreamWorld, parent_version_id: int,
              variant_version_id: int, evaluator: DreamEvaluator,
              minimum_samples: int, tolerance: float) -> dict:
    changed = changed_genes(parent_version_id, variant_version_id)
    uncovered = [gene for gene in changed
                 if not _covered(gene, evaluator.observed_genes)]
    if uncovered:
        raise DreamError(
            f"evaluator {evaluator.name!r} cannot observe changed gene(s): "
            f"{', '.join(uncovered)}. Dreaming faster cannot repair an "
            "experiment that asks the wrong question.")
    if minimum_samples <= 0:
        raise DreamError("minimum_samples must be positive")
    if tolerance < 0:
        raise DreamError("tolerance cannot be negative")

    return {
        "schema_version": 1,
        "world": world.manifest(),
        "parent_version_id": parent_version_id,
        "variant_version_id": variant_version_id,
        "changed_genes": changed,
        "evaluator": evaluator.name,
        "observable_genes": sorted(evaluator.observed_genes),
        "metric": evaluator.metric,
        "minimum_samples": int(minimum_samples),
        "tolerance": float(tolerance),
        "evidence_scope": EVIDENCE_SCOPE,
        "promotion_eligible": False,
    }


def _confidence_brier(world: DreamWorld, parent_version_id: int,
                      variant_version_id: int) -> dict:
    parent_params = scoring.decision_params_of(parent_version_id)
    variant_params = scoring.decision_params_of(variant_version_id)
    rows = []
    for obs in world.observations:
        parent_prob = scoring.confidence_to_prob(
            obs.confidence, params=parent_params)
        variant_prob = scoring.confidence_to_prob(
            obs.confidence, params=variant_params)
        parent_brier = scoring.brier_score(parent_prob, obs.outcome)
        variant_brier = scoring.brier_score(variant_prob, obs.outcome)
        rows.append({
            "prediction_id": obs.prediction_id,
            "date": obs.date,
            "code": obs.code,
            "confidence": obs.confidence,
            "outcome": obs.outcome,
            "parent_prob": parent_prob,
            "variant_prob": variant_prob,
            "parent_brier": parent_brier,
            "variant_brier": variant_brier,
        })

    n = len(rows)
    if not rows:
        return {
            "n": 0, "parent_mean_brier": None,
            "variant_mean_brier": None, "delta_brier": None, "rows": [],
        }
    parent_mean = sum(r["parent_brier"] for r in rows) / n
    variant_mean = sum(r["variant_brier"] for r in rows) / n
    return {
        "n": n,
        "parent_mean_brier": round(parent_mean, 6),
        "variant_mean_brier": round(variant_mean, 6),
        "delta_brier": round(variant_mean - parent_mean, 6),
        "rows": rows,
    }


def _evaluate(world: DreamWorld, parent_version_id: int,
              variant_version_id: int, evaluator: DreamEvaluator) -> dict:
    if evaluator.name == CONFIDENCE_BRIER.name:
        return _confidence_brier(
            world, parent_version_id, variant_version_id)
    raise DreamError(f"No implementation for evaluator {evaluator.name!r}")


def _record(conn: sqlite3.Connection, manifest: dict, result: dict) -> int:
    init_schema(conn)
    manifest_blob = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    result_blob = json.dumps(
        result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    cursor = conn.execute(
        "INSERT INTO dream_experiments "
        "(parent_version_id, variant_version_id, evaluator, world_hash, "
        " manifest_json, manifest_hash, result_json, result_hash, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (manifest["parent_version_id"], manifest["variant_version_id"],
         manifest["evaluator"], manifest["world"]["world_hash"],
         manifest_blob, _hash(manifest), result_blob, _hash(result),
         clock.now().isoformat()))
    return int(cursor.lastrowid)


def evaluate_variant(*, world: DreamWorld, parent_version_id: int,
                     variant_version_id: int,
                     evaluator_name: str = CONFIDENCE_BRIER.name,
                     minimum_samples: int = 20, tolerance: float = 0.0,
                     conn: sqlite3.Connection | None = None) -> dict:
    """Replay one frozen variant on one frozen historical world.

    Survivor means only "worth paying forward-shadow cost". Retire means the
    variant failed this historical screen. Neither outcome is promotable.
    """
    evaluator = EVALUATORS.get(evaluator_name)
    if evaluator is None:
        raise DreamError(
            f"Unknown Dream evaluator {evaluator_name!r}; registered: "
            f"{sorted(EVALUATORS)}")
    manifest = _manifest(
        world=world, parent_version_id=parent_version_id,
        variant_version_id=variant_version_id, evaluator=evaluator,
        minimum_samples=minimum_samples, tolerance=tolerance)
    measured = _evaluate(
        world, parent_version_id, variant_version_id, evaluator)

    if measured["n"] < minimum_samples:
        outcome = INSUFFICIENT
        reason = (
            f"only {measured['n']} historical sample(s); "
            f"need {minimum_samples}")
    elif measured["delta_brier"] < -tolerance:
        outcome = SURVIVOR
        reason = (
            f"variant improved mean Brier by "
            f"{abs(measured['delta_brier']):.6f} on the fixed DreamWorld")
    else:
        outcome = RETIRE
        reason = (
            f"variant did not improve mean Brier beyond tolerance "
            f"{tolerance:.6f}; delta={measured['delta_brier']:.6f}")

    result = {
        "outcome": outcome,
        "reason": reason,
        "evidence_scope": EVIDENCE_SCOPE,
        "promotion_eligible": False,
        "world_hash": world.world_hash,
        "parent_version_id": parent_version_id,
        "variant_version_id": variant_version_id,
        "evaluator": evaluator.name,
        "metric": evaluator.metric,
        **measured,
    }

    if conn is None:
        with memory_store._write_lock:
            target = memory_store._get_conn()
            with target:
                experiment_id = _record(target, manifest, result)
    else:
        with conn:
            experiment_id = _record(conn, manifest, result)
    return {
        "experiment_id": experiment_id,
        "manifest": manifest,
        "manifest_hash": _hash(manifest),
        "result": result,
        "result_hash": _hash(result),
    }


def experiments(conn: sqlite3.Connection | None = None) -> list[dict]:
    conn = conn if conn is not None else memory_store._get_conn()
    init_schema(conn)
    rows = conn.execute(
        "SELECT * FROM dream_experiments ORDER BY id").fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["manifest"] = json.loads(item.pop("manifest_json"))
        item["result"] = json.loads(item.pop("result_json"))
        out.append(item)
    return out


class DreamAgent:
    """Orchestrate multiple frozen variants on exactly the same DreamWorld."""

    def screen(self, *, world: DreamWorld, parent_version_id: int,
               variant_version_ids: list[int],
               evaluator_name: str = CONFIDENCE_BRIER.name,
               minimum_samples: int = 20, tolerance: float = 0.0,
               conn: sqlite3.Connection | None = None) -> dict:
        results = []
        refused = []
        for version_id in variant_version_ids:
            try:
                results.append(evaluate_variant(
                    world=world,
                    parent_version_id=parent_version_id,
                    variant_version_id=version_id,
                    evaluator_name=evaluator_name,
                    minimum_samples=minimum_samples,
                    tolerance=tolerance,
                    conn=conn))
            except DreamError as exc:
                refused.append({
                    "variant_version_id": version_id,
                    "reason": str(exc),
                })

        def ids(outcome: str) -> list[int]:
            return [
                item["result"]["variant_version_id"] for item in results
                if item["result"]["outcome"] == outcome
            ]

        return {
            "world_hash": world.world_hash,
            "parent_version_id": parent_version_id,
            "evaluator": evaluator_name,
            "survivors": ids(SURVIVOR),
            "retired": ids(RETIRE),
            "insufficient": ids(INSUFFICIENT),
            "refused": refused,
            "experiments": results,
            "promotion_eligible": False,
            "next_step": "survivors may enter forward shadow; Dream never promotes",
        }
