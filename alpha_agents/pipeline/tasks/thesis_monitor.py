"""Check every live thesis against the market, every cycle, without an LLM.

This is what replaces asking the agent "what do you think now" 48 times a
session. The agent wrote its exit plan when it opened the position; here
the plan is simply evaluated. A cycle where nothing fired costs no model
call at all, which is both cheaper and — more importantly — *consistent*:
the same market cannot produce hold at 09:35 and sell at 09:40 because
the model happened to weight a sentence differently.

Nothing here closes a position. A crossed invalidation and a run-out
horizon both **wake** the agent instead of closing on it: the plan is
evaluated in code for free, but what to do about it stays the trader's
answer. See
docs/exec-plans/active/2026-09-26-thesis-horizon-wakes-the-agent.md.
"""

from __future__ import annotations

import logging
from datetime import datetime, time as dtime

from alpha_agents.data import thesis as T
from alpha_agents.data.memory_store import get_theme_by_name
from alpha_agents.data.portfolio import get_open_positions

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


def _wake(th: T.Thesis, fired: T.Condition, mv: T.MarketView) -> dict:
    """The signal a crossed invalidation hands to the exit agent.

    Carries three things the agent cannot reconstruct: its own words from
    entry day (``fired.note``), what the stock has done since (peak,
    drawdown, days held), and **how many times it has already crossed this
    same line and chosen to hold**.

    That last one is deliberately not suppressed. A trader who is at the
    line for the third time, having overridden itself twice, is in a
    different situation from one seeing it for the first time, and hiding
    the repetition would be withholding the agent's own history from it.
    """
    drawdown = max(0.0, (mv.peak_return_pct or 0.0) - mv.current_return_pct)
    prior = [c for c in th.checkpoints
             if c.get("verdict") == T.TRIGGERED and c.get("kind") == fired.kind]
    evidence = (f"峰值{mv.peak_return_pct:+.1f}% 回撤{drawdown:.1f}% "
                f"浮动{mv.current_return_pct:+.2f}% 持仓{mv.holding_days}天")
    T.add_checkpoint(th.id, f"{T.describe(fired)} | {evidence}",
                     T.TRIGGERED, kind=fired.kind)

    said = f"你买入时写的是「{fired.note}」。" if fired.note else ""
    again = (f"这是第 {len(prior) + 1} 次触及，前 {len(prior)} 次你都选择了继续持有。"
             if prior else "")
    logger.info("Thesis #%d triggered (not closed): %s %s — %s | %s | 第%d次",
                th.id, th.code, th.name, T.describe(fired), evidence,
                len(prior) + 1)
    return {
        "type": "signal",
        "code": th.code,
        "reason": (f"你声明的失效条件触发：{T.describe(fired)}。{said}"
                   f"当前 {evidence}。{again}还持有吗？"),
        "thesis_id": th.id,
        "kind": fired.kind,
    }


def _horizon_wake(th: T.Thesis, mv: T.MarketView) -> dict:
    """The signal a run-out horizon hands to the exit agent.

    Distinct from an invalidation wake in what it says: no line was
    crossed, the agent's own deadline simply arrived without proving the
    claim. It carries the return the horizon ended on and how long the
    position ran, so the trader answers from its own numbers rather than
    from a calendar.

    The thesis stays active. Whether it "played out" is a fact the review
    and the market grading record — closing on the horizon would let the
    close action define the verdict instead.
    """
    evidence = (f"浮动{mv.current_return_pct:+.2f}% "
                f"峰值{mv.peak_return_pct:+.1f}% "
                f"持仓{mv.holding_days}天/期限{th.horizon_days}天")
    logger.info("Thesis #%d horizon due (not closed): %s %s — %s",
                th.id, th.code, th.name, evidence)
    return {
        "type": "signal",
        "code": th.code,
        "reason": (f"你买入时写的期限（{th.horizon_days}天）到了，到期时 "
                   f"{evidence}。没有触发你声明的任何失效条件。"
                   "现在怎么处理？"),
        "thesis_id": th.id,
        "kind": "horizon_due",
    }


