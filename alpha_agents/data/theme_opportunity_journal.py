"""Append-only direction-level opportunity journal.

The stock Opportunity Journal starts after a panel already exists. Sector-first
selection needs the upstream evidence: which directions were evaluated, shown
to the Trader, researched, selected, rejected, or unassessable.

Absence of a model rationale is never rewritten as a rejection.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

from alpha_agents.data import clock, memory_store, policy_registry


_SET_TABLE = """
CREATE TABLE IF NOT EXISTS theme_opportunity_sets (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL,
    trader_id TEXT NOT NULL,
    day TEXT NOT NULL,
    phase TEXT NOT NULL,
    information_cutoff TEXT NOT NULL,
    architecture TEXT NOT NULL,
    policy_ref TEXT,
    shortlist_json TEXT NOT NULL,
    selected_json TEXT NOT NULL,
    research_json TEXT,
    refusals_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""

_ITEM_TABLE = """
CREATE TABLE IF NOT EXISTS theme_opportunity_items (
    id INTEGER PRIMARY KEY,
    theme_opportunity_set_id INTEGER NOT NULL,
    sector_id TEXT NOT NULL,
    status TEXT NOT NULL,
    researched INTEGER NOT NULL,
    selected INTEGER NOT NULL,
    reason TEXT,
    snapshot_json TEXT NOT NULL,
    FOREIGN KEY(theme_opportunity_set_id) REFERENCES theme_opportunity_sets(id),
    UNIQUE(theme_opportunity_set_id, sector_id)
)
"""

_GUARDS = (
    "CREATE TRIGGER IF NOT EXISTS theme_opportunity_sets_no_update "
    "BEFORE UPDATE ON theme_opportunity_sets BEGIN "
    "SELECT RAISE(ABORT, 'theme_opportunity_sets is append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS theme_opportunity_sets_no_delete "
    "BEFORE DELETE ON theme_opportunity_sets BEGIN "
    "SELECT RAISE(ABORT, 'theme_opportunity_sets is append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS theme_opportunity_items_no_update "
    "BEFORE UPDATE ON theme_opportunity_items BEGIN "
    "SELECT RAISE(ABORT, 'theme_opportunity_items is append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS theme_opportunity_items_no_delete "
    "BEFORE DELETE ON theme_opportunity_items BEGIN "
    "SELECT RAISE(ABORT, 'theme_opportunity_items is append-only'); END",
)


def _dump(value) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False)


def _hash(value) -> str:
    return hashlib.sha256(_dump(value).encode("utf-8")).hexdigest()


def init_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_SET_TABLE)
    conn.execute(_ITEM_TABLE)
    for statement in _GUARDS:
        conn.execute(statement)


def _research_set(research: dict | None) -> set[str]:
    if not research:
        return set()
    values = research.get("deep_dive_themes") or research.get("themes") or []
    return {str(value).strip() for value in values if str(value).strip()}


def _refusal_map(refusals: list[dict]) -> dict[str, dict]:
    out = {}
    for item in refusals:
        sector_id = str(item.get("sector_id") or "").strip()
        if sector_id:
            out[sector_id] = item
    return out


def classify(*, snapshots: list[dict], shortlist: list[str],
             selected: list[str], research: dict | None,
             refusals: list[dict]) -> list[dict]:
    shortlisted = {str(value).strip() for value in shortlist}
    selected_set = {str(value).strip() for value in selected}
    researched = _research_set(research)
    refused = _refusal_map(refusals)
    out = []

    for snapshot in snapshots:
        sector_id = str(snapshot.get("sector_id") or "").strip()
        if not sector_id:
            continue
        reason = None
        rank = snapshot.get("rank")
        if rank is None:
            status = "unassessable"
            reason = str(snapshot.get("reason") or "missing_required_fact")
        elif sector_id in selected_set:
            status = "agent_selected"
        elif sector_id in refused:
            status = "agent_rejected"
            item = refused[sector_id]
            reason = str(item.get("reason") or item.get("detail") or "")
        elif sector_id in researched:
            status = "researched_not_selected"
        elif sector_id in shortlisted:
            status = "offered_not_researched"
        else:
            status = "evaluated_not_offered"

        out.append({
            "sector_id": sector_id,
            "status": status,
            "researched": sector_id in researched,
            "selected": sector_id in selected_set,
            "reason": reason,
            "snapshot": snapshot,
        })
    return out


def record(*, run_id: str, trader_id: str, day: str, phase: str,
           information_cutoff: str, architecture: str,
           snapshots: list[dict], shortlist: list[str],
           selected: list[str], research: dict | None = None,
           refusals: list[dict] | None = None,
           conn: sqlite3.Connection | None = None) -> int:
    if phase not in {"open", "close"}:
        raise ValueError(f"phase must be open/close, got {phase!r}")
    if not snapshots:
        raise ValueError("theme opportunity journal needs sector snapshots")
    refusals = refusals or []
    policy_ref = policy_registry.active_ref()
    payload = {
        "run_id": str(run_id),
        "trader_id": str(trader_id),
        "day": str(day)[:10],
        "phase": phase,
        "information_cutoff": str(information_cutoff),
        "architecture": str(architecture),
        "policy_ref": policy_ref,
        "snapshots": snapshots,
        "shortlist": shortlist,
        "selected": selected,
        "research": research,
        "refusals": refusals,
    }
    items = classify(
        snapshots=snapshots, shortlist=shortlist, selected=selected,
        research=research, refusals=refusals)

    target = conn if conn is not None else memory_store._get_conn()

    def _write() -> int:
        init_schema(target)
        cursor = target.execute(
            "INSERT INTO theme_opportunity_sets "
            "(run_id,trader_id,day,phase,information_cutoff,architecture,"
            "policy_ref,shortlist_json,selected_json,research_json,"
            "refusals_json,content_hash,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (payload["run_id"], payload["trader_id"], payload["day"],
             payload["phase"], payload["information_cutoff"],
             payload["architecture"], payload["policy_ref"],
             _dump(shortlist), _dump(selected),
             _dump(research) if research is not None else None,
             _dump(refusals), _hash(payload), clock.today()))
        set_id = int(cursor.lastrowid)
        target.executemany(
            "INSERT INTO theme_opportunity_items "
            "(theme_opportunity_set_id,sector_id,status,researched,selected,"
            "reason,snapshot_json) VALUES (?,?,?,?,?,?,?)",
            [(set_id, item["sector_id"], item["status"],
              int(item["researched"]), int(item["selected"]), item["reason"],
              _dump(item["snapshot"])) for item in items])
        return set_id

    if conn is not None:
        with target:
            return _write()
    with memory_store._write_lock:
        with target:
            return _write()


def sets(conn: sqlite3.Connection | None = None) -> list[dict]:
    conn = conn if conn is not None else memory_store._get_conn()
    init_schema(conn)
    return [
        dict(row) for row in conn.execute(
            "SELECT * FROM theme_opportunity_sets ORDER BY id").fetchall()
    ]


def items(set_id: int,
          conn: sqlite3.Connection | None = None) -> list[dict]:
    conn = conn if conn is not None else memory_store._get_conn()
    init_schema(conn)
    return [
        dict(row) for row in conn.execute(
            "SELECT * FROM theme_opportunity_items "
            "WHERE theme_opportunity_set_id=? ORDER BY id", (set_id,)
        ).fetchall()
    ]
