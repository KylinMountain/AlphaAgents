"""Immutable opportunity sets: what the Trader saw, researched and chose.

Closed trades are sparse evidence. Every decision starts with a much denser
cross-section: names offered, names investigated, names selected, and names
left alone. This module preserves that surface without pretending that
"not selected" has a rationale the model never stated.

It records decision evidence only. Orders, fills and P&L stay in their existing
ledgers; later evaluators may join them by date/code/run, but this table never
rewrites history after an outcome is known.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

from alpha_agents.data import clock, memory_store, policy_registry

_SET_TABLE = """
CREATE TABLE IF NOT EXISTS opportunity_sets (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL,
    trader_id TEXT NOT NULL,
    day TEXT NOT NULL,
    phase TEXT NOT NULL,
    information_cutoff TEXT NOT NULL,
    policy_ref TEXT,
    panel_json TEXT NOT NULL,
    orders_json TEXT NOT NULL,
    refusals_json TEXT NOT NULL,
    research_json TEXT,
    parse_error TEXT,
    raw_hash TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""

_ITEM_TABLE = """
CREATE TABLE IF NOT EXISTS opportunity_items (
    id INTEGER PRIMARY KEY,
    opportunity_set_id INTEGER NOT NULL,
    code TEXT NOT NULL,
    status TEXT NOT NULL,
    researched INTEGER NOT NULL,
    selected INTEGER NOT NULL,
    reason TEXT,
    panel_row_json TEXT NOT NULL,
    FOREIGN KEY(opportunity_set_id) REFERENCES opportunity_sets(id),
    UNIQUE(opportunity_set_id, code)
)
"""

_CONTEXT_TABLE = """
CREATE TABLE IF NOT EXISTS opportunity_contexts (
    opportunity_set_id INTEGER PRIMARY KEY,
    context_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    FOREIGN KEY(opportunity_set_id) REFERENCES opportunity_sets(id)
)
"""

_GUARDS = (
    "CREATE TRIGGER IF NOT EXISTS opportunity_sets_no_update "
    "BEFORE UPDATE ON opportunity_sets BEGIN "
    "SELECT RAISE(ABORT, 'opportunity_sets is append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS opportunity_sets_no_delete "
    "BEFORE DELETE ON opportunity_sets BEGIN "
    "SELECT RAISE(ABORT, 'opportunity_sets is append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS opportunity_items_no_update "
    "BEFORE UPDATE ON opportunity_items BEGIN "
    "SELECT RAISE(ABORT, 'opportunity_items is append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS opportunity_items_no_delete "
    "BEFORE DELETE ON opportunity_items BEGIN "
    "SELECT RAISE(ABORT, 'opportunity_items is append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS opportunity_contexts_no_update "
    "BEFORE UPDATE ON opportunity_contexts BEGIN "
    "SELECT RAISE(ABORT, 'opportunity_contexts is append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS opportunity_contexts_no_delete "
    "BEFORE DELETE ON opportunity_contexts BEGIN "
    "SELECT RAISE(ABORT, 'opportunity_contexts is append-only'); END",
)


def init_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_SET_TABLE)
    conn.execute(_ITEM_TABLE)
    conn.execute(_CONTEXT_TABLE)
    for statement in _GUARDS:
        conn.execute(statement)


def _dump(value) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False)


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _content_hash(payload: dict) -> str:
    return _hash_text(_dump(payload))


def _refusal_map(refusals: list[dict]) -> dict[str, dict]:
    out = {}
    for item in refusals:
        code = str(item.get("code") or "").strip()
        if code:
            out[code] = item
    return out


def _research_map(research: dict | None) -> set[str]:
    if not research:
        return set()
    names = research.get("deep_dive_names") or []
    return {str(code).strip() for code in names if str(code).strip()}


