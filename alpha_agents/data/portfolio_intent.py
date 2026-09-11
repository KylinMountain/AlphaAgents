"""The legacy public API, as intents.

``portfolio`` owns the order lifecycle and ``portfolio_exit`` owns the
close; between them they expose the names every caller has always used —
``create_pending_order``, ``open_position``, ``add_to_position``,
``close_position``. S5 put one door in front of those writes
(``intent.submit_intent``), and the wrappers that build an intent are
this module. They live here rather than in ``portfolio`` because
``portfolio`` had grown past the 1200-line ceiling and the wrappers are
the cleanest seam: they hold no state, call no SQL, and touch no
database connection of their own beyond the one they hand down.

The connection is threaded explicitly. Each wrapper resolves
``_get_conn`` from the module whose data it is about — ``portfolio`` for
an order or an add, ``portfolio_exit`` for a close — and passes it into
``submit_intent``. Without that the audit row would be written on
whichever connection ``intent`` happened to bind, and a caller (or a
test) that points only the owning module at a different database would
get the action on one connection and its record on another.

Nothing here imports ``portfolio`` at module scope: the implementations
are pulled in inside each function, which is the pattern the rest of the
data layer already uses for exactly this reason.
"""

from __future__ import annotations

import logging

from alpha_agents.data import intent
from alpha_agents.data.trader import DEFAULT_TRADER

logger = logging.getLogger(__name__)


def create_pending_order(
    *,
    code: str,
    name: str,
    theme: str,
    order_date: str,
    entry_low: float | None = None,
    entry_high: float | None = None,
    stop_loss: float | None = None,
    target_price: float | None = None,
    source: str = "morning",
    reason: str = "",
    trader_id: str = DEFAULT_TRADER,
    prediction_id: int | None = None,
    thesis_id: int | None = None,
) -> int | None:
    """Place a pending order. Compatibility wrapper over the intent path.

    Same signature and same return value as before S5: the order id, or
    ``None`` when the order was refused. What changed is that the refusal
    is now recorded — the intent row names the action, the evidence and
    the outcome — so "why was nothing created" is answerable from the
    book instead of from a log line.
    """
    from alpha_agents.data import portfolio as P
    result = intent.submit_intent(
        intent.TradeIntent(
            action=intent.OPEN, code=code, name=name, theme=theme,
            order_date=order_date, entry_low=entry_low, entry_high=entry_high,
            stop_loss=stop_loss, target_price=target_price, source=source,
            reason=reason, trader_id=trader_id, prediction_id=prediction_id,
            thesis_id=thesis_id, information_cutoff=order_date),
        conn=P._get_conn())
    return result.result if result.accepted else None


def open_position(
    *,
    code: str,
    name: str,
    theme: str,
    open_date: str,
    open_price: float,
    stop_loss: float | None = None,
    target_price: float | None = None,
    source: str = "manual",
    reason: str = "",
    shares: int | None = None,
    trader_id: str = DEFAULT_TRADER,
    prediction_id: int | None = None,
    thesis_id: int | None = None,
) -> int | None:
    """Insert an already-filled position. Compatibility wrapper.

    Same signature and return value as before S5 — the position id, or
    ``None`` when refused.
    """
    from alpha_agents.data import portfolio as P
    result = intent.submit_intent(
        intent.TradeIntent(
            action=intent.OPEN_NOW, code=code, name=name, theme=theme,
            order_date=open_date, price=open_price, stop_loss=stop_loss,
            target_price=target_price, source=source, reason=reason,
            shares=shares, trader_id=trader_id, prediction_id=prediction_id,
            thesis_id=thesis_id, information_cutoff=open_date),
        conn=P._get_conn())
    return result.result if result.accepted else None


def add_to_position(position_id: int, *, price: float, reason: str,
                    size_pct: float | None = None,
                    recalc_stop: bool = False) -> dict | None:
    """Buy more of an open position. Compatibility wrapper.

    Same return shape as before S5 (``shares`` / ``avg_price`` /
    ``total_shares``, plus ``stop_loss``) and ``None`` when there was no
    room.

    ``recalc_stop`` is the automated top-up's policy, not the agent's:
    when a rule adds on a pullback it keeps the stop the same percentage
    below the new average, so averaging down does not quietly widen the
    risk per share. An agent-initiated add leaves it False — the stop is
    the agent's decision and a rule must not move it behind the agent's
    back.
    """
    from alpha_agents.data import portfolio as P
    result = intent.submit_intent(
        intent.TradeIntent(
            action=intent.ADD, position_id=position_id, price=price,
            reason=reason, size_pct=size_pct, recalc_stop=recalc_stop),
        conn=P._get_conn())
    return result.result if result.accepted else None


def cancel_order(order_id: int, reason: str) -> None:
    """Cancel a pending order. Compatibility wrapper.

    Routed through the intent path so an automated cancel (the weaker
    theme, the expiry, the run-away price in ``check_pending_orders``)
    leaves the same audit trail a business-initiated one does.
    """
    from alpha_agents.data import portfolio as P
    intent.submit_intent(
        intent.TradeIntent(action=intent.CANCEL, position_id=order_id,
                           reason=reason),
        conn=P._get_conn())