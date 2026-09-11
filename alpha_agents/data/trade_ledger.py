"""Append-only simulated exit evidence; not a complete cash/fill ledger.

A caller-supplied command ID identifies retries. Equal date/price/quantity
never identifies an execution: independent sales may have identical terms.
Legacy aggregate P&L is preserved separately, without fabricated fills.
The caller owns the write lock and transaction, including position updates.
"""

from __future__ import annotations

import json
import math
import sqlite3
from uuid import uuid4

_EMPTY = {"net_amount": 0.0, "cost_basis": 0.0, "shares": 0, "legs": 0}


def positive_price(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def exit_request(price: float, shares: int | None, reason: str) -> str:
    """Freeze original command arguments, not a quantity clipped to inventory."""
    return json.dumps([float(price), shares, reason], ensure_ascii=False, allow_nan=False)


def find_command(conn, position_id: int, command_id: str):
    return conn.execute(
        "SELECT * FROM position_exits WHERE position_id=? AND command_id=?",
        (position_id, command_id),
    ).fetchone()


def record_exit(conn: sqlite3.Connection, *, position_id: int, trader_id: str,
                code: str, exit_date: str, price: float, shares: int,
                cost_basis: float, gross_amount: float, costs: float,
                net_amount: float, return_pct: float, reason: str = "",
                command_id: str | None = None, request_json: str | None = None,
                thesis_id: int | None = None) -> int:
    """Append one exit; an explicit matching retry returns the original ID.

    Without a command ID each call is a new instruction. Upstream adapters
    must retain IDs across retries to obtain end-to-end idempotency.

    ``thesis_id`` is copied from the position, not looked up: the leg has to
    name the idea on its own, because attribution that depends on a live
    join to a mutable position row is attribution that can evaporate.
    """
    if not positive_price(price) or type(shares) is not int or shares <= 0:
        raise ValueError("An exit needs a finite positive price and integer shares")
    amounts = (cost_basis, gross_amount, costs, net_amount, return_pct)
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in amounts):
        raise ValueError("Exit amounts must be finite numbers")
    if cost_basis < 0 or costs < 0:
        raise ValueError("Exit cost basis and costs cannot be negative")
    if command_id is None:
        command_id = uuid4().hex
    if not isinstance(command_id, str) or not command_id.strip():
        raise ValueError("A command ID must be a nonempty string")
    request_json = request_json or exit_request(price, shares, reason)
    values = (trader_id, code, exit_date, price, shares, round(cost_basis, 2),
              round(gross_amount, 2), round(costs, 2), round(net_amount, 2),
              round(return_pct, 4), reason[:200], request_json, thesis_id)
    existing = find_command(conn, position_id, command_id)
    if existing:
        fields = ("trader_id", "code", "exit_date", "price", "shares", "cost_basis",
                  "gross_amount", "costs", "net_amount", "return_pct", "reason",
                  "request_json", "thesis_id")
        if tuple(existing[k] for k in fields) != values:
            raise ValueError("Command ID reused with different exit arguments")
        return existing["id"]
    cur = conn.execute(
        "INSERT INTO position_exits "
        "(position_id, command_id, trader_id, code, exit_date, price, shares, cost_basis, "
        "gross_amount, costs, net_amount, return_pct, reason, request_json, thesis_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (position_id, command_id, *values),
    )
    return cur.lastrowid


def realized_for_position(conn: sqlite3.Connection, position_id: int) -> dict:
    """Only recorded exit legs; legacy aggregates are never synthetic legs."""
    row = conn.execute(
        "SELECT COALESCE(SUM(net_amount), 0) net_amount, "
        "COALESCE(SUM(cost_basis), 0) cost_basis, "
        "COALESCE(SUM(shares), 0) shares, COUNT(*) legs "
        "FROM position_exits WHERE position_id = ?", (position_id,),
    ).fetchone()
    return dict(row) if row and row["legs"] else dict(_EMPTY)


def realized_total(conn: sqlite3.Connection, trader_id: str) -> float:
    """Recorded net P&L plus labelled legacy aggregates, each counted once.

    Legacy cash is a compatibility estimate, not verified historical cash.
    No historical fees, execution prices, or sold quantities are reconstructed.
    """
    legs = conn.execute(
        "SELECT COALESCE(SUM(net_amount), 0) FROM position_exits WHERE trader_id=?",
        (trader_id,),
    ).fetchone()[0]
    legacy = conn.execute(
        "SELECT COALESCE(SUM(COALESCE(p.legacy_realized_amount, "
        "CASE WHEN NOT EXISTS (SELECT 1 FROM position_exits e WHERE e.position_id=p.id) "
        "THEN p.return_amount ELSE 0 END)), 0) FROM virtual_portfolio p WHERE trader_id=?",
        (trader_id,),
    ).fetchone()[0]
    return float(legs + legacy)
