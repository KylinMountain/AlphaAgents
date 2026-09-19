"""Selection diagnostics and deterministic ranking baselines for Dream RSI.

This module evaluates a fixed OpportunityDreamWorld. It does not create policy
versions and cannot promote anything. The first job is to measure whether the
Trader's actual selection added value relative to the opportunities it saw.
"""

from __future__ import annotations

import statistics

from alpha_agents.config import DATA_DIR
from alpha_agents.data import opportunity_outcomes, scoring, selection_policy
from alpha_agents.evolution import dream_agent
from alpha_agents.evolution.dream_world import OpportunityDreamWorld

EVIDENCE_SCOPE = "historical_dream_only"


def _ret(item, horizon: int) -> float | None:
    value = (item.outcome.get("forward_close_return_pct") or {}).get(
        str(horizon))
    return float(value) if value is not None else None


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def _median(values: list[float]) -> float | None:
    return round(float(statistics.median(values)), 4) if values else None


def _summary(values: list[float]) -> dict:
    return {"n": len(values), "mean": _mean(values), "median": _median(values)}


def selection_skill(world: OpportunityDreamWorld, *, horizon: int = 5) -> dict:
    """Measure the observed selector without inventing a counterfactual cause."""
    selected_all = []
    researched_not_selected = []
    not_researched = []
    all_items = []
    regrets = []
    above_median = 0
    comparable_sets = 0
    abstained_sets = 0
    unreadable_sets = 0
    abstention_best = []

    for group in world.sets:
        if group.parse_error:
            unreadable_sets += 1
            continue
        pairs = [(item, _ret(item, horizon)) for item in group.items]
        pairs = [(item, ret) for item, ret in pairs if ret is not None]
        if not pairs:
            continue
        values = [ret for _, ret in pairs]
        all_items.extend(values)
        for item, ret in pairs:
            if item.selected:
                selected_all.append(ret)
            elif item.status == "researched_not_selected":
                researched_not_selected.append(ret)
            elif item.status == "offered_not_researched":
                not_researched.append(ret)

        chosen = [ret for item, ret in pairs if item.selected]
        if not chosen:
            abstained_sets += 1
            abstention_best.append(max(values))
            continue
        comparable_sets += 1
        best_selected = max(chosen)
        best_panel = max(values)
        regrets.append(best_panel - best_selected)
        if best_selected > statistics.median(values):
            above_median += 1

    selected_mean = _mean(selected_all)
    panel_mean = _mean(all_items)
    return {
        "world_hash": world.world_hash,
        "horizon": horizon,
        "evidence_scope": EVIDENCE_SCOPE,
        "promotion_eligible": False,
        "sets": world.n_sets,
        "items": world.n_items,
        "comparable_sets": comparable_sets,
        "abstained_sets": abstained_sets,
        "unreadable_sets": unreadable_sets,
        "selected": _summary(selected_all),
        "researched_not_selected": _summary(researched_not_selected),
        "offered_not_researched": _summary(not_researched),
        "panel": _summary(all_items),
        "selected_lift_vs_panel_mean": (
            round(selected_mean - panel_mean, 4)
            if selected_mean is not None and panel_mean is not None else None),
        "mean_regret": _mean(regrets),
        "selected_above_panel_median_rate": (
            round(above_median / comparable_sets, 4)
            if comparable_sets else None),
        "abstention_best_available": _summary(abstention_best),
    }