def check_all(price_map: dict[str, float],
              sector_ranks: dict[str, int] | None = None,
              sector_flows: dict[str, float] | None = None,
              breadth_ratio: float | None = None,
              now: datetime | None = None,
              trader_id: str | None = None) -> dict:
    """Evaluate every live thesis. Close nothing; wake the agent instead.

    Returns {"closed": [...], "narrative_due": [...], "signals": [...]}.
    ```closed``` is kept for caller compatibility and is now always empty.
    The caller hands ``narrative_due`` to the LLM re-read, and **must pass
    ``signals`` to the exit agent** — neither a crossed invalidation nor a
    run-out horizon closes anything by itself. A caller that drops them
    silently reverts the position to being held with no one asked.
    ``trader_id`` scopes it to one book, so the narrative re-read that
    follows goes to that trader's own prompt rather than to whichever
    trader happened to run the cycle.
    """
    now = now or datetime.now()
    positions = {p["id"]: p for p in get_open_positions(trader_id)}
    closed, narrative_due, signals = [], [], []

    for th in T.get_active(trader_id=trader_id):
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
            # Wake the agent; do not close. A number written on entry day
            # closing a position five sessions later, with the agent never
            # looking at the stock again, is a mechanical stop wearing a
            # thesis. What the condition is *for* is the commitment: the
            # agent said in advance what would prove it wrong, and the
            # useful moment is when that line is crossed and it has to
            # answer. See docs/exec-plans/active/invalidation-wakes-the-agent.md
            signals.append(_wake(th, fired, mv))
            continue

        if mv.holding_days >= th.horizon_days:
            # Wake the agent; do not close. The horizon is a number the
            # agent wrote on entry day, and closing on it makes the trade's
            # outcome measure that constant instead of the trader's
            # judgement — the same reason an agent-placed stop is not
            # executed. Expiry is the other half of the invalidation
            # commitment: one is "I now know I was wrong early", the other
            # is "my own deadline ran out without me being right". Both are
            # questions the trader answers. See
            # docs/exec-plans/active/2026-09-26-thesis-horizon-wakes-the-agent.md
            signals.append(_horizon_wake(th, mv))
            continue

        if T.needs_narrative_review(th.conditions) and _narrative_due(now):
            narrative_due.append((th, mv))

    return {"closed": closed, "narrative_due": narrative_due,
            "signals": signals}


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


def settle_orphans(price_map: dict[str, float],
                   trader_id: str | None = None) -> list[T.Thesis]:
    """Reconcile theses whose position is gone.

    A position closed by the hard floor — the 8% maximum loss, or an
    archived theme — leaves a live thesis pointing at nothing. That is
    precisely the interesting case: the trade failed in a way the agent
    did not write down. Marking it ``blind_spot`` is what lets the review
    count "how often did I lose money for a reason I never considered",
    which is the only failure statistic that generalises.

    Returns the theses just marked, for the review to ask about.
    """
    live_ids = {p["id"] for p in get_open_positions(trader_id)}
    orphaned = []
    for th in T.get_active(trader_id=trader_id):
        if not th.position_id or th.position_id in live_ids:
            continue
        # Not every orphan is a blind spot, and the difference is the whole
        # value of the statistic. A thesis that was woken by its own stated
        # invalidation and then closed is the opposite of a blind spot: the
        # agent wrote down what would prove it wrong, the line was crossed,
        # and it acted. Marking that blind would poison the one failure
        # number the design leans on — and in the autonomous arm there is no
        # hard floor at all, so the old note asserted a mechanism that never
        # ran.
        warned = next((c for c in reversed(th.checkpoints)
                       if c.get("verdict") == T.TRIGGERED), None)
        if warned:
            T.close(th.id, T.INVALIDATED, close_kind=warned.get("kind", ""),
                    close_note=f"agent 在自己声明的条件触发后平仓："
                               f"{warned.get('observation', '')}"[:300])
            logger.info("Thesis #%d invalidated: %s %s — the agent acted on "
                        "its own line (%s)", th.id, th.code, th.name,
                        warned.get("kind", "?"))
        else:
            T.close(th.id, T.BLIND_SPOT,
                    close_note=f"仓位已平，但 agent 列出的失效条件一条也没触发："
                               f"{_close_reason(th.position_id) or '原因未记录'}"[:300])
            logger.info("Thesis #%d blind_spot: %s %s — closed with nothing "
                        "fired", th.id, th.code, th.name)
        orphaned.append(th)
    return orphaned


def _close_reason(position_id: int) -> str:
    """Why the book says the position ended, for the blind-spot note.

    The note used to assert "closed by the hard floor" for every orphan.
    In the autonomous arm the floor is switched off, so that sentence named
    a mechanism that could not have run — and a reader chasing a blind spot
    would have started from a false premise.
    """
    try:
        from alpha_agents.data.memory_store import _get_conn
        row = _get_conn().execute(
            "SELECT close_reason FROM virtual_portfolio WHERE id = ?",
            (position_id,)).fetchone()
        return (row["close_reason"] or "") if row else ""
    except Exception as exc:                          # noqa: BLE001
        logger.debug("close reason unavailable for #%s: %s", position_id, exc)
        return ""
