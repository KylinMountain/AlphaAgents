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


def _column(row, name: str):
    """Read ``name`` off a ``dict`` or a ``sqlite3.Row``.

    An *absent* column raises. ``initial_stop_loss`` is NULL on rows that
    predate it, and that is a value; confusing the two is what let D29 hide.
    The old guard read ``"initial_stop_loss" in pos.keys()``, which tests the
    *query's* column list — so a SELECT that forgot the column made the caller
    decline silently, and a silent decline looks exactly like a policy call.
    """
    keys = row.keys() if hasattr(row, "keys") else row
    if name not in keys:
        raise KeyError(
            f"{name} was not selected — a NULL column and an absent one are "
            f"not the same thing (D29)")
    return row[name]


def entry_stop(pos, open_price: float, fallback_pct: float | None) -> float:
    """The stop a position was *opened* with, in price terms. ``0.0`` if unknown.

    Reads ``initial_stop_loss`` — written once, at the fill — and never
    ``stop_loss``: that column is where the trailing rule writes its own output,
    so a distance measured from it is a distance the rule measured from itself.
    On 2026-09-16 that fed back into itself until ``000510 新金路`` carried a
    stop of ``15,352,643.13`` on a ``16.31`` entry, growing by exactly
    ``peak / open`` every cycle (D29).

    The fallbacks, in order, are a ``stop_loss`` that still sits at or below the
    entry (a row from before the column existed and not yet ratcheted past cost),
    then ``open_price × (1 − fallback_pct)``. The caller supplies that last
    distance rather than this module choosing one, because the two callers want
    different answers: the trailing rule always has to name a stop, while a
    top-up can decline to move one it cannot measure. A fallback baked in here
    would decide that for them.

    ``None`` for ``fallback_pct`` is that refusal, spelled out: the answer is
    then ``0.0``, which the top-up reads as "cannot measure it, leave the stop
    alone". Averaging down narrows risk per share whether or not the stop moves,
    so declining is safe there — it is a gap, not a decision.

    It lives in the read module because it answers a question about a row, and
    because two callers deriving it separately is how the two would drift.
    """
    if open_price <= 0:
        return 0.0
    frozen = _column(pos, "initial_stop_loss")
    if frozen and 0 < frozen <= open_price:
        return float(frozen)
    current = _column(pos, "stop_loss") or 0
    if 0 < current <= open_price:
        return float(current)
    if fallback_pct is None:
        return 0.0
    return round(open_price * (1 - fallback_pct), 2)


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


# ── 下单前的两个读账本助手 ──────────────────────────────────
#
# 从 ``portfolio`` 搬来：它们只读行、不碰钱、不定价，正是本模块的职责。
# 搬动的原因是 ``portfolio`` 越过了 1200 行上限，而这两个函数与
# ``_create_pending_order_impl`` 之间没有共享状态——它们各自被两处调用，
# 所以放在这里不制造新的耦合。


def _valid_prediction(conn, prediction_id, code: str, trader_id: str) -> bool:
    """这份预测确实是这个交易员对这只票的判断吗？"""
    if prediction_id is None:
        return True
    if type(prediction_id) is not int or prediction_id <= 0:
        return False
    return conn.execute(
        "SELECT 1 FROM predictions WHERE id=? AND code=? AND trader_id=?",
        (prediction_id, code, trader_id),
    ).fetchone() is not None


def _expire_days_for(thesis_id, trader_id: str, *, thesis_mod, trader_pct,
                     default: int) -> int:
    """How many days this order is worth waiting for.

    The idea declares its own horizon, so the order inherits it. A three-day
    breakout setup whose price has not arrived by day three has not been
    unlucky — it has not happened, and the agent said three days when it
    wrote the thing down. ``_create_pending_order_impl`` used to write a
    flat ``PENDING_EXPIRE_DAYS`` here, which meant every declaration was
    overridden by a 2 that the monitor then never read anyway.

    The trader's own default is the second choice, so a book whose agent
    omitted the horizon still expires on the schedule that book believes
    in. ``PENDING_EXPIRE_DAYS`` is the last resort and nothing more.

    ``thesis_mod`` / ``trader_pct`` are injected rather than imported: both
    live in ``portfolio``, which imports this module, so a module-scope
    import here would be a cycle.
    """
    th = thesis_mod.get_by_id(thesis_id)
    if th and th.horizon_days:
        return int(th.horizon_days)
    return int(trader_pct(trader_id, "default_horizon_days", default))
