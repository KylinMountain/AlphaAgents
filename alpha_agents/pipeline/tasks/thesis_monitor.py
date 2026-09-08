"""Check every live thesis against the market, every cycle, without an LLM.

This is what replaces asking the agent "what do you think now" 48 times a
session. The agent wrote its exit plan when it opened the position; here
the plan is simply evaluated. A cycle where nothing fired costs no model
call at all, which is both cheaper and — more importantly — *consistent*:
the same market cannot produce hold at 09:35 and sell at 09:40 because
the model happened to weight a sentence differently.

The model is called back in exactly two situations:

  1. a ``narrative`` condition exists and the review slot has arrived —
     the agent said something that cannot be mechanised and it has to
     re-read it itself;
  2. the hard floor closed a position with no condition fired — a
     ``blind_spot``. Nothing to decide there, but the agent is asked what
     it missed, and that answer is the learning signal the whole design
     exists to produce.
"""

from __future__ import annotations

import logging
from datetime import datetime, time as dtime

from alpha_agents.data import thesis as T
from alpha_agents.data.memory_store import get_theme_by_name
from alpha_agents.data.portfolio import close_position, get_open_positions

logger = logging.getLogger(__name__)

# One narrative re-read per session, late enough that the day has shown
# its hand and early enough to still act on it.
NARRATIVE_REVIEW_AT = dtime(14, 0)
_NARRATIVE_WINDOW_MINUTES = 10


def build_view(pos: dict, price: float,
               sector_ranks: dict[str, int] | None = None,
               sector_flows: dict[str, float] | None = None,
               breadth_ratio: float | None = None) -> T.MarketView:
    """Assemble one position's world, from data the cycle already has."""
    open_price = pos.get("open_price") or 0
    ret = round((price - open_price) / open_price * 100, 2) if open_price else 0.0

    theme_name = pos.get("theme") or ""
    strength = daily = status = None
    if theme_name:
        theme = get_theme_by_name(theme_name)
        if theme:
            strength = theme.get("strength")
            daily = theme.get("daily_score")
            status = theme.get("status")

    return T.MarketView(
        price=price,
        current_return_pct=ret,
        peak_return_pct=pos.get("peak_return_pct") or 0.0,
        holding_days=pos.get("holding_days") or 0,
        theme_strength=strength,
        theme_daily_score=daily,
        theme_status=status,
        theme_rank=(sector_ranks or {}).get(theme_name),
        theme_net_flow_yi=(sector_flows or {}).get(theme_name),
        breadth_ratio=breadth_ratio,
    )


def _horizon_verdict(th: T.Thesis, mv: T.MarketView) -> str:
    """Did a thesis that ran its full horizon actually play out?

    ``validated`` is deliberately not "made money". A thesis that drifted
    up 0.4% over five days did not play out; it just failed to break. The
    ±1% band matches ``no_progress_by_day`` so the two agree about what
    counts as movement.
    """
    if mv.current_return_pct >= 1.0:
        return T.VALIDATED
    return T.EXPIRED


def check_all(price_map: dict[str, float],
              sector_ranks: dict[str, int] | None = None,
              sector_flows: dict[str, float] | None = None,
              breadth_ratio: float | None = None,
              now: datetime | None = None) -> dict:
    """Evaluate every live thesis. Closes what fired.

    Returns {"closed": [...], "narrative_due": [...]}. The caller pushes
    the closes and hands ``narrative_due`` to the LLM re-read.
    """
    now = now or datetime.now()
    positions = {p["id"]: p for p in get_open_positions()}
    closed, narrative_due = [], []

    for th in T.get_active():
        pos = positions.get(th.position_id) if th.position_id else None
        if not pos:
            # Thesis without a live position: either the order never
            # filled or the position was closed by the hard floor. Both
            # are handled elsewhere (settle_orphans); skip, don't guess.
            continue
        price = price_map.get(th.code)
        if not price:
            continue

        mv = build_view(pos, price, sector_ranks, sector_flows, breadth_ratio)

        fired = T.evaluate(th.conditions, mv)
        if fired:
            reason = f"论点失效: {T.describe(fired)}"
            if close_position(pos["id"], close_price=price, close_reason=reason):
                T.close(th.id, T.INVALIDATED, close_kind=fired.kind,
                        close_note=T.describe(fired))
                logger.info("Thesis #%d invalidated: %s %s — %s",
                            th.id, th.code, th.name, T.describe(fired))
                closed.append({"type": "thesis_invalidated", "code": th.code,
                               "name": th.name, "reason": reason,
                               "close_price": price,
                               "return_pct": mv.current_return_pct})
            continue

        if mv.holding_days >= th.horizon_days:
            status = _horizon_verdict(th, mv)
            label = "论点兑现" if status == T.VALIDATED else "到期未兑现"
            reason = f"{label}（{th.horizon_days}天期限）"
            if close_position(pos["id"], close_price=price, close_reason=reason):
                T.close(th.id, status, close_note=reason)
                logger.info("Thesis #%d %s: %s %s %+.2f%%",
                            th.id, status, th.code, th.name,
                            mv.current_return_pct)
                closed.append({"type": f"thesis_{status}", "code": th.code,
                               "name": th.name, "reason": reason,
                               "close_price": price,
                               "return_pct": mv.current_return_pct})
            continue

        if T.needs_narrative_review(th.conditions) and _narrative_due(now):
            narrative_due.append((th, mv))

    return {"closed": closed, "narrative_due": narrative_due}


def _narrative_due(now: datetime) -> bool:
    """True only inside the one review window per session.

    A window rather than an exact time because the cycle runs every five
    minutes and can be late; a boolean flag on the thesis would be more
    precise but would also need a daily reset that has its own failure
    modes. Re-reading a narrative twice in a session is harmless — it
    writes a checkpoint, it does not double-close anything.
    """
    minutes_since = ((now.hour - NARRATIVE_REVIEW_AT.hour) * 60
                     + now.minute - NARRATIVE_REVIEW_AT.minute)
    return 0 <= minutes_since < _NARRATIVE_WINDOW_MINUTES


def settle_orphans(price_map: dict[str, float]) -> list[T.Thesis]:
    """Reconcile theses whose position is gone.

    A position closed by the hard floor — the 8% maximum loss, or an
    archived theme — leaves a live thesis pointing at nothing. That is
    precisely the interesting case: the trade failed in a way the agent
    did not write down. Marking it ``blind_spot`` is what lets the review
    count "how often did I lose money for a reason I never considered",
    which is the only failure statistic that generalises.

    Returns the theses just marked, for the review to ask about.
    """
    live_ids = {p["id"] for p in get_open_positions()}
    orphaned = []
    for th in T.get_active():
        if th.position_id and th.position_id not in live_ids:
            T.close(th.id, T.BLIND_SPOT,
                    close_note="仓位已被风控硬线平掉，但没有任何列出的失效条件触发")
            logger.info("Thesis #%d blind_spot: %s %s — closed with nothing fired",
                        th.id, th.code, th.name)
            orphaned.append(th)
    return orphaned
