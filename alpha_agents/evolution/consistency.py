"""Did it do what it said it would do?

Every other learning signal in this system is polluted by the market. A
trade's outcome is mostly noise: with a per-trade standard deviation
around 6% and an edge of maybe 1%, separating skill from luck takes on
the order of 144 closed trades — and 576 if the edge is half that. The
market regime changes faster than that, so P&L learning arrives after the
thing it learned has stopped being true.

This module measures something the market cannot touch. "I said I would
exit below 49.5" and "the price went to 49.2 and I held" is a
contradiction settled entirely inside the system. **Zero noise, one
sample per event, readable immediately.**

It is also the failure that matters most in a discretionary trader,
human or otherwise. An agent that writes excellent theses and then
ignores them has not got a thinking problem — it has a discipline
problem, and no amount of better reasoning fixes it.

Three checks, in descending order of how damning they are:

  * **broken_promise** — a listed condition was true and the position
    stayed open anyway. The plan was written and then not followed.
  * **unexplained_exit** — closed with no condition fired and no agent
    reason on record. Something sold it and nobody can say what.
  * **flat_sizing** — conviction and size_pct carry no relationship.
    Stating a probability while betting the same amount every time means
    the probability was decoration.
"""

from __future__ import annotations

import logging
from datetime import datetime

from alpha_agents.data import thesis as T
from alpha_agents.data.memory_store import get_theme_by_name
from alpha_agents.data.portfolio import get_open_positions

logger = logging.getLogger(__name__)

# A condition that was true for less than this long is not a broken
# promise — the monitor runs every five minutes and a price that dipped
# through a level between two cycles was never actionable. Anything
# beyond one cycle means it was seen and not acted on.
_GRACE_MINUTES = 10

# Below this spread between the smallest and largest position, sizing
# carries no information regardless of what the probabilities said.
_FLAT_SIZING_SPREAD = 0.005


def broken_promises(price_map: dict[str, float],
                    now: datetime | None = None) -> list[dict]:
    """Live positions whose own exit condition is currently true.

    Called with the same prices the monitor just used, so this asks the
    question at exactly the moment the monitor should have acted. A
    non-empty result means the plan and the behaviour have diverged
    *right now*, which is worth surfacing before it becomes history.
    """
    positions = {p["id"]: p for p in get_open_positions()}
    out = []
    for th in T.get_active():
        pos = positions.get(th.position_id) if th.position_id else None
        price = price_map.get(th.code)
        if not pos or not price:
            continue

        open_price = pos.get("open_price") or 0
        mv = T.MarketView(
            price=price,
            current_return_pct=round((price - open_price) / open_price * 100, 2)
            if open_price else 0.0,
            peak_return_pct=pos.get("peak_return_pct") or 0.0,
            holding_days=pos.get("holding_days") or 0,
        )
        if th.theme:
            row = get_theme_by_name(th.theme)
            if row:
                mv.theme_strength = row.get("strength")
                mv.theme_daily_score = row.get("daily_score")

        fired = T.evaluate(th.conditions, mv)
        if fired:
            out.append({
                "code": th.code, "name": th.name, "thesis_id": th.id,
                "condition": T.describe(fired), "kind": fired.kind,
                "price": price, "return_pct": mv.current_return_pct,
            })
    return out


def unexplained_exits(days: int = 30) -> list[dict]:
    """Closed theses where nothing recorded says why.

    A thesis that ends without a fired condition *and* without an agent
    reason means the position was disposed of by something nobody can
    name. It is not necessarily a bug — the hard floor closes positions
    and that is by design — but a high rate of them means the exit
    reasoning is not reaching the record, and the review is grading
    decisions it cannot see.
    """
    out = []
    for th in T.get_closed(days=days):
        if th.close_kind:
            continue                      # a condition fired: explained
        if th.status == T.BLIND_SPOT:
            continue                      # explained precisely by being one
        if (th.close_note or "").strip():
            continue                      # the agent said something
        out.append({"code": th.code, "name": th.name, "thesis_id": th.id,
                    "status": th.status, "closed_at": th.closed_at})
    return out


def sizing_follows_conviction(days: int = 60) -> dict:
    """Does it bet more when it says it is more sure?

    Stating a probability and then sizing every position identically
    means the probability was paperwork. The check is deliberately crude
    — a rank correlation over a handful of trades would be noise — so it
    asks only whether size varies at all, and whether the high-confidence
    half is larger on average than the low-confidence half.
    """
    theses = [t for t in (T.get_active() + T.get_closed(days=days))
              if t.size_pct]
    if len(theses) < 4:
        return {"n": len(theses)}

    sizes = [t.size_pct for t in theses]
    spread = max(sizes) - min(sizes)
    ordered = sorted(theses, key=lambda t: t.prob or 0.5)
    half = len(ordered) // 2
    low = sum(t.size_pct for t in ordered[:half]) / half
    high = sum(t.size_pct for t in ordered[-half:]) / half

    return {
        "n": len(theses), "spread": round(spread, 4),
        "low_conf_avg": round(low, 4), "high_conf_avg": round(high, 4),
        "flat": spread < _FLAT_SIZING_SPREAD,
        # Backwards is worse than flat: it means the stated probability is
        # not merely ignored but inverted.
        "backwards": high < low - 1e-9,
    }


def inject_consistency(price_map: dict[str, float] | None = None,
                       days: int = 30) -> str:
    """The discipline report, for the review and the agent's own prompt."""
    sections = []

    if price_map:
        broken = broken_promises(price_map)
        if broken:
            lines = [f"【说了没做】{len(broken)} 个持仓，它自己写的退出条件"
                     f"**现在就是成立的**，但仓位还在："]
            for b in broken[:5]:
                lines.append(f"• {b['code']} {b['name']} — {b['condition']}"
                             f"（现价 {b['price']:.2f}，浮动 {b['return_pct']:+.1f}%）")
            lines.append("→ 写下的计划没有被执行。这不是判断问题，是纪律问题，"
                         "想得再对也没用。")
            sections.append("\n".join(lines))

    unexplained = unexplained_exits(days=days)
    if unexplained:
        sections.append(
            f"【无法解释的平仓】近{days}天 {len(unexplained)} 笔平仓，"
            f"既没有条件触发也没有理由记录 —— 复盘无法评价看不见的决策。")

    sizing = sizing_follows_conviction(days=days)
    if sizing.get("backwards"):
        sections.append(
            f"【仓位与信心相反】把握大的那一半平均 {sizing['high_conf_avg']:.1%}，"
            f"把握小的反而 {sizing['low_conf_avg']:.1%}。"
            f"→ 报出来的概率不是被忽略，是被反着用了。")
    elif sizing.get("flat"):
        sections.append(
            f"【仓位不随信心变化】{sizing['n']} 条论点的仓位几乎一样"
            f"（极差 {sizing['spread']:.2%}）。"
            f"→ 报概率却每次押一样多，那个概率就只是走过场。")

    return "\n\n".join(sections)
