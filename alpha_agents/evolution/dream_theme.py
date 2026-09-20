"""Theme-gate counterfactuals over real opportunity sets.

This is a module-level Dream evaluator. It holds the selected intent and market
outcome fixed, changes only the theme-gate parameters, and asks whether the gate
would admit or refuse that same intent.

It does not simulate the downstream book after a flip and is never promotion
evidence by itself.
"""

from __future__ import annotations

from alpha_agents.data import memory_store, policy_registry, scoring
from alpha_agents.evolution import dream_agent, gene_registry
from alpha_agents.evolution.dream_world import OpportunityDreamWorld

EVIDENCE_SCOPE = "historical_dream_only"


class ThemeDreamError(ValueError):
    """The fixed world cannot support the requested theme intervention."""


def _version_ref(version_id: int) -> str:
    version = policy_registry.get_version(version_id)
    if version is None:
        raise ThemeDreamError(f"No policy version #{version_id}")
    return (
        f"{version['policy_key']}#{version['id']}@"
        f"{version['content_hash'][:12]}")


def _ret(item, horizon: int) -> float | None:
    value = (item.outcome.get("forward_close_return_pct") or {}).get(
        str(horizon))
    return float(value) if value is not None else None


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def _snapshot(conn, theme: str, cutoff: str):
    try:
        return conn.execute(
            "SELECT as_of, score, flow_pct, rel_pct, confirm, source "
            "FROM theme_score_history WHERE theme=? AND as_of<=? "
            "ORDER BY as_of DESC LIMIT 1", (theme, cutoff)).fetchone()
    except Exception as exc:
        raise ThemeDreamError(
            f"theme_score_history is unavailable: {exc}") from exc


def _verdict(snapshot, version_id: int) -> dict:
    params = scoring.decision_params_of(version_id)
    gate = params.get("theme_gate") or {}
    required = ("w_flow", "w_rel", "w_confirm", "admit_score")
    missing = [name for name in required if gate.get(name) is None]
    if missing:
        raise ThemeDreamError(
            f"policy version #{version_id} lacks theme gate fields {missing}")
    components = {
        "flow_pct": snapshot["flow_pct"],
        "rel_pct": snapshot["rel_pct"],
        "confirm": snapshot["confirm"],
    }
    if any(value is None for value in components.values()):
        raise ThemeDreamError(
            "theme snapshot lacks one of flow_pct/rel_pct/confirm")
    score = (
        float(gate["w_flow"]) * float(components["flow_pct"]) +
        float(gate["w_rel"]) * float(components["rel_pct"]) +
        float(gate["w_confirm"]) * float(components["confirm"])
    )
    bar = float(gate["admit_score"])
    return {
        "score": round(score, 6),
        "admit_score": bar,
        "admitted": score >= bar,
        **components,
    }


def compare(*, world: OpportunityDreamWorld, parent_version_id: int,
            variant_version_id: int, horizon: int = 5,
            conn=None) -> dict:
    """Compare one theme-gate variant on intents selected in a fixed world."""
    changed = dream_agent.changed_genes(
        parent_version_id, variant_version_id)
    try:
        gene_registry.assert_exact_coverage(
            changed, gene_registry.THEME_GATE_GENES,
            actor="theme evaluator")
    except gene_registry.GeneRegistryError as exc:
        raise ThemeDreamError(str(exc)) from exc

    parent_ref = _version_ref(parent_version_id)
    conn = conn if conn is not None else memory_store._get_conn()

    rows = []
    missing_theme = 0
    missing_snapshot = 0
    wrong_parent = 0
    missing_outcome = 0

    for group in world.sets:
        if group.parse_error:
            continue
        if group.policy_ref != parent_ref:
            wrong_parent += 1
            continue
        theme = str(group.context.get("run_theme") or "").strip()
        if not theme:
            missing_theme += 1
            continue
        snapshot = _snapshot(conn, theme, group.information_cutoff)
        if snapshot is None:
            missing_snapshot += 1
            continue

        parent = _verdict(snapshot, parent_version_id)
        variant = _verdict(snapshot, variant_version_id)
        for item in group.items:
            if not item.selected:
                continue
            ret = _ret(item, horizon)
            if ret is None:
                missing_outcome += 1
                continue
            rows.append({
                "set_id": group.set_id,
                "day": group.day,
                "code": item.code,
                "theme": theme,
                "theme_as_of": snapshot["as_of"],
                "return_pct": ret,
                "parent": parent,
                "variant": variant,
                "changed": parent["admitted"] != variant["admitted"],
            })

    flips = [row for row in rows if row["changed"]]
    newly_blocked = [
        row["return_pct"] for row in flips
        if row["parent"]["admitted"] and not row["variant"]["admitted"]
    ]
    newly_admitted = [
        row["return_pct"] for row in flips
        if not row["parent"]["admitted"] and row["variant"]["admitted"]
    ]
    parent_admitted = [
        row["return_pct"] for row in rows if row["parent"]["admitted"]
    ]
    variant_admitted = [
        row["return_pct"] for row in rows if row["variant"]["admitted"]
    ]

    return {
        "world_hash": world.world_hash,
        "parent_version_id": parent_version_id,
        "variant_version_id": variant_version_id,
        "changed_genes": changed,
        "horizon": horizon,
        "evidence_scope": EVIDENCE_SCOPE,
        "promotion_eligible": False,
        "comparable_intents": len(rows),
        "behavior_flips": len(flips),
        "flip_rate": round(len(flips) / len(rows), 4) if rows else None,
        "newly_blocked": {
            "n": len(newly_blocked), "mean_return": _mean(newly_blocked)},
        "newly_admitted": {
            "n": len(newly_admitted), "mean_return": _mean(newly_admitted)},
        "parent_admitted": {
            "n": len(parent_admitted), "mean_return": _mean(parent_admitted)},
        "variant_admitted": {
            "n": len(variant_admitted), "mean_return": _mean(variant_admitted)},
        "coverage": {
            "wrong_parent_policy_sets": wrong_parent,
            "missing_theme_sets": missing_theme,
            "missing_theme_snapshot_sets": missing_snapshot,
            "missing_outcome_items": missing_outcome,
        },
        "rows": rows,
        "note": (
            "Same selected intent, same theme snapshot, different gate "
            "parameters. This measures admission only, not downstream P&L."),
    }
