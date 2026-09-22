"""Adding a second tranche to a position already open.

Split out of ``portfolio`` on 2026-09-22, when the deadlock fix here (reading
the sentiment cap before taking the write lock, not inside it) pushed that
file past the 1200-line ceiling. It is a coherent slice: everything about how
much more of something the agent may buy, and nothing about opening or
closing.

Like ``portfolio_intent``, nothing here imports ``portfolio`` at module
scope — that module imports this one at its bottom, and a module-level import
back would close the cycle.
"""

from __future__ import annotations

import logging

from alpha_agents.data import clock, settlement
from alpha_agents.data.memory_store import _get_conn, _write_lock
from alpha_agents.data.portfolio_book import entry_stop
from alpha_agents.data.portfolio_exit import LOT_SIZE
from alpha_agents.data.portfolio_sizing import (
    _calc_shares,
    _trader_pct,
    get_sentiment_exposure_limit,
)
from alpha_agents.data.trader import DEFAULT_TRADER

logger = logging.getLogger(__name__)


def _round_lot(shares: int) -> int:
    """Down to a whole 手. A-shares sell in multiples of 100."""
    return (int(shares) // LOT_SIZE) * LOT_SIZE


def _add_to_position_impl(position_id: int, *, price: float, reason: str,
                          size_pct: float | None = None,
                          recalc_stop: bool = False) -> dict | None:
    """Buy the second tranche of a position the agent wants more of.

    How many times to add, and how much each time, is the agent's call —
    the only bound is the per-stock backstop. Returns None when the room
    is gone, which is a legitimate answer and not a failure.

    ``size_pct`` is the share of the book to add; without one it adds the
    default position size again.

    ``recalc_stop`` keeps the stop the same percentage below the new
    (lower) average. It is on for the automated pullback top-up, off for
    the agent's own adds — see the wrapper.
    """
    from alpha_agents.data import portfolio as P

    # Read before the lock, like ``_fill_order`` does. ``get_sentiment_cycle``
    # falls back to ``compute_and_save_sentiment`` when the day's phase was
    # never written, and that saves — inside this lock it would deadlock on the
    # first add of any day the review task had not run.
    _owner = _get_conn().execute(
        "SELECT trader_id FROM virtual_portfolio WHERE id = ? AND status = 'open'",
        (position_id,)).fetchone()
    sentiment_cap = get_sentiment_exposure_limit(
        (_owner["trader_id"] if _owner else None) or DEFAULT_TRADER)

    with _write_lock:
        conn = _get_conn()
        pos = conn.execute(
            "SELECT code, name, theme, open_price, shares, reason, trader_id, "
            "stop_loss, initial_stop_loss "
            "FROM virtual_portfolio WHERE id = ? AND status = 'open'",
            (position_id,)).fetchone()
        if not pos or price <= 0:
            return None

        trader_id = pos["trader_id"] or DEFAULT_TRADER
        capital = P.trader_capital(trader_id)
        held = pos["shares"] or 0
        cost = (pos["open_price"] or 0) * held
        default_pct = _trader_pct(trader_id, "default_size_pct",
                                  P.DEFAULT_POSITION_PCT)
        wanted = capital * max(0.005, min(1.0, size_pct or default_pct))
        available = P.get_available_capital(trader_id)

        room = min(
            wanted,
            available,
            capital * P.MAX_POSITION_WITH_ADD - cost,
            capital * P.MAX_THEME_PCT
            - P.get_theme_exposure(pos["theme"] or "", trader_id),
            max(0.0, sentiment_cap - P.get_invested_capital(trader_id)),
        )
        add_shares = _calc_shares(price, max(0.0, room))
        if add_shares <= 0:
            logger.info("Add refused for %s: no room (%.0f元)", pos["code"], room)
            return None

        total = held + add_shares
        # Weighted average: the position's cost basis is now both buys, and
        # every return the monitor computes has to be against that or the
        # add would flatter the numbers for free.
        avg = round((cost + add_shares * price) / total, 3)
        # ``recalc_stop`` keeps the stop the same fraction below the new
        # average: averaging down must not widen the risk per share. Only
        # the automated top-up sets it; the agent's own adds leave the
        # stop where the agent put it.
        #
        # The anchor comes from ``entry_stop``, the single definition of "the
        # stop this position was opened with", shared with the trailing rule
        # instead of re-derived here. Two derivations can disagree, and the
        # disagreement is invisible until the numbers drift (D29).
        #
        # The lookup used to be guarded with ``"initial_stop_loss" in
        # pos.keys()``, which tests the *query's* column list, not the table's:
        # when the SELECT above forgot the column the guard answered False and
        # the stop was silently left un-recalculated — a decline that looked
        # exactly like a policy decision. The column is selected now, and the
        # sanity check is on the value.
        new_stop = None
        if recalc_stop:
            old_open = pos["open_price"] or 0
            # ``None`` fallback = "do not invent a distance". Declining here is
            # safe (averaging down narrows risk per share with or without the
            # stop moving) but it is a gap, so it says so out loud.
            anchor = entry_stop(pos, old_open, None)
            if old_open > 0 and anchor > 0:
                new_stop = round(avg * (1 - (old_open - anchor) / old_open), 2)
            else:
                logger.warning(
                    "Top-up #%d %s left the stop alone: the entry stop cannot "
                    "be measured (open=%.3f, stop=%s), so no fraction can be "
                    "held (D29)",
                    position_id, pos["code"], old_open, pos["stop_loss"])
        if new_stop is not None:
            conn.execute(
                "UPDATE virtual_portfolio SET shares = ?, open_price = ?, "
                "stop_loss = ?, initial_stop_loss = ?, reason = ? WHERE id = ?",
                (total, avg, new_stop, new_stop,
                 f"{pos['reason'] or ''} | {reason}"[:300], position_id))
        else:
            conn.execute(
                "UPDATE virtual_portfolio SET shares = ?, open_price = ?, "
                "reason = ? WHERE id = ?",
                (total, avg, f"{pos['reason'] or ''} | {reason}"[:300],
                 position_id))
        # T+1 share-side: the add gets its own lot so its shares are
        # sellable only from add_date + 1 onward. Without this, the
        # entire position looked like one old batch on the monitor's
        # T+1 check, and the agent could sell today's shares today.
        add_date = clock.today()
        settlement.create_lot(
            conn, position_id=position_id, trader_id=trader_id,
            code=pos["code"], shares=add_shares, open_date=add_date,
            open_price=price, source="add")
        conn.commit()

    logger.info("Added to #%d %s: +%d股 @ %.2f → %d股 均价%.2f — %s",
                position_id, pos["code"], add_shares, price, total, avg, reason)
    return {"shares": add_shares, "avg_price": avg, "total_shares": total,
            "stop_loss": new_stop}

