"""Cash reservations for pending orders.

A pending order is an intent to spend, not a spent amount. The old
``get_available_capital`` ignored that and reported a number two
simultaneous fills could both respect: nothing had earmarked the cash
for the first, so the second fill happened and the "available" figure
had been a polite suggestion rather than a constraint.

Reservations close the gap. ``reserve_for_order`` is called when an
order is created and inserts a ``held`` row for the order's worst-case
cost. A successful fill calls ``consume_reservation``, which transitions
the row to ``consumed`` and shrinks it to the actual cost — the
over-reserve (entry_high routinely over-estimates the fill) goes straight
back to available, and the consumed row stays only as reconciliation
evidence. A cancel, expiry or rejection calls ``release_reservation`` to
drop the hold entirely.

``unconsumed_total`` is what ``get_available_capital`` subtracts: the
sum of amounts over rows still in ``held`` state. Consumed rows bind
nothing — their cost lives in ``invested`` — and released rows are
gone.

The held amount is a backstop, not a precise forecast. Reserving
``trader_capital * MAX_POSITION_PCT * (1 + slippage)`` per pending
order is conservative; the consequence is that N pending orders
genuinely cannot fit in a pot that holds less than N times the
backstop. That is the whole point — the old code's overestimate of
available cash was the bug.

The reservation state has its own tiny graph: ``held → {consumed,
released}``, and both terminals are final. ``consume`` and ``release``
refuse to act on a row already in the other terminal, so a double
application of either (which would mean a bug in the call site, not
in the kernel) is loud rather than silent.
"""

from __future__ import annotations

import sqlite3

HELD = "held"
CONSUMED = "consumed"
RELEASED = "released"
CASH_RESERVE = "cash_reserve"
THEME_RISK_PREFIX = "theme_risk:"


def theme_risk_kind(theme: str) -> str:
    value = str(theme or "").strip()
    if not value:
        raise ValueError("theme is required for a risk reservation")
    return THEME_RISK_PREFIX + value


def held_total(conn: sqlite3.Connection, trader_id: str, *,
               kind: str) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM reservations "
        "WHERE trader_id=? AND kind=? AND state='held'",
        (trader_id, kind),
    ).fetchone()
    return float(row["total"] or 0.0)


def release_all_held_for_order(conn: sqlite3.Connection, order_id: int,
                               reason: str, *,
                               include_cash: bool = True) -> list[int]:
    """Release every still-held reservation for one order.

    Used by terminal order paths so new reservation kinds cannot leak merely
    because a caller only knew about the original cash kind.
    """
    sql = "SELECT id,kind FROM reservations WHERE order_id=? AND state='held'"
    args: list = [int(order_id)]
    if not include_cash:
        sql += " AND kind<>?"
        args.append(CASH_RESERVE)
    rows = conn.execute(sql + " ORDER BY id", args).fetchall()
    released = []
    for row in rows:
        conn.execute(
            "UPDATE reservations SET state='released', reason=?, "
            "released_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
            "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
            (reason, row["id"]),
        )
        released.append(int(row["id"]))
    return released


class ReservationStateError(ValueError):
    """A reservation was driven into an illegal state."""


def reserve_for_order(conn: sqlite3.Connection, *, order_id: int,
                      trader_id: str, code: str, amount: float,
                      kind: str = CASH_RESERVE,
                      reason: str = "") -> int:
    """Insert one held reservation. Idempotent: a second call for the
    same ``(order_id, kind)`` is a no-op returning the existing id.

    The caller decides the amount. This module deliberately does not
    know about the per-order sizing rules — those are the portfolio
    module's job — so the backstop-vs-precise policy lives in one
    place.
    """
    if not (isinstance(amount, (int, float)) and amount > 0
            and amount != float("inf") and amount == amount):
        raise ValueError(f"Reservation amount must be a finite positive number, got {amount!r}")
    cur = conn.execute(
        "INSERT INTO reservations (order_id, trader_id, code, kind, amount, "
        "state, reason) VALUES (?, ?, ?, ?, ?, 'held', ?) "
        "ON CONFLICT (order_id, kind) DO NOTHING",
        (order_id, trader_id, code, kind, float(amount), reason),
    )
    if cur.lastrowid:
        return cur.lastrowid
    existing = conn.execute(
        "SELECT id FROM reservations WHERE order_id = ? AND kind = ?",
        (order_id, kind),
    ).fetchone()
    return int(existing["id"])