def classify(*, panel: list[dict], orders: list[dict], refusals: list[dict],
             research: dict | None, parse_error: str | None) -> list[dict]:
    """Classify each offered code using only observable decision facts.

    We deliberately do not label every unselected name "rejected". If the model
    never researched it, the fact is only that it was offered and ignored.
    """
    selected = {str(o.get("code") or "").strip() for o in orders}
    refused = _refusal_map(refusals)
    researched = _research_map(research)
    items = []

    for row in panel:
        code = str(row.get("code") or "").strip()
        if not code:
            continue
        reason = None
        if parse_error:
            status = "unreadable_decision"
        elif code in selected:
            status = "agent_selected"
            reason = next(
                (str(o.get("reason") or "") for o in orders
                 if str(o.get("code") or "").strip() == code), "")
        elif code in refused:
            status = "agent_attempt_refused"
            r = refused[code]
            reason = f"{r.get('why') or ''}: {r.get('detail') or ''}".strip(": ")
        elif code in researched:
            status = "researched_not_selected"
        else:
            status = "offered_not_researched"

        items.append({
            "code": code,
            "status": status,
            "researched": code in researched,
            "selected": code in selected,
            "reason": reason,
            "panel_row": row,
        })
    return items


def record_decision(*, run_id: str, trader_id: str, day: str, phase: str,
                    information_cutoff: str, panel: list[dict],
                    orders: list[dict], refusals: list[dict],
                    research: dict | None, parse_error: str | None,
                    raw: str, context: dict | None = None,
                    conn: sqlite3.Connection | None = None) -> int:
    """Append one opportunity set and its per-code classifications."""
    if phase not in {"open", "close"}:
        raise ValueError(f"phase must be open/close, got {phase!r}")
    if not panel:
        raise ValueError("an opportunity set needs a non-empty panel")

    policy_ref = policy_registry.active_ref()
    raw_hash = _hash_text(raw or "")
    payload = {
        "run_id": str(run_id),
        "trader_id": str(trader_id),
        "day": str(day)[:10],
        "phase": phase,
        "information_cutoff": str(information_cutoff),
        "policy_ref": policy_ref,
        "panel": panel,
        "orders": orders,
        "refusals": refusals,
        "research": research,
        "parse_error": parse_error,
        "raw_hash": raw_hash,
        "context": context or {},
    }
    items = classify(
        panel=panel, orders=orders, refusals=refusals,
        research=research, parse_error=parse_error)

    target = conn if conn is not None else memory_store._get_conn()

    def _write() -> int:
        init_schema(target)
        cursor = target.execute(
            "INSERT INTO opportunity_sets "
            "(run_id, trader_id, day, phase, information_cutoff, policy_ref, "
            " panel_json, orders_json, refusals_json, research_json, "
            " parse_error, raw_hash, content_hash, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (payload["run_id"], payload["trader_id"], payload["day"],
             payload["phase"], payload["information_cutoff"],
             payload["policy_ref"], _dump(panel), _dump(orders),
             _dump(refusals), _dump(research) if research is not None else None,
             parse_error, raw_hash, _content_hash(payload), clock.today()))
        set_id = int(cursor.lastrowid)
        target.executemany(
            "INSERT INTO opportunity_items "
            "(opportunity_set_id, code, status, researched, selected, reason, "
            " panel_row_json) VALUES (?,?,?,?,?,?,?)",
            [(set_id, item["code"], item["status"],
              int(item["researched"]), int(item["selected"]), item["reason"],
              _dump(item["panel_row"])) for item in items])
        if context:
            target.execute(
                "INSERT INTO opportunity_contexts "
                "(opportunity_set_id, context_json, content_hash) "
                "VALUES (?,?,?)",
                (set_id, _dump(context), _content_hash(context)))
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
    rows = conn.execute(
        "SELECT * FROM opportunity_sets ORDER BY id").fetchall()
    return [dict(row) for row in rows]


def items(opportunity_set_id: int,
          conn: sqlite3.Connection | None = None) -> list[dict]:
    conn = conn if conn is not None else memory_store._get_conn()
    init_schema(conn)
    rows = conn.execute(
        "SELECT * FROM opportunity_items WHERE opportunity_set_id=? "
        "ORDER BY id", (opportunity_set_id,)).fetchall()
    return [dict(row) for row in rows]



def context_for(opportunity_set_id: int,
                conn: sqlite3.Connection | None = None) -> dict:
    """Decision-time context recorded with an opportunity set, or empty."""
    conn = conn if conn is not None else memory_store._get_conn()
    init_schema(conn)
    row = conn.execute(
        "SELECT context_json, content_hash FROM opportunity_contexts "
        "WHERE opportunity_set_id=?", (opportunity_set_id,)).fetchone()
    if row is None:
        return {}
    payload = json.loads(row["context_json"])
    if _content_hash(payload) != row["content_hash"]:
        raise ValueError(
            f"opportunity context #{opportunity_set_id} failed hash check")
    return payload