def preselection_skill(world: OpportunityDreamWorld, *,
                       horizon: int = 5) -> dict:
    """Evaluate stock choice before the shared trade planner.

    OpportunityObservation.selected means a final order survived planning.
    Sector-first runs additionally freeze context.stock_preselection: names
    chosen by the stock selector before entry/stop/target construction.

    A missing context key is different from an explicit empty selection.
    Incumbent dual-rank runs have no stock-preselection stage and therefore
    report available=False instead of being treated as abstentions.
    """
    preselected_all = []
    panel_all = []
    regrets = []
    above_median = 0
    comparable_sets = 0
    abstained_sets = 0
    missing_context_sets = 0
    unreadable_sets = 0
    final_from_preselected = 0
    preselected_count = 0
    planner_rejected_returns = []

    for group in world.sets:
        if group.parse_error:
            unreadable_sets += 1
            continue
        if "stock_preselection" not in group.context:
            missing_context_sets += 1
            continue

        selected_codes = {
            str(code) for code in (
                group.context.get("stock_preselection") or [])
        }
        pairs = [(item, _ret(item, horizon)) for item in group.items]
        pairs = [(item, ret) for item, ret in pairs if ret is not None]
        if not pairs:
            continue

        values = [ret for _, ret in pairs]
        panel_all.extend(values)
        chosen_pairs = [
            (item, ret) for item, ret in pairs
            if item.code in selected_codes
        ]

        if not selected_codes:
            abstained_sets += 1
            continue
        if not chosen_pairs:
            continue

        comparable_sets += 1
        chosen = [ret for _, ret in chosen_pairs]
        preselected_all.extend(chosen)
        preselected_count += len(chosen_pairs)
        final_codes = {
            item.code for item, _ret_value in pairs if item.selected
        }
        final_from_preselected += sum(
            item.code in final_codes for item, _ret_value in chosen_pairs)
        planner_rejected_returns.extend(
            ret for item, ret in chosen_pairs
            if item.code not in final_codes)

        best_selected = max(chosen)
        best_panel = max(values)
        regrets.append(best_panel - best_selected)
        if best_selected > statistics.median(values):
            above_median += 1

    preselected_mean = _mean(preselected_all)
    panel_mean = _mean(panel_all)
    return {
        "world_hash": world.world_hash,
        "horizon": horizon,
        "evidence_scope": EVIDENCE_SCOPE,
        "promotion_eligible": False,
        "available": missing_context_sets < world.n_sets,
        "sets": world.n_sets,
        "comparable_sets": comparable_sets,
        "abstained_sets": abstained_sets,
        "missing_context_sets": missing_context_sets,
        "unreadable_sets": unreadable_sets,
        "preselected": _summary(preselected_all),
        "panel": _summary(panel_all),
        "preselection_lift_vs_panel_mean": (
            round(preselected_mean - panel_mean, 4)
            if preselected_mean is not None and panel_mean is not None
            else None),
        "mean_regret": _mean(regrets),
        "preselected_above_panel_median_rate": (
            round(above_median / comparable_sets, 4)
            if comparable_sets else None),
        "planner_acceptance_rate": (
            round(final_from_preselected / preselected_count, 4)
            if preselected_count else None),
        "planner_rejected_preselection": _summary(
            planner_rejected_returns),
        "note": (
            "Preselection is stock-choice evidence. Final orders are a later "
            "trade-planning layer and are reported separately."),
    }


def stock_layer_report(world: OpportunityDreamWorld, *,
                       horizon: int = 5) -> dict:
    """One stable artifact for A/B/C/D stock-layer comparison."""
    architectures = sorted({
        str(group.context.get("selection_architecture") or "unknown")
        for group in world.sets
    })
    final_skill = selection_skill(world, horizon=horizon)
    preselection = preselection_skill(world, horizon=horizon)
    return {
        "world_hash": world.world_hash,
        "horizon": horizon,
        "evidence_scope": EVIDENCE_SCOPE,
        "promotion_eligible": False,
        "architectures": architectures,
        "final_order_skill": final_skill,
        "preselection_skill": preselection,
        "layer_semantics": {
            "panel": "all stocks offered to stock choice / planning",
            "preselection": (
                "stock chooser output before shared price/risk planning; "
                "absent on legacy dual-rank runs"),
            "final_order": "orders that survived trade planning",
        },
    }


_RANK_FIELDS = {
    "change_pct": True,
    "turnover_rate": True,
    "net_amount": True,
    "consecutive_limits": True,
}


def ranking_baseline(world: OpportunityDreamWorld, *, feature: str,
                     horizon: int = 5, top_k: int = 1,
                     descending: bool = True) -> dict:
    """Replay one transparent panel-field ranking on the same opportunities."""
    if feature not in _RANK_FIELDS:
        raise ValueError(
            f"unsupported ranking feature {feature!r}; "
            f"allowed={sorted(_RANK_FIELDS)}")
    if top_k <= 0:
        raise ValueError("top_k must be positive")

    picked = []
    regrets = []
    comparable = 0
    missing_feature_sets = 0
    champion = []

    for group in world.sets:
        if group.parse_error:
            continue
        rows = []
        for item in group.items:
            ret = _ret(item, horizon)
            value = item.panel_row.get(feature)
            if ret is None or value is None:
                continue
            rows.append((float(value), item, ret))
        if not rows:
            missing_feature_sets += 1
            continue

        rows.sort(key=lambda x: x[0], reverse=descending)
        chosen = rows[:top_k]
        picked.extend(ret for _, _, ret in chosen)
        best_panel = max(ret for _, _, ret in rows)
        best_pick = max(ret for _, _, ret in chosen)
        regrets.append(best_panel - best_pick)
        champion.extend(ret for _, item, ret in rows if item.selected)
        comparable += 1

    return {
        "world_hash": world.world_hash,
        "feature": feature,
        "descending": descending,
        "top_k": top_k,
        "horizon": horizon,
        "evidence_scope": EVIDENCE_SCOPE,
        "promotion_eligible": False,
        "comparable_sets": comparable,
        "missing_feature_sets": missing_feature_sets,
        "baseline": _summary(picked),
        "champion_selected": _summary(champion),
        "mean_regret": _mean(regrets),
        "observed_fields": [f"panel.{feature}"],
    }


class SelectionDreamAgent:
    """Read-only diagnostics over one immutable OpportunityDreamWorld."""

    def diagnose(self, world: OpportunityDreamWorld, *, horizon: int = 5) -> dict:
        return selection_skill(world, horizon=horizon)

    def compare_ranking(self, world: OpportunityDreamWorld, *, feature: str,
                        horizon: int = 5, top_k: int = 1,
                        descending: bool = True) -> dict:
        return ranking_baseline(
            world, feature=feature, horizon=horizon, top_k=top_k,
            descending=descending)



