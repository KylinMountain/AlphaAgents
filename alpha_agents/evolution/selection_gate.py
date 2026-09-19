"""Promotion gate for a sealed forward selection shadow.

This module deliberately reuses the repository's persisted gate_decisions
boundary. policy_registry already knows how to require:
- a persisted verdict bound to one policy version;
- enough forward evidence;
- candidate-policy scope;
- a separate human approval;
- a live configuration hash that still matches the version.

What differs here is only the evaluator: paired forward panel-return deltas
instead of paired Brier deltas.
"""

from __future__ import annotations

import json
import math
import sqlite3

from alpha_agents.data import memory_store, policy_registry
from alpha_agents.evolution import holdout_gate, selection_shadow


class SelectionGateError(ValueError):
    pass


def _paired_test(values: list[float]) -> dict:
    n = len(values)
    if n < 2:
        return {
            "n": n,
            "mean_diff": (values[0] if values else None),
            "t_stat": None,
        }
    mean = sum(values) / n
    var = sum((value - mean) ** 2 for value in values) / (n - 1)
    if var <= 0:
        return {
            "n": n,
            "mean_diff": round(mean, 6),
            "t_stat": 0.0 if mean == 0 else None,
        }
    se = math.sqrt(var / n)
    return {
        "n": n,
        "mean_diff": round(mean, 6),
        "t_stat": round(mean / se, 4) if se else None,
    }


def evaluate(run_id: int, conn: sqlite3.Connection | None = None) -> dict:
    """Evaluate exactly the sealed sample; never extend it after looking."""
    conn = conn if conn is not None else memory_store._get_conn()
    selection_shadow.init_schema(conn)
    run = selection_shadow.get_run(run_id, conn)
    if run is None:
        raise SelectionGateError(f"No selection shadow run #{run_id}")
    seal = selection_shadow.seal(run_id, conn)
    if seal is None:
        raise SelectionGateError(
            f"selection shadow run #{run_id} is not sealed")

    manifest = json.loads(run["manifest_json"])
    summary = json.loads(seal["summary_json"])
    sample_count = int(seal["sample_count"])
    floor = max(
        int(manifest["minimum_sets"]),
        int(holdout_gate.GOVERNANCE_MIN_SAMPLES),
    )
    behavior_floor = int(manifest.get("minimum_behavior_changes") or 0)
    mean_floor = float(manifest.get("minimum_mean_delta") or 0.0)

    rows = conn.execute(
        "SELECT day, result_json FROM selection_shadow_rows "
        "WHERE run_id=? ORDER BY id LIMIT ?",
        (run_id, sample_count)).fetchall()
    deltas = []
    days = set()
    changed = 0
    for row in rows:
        result = json.loads(row["result_json"])
        delta = result.get("variant_minus_parent_mean")
        if delta is None:
            continue
        deltas.append(float(delta))
        days.add(str(row["day"]))
        changed += int(result.get("behavior_changed_sets") or 0)

    test = _paired_test(deltas)
    base = {
        "policy_version_id": int(run["variant_version_id"]),
        "n": sample_count,
        "validation_days": len(days),
        "evidence_scope": policy_registry.SCOPE_CANDIDATE,
        "manifest_id": int(run_id),
        "manifest_hash": run["manifest_hash"],
        "mean_diff": test["mean_diff"],
        "t_stat": test["t_stat"],
        "gate_kind": "selection_shadow_v1",
        "selection_shadow_seal_id": int(seal["id"]),
        "selection_shadow_seal_hash": seal["summary_hash"],
        "behavior_changed_sets": changed,
        "minimum_behavior_changes": behavior_floor,
        "minimum_mean_delta": mean_floor,
    }

    if sample_count < floor or len(deltas) < floor:
        return {
            **base,
            "promote": False,
            "outcome": "insufficient",
            "abstained": True,
            "reason": (
                f"forward selection sample {len(deltas)} < {floor}; "
                "keep champion"),
        }
    if changed < behavior_floor:
        return {
            **base,
            "promote": False,
            "outcome": "insufficient",
            "abstained": True,
            "reason": (
                f"selection behavior changed on {changed} set(s), "
                f"below preregistered floor {behavior_floor}; an inert gene "
                "cannot earn promotion evidence"),
        }
    if test["mean_diff"] is None:
        return {
            **base,
            "promote": False,
            "outcome": "insufficient",
            "abstained": True,
            "reason": "no paired panel-return delta is available",
        }

    improved = float(test["mean_diff"]) > mean_floor
    return {
        **base,
        "promote": improved,
        "outcome": "promote" if improved else "reject",
        "abstained": False,
        "reason": (
            f"forward panel mean delta {test['mean_diff']:+.4f}pp "
            f"(t={test['t_stat']}, n={test['n']}); "
            + ("passes" if improved else "does not pass")
            + f" preregistered floor {mean_floor:+.4f}pp"),
        "shadow_summary": summary,
    }


def run_gate(run_id: int, *, today: str | None = None,
             conn: sqlite3.Connection | None = None) -> dict:
    """Persist one gate verdict for one immutable selection-shadow seal."""
    conn = conn if conn is not None else memory_store._get_conn()
    selection_shadow.init_schema(conn)
    run = selection_shadow.get_run(run_id, conn)
    if run is None:
        raise SelectionGateError(f"No selection shadow run #{run_id}")

    candidate_name = f"selection-shadow:{run_id}"
    try:
        previous = conn.execute(
            "SELECT * FROM gate_decisions WHERE candidate=? "
            "AND policy_version_id=? ORDER BY id DESC LIMIT 1",
            (candidate_name, run["variant_version_id"])).fetchone()
    except sqlite3.OperationalError:
        previous = None
    if previous is not None:
        raise SelectionGateError(
            f"selection shadow run #{run_id} already has gate verdict "
            f"#{previous['id']}; the sealed sample is one question, not a "
            "sequence of chances to stop when the answer looks good")

    decision = evaluate(run_id, conn)
    gate_id = holdout_gate.record_gate_decision(
        candidate_name, decision, today=today)
    if gate_id is None:
        raise SelectionGateError("gate verdict could not be persisted")
    decision = dict(decision)
    decision["id"] = int(gate_id)
    return decision
