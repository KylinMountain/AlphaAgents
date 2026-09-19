"""Direction-level Dream world and selection diagnostics.

This evaluator asks whether the Trader picked useful directions from the exact
set of sectors that was observable at the decision cutoff.

It is historical Dream evidence only. It cannot promote a policy and it does
not pretend sector returns are executable portfolio P&L.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import sqlite3
import statistics

from alpha_agents.data import (
    memory_store,
    theme_opportunity_journal,
    theme_opportunity_outcomes,
)

EVIDENCE_SCOPE = "historical_dream_only"


class DirectionDreamError(ValueError):
    pass


@dataclass(frozen=True)
class DirectionObservation:
    item_id: int
    sector_id: str
    status: str
    researched: bool
    selected: bool
    reason: str | None
    snapshot: dict
    outcome: dict


@dataclass(frozen=True)
class DirectionSetObservation:
    set_id: int
    day: str
    phase: str
    information_cutoff: str
    trader_id: str
    architecture: str
    policy_ref: str | None
    parse_error: str | None
    shortlist: tuple[str, ...]
    selected: tuple[str, ...]
    items: tuple[DirectionObservation, ...]


@dataclass(frozen=True)
class DirectionDreamWorld:
    start: str
    end: str
    sets: tuple[DirectionSetObservation, ...]
    world_hash: str
    evidence_scope: str = EVIDENCE_SCOPE
    opportunity_scope: str = "all_assessable_directions"

    @property
    def n_sets(self) -> int:
        return len(self.sets)

    @property
    def n_items(self) -> int:
        return sum(len(group.items) for group in self.sets)

    def manifest(self) -> dict:
        return {
            "start": self.start,
            "end": self.end,
            "n_sets": self.n_sets,
            "n_items": self.n_items,
            "world_hash": self.world_hash,
            "evidence_scope": self.evidence_scope,
            "opportunity_scope": self.opportunity_scope,
        }


def _dump(value) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False)


def _hash(value) -> str:
    return hashlib.sha256(_dump(value).encode("utf-8")).hexdigest()


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def _median(values: list[float]) -> float | None:
    return round(float(statistics.median(values)), 4) if values else None


def _summary(values: list[float]) -> dict:
    return {
        "n": len(values),
        "mean": _mean(values),
        "median": _median(values),
    }


def build_world(*, start: str, end: str,
                trader_id: str | None = None,
                run_id: str | None = None,
                architecture: str | None = "sector_first_v0",
                label_version: str = theme_opportunity_outcomes.LABEL_VERSION,
                conn: sqlite3.Connection | None = None,
                require_complete: bool = True) -> DirectionDreamWorld:
    start = str(start)[:10]
    end = str(end)[:10]
    if not start or not end or start > end:
        raise DirectionDreamError(
            f"invalid direction dream window: {start!r}..{end!r}")

    conn = conn if conn is not None else memory_store._get_conn()
    theme_opportunity_journal.init_schema(conn)
    theme_opportunity_outcomes.init_schema(conn)

    where = ["day BETWEEN ? AND ?"]
    params: list = [start, end]
    if trader_id is not None:
        where.append("trader_id=?")
        params.append(trader_id)
    if run_id is not None:
        where.append("run_id=?")
        params.append(run_id)
    if architecture is not None:
        where.append("architecture=?")
        params.append(architecture)

    sets = conn.execute(
        "SELECT * FROM theme_opportunity_sets WHERE "
        + " AND ".join(where) + " ORDER BY day,id",
        params,
    ).fetchall()

    groups = []
    for set_row in sets:
        item_rows = conn.execute(
            "SELECT i.*, o.outcome_json "
            "FROM theme_opportunity_items i "
            "LEFT JOIN theme_opportunity_outcomes o "
            "ON o.theme_opportunity_item_id=i.id AND o.label_version=? "
            "WHERE i.theme_opportunity_set_id=? ORDER BY i.id",
            (label_version, set_row["id"]),
        ).fetchall()
        if not item_rows:
            continue

        assessable = [
            row for row in item_rows
            if str(row["status"]) != "unassessable"
        ]
        missing = [
            row["id"] for row in assessable
            if row["outcome_json"] is None
        ]
        if missing and require_complete:
            continue

        items = []
        for row in item_rows:
            if row["outcome_json"] is None:
                continue
            items.append(DirectionObservation(
                item_id=int(row["id"]),
                sector_id=str(row["sector_id"]),
                status=str(row["status"]),
                researched=bool(row["researched"]),
                selected=bool(row["selected"]),
                reason=row["reason"],
                snapshot=json.loads(row["snapshot_json"]),
                outcome=json.loads(row["outcome_json"]),
            ))
        if not items:
            continue

        groups.append(DirectionSetObservation(
            set_id=int(set_row["id"]),
            day=str(set_row["day"]),
            phase=str(set_row["phase"]),
            information_cutoff=str(set_row["information_cutoff"]),
            trader_id=str(set_row["trader_id"]),
            architecture=str(set_row["architecture"]),
            policy_ref=set_row["policy_ref"],
            parse_error=set_row["parse_error"],
            shortlist=tuple(json.loads(set_row["shortlist_json"])),
            selected=tuple(json.loads(set_row["selected_json"])),
            items=tuple(items),
        ))

    payload = {
        "start": start,
        "end": end,
        "trader_id": trader_id,
        "run_id": run_id,
        "architecture": architecture,
        "label_version": label_version,
        "sets": [asdict(group) for group in groups],
    }
    return DirectionDreamWorld(
        start=start,
        end=end,
        sets=tuple(groups),
        world_hash=_hash(payload),
    )


def _ret(item: DirectionObservation, horizon: int,
         min_coverage: float) -> float | None:
    coverage = (item.outcome.get("coverage") or {}).get(str(horizon)) or {}
    ratio = coverage.get("ratio")
    if ratio is None or float(ratio) < min_coverage:
        return None
    value = (item.outcome.get("forward_median_return_pct") or {}).get(
        str(horizon))
    return float(value) if value is not None else None


def selection_skill(world: DirectionDreamWorld, *, horizon: int = 5,
                    min_coverage: float = 0.5) -> dict:
    """Decompose direction discovery and final direction selection."""
    if not 0 < min_coverage <= 1:
        raise DirectionDreamError("min_coverage must be in (0,1]")

    selected_all = []
    researched_not_selected = []
    offered_not_researched = []
    agent_rejected = []
    evaluated_not_offered = []
    all_assessable = []
    shortlist_all = []
    regrets = []
    selected_above_median = 0
    comparable_sets = 0
    abstained_sets = 0
    unreadable_sets = 0
    low_coverage_items = 0
    abstention_best = []
    head_dependence_gaps = []

    for group in world.sets:
        if group.parse_error:
            unreadable_sets += 1
            continue

        pairs = []
        for item in group.items:
            if item.status == "unassessable":
                continue
            ret = _ret(item, horizon, min_coverage)
            if ret is None:
                low_coverage_items += 1
                continue
            pairs.append((item, ret))

        if not pairs:
            continue

        values = [ret for _, ret in pairs]
        all_assessable.extend(values)
        shortlist = set(group.shortlist)

        for item, ret in pairs:
            if item.sector_id in shortlist:
                shortlist_all.append(ret)
            if item.selected:
                selected_all.append(ret)
                ex_top1 = (
                    item.outcome.get(
                        "ex_top1_forward_median_return_pct") or {}
                ).get(str(horizon))
                if ex_top1 is not None:
                    head_dependence_gaps.append(ret - float(ex_top1))
            elif item.status == "researched_not_selected":
                researched_not_selected.append(ret)
            elif item.status == "offered_not_researched":
                offered_not_researched.append(ret)
            elif item.status == "agent_rejected":
                agent_rejected.append(ret)
            elif item.status == "evaluated_not_offered":
                evaluated_not_offered.append(ret)

        chosen = [ret for item, ret in pairs if item.selected]
        if not chosen:
            abstained_sets += 1
            abstention_best.append(max(values))
            continue

        comparable_sets += 1
        best_selected = max(chosen)
        best_available = max(values)
        regrets.append(best_available - best_selected)
        if best_selected > statistics.median(values):
            selected_above_median += 1

    selected_mean = _mean(selected_all)
    shortlist_mean = _mean(shortlist_all)
    all_mean = _mean(all_assessable)
    outside_mean = _mean(evaluated_not_offered)

    return {
        "world_hash": world.world_hash,
        "horizon": horizon,
        "min_coverage": min_coverage,
        "evidence_scope": EVIDENCE_SCOPE,
        "promotion_eligible": False,
        "sets": world.n_sets,
        "items": world.n_items,
        "comparable_sets": comparable_sets,
        "abstained_sets": abstained_sets,
        "unreadable_sets": unreadable_sets,
        "low_coverage_items": low_coverage_items,
        "selected": _summary(selected_all),
        "researched_not_selected": _summary(researched_not_selected),
        "offered_not_researched": _summary(offered_not_researched),
        "agent_rejected": _summary(agent_rejected),
        "evaluated_not_offered": _summary(evaluated_not_offered),
        "shortlist": _summary(shortlist_all),
        "all_assessable": _summary(all_assessable),
        "direction_discovery_lift_vs_outside": (
            round(shortlist_mean - outside_mean, 4)
            if shortlist_mean is not None and outside_mean is not None
            else None),
        "agent_selection_lift_vs_shortlist": (
            round(selected_mean - shortlist_mean, 4)
            if selected_mean is not None and shortlist_mean is not None
            else None),
        "selected_lift_vs_all": (
            round(selected_mean - all_mean, 4)
            if selected_mean is not None and all_mean is not None
            else None),
        "mean_regret": _mean(regrets),
        "selected_above_available_median_rate": (
            round(selected_above_median / comparable_sets, 4)
            if comparable_sets else None),
        "abstention_best_available": _summary(abstention_best),
        "selected_head_dependence_gap": _summary(head_dependence_gaps),
    }


def transparent_rank_baseline(world: DirectionDreamWorld, *, horizon: int = 5,
                              top_k: int = 3,
                              min_coverage: float = 0.5) -> dict:
    """Compare the Agent with the frozen coarse sector rank on the same sets."""
    if top_k <= 0:
        raise DirectionDreamError("top_k must be positive")

    baseline = []
    champion = []
    comparable_sets = 0

    for group in world.sets:
        if group.parse_error:
            continue
        rows = []
        for item in group.items:
            rank = item.snapshot.get("rank")
            if rank is None:
                continue
            ret = _ret(item, horizon, min_coverage)
            if ret is None:
                continue
            rows.append((int(rank), item.sector_id, item, ret))
        if not rows:
            continue

        picked = [
            row for row in sorted(rows, key=lambda row: (row[0], row[1]))
            if row[0] <= top_k
        ]
        chosen = [row for row in rows if row[2].selected]
        if not picked or not chosen:
            continue

        comparable_sets += 1
        baseline.extend(row[3] for row in picked)
        champion.extend(row[3] for row in chosen)

    return {
        "world_hash": world.world_hash,
        "horizon": horizon,
        "top_k": top_k,
        "min_coverage": min_coverage,
        "evidence_scope": EVIDENCE_SCOPE,
        "promotion_eligible": False,
        "comparable_sets": comparable_sets,
        "transparent_rank": _summary(baseline),
        "agent_selected": _summary(champion),
        "agent_minus_rank_mean": (
            round(_mean(champion) - _mean(baseline), 4)
            if _mean(champion) is not None and _mean(baseline) is not None
            else None),
    }
