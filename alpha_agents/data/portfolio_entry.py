"""Pre-fill checks that depend on the order's thesis."""

from __future__ import annotations

import logging

from alpha_agents.data.memory_store import get_theme_by_name
from alpha_agents.data.trader import DEFAULT_TRADER


logger = logging.getLogger(__name__)


def order_conviction(code: str, trader_id: str = DEFAULT_TRADER) -> float:
    """The stated conviction behind an order, or neutral 0.5 for legacy rows."""
    try:
        from alpha_agents.data.thesis import get_active
        theses = get_active(code=code, trader_id=trader_id)
        return theses[-1].conviction if theses else 0.5
    except Exception as exc:
        logger.debug("Conviction sort fallback for %s: %s", code, exc)
        return 0.5


def _attach_theme_flow(view, theme: str) -> None:
    """The same windowed measure the post-fill check reads.

    One source for both, on purpose: an order refused at the door and a
    position closed the next morning must be answering the same question,
    or the replay measures a rule production does not run.

    A failed read leaves the fields None, which ``thesis.evaluate`` treats
    as "could not check" and never as "the thesis broke". Refusing a fill
    because a database was busy would be the worse error.
    """
    try:
        from alpha_agents.data.clock import today
        from alpha_agents.data.theme_state import concept_state
        ranks, flows = concept_state(today())
        if theme in ranks:
            view.theme_rank = ranks[theme]
        if theme in flows:
            view.theme_net_flow_yi = flows[theme]
    except Exception as exc:                          # noqa: BLE001
        logger.debug("Theme flow unavailable for the pre-fill check of %s: %s",
                     theme, exc)


def thesis_already_broken(code: str, price: float, order: dict) -> str | None:
    """Return the invalidation already true at fill time, if any.

    The gap between the decision and the fill is where this matters, and for
    a pullback trader that gap is the whole strategy: it picks a theme
    because money is flowing in, then waits for a dip. Measured on a 20-day
    replay, **six of the seven theme_flow_negative closes were orders that
    waited two to four sessions and then closed on day 0 of holding** —
    bought and sold the same session, paying a round trip for a premise that
    had already died while the order sat.

    300475 is the clean case. Ordered 2026-01-08 on a table showing 存储芯片
    at +294.2亿 over five sessions; the agent wrote "out if this turns to a
    30亿 outflow", a 324亿 buffer. It filled 2026-01-12, by which time the
    same measure read **−72.7亿** — the window had rolled −110, −6 and −251
    across three sessions, more than the level itself. The invalidation
    fired on the fill.

    The check existed and could not see it. The view was built from price
    and the theme table only, so ``theme_net_flow_yi``, ``theme_rank`` and
    ``breadth_ratio`` were all None and ``evaluate`` skipped every condition
    that needs them — the one condition that would have caught this was
    structurally unable to run at the one moment it mattered. With the flow
    supplied, these become cancellations, which cost nothing, instead of
    same-session round trips, which cost spread twice and enter the learning
    loop as trades.
    """
    try:
        from alpha_agents.data import thesis as thesis_data
    except Exception as exc:
        logger.debug("Thesis pre-check unavailable for %s: %s", code, exc)
        return None

    try:
        theses = [item for item in thesis_data.get_active(
            code=code, trader_id=order.get("trader_id") or DEFAULT_TRADER)
            if item.position_id is None]
        if not theses:
            return None
        thesis = theses[-1]
        open_price = order.get("entry_high") or order.get("entry_low") or price
        view = thesis_data.MarketView(
            price=price,
            current_return_pct=round(
                (price - open_price) / open_price * 100, 2)
            if open_price else 0.0,
        )
        theme = order.get("theme")
        if theme:
            row = get_theme_by_name(theme)
            if row:
                view.theme_strength = row.get("strength")
                view.theme_daily_score = row.get("daily_score")
                view.theme_status = row.get("status")
            _attach_theme_flow(view, theme)
        fired = thesis_data.evaluate(thesis.conditions, view)
        if fired:
            # Unlocked: `portfolio.check_pending_orders` holds `_write_lock`
            # across the fill loop and this runs inside it. `close` would
            # take the same non-reentrant lock and block forever.
            thesis_data.close_unlocked(
                thesis.id, thesis_data.INVALIDATED, close_kind=fired.kind,
                close_note=f"成交前失效：{thesis_data.describe(fired)}")
            return thesis_data.describe(fired)
    except Exception as exc:
        logger.warning("Thesis pre-check failed for %s: %s", code, exc)
    return None
