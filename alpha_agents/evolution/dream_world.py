"""Bounded historical worlds for Dream Agent replay.

A DreamWorld is not a market simulator. It is a frozen set of historical
observations the repository actually recorded. The first world type is built
from scored champion predictions, which means it can replay calibration changes
without pretending it can replay opportunities the champion never considered.

That limitation is part of the object and part of its hash.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass

from alpha_agents.data import memory_store


class DreamWorldError(ValueError):
    """A requested historical world cannot be stated honestly."""


@dataclass(frozen=True)
class DreamObservation:
    prediction_id: int
    date: str
    code: str
    confidence: str
    outcome: bool
    horizon_days: int | None
    trader_id: str


@dataclass(frozen=True)
class DreamWorld:
    start: str
    end: str
    report_type: str
    observations: tuple[DreamObservation, ...]
    world_hash: str
    evidence_scope: str = "historical_dream_only"
    opportunity_scope: str = "champion_forecast_panel_only"

    @property
    def n(self) -> int:
        return len(self.observations)

    def manifest(self) -> dict:
        return {
            "start": self.start,
            "end": self.end,
            "report_type": self.report_type,
            "n": self.n,
            "world_hash": self.world_hash,
            "evidence_scope": self.evidence_scope,
            "opportunity_scope": self.opportunity_scope,
        }


def _hash_payload(payload: dict) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def build_prediction_world(*, start: str, end: str, report_type: str,
                           trader_id: str | None = None,
                           conn: sqlite3.Connection | None = None) -> DreamWorld:
    """Freeze scored historical predictions into one replay world.

    Only rows whose outcome is already known are admitted. This world can answer
    counterfactual questions about probability mappings on the same historical
    opportunities. It cannot answer "what would the Trader have selected
    instead?" because stocks the champion never forecast are absent by design.
    """
    start = str(start)[:10]
    end = str(end)[:10]
    if not start or not end or start > end:
        raise DreamWorldError(f"invalid dream window: {start!r}..{end!r}")
    if not isinstance(report_type, str) or not report_type.strip():
        raise DreamWorldError("DreamWorld needs a nonempty report_type")

    conn = conn if conn is not None else memory_store._get_conn()
    query = [
        "SELECT id, date, code, confidence, hit, horizon_days, trader_id ",
        "FROM predictions WHERE date BETWEEN ? AND ? ",
        "AND report_type = ? AND hit IS NOT NULL ",
        "AND confidence IS NOT NULL AND trim(confidence) != '' ",
    ]
    params: list = [start, end, report_type.strip()]
    if trader_id is not None:
        query.append("AND trader_id = ? ")
        params.append(trader_id)
    query.append("ORDER BY date, id")
    rows = conn.execute("".join(query), params).fetchall()

    observations = tuple(
        DreamObservation(
            prediction_id=int(row["id"]),
            date=str(row["date"])[:10],
            code=str(row["code"] or ""),
            confidence=str(row["confidence"]).lower(),
            outcome=bool(row["hit"]),
            horizon_days=(int(row["horizon_days"])
                          if row["horizon_days"] is not None else None),
            trader_id=str(row["trader_id"] or "default"),
        )
        for row in rows
    )
    payload = {
        "start": start,
        "end": end,
        "report_type": report_type.strip(),
        "trader_id": trader_id,
        "opportunity_scope": "champion_forecast_panel_only",
        "observations": [asdict(row) for row in observations],
    }
    return DreamWorld(
        start=start,
        end=end,
        report_type=report_type.strip(),
        observations=observations,
        world_hash=_hash_payload(payload),
    )



@dataclass(frozen=True)
class OpportunityObservation:
    item_id: int
    code: str
    status: str
    researched: bool
    selected: bool
    panel_row: dict
    outcome: dict


@dataclass(frozen=True)
class OpportunitySetObservation:
    set_id: int
    day: str
    phase: str
    information_cutoff: str
    trader_id: str
    policy_ref: str | None
    parse_error: str | None
    context: dict
    items: tuple[OpportunityObservation, ...]


@dataclass(frozen=True)
class OpportunityDreamWorld:
    start: str
    end: str
    sets: tuple[OpportunitySetObservation, ...]
    world_hash: str
    evidence_scope: str = "historical_dream_only"
    opportunity_scope: str = "full_offered_panel"

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


def build_opportunity_world(*, start: str, end: str,
                            trader_id: str | None = None,
                            run_id: str | None = None,
                            label_version: str = "forward_close_v1",
                            conn: sqlite3.Connection | None = None,
                            require_complete: bool = True
                            ) -> OpportunityDreamWorld:
    """Freeze opportunity sets whose market outcomes have matured.

    A complete set means every offered code has the same immutable outcome
    label. This prevents an evaluator from getting a flattering panel merely
    because the inconvenient names have not matured yet.
    """
    from alpha_agents.data import opportunity_journal as journal
    from alpha_agents.data import opportunity_outcomes as outcomes

    start = str(start)[:10]
    end = str(end)[:10]
    if not start or not end or start > end:
        raise DreamWorldError(f"invalid opportunity window: {start!r}..{end!r}")

    conn = conn if conn is not None else memory_store._get_conn()
    journal.init_schema(conn)
    outcomes.init_schema(conn)

    where = ["day BETWEEN ? AND ?"]
    params: list = [start, end]
    if trader_id is not None:
        where.append("trader_id=?")
        params.append(trader_id)
    if run_id is not None:
        where.append("run_id=?")
        params.append(run_id)

    set_rows = conn.execute(
        "SELECT * FROM opportunity_sets WHERE " + " AND ".join(where) +
        " ORDER BY day, id", params).fetchall()

    groups = []
    for set_row in set_rows:
        item_rows = conn.execute(
            "SELECT i.*, o.outcome_json, o.source_hash "
            "FROM opportunity_items i "
            "LEFT JOIN opportunity_outcomes o "
            "ON o.opportunity_item_id=i.id AND o.label_version=? "
            "WHERE i.opportunity_set_id=? ORDER BY i.id",
            (label_version, set_row["id"])).fetchall()
        if not item_rows:
            continue
        missing = [row["id"] for row in item_rows
                   if row["outcome_json"] is None]
        if missing and require_complete:
            continue

        items = []
        for row in item_rows:
            if row["outcome_json"] is None:
                continue
            items.append(OpportunityObservation(
                item_id=int(row["id"]),
                code=str(row["code"]),
                status=str(row["status"]),
                researched=bool(row["researched"]),
                selected=bool(row["selected"]),
                panel_row=json.loads(row["panel_row_json"]),
                outcome=json.loads(row["outcome_json"]),
            ))
        if not items:
            continue
        groups.append(OpportunitySetObservation(
            set_id=int(set_row["id"]),
            day=str(set_row["day"]),
            phase=str(set_row["phase"]),
            information_cutoff=str(set_row["information_cutoff"]),
            trader_id=str(set_row["trader_id"]),
            policy_ref=set_row["policy_ref"],
            parse_error=set_row["parse_error"],
            context=journal.context_for(int(set_row["id"]), conn),
            items=tuple(items),
        ))

    payload = {
        "start": start,
        "end": end,
        "trader_id": trader_id,
        "run_id": run_id,
        "label_version": label_version,
        "sets": [asdict(group) for group in groups],
    }
    return OpportunityDreamWorld(
        start=start, end=end, sets=tuple(groups),
        world_hash=_hash_payload(payload))
