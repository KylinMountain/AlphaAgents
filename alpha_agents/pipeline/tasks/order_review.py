"""A resting order the theme lifecycle has turned against — asked, not pulled.

``check_pending_orders`` used to cancel any order whose theme was
``declining`` / ``archived`` or scored under the cancel bar. On the live book,
2026-09-10 → 09-23, that was about 25 cancels, some within minutes of
placement: the direction stage chose a line on its fund flow while the
lifecycle table had already archived a theme of the same name. The agent
had written its own theme invalidation into the thesis — the pullback
persona is told it *must* — and a rule it never wrote overrode it.

With ``wake_agent`` on, an order that carries a thesis comes back from that
check as an ``order_signal`` instead, and this module puts it to the agent:
keep or cancel, with a reason. The reason is written to the order's close
reason on a cancel and to the thesis as a checkpoint either way, so the
review can read what the agent did with the lifecycle's evidence.

Every failure keeps the order. A resting order spends nothing, the agent's
own invalidations still run every cycle, and the hard stop is under the
position if it fills.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

from alpha_agents.data import thesis as T
from alpha_agents.data.portfolio import _cancel_order, get_pending_orders
from alpha_agents.pipeline.tasks.exit_decision import (
    _theme_line, extract_json_array,
)

logger = logging.getLogger(__name__)

_TIMEOUT = int(os.environ.get("ORDER_REVIEW_TIMEOUT", "120"))

VALID_ACTIONS = ("keep", "cancel")

#: (order_id, day, kind of reason) already put to the agent. The lifecycle
#: moves once a day; the score in ``评分0.30<0.35`` moves every cycle, so the
#: key is the reason's kind and not its text. Process-local on purpose: a
#: restart asks once more, which costs one call, and a persisted marker
#: that outlived its order would cost nothing but would need its own cleanup.
_asked: set[tuple[int, str, str]] = set()


def _kind(reason: str) -> str:
    return (reason or "").split("(")[0].split("'")[0]


def due(signals: list[dict], today: str) -> list[dict]:
    """The order signals not yet put to the agent today."""
    out = []
    for s in signals or []:
        if s.get("type") != "order_signal":
            continue
        key = (int(s["order_id"]), today, _kind(s.get("reason", "")))
        if key in _asked:
            continue
        out.append(s)
    return out


def build_context(signals: list[dict], orders: dict[int, dict],
                  price_map: dict[str, float]) -> str:
    lines = ["下面这些是你自己挂着、还没成交的单。主线跟踪系统对它们的主线给出了"
             "负面判断。以前系统会直接替你撤单；现在由你决定。", ""]
    for s in signals:
        order = orders.get(int(s["order_id"])) or {}
        th = T.get_by_id(s.get("thesis_id"))
        price = price_map.get(s["code"])
        lines.append(
            f"● 订单#{s['order_id']} {s['code']} {s.get('name', '')} "
            f"挂单区间 {order.get('entry_low')}–{order.get('entry_high')} "
            f"现价 {price if price else '无报价'} 挂单日 {order.get('order_date', '')}")
        lines.append(f"  主线系统的判断: {s.get('reason', '')}")
        lines.append(f"  主线当前: {_theme_line(s.get('theme', ''))}")
        if th is not None:
            lines.append(f"  你当时的论点: {th.claim or '未记录'}")
            if th.conditions:
                lines.append("  你自己写的失效条件（系统每轮照常检查）: "
                             + "；".join(T.describe(c) for c in th.conditions))
        lines.append("")
    return "\n".join(lines)


_INSTRUCTIONS = """你是这个虚拟组合的交易员。下面是你自己挂着的单，主线跟踪系统\
（按每日评分和生命周期维护的一张表）认为它们的主线已经走弱或归档。

这是证据，不是命令。主线跟踪表和你选方向时看的资金流是两套系统，它们可能不一致：
- 如果你的买入逻辑确实依赖这条主线、而它真的在退潮，撤单
- 如果那张表的判断和你看到的资金、价格对不上，或者你的逻辑本来就不依赖它，保留
- 你自己写的失效条件系统每轮都会照常检查，保留不等于放弃止损

输出：先用一两句说明判断，然后必须输出一个 JSON 数组，每张单一条：

```json
[{"order_id": 123, "action": "keep", "reason": "主线表归档，但近5日资金仍净流入+12亿，逻辑未破"}]
```

