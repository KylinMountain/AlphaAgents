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


def thesis_already_broken(code: str, price: float, order: dict) -> str | None:
    """Return the invalidation already true at fill time, if any."""
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
        fired = thesis_data.evaluate(thesis.conditions, view)
        if fired:
            thesis_data.close(
                thesis.id, thesis_data.INVALIDATED, close_kind=fired.kind,
                close_note=f"成交前失效：{thesis_data.describe(fired)}")
            return thesis_data.describe(fired)
    except Exception as exc:
        logger.warning("Thesis pre-check failed for %s: %s", code, exc)
    return None
