"""The order book's rows, and the one way a row leaves without trading.

Split out of ``portfolio`` for headroom, but the seam is real rather than
arithmetic. The 2026-09-14 pending-order work gave ``check_pending_orders``
three duties it did not have — adopting orders written before cash
reservations existed, re-reading each order's thesis on every cycle instead
of only once the price was already inside the entry zone, and honouring
``expire_days`` — and that pushed the file past the 1200-line ceiling. What
left is the part that neither prices nor sizes anything: reading which rows
are pending, open or finished, and taking a pending row out.

The cancel path lives here and not beside the fill path on purpose. It is
the one choke point where a decision stops being a decision — the state
machine's ``pending → cancelled`` edge, the episode closed as never-traded,
the held cash released — and every caller in ``portfolio`` reaches it
through this module. ``_fill_order`` is its mirror image and stays where
the money is.

Layer: ``data``. Everything imported here is ``data`` as well, so this
module cannot introduce an upward dependency. ``portfolio_intent`` is
reached lazily inside ``_cancel_order``, because that module imports
``portfolio`` back.
"""

import logging

from alpha_agents.data import episodes, order_state, reservations
from alpha_agents.data.memory_store import _get_conn, _write_lock

logger = logging.getLogger(__name__)


def _cancel_order_unlocked(order_id: int, reason: str) -> None:
    """Cancel a pending order. Caller must already hold _write_lock.

    A cancel request is meaningful only while the row is still pending (or
    a cancellation is in flight). Once the row has been filled or already
    finished some other way, the first finisher owns its history and a
    later cancel is a logged no-op — overwriting a closed row's record
    would be the silent history edit the state machine exists to stop.
    """
    conn = _get_conn()
    current = conn.execute(
        "SELECT status FROM virtual_portfolio WHERE id = ?", (order_id,),
    ).fetchone()
    if not current:
        logger.warning("Cancel requested for unknown order #%d", order_id)
        return
    current_status = current["status"]
    if current_status not in (order_state.PENDING, order_state.CANCEL_PENDING):
        logger.info("Order #%d no longer pending (%s), cancel ignored",
                    order_id, current_status)
        return
    target = order_state.assert_transition(current_status,
                                          order_state.CANCELLED)
    conn.execute(
        "UPDATE virtual_portfolio SET status = ?, close_reason = ? WHERE id = ?",
        (target, reason, order_id),
    )
    # Release the held cash back to available. Only pending /
    # cancel-pending rows reach this point (the earlier guard
    # short-circuits everything else), so the reservation is held and
    # this is a release, not a refund of an already-consumed one.
    # Recorded here rather than at the intent door because two of the cancels
    # — the drawdown gate and the unaffordable lot in _fill_order — never pass
    # through it, and closing the episode says this decision never traded.
    episodes.note_cancel(conn, order_id, reason)
    reservations.release_reservation(conn, order_id=order_id, reason=reason)
    conn.commit()
    logger.info("Cancelled order #%d: %s", order_id, reason)


def _cancel_order(order_id: int, reason: str) -> None:
    """Cancel a pending order through the intent path (compat alias).

    Internal callers — the malformed-date, weak-theme, expiry and
    run-away-price cancels in ``portfolio.check_pending_orders`` — reach
    the same audit trail a business-initiated cancel does.
    """
    from alpha_agents.data.portfolio_intent import cancel_order
    cancel_order(order_id, reason)


def _cancel_order_impl(order_id: int, reason: str) -> None:
    """Cancel a pending order (acquires _write_lock)."""
    with _write_lock:
        _cancel_order_unlocked(order_id, reason)


def _trader_filter(trader_id: str | None) -> str:
    """SQL fragment scoping a query to one trader, or to all of them.

    None means every trader on purpose: the dashboard, the drawdown gate
    and the correlation check all reason about total exposure, and
    scoping those to one book would understate the risk actually taken.
    """
    return " AND trader_id = ?" if trader_id else ""


def _trader_args(trader_id: str | None) -> tuple:
    return (trader_id,) if trader_id else ()


def get_pending_orders(trader_id: str | None = None) -> list[dict]:
    """Get all pending orders."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM virtual_portfolio WHERE status = 'pending'"
        + _trader_filter(trader_id) + " ORDER BY order_date",
        _trader_args(trader_id)
    ).fetchall()
    return [dict(r) for r in rows]


# ── Open Positions ──────────────────────────────────────────

def get_open_positions(trader_id: str | None = None) -> list[dict]:
    """Get all currently open (filled) positions."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM virtual_portfolio WHERE status = 'open'"
        + _trader_filter(trader_id) + " ORDER BY open_date",
        _trader_args(trader_id)
    ).fetchall()
    return [dict(r) for r in rows]


def get_closed_positions(limit: int = 50,
                         trader_id: str | None = None) -> list[dict]:
    """Trades that finished, newest first.

    The dashboard could see what was bought and never what happened to
    it, which is the half that says whether any of the picking works.
    Includes cancelled orders: an order that expired without filling is a
    real outcome, not an absence of one.
    """
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM virtual_portfolio WHERE status NOT IN ('pending', 'open')"
        + _trader_filter(trader_id) +
        " ORDER BY COALESCE(close_date, order_date) DESC, id DESC LIMIT ?",
        _trader_args(trader_id) + (limit,),
    ).fetchall()
    return [dict(r) for r in rows]