def _history_conn():
    import sqlite3
    conn = sqlite3.connect(
        f"file:{DATA_DIR / 'market_history.db'}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _adv20(history_conn, code: str, before: str) -> float | None:
    rows = history_conn.execute(
        "SELECT volume FROM daily_kline WHERE code=? AND date<=? "
        "ORDER BY date DESC LIMIT 20", (code, before)).fetchall()
    if len(rows) < 20:
        return None
    vols = [float(row["volume"] or 0) for row in rows]
    return sum(vols) / len(vols) if vols else None


def _policy_panel(group, *, params: dict, history_conn) -> list[str]:
    pool = group.context.get("candidate_pool") or []
    limit = int(group.context.get("panel_limit") or 0)
    ranking_day = str(group.context.get("ranking_day") or "")
    if not pool or limit <= 0 or not ranking_day:
        return []

    return selection_policy.materialize_codes(
        pool, limit=limit, params=params,
        eligible=lambda code: (
            (_adv20(history_conn, code, ranking_day) or 0) > 0))


def panel_policy_counterfactual(
        world: OpportunityDreamWorld, *,
        parent_version_id: int, variant_version_id: int,
        horizon: int = 5, history_conn=None) -> dict:
    """Replay the actual T1 panel-construction gene on the same candidate pools.

    This evaluates panel quality, not the LLM's final stock choice. It is the
    missing causal bridge for t1_change_rank: the live panel, the Dream replay
    and the policy variant all execute selection_rank.change_share.
    """
    changed = dream_agent.changed_genes(
        parent_version_id, variant_version_id)
    unsupported = [
        gene for gene in changed
        if not gene.startswith("decision.selection_rank.")
    ]
    if unsupported:
        raise ValueError(
            "selection evaluator cannot observe changed gene(s): " +
            ", ".join(unsupported))

    own = history_conn is None
    history = history_conn or _history_conn()
    parent_params = scoring.decision_params_of(parent_version_id)
    variant_params = scoring.decision_params_of(variant_version_id)

    comparable = changed_sets = missing_context = missing_outcomes = 0
    parent_returns = []
    variant_returns = []
    added_returns = []
    removed_returns = []
    rows = []

    try:
        for group in world.sets:
            parent_codes = _policy_panel(
                group, params=parent_params, history_conn=history)
            variant_codes = _policy_panel(
                group, params=variant_params, history_conn=history)
            if not parent_codes or not variant_codes:
                missing_context += 1
                continue

            union = sorted(set(parent_codes) | set(variant_codes))
            outcomes = {}
            for code in union:
                got = opportunity_outcomes.compute(
                    code=code, day=group.day, phase=group.phase,
                    history_conn=history)
                if got is None:
                    missing_outcomes += 1
                    continue
                value = (
                    got.get("forward_close_return_pct") or {}
                ).get(str(horizon))
                if value is not None:
                    outcomes[code] = float(value)

            if any(code not in outcomes for code in union):
                continue

            parent_values = [outcomes[code] for code in parent_codes]
            variant_values = [outcomes[code] for code in variant_codes]
            added = sorted(set(variant_codes) - set(parent_codes))
            removed = sorted(set(parent_codes) - set(variant_codes))

            comparable += 1
            if added or removed:
                changed_sets += 1
            parent_returns.extend(parent_values)
            variant_returns.extend(variant_values)
            added_returns.extend(outcomes[code] for code in added)
            removed_returns.extend(outcomes[code] for code in removed)
            rows.append({
                "set_id": group.set_id,
                "day": group.day,
                "parent_codes": parent_codes,
                "variant_codes": variant_codes,
                "added": added,
                "removed": removed,
                "parent_mean_return": _mean(parent_values),
                "variant_mean_return": _mean(variant_values),
            })
    finally:
        if own:
            history.close()

    pmean = _mean(parent_returns)
    vmean = _mean(variant_returns)
    return {
        "world_hash": world.world_hash,
        "parent_version_id": parent_version_id,
        "variant_version_id": variant_version_id,
        "changed_genes": changed,
        "horizon": horizon,
        "evidence_scope": EVIDENCE_SCOPE,
        "promotion_eligible": False,
        "comparable_sets": comparable,
        "behavior_changed_sets": changed_sets,
        "behavior_change_rate": (
            round(changed_sets / comparable, 4) if comparable else None),
        "parent_panel": _summary(parent_returns),
        "variant_panel": _summary(variant_returns),
        "variant_minus_parent_mean": (
            round(vmean - pmean, 4)
            if pmean is not None and vmean is not None else None),
        "added_names": _summary(added_returns),
        "removed_names": _summary(removed_returns),
        "coverage": {
            "missing_context_sets": missing_context,
            "missing_outcome_rows": missing_outcomes,
        },
        "rows": rows,
        "note": (
            "This replays panel construction only. It proves a selection gene "
            "changes the opportunity surface; final LLM choice remains a "
            "separate evaluator."),
    }
