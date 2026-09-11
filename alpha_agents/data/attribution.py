"""Decision attribution: the frozen information boundary and the chain.

Two responsibilities, both about making ownership a stored fact rather
than a re-derivation.

**The boundary.** Invariant 4 says a decision may use only what was
available when it was made, and that later corrections must not rewrite
that boundary. That can only hold if the boundary is written down at the
moment of deciding, so ``freeze`` records the declared inputs, the
information cutoff, and the provenance of the producers that fed them,
and the table refuses UPDATE and DELETE. A revised view is a new row
naming the one it supersedes — the old row is evidence, not a draft.

**The chain.** Design §4's target is ``thesis_id → order_id → fill_id →
ledger_entry_id``. What is stored here is the part of that which exists
without inventing a record: ``theses.id`` — ``virtual_portfolio.thesis_id``
— ``position_exits.thesis_id``. There is no separate fills table, so the
entry fill *is* the position row (its ``open_price``, ``open_date``,
``shares``) and the ledger entries *are* the exit legs, which makes
``ledger_entry_id`` resolve to ``position_exits.id``. ``resolve_chain``
walks exactly that and claims nothing further.

The alternative — finding the nearest live thesis with the same stock
code — is not an ownership relation, and with two traders on one stock it
hands one book's result to the other's idea.

Outcomes are deliberately *not* unified here. Forecast and trade results
have separate accessors because §9 requires them to be independently
inspectable: one episode can simultaneously hold a correct forecast and a
negative trading P&L, and a reader must be able to see both. Process
outcomes are the third of that trio but live in
``alpha_agents.evolution.process_quality`` — this module is in ``data``
and may not import upward.

Nothing here commits. The caller owns the write lock and the transaction,
same convention as ``trade_ledger``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3

from alpha_agents.data import clock

logger = logging.getLogger(__name__)


# The fields the hash covers, and therefore the fields a rewrite has to
# preserve to go undetected. Declared once: the writer and the verifier
# both project through this tuple, so a field added to the boundary cannot
# end up covered by the write and missing from the check.
_FROZEN_FIELDS = (
    "trader_id", "code", "information_cutoff", "decided_at", "thesis_id",
    "order_id", "prediction_id", "policy_ref", "model_ref", "sources",
    "payload", "supersedes_id",
)


def _frozen(values: dict) -> dict:
    """Project a record onto exactly the hashed fields."""
    return {name: values[name] for name in _FROZEN_FIELDS}


def _hash(fields: dict) -> str:
    """Content hash over the frozen fields, order-independent.

    Lets a consumer detect a snapshot rewritten after the fact. The
    triggers already forbid that; this is the second lock on the same
    door, because every downstream evaluation rests on the assumption.
    Prefer ``verify_snapshot`` over calling this directly.
    """
    blob = json.dumps(fields, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _text(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"A decision snapshot needs a nonempty {field}")
    return value.strip()


def valid_thesis(conn: sqlite3.Connection, thesis_id: int | None,
                 code: str, trader_id: str) -> bool:
    """Does this thesis belong to this order?

    Absent is allowed — a manual order has no stated idea, and inventing
    one would be worse than the gap. Present must match on both axes:
    same instrument and same book. A thesis that cannot say what it is
    about is not a usable attribution.
    """
    if thesis_id is None:
        return True
    if type(thesis_id) is not int or thesis_id <= 0:
        return False
    return conn.execute(
        "SELECT 1 FROM theses WHERE id = ? AND code = ? AND trader_id = ?",
        (thesis_id, code, trader_id),
    ).fetchone() is not None


def freeze(conn: sqlite3.Connection, *, trader_id: str, code: str,
           information_cutoff: str, decided_at: str, payload: dict,
           thesis_id: int | None = None, order_id: int | None = None,
           prediction_id: int | None = None, policy_ref: str | None = None,
           model_ref: str | None = None, sources: list | None = None,
           supersedes_id: int | None = None) -> int:
    """Write one immutable decision snapshot. Returns its id.

    ``information_cutoff`` is the latest instant the decision was allowed
    to use — not when the job ran. A 09:00 scan that legitimately reads
    yesterday's close has a cutoff of yesterday's close, and recording the
    wall clock instead would make every later availability check trivially
    true while proving nothing.

    S6 makes the cutoff load-bearing: a boundary dated after the kernel
    clock is refused. Invariant 4 ("a decision may use only what was
    available when it was made") was until now kept on the honour
    system — this column was written faithfully and then read by nothing,
    so claiming to have seen next week cost nothing, and under replay the
    future would have leaked silently into the learning data.
    """
    trader_id = _text(trader_id, "trader_id")
    code = _text(code, "code")
    information_cutoff = _text(information_cutoff, "information_cutoff")
    decided_at = _text(decided_at, "decided_at")
    clock.assert_not_from_the_future(
        information_cutoff, what="decision information_cutoff")
    clock.assert_not_from_the_future(decided_at, what="decision decided_at")
    if not isinstance(payload, dict):
        raise ValueError("A decision snapshot payload must be an object")
    sources = list(sources or [])
    # Projected through _frozen so writer and verifier cannot disagree
    # about which fields the hash covers.
    boundary = {
        "trader_id": trader_id, "code": code,
        "information_cutoff": information_cutoff, "decided_at": decided_at,
        "thesis_id": thesis_id, "order_id": order_id,
        "prediction_id": prediction_id, "policy_ref": policy_ref,
        "model_ref": model_ref, "sources": sources,
        "payload": payload, "supersedes_id": supersedes_id,
    }
    cur = conn.execute(
        "INSERT INTO decision_snapshots "
        "(thesis_id, order_id, prediction_id, trader_id, code, "
        " information_cutoff, decided_at, payload_json, policy_ref, "
        " model_ref, sources_json, content_hash, supersedes_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (thesis_id, order_id, prediction_id, trader_id, code,
         information_cutoff, decided_at,
         json.dumps(payload, ensure_ascii=False, sort_keys=True,
                    allow_nan=False),
         policy_ref, model_ref,
         json.dumps(sources, ensure_ascii=False, sort_keys=True,
                    allow_nan=False),
         _hash(_frozen(boundary)), supersedes_id),
    )
    return cur.lastrowid


def _row_to_snapshot(row) -> dict:
    return {
        "id": row["id"], "thesis_id": row["thesis_id"],
        "order_id": row["order_id"], "prediction_id": row["prediction_id"],
        "trader_id": row["trader_id"], "code": row["code"],
        "information_cutoff": row["information_cutoff"],
        "decided_at": row["decided_at"],
        "payload": json.loads(row["payload_json"]),
        "policy_ref": row["policy_ref"], "model_ref": row["model_ref"],
        "sources": json.loads(row["sources_json"] or "[]"),
        "content_hash": row["content_hash"],
        "supersedes_id": row["supersedes_id"],
        "created_at": row["created_at"],
    }


def snapshot_for_order(conn: sqlite3.Connection, order_id: int) -> dict | None:
    """The newest snapshot taken for this order, or None.

    Newest rather than oldest because a re-frozen decision (superseding an
    earlier one) is the live boundary; the superseded rows stay readable
    through ``snapshot_history``.
    """
    row = conn.execute(
        "SELECT * FROM decision_snapshots WHERE order_id = ? "
        "ORDER BY id DESC LIMIT 1", (order_id,),
    ).fetchone()
    return _row_to_snapshot(row) if row else None


def snapshot_history(conn: sqlite3.Connection,
                     order_id: int) -> list[dict]:
    """Every boundary this order was decided under, oldest first."""
    rows = conn.execute(
        "SELECT * FROM decision_snapshots WHERE order_id = ? ORDER BY id",
        (order_id,),
    ).fetchall()
    return [_row_to_snapshot(r) for r in rows]


def snapshot_by_id(conn: sqlite3.Connection, snapshot_id: int) -> dict | None:
    """One snapshot addressed directly, or None if unknown."""
    row = conn.execute(
        "SELECT * FROM decision_snapshots WHERE id = ?", (snapshot_id,),
    ).fetchone()
    return _row_to_snapshot(row) if row else None


def verify_snapshot(conn: sqlite3.Connection, snapshot_id: int) -> bool:
    """Recompute a snapshot's hash and report whether it still matches.

    The triggers make a rewrite fail at write time. This makes one
    *detectable* if a write ever got past them — a dropped trigger, a
    restored backup, a hand-edited file. That is the promise
    ``content_hash`` makes, and without an entry point for it a consumer
    would have to reimplement the field list and could quietly check the
    wrong thing.

    An unknown id returns False rather than raising: "absent" and
    "tampered" are both reasons not to trust a snapshot, and a caller
    asking this question wants one answer.
    """
    snap = snapshot_by_id(conn, snapshot_id)
    if snap is None:
        return False
    return _hash(_frozen(snap)) == snap["content_hash"]


def resolve_chain(conn: sqlite3.Connection, order_id: int) -> dict | None:
    """Walk ``thesis → order → exits`` for one order, or None if unknown.

    Returns the links, not a verdict. Whether the forecast was right and
    whether the trade made money are two different questions with two
    different accessors below; merging them here is the mistake §9 exists
    to prevent.
    """
    row = conn.execute(
        "SELECT id, code, trader_id, thesis_id, prediction_id, status "
        "FROM virtual_portfolio WHERE id = ?", (order_id,),
    ).fetchone()
    if not row:
        return None
    exits = conn.execute(
        "SELECT id, exit_date, price, shares, net_amount, return_pct, "
        "reason, thesis_id FROM position_exits WHERE position_id = ? "
        "ORDER BY id", (order_id,),
    ).fetchall()
    snapshot = snapshot_for_order(conn, order_id)
    return {
        "order_id": row["id"],
        "status": row["status"],
        "code": row["code"],
        "trader_id": row["trader_id"],
        "thesis_id": row["thesis_id"],
        "prediction_id": row["prediction_id"],
        "snapshot_id": snapshot["id"] if snapshot else None,
        "information_cutoff": (snapshot["information_cutoff"]
                               if snapshot else None),
        "exits": [dict(e) for e in exits],
        "exit_ids": [e["id"] for e in exits],
    }


# ── Outcomes: forecast and trade, kept apart on purpose ─────────────
#
# §9 gives each outcome its own measurement contract and names the
# substitution that is prohibited for it. The two accessors below read
# disjoint storage — a forecast label lives in ``predictions``, a trade
# result lives in ``position_exits`` — so neither can quietly become an
# input to the other. They are separate functions rather than one
# "outcome" dict because the whole point is that a caller has to name
# which question it is asking.

def forecast_outcome(conn: sqlite3.Connection,
                     prediction_id: int) -> dict | None:
    """The fixed-horizon label for one forecast.

    ``hit`` answers "did this beat the market over the declared window".
    It is written by the forecast evaluator when the horizon matures and
    by nothing else. In particular it is not written by a position
    closing: a stop-out at -8% after a +2% first day is a failed trade,
    not a failed forecast, and overwriting the label with the trade
    result made the forecast score unrecoverable.
    """
    row = conn.execute(
        "SELECT id, code, date, trader_id, direction, prob, hit, "
        "next_day_return, week_return, excess_return, brier, log_score, "
        "scored_at, review_note FROM predictions WHERE id = ?",
        (prediction_id,),
    ).fetchone()
    if not row:
        return None
    out = dict(row)
    # A label is "graded" only once the evaluator stamped it. Returning
    # hit=None would otherwise read as a miss to a careless caller.
    out["graded"] = row["scored_at"] is not None or row["hit"] is not None
    return out


def trade_outcome(conn: sqlite3.Connection, thesis_id: int) -> dict | None:
    """Realised economics of the position a thesis produced.

    Derived from the exit ledger only — never from a forecast, and never
    from a hypothetical price move. A thesis whose order never filled has
    no trading result at all, which is represented as ``fills == 0``
    rather than a zero return: "did not trade" and "traded and broke even"
    are different facts and only one of them is evidence about skill.
    """
    row = conn.execute(
        "SELECT position_id FROM theses WHERE id = ?", (thesis_id,),
    ).fetchone()
    if not row or not row["position_id"]:
        return {"thesis_id": thesis_id, "position_id": None, "fills": 0,
                "net_amount": 0.0, "cost_basis": 0.0, "shares": 0,
                "return_pct": None, "opened": False}
    position_id = row["position_id"]
    agg = conn.execute(
        "SELECT COALESCE(SUM(net_amount), 0) net_amount, "
        "COALESCE(SUM(cost_basis), 0) cost_basis, "
        "COALESCE(SUM(shares), 0) shares, COUNT(*) fills "
        "FROM position_exits WHERE position_id = ?", (position_id,),
    ).fetchone()
    pos = conn.execute(
        "SELECT status, shares, open_price, open_date, close_date "
        "FROM virtual_portfolio WHERE id = ?", (position_id,),
    ).fetchone()
    basis = agg["cost_basis"] or 0.0
    return {
        "thesis_id": thesis_id,
        "position_id": position_id,
        "fills": agg["fills"],
        "shares": agg["shares"],
        "net_amount": round(agg["net_amount"], 2),
        "cost_basis": round(basis, 2),
        "return_pct": (round(agg["net_amount"] / basis * 100, 2)
                       if basis > 0 else None),
        "opened": bool(pos and pos["status"] != "pending"),
        "status": pos["status"] if pos else None,
        "open_date": pos["open_date"] if pos else None,
        "close_date": pos["close_date"] if pos else None,
    }