def consume_reservation(conn: sqlite3.Connection, order_id: int,
                        actual_cost: float,
                        kind: str = CASH_RESERVE) -> tuple[int, float]:
    """Convert a held reservation into its actual cost, and **give the
    over-reserve back**.

    Returns ``(reservation_id, released_amount)``. ``released_amount``
    is what the over-reserve was — ``amount - actual_cost`` if positive,
    else 0. A negative "release" would mean the fill exceeded the
    backstop; that should not happen because the fill was sized to fit
    under the backstop, and clamping to 0 is safer than a silent
    negative cash flow.

    The over-reserve is returned by shrinking ``amount`` to the actual
    cost, so a consumed row binds nothing: ``unconsumed_total`` reads a
    consumed row as 0. The first version kept ``amount`` intact and let
    ``unconsumed_total`` keep counting ``amount − consumed_amount`` on
    every filled order, on the promise that reconciliation would release
    it later — but reconciliation is read-only against this table and
    never did. Measured on a 20-day replay: eight consumed rows held
    647k of a 1M pot, and the agent was shown 可用 −41,624 while the
    equity curve reported nearly a million in cash. A position's own
    cost is already in ``invested``; keeping the over-reserve bound as
    well charged it twice.
    """
    if not (isinstance(actual_cost, (int, float)) and actual_cost > 0
            and actual_cost != float("inf") and actual_cost == actual_cost):
        raise ValueError(f"Actual cost must be a finite positive number, got {actual_cost!r}")
    row = conn.execute(
        "SELECT id, state, amount FROM reservations "
        "WHERE order_id = ? AND kind = ?", (order_id, kind),
    ).fetchone()
    if row is None:
        raise ReservationStateError(
            f"No {kind} reservation for order #{order_id} to consume. "
            f"A fill must have been preceded by a reserve; missing the "
            f"reservation is a bug, not a recovery.")
    if row["state"] != HELD:
        raise ReservationStateError(
            f"Reservation for order #{order_id} is {row['state']}, not "
            f"held. Consume runs once; a second call is a bug.")
    released = max(0.0, float(row["amount"]) - float(actual_cost))
    conn.execute(
        "UPDATE reservations SET state = 'consumed', "
        "consumed_amount = ?, amount = ?, "
        "reason = COALESCE(reason, '') || 'consumed', "
        "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
        "WHERE id = ?", (float(actual_cost),
                         float(min(actual_cost, row["amount"])),
                         row["id"]),
    )
    return int(row["id"]), released


def release_reservation(conn: sqlite3.Connection, order_id: int,
                        reason: str,
                        kind: str = CASH_RESERVE) -> int:
    """Mark a held reservation as ``released`` (full backstop returned).

    Returns the reservation id, or raises if no held row exists. A
    release of an already-released or already-consumed row is a bug:
    it would mean the order was finished twice.
    """
    row = conn.execute(
        "SELECT id, state FROM reservations "
        "WHERE order_id = ? AND kind = ?", (order_id, kind),
    ).fetchone()
    if row is None:
        raise ReservationStateError(
            f"No {kind} reservation for order #{order_id} to release. "
            f"Reservations are created with the order; missing one at "
            f"release time is a bug.")
    if row["state"] != HELD:
        raise ReservationStateError(
            f"Reservation for order #{order_id} is {row['state']}, not "
            f"held. Release runs once on a held row; calling it on a "
            f"consumed or released row is a bug.")
    conn.execute(
        "UPDATE reservations SET state = 'released', reason = ?, "
        "released_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), "
        "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
        "WHERE id = ?",
        (reason, row["id"]),
    )
    return int(row["id"])


def reservation_for_order(conn: sqlite3.Connection, order_id: int,
                          kind: str = CASH_RESERVE) -> dict | None:
    """The reservation row for an order, or None if none exists."""
    row = conn.execute(
        "SELECT * FROM reservations WHERE order_id = ? AND kind = ?",
        (order_id, kind),
    ).fetchone()
    return dict(row) if row else None


def unconsumed_total(conn: sqlite3.Connection, trader_id: str) -> float:
    """Sum of still-binding reservation amounts for one trader.

    ``held`` rows count their full amount — that is live cash a pending
    order may still spend; ``consumed`` rows count **0**, because a fill
    shrank the row to the actual cost (see :func:`consume_reservation`)
    and that cost is already inside ``invested`` — counting it here too
    would charge the position twice; ``released`` rows count 0.
    """
    row = conn.execute(
        "SELECT "
        "  COALESCE(SUM(CASE WHEN state = 'held' THEN amount "
        "                   ELSE 0 END), 0) AS held_total "
        "FROM reservations WHERE trader_id = ? AND kind = ?",
        (trader_id, CASH_RESERVE),
    ).fetchone()
    return float(row["held_total"] or 0)