action 只能是 keep 或 cancel。reason 必须具体、带数据——它会写进交易记录，
复盘时用来判断你当时对不对。"""


async def decide(context: str, trader=None, *, model=None) -> list[dict]:
    from agents import Agent, Runner

    if model is None:
        from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
        from openai import AsyncOpenAI

        from alpha_agents import llm_roles

        api_key, base_url, agent_model = llm_roles.resolve(llm_roles.AGENT)
        model = OpenAIChatCompletionsModel(
            model=agent_model or "qwen-plus",
            openai_client=AsyncOpenAI(api_key=api_key, base_url=base_url))
    instructions = _INSTRUCTIONS
    if trader is not None and getattr(trader, "extra_prompt", ""):
        instructions += f"\n\n## 你是谁\n\n{trader.extra_prompt.strip()}\n"
    agent = Agent(name=f"order_review:{getattr(trader, 'id', 'default')}",
                  instructions=instructions, model=model, tools=[])
    result = await asyncio.wait_for(Runner.run(agent, context, max_turns=4),
                                    timeout=_TIMEOUT)
    return parse(result.final_output or "")


def parse(output: str) -> list[dict]:
    """Decisions from the reply; ``[]`` — keep everything — if unreadable."""
    payload = extract_json_array(output)
    if payload is None:
        logger.warning("Order review returned no readable decision list — keeping all")
        return []
    try:
        items = json.loads(payload)
    except json.JSONDecodeError as e:
        logger.warning("Order review decisions unparseable (%s) — keeping all", e)
        return []
    out = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            order_id = int(item.get("order_id"))
        except (TypeError, ValueError):
            continue
        action = str(item.get("action", "")).strip().lower()
        reason = str(item.get("reason", "")).strip()
        if action not in VALID_ACTIONS:
            continue
        if action == "cancel" and not reason:
            logger.warning("Order review cancel for #%d has no reason — keeping",
                           order_id)
            continue
        out.append({"order_id": order_id, "action": action, "reason": reason})
    return out


def apply(decisions: list[dict], signals: list[dict]) -> list[dict]:
    """Cancel what the agent cancelled; note every answer on its thesis."""
    by_id = {int(s["order_id"]): s for s in signals}
    alerts = []
    for d in decisions:
        s = by_id.get(d["order_id"])
        if s is None:
            logger.warning("Order review answered #%d, which it was not asked about",
                           d["order_id"])
            continue
        note = f"挂单期间{s.get('reason', '')}；agent {d['action']}: {d['reason']}"
        if s.get("thesis_id"):
            try:
                T.add_checkpoint(int(s["thesis_id"]), note,
                                 T.TRIGGERED if d["action"] == "cancel" else T.ACTIVE,
                                 kind="theme_gate")
            except Exception as e:
                logger.warning("Could not note the order review on thesis #%s: %s",
                               s.get("thesis_id"), e)
        if d["action"] == "cancel":
            _cancel_order(d["order_id"], f"agent撤单: {d['reason']}")
            alerts.append({"type": "cancelled", "code": s["code"],
                           "name": s.get("name", ""),
                           "reason": f"agent撤单({d['reason'][:40]})"})
    return alerts


async def run(signals: list[dict], price_map: dict[str, float], today: str,
              trader_id: str | None = None) -> list[dict]:
    """Put today's new order signals to the agent. Returns cancel alerts."""
    asking = due(signals, today)
    if not asking:
        return []
    for s in asking:
        _asked.add((int(s["order_id"]), today, _kind(s.get("reason", ""))))
    orders = {o["id"]: o for o in get_pending_orders(trader_id)}
    asking = [s for s in asking if int(s["order_id"]) in orders]
    if not asking:
        return []
    trader = None
    if trader_id:
        from alpha_agents.data.trader import get_trader
        trader = get_trader(trader_id)
    context = build_context(asking, orders, price_map)
    try:
        decisions = await decide(context, trader)
    except asyncio.TimeoutError:
        logger.warning("Order review timed out after %ds — keeping all", _TIMEOUT)
        return []
    except Exception as e:
        logger.warning("Order review failed (%s) — keeping all", e)
        return []
    logger.info("Order review [%s]: %s", trader_id,
                ", ".join(f"#{d['order_id']}={d['action']}" for d in decisions)
                or "no readable answer")
    return apply(decisions, asking)
