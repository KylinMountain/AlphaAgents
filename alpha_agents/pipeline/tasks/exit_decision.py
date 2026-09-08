"""The sell side, decided by the agent rather than by a table of rules.

Until this module existed the agent picked stocks and the rules disposed
of them: a trailing stop, a theme threshold, a holding cap, all pure
Python in ``portfolio.check_positions``. That split has a cost the
learning layer cannot pay off — a closed trade carried no reasoning to
grade, so the review could reflect on *what it bought* and never on *how
it sold*, and 卖出经验 was structurally unlearnable.

Here the agent gets what a discretionary trader would have on the screen:
the position and its cost, the live quote, unrealised P/L against the
peak, how long it has been held, why it was bought in the first place,
the state of the theme behind it, the flashes from the last few hours
that mention that theme, and the mechanical signals that *would* have
closed it. It answers 卖 / 持有 / 减仓 with a reason, and the reason is
what gets stored on the trade and read back at review.

Two lines stay mechanical and the agent cannot argue with them — see
``portfolio._is_hard_exit``. Discretion above the floor, not instead of
it.

Off by default: set ``AGENT_EXIT_DECISIONS=1``. With it off, every
trigger closes as before.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re

from alpha_agents.data.memory_store import get_theme_by_name
from alpha_agents.data.portfolio import (
    HARD_STOP_PCT, close_position, get_open_positions,
)

logger = logging.getLogger(__name__)

# How much of the flash corpus to put in front of the agent per theme.
# Two is enough to establish whether the story changed; more and the
# position rows drown in news.
_NEWS_PER_THEME = 2
_NEWS_LOOKBACK_HOURS = 8

_DECISION_TIMEOUT = int(os.environ.get("EXIT_DECISION_TIMEOUT", "120"))

_JSON_BLOCK = re.compile(r"<!--\s*DECISIONS:\s*(\[.*?\])\s*-->", re.DOTALL)

VALID_ACTIONS = ("sell", "trim", "hold")


def enabled() -> bool:
    return os.environ.get("AGENT_EXIT_DECISIONS", "").lower() in (
        "1", "true", "yes", "on")


def _theme_line(theme_name: str) -> str:
    if not theme_name:
        return "无关联主线"
    theme = get_theme_by_name(theme_name)
    if not theme:
        return f"{theme_name}（已不在跟踪列表）"
    return (f"{theme_name} 累计强度{theme.get('strength', 0)}/10 "
            f"今日{theme.get('daily_score', 0):+d} {theme.get('status', '')}")


def _news_for_theme(theme_name: str) -> list[str]:
    if not theme_name:
        return []
    try:
        from alpha_agents.data.news_index import search_news
        hits = search_news(theme_name, hours=_NEWS_LOOKBACK_HOURS,
                           top_k=_NEWS_PER_THEME)
    except Exception as e:
        logger.debug("Exit news lookup failed for %s: %s", theme_name, e)
        return []
    return [f"[{(h.get('time') or '')[11:16]} {h.get('source', '')}] "
            f"{h.get('title', '')[:70]}" for h in hits]


def build_context(positions: list[dict], price_map: dict[str, float],
                  signals: list[dict]) -> str:
    """Everything the agent needs to decide, as one block of text."""
    by_code: dict[str, list[str]] = {}
    for s in signals:
        if s.get("type") == "signal":
            by_code.setdefault(s["code"], []).append(s["reason"])

    lines = [f"【风控硬线】亏损达 -{HARD_STOP_PCT:.0f}% 或主线归档时系统强制平仓，"
             f"你的判断不能覆盖这两条。以下持仓都还没触及硬线。", ""]

    for pos in positions:
        code = pos["code"]
        price = price_map.get(code)
        if not price:
            continue
        open_price = pos.get("open_price") or 0
        ret = round((price - open_price) / open_price * 100, 2) if open_price else 0
        peak = pos.get("peak_return_pct") or 0
        lines.append(
            f"● {code} {pos.get('name', '')} {pos.get('shares', 0)}股 "
            f"成本{open_price:.2f} 现价{price:.2f} "
            f"浮动{ret:+.2f}%（峰值{peak:+.1f}%，回撤{max(0, peak - ret):.1f}%） "
            f"持仓{pos.get('holding_days', 0)}天"
        )
        lines.append(f"  买入理由: {pos.get('reason') or '未记录'}")
        lines.append(f"  主线: {_theme_line(pos.get('theme', ''))}")
        news = _news_for_theme(pos.get("theme", ""))
        if news:
            for n in news:
                lines.append(f"  近期快讯: {n}")
        else:
            lines.append("  近期快讯: 无（主线无新消息，不等于逻辑破坏）")
        fired = by_code.get(code)
        if fired:
            lines.append(f"  规则信号: {'; '.join(fired)}")
        else:
            lines.append("  规则信号: 无")
        lines.append("")

    return "\n".join(lines)


_INSTRUCTIONS = """你是这个虚拟组合的交易员，负责卖出决策。买入不归你管——\
这些仓位是早盘或盘中选出来的，你要决定的是现在该不该走。

## 你的判断依据（按优先级）
1. **买入逻辑是否还成立**：仓位记录了当初为什么买。如果那个理由已经被证伪
   （主线归档、催化落地后资金退潮、消息反转），就该走，哪怕还在浮盈。
2. **资金和主线状态**：主线累计强度是生命周期，今日分是当天强弱。今日分转负
   而累计强度还高，说明是这条线第一天走弱——比连续走弱更值得警惕。
3. **规则信号**：移动止损、止盈、持有上限触发了，说明机械规则想平仓。这是证据
   不是命令。intraday 的一根下影线打穿移动止损，和主线真的走坏，是两回事。
4. **回撤幅度**：从峰值回撤多少。浮盈回吐超过一半通常意味着这一波结束了。

## 不要做的事
- 不要因为"还没到止损"就一律持有——那是规则的活，不是你的
- 不要因为浮亏就恐慌卖出——硬线在 -{hard}% ，那之前是你的判断空间
- 不要给买入建议，不要推荐新股票
- 不要为了做决定而做决定：大多数时候正确答案是 hold

## 输出格式
先用两三句说明整体判断，然后必须输出一个 JSON 块，每个持仓一条：

<!-- DECISIONS: [{{"code":"600835","action":"hold","reason":"主线今日+2仍在流入，\
浮盈3%未见回撤，买入逻辑未破","confidence":"high"}}] -->

action 只能是 sell / trim / hold。trim 表示减半仓。
reason 必须具体：说出是哪条逻辑成立或破坏，带上数据。写"技术面走弱"这种\
没有信息量的理由等于没写——它会被存进交易记录，复盘时要用它来判断你当时想对了没有。
confidence 是 high / medium / low。"""


async def decide(context: str) -> list[dict]:
    """Ask the agent what to do with each position.

    Returns the parsed decision list; an empty list means "no decision",
    which the caller must treat as hold rather than as sell.
    """
    from agents import Agent, Runner
    from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
    from openai import AsyncOpenAI

    from alpha_agents.config import AGENT_API_KEY, AGENT_BASE_URL, AGENT_MODEL
    from alpha_agents.tools.registry import (
        get_stock_fund_flow, get_sector_data, search_news,
    )

    client = AsyncOpenAI(api_key=AGENT_API_KEY, base_url=AGENT_BASE_URL)
    model = OpenAIChatCompletionsModel(
        model=AGENT_MODEL or "qwen-plus", openai_client=client)
    agent = Agent(
        name="exit_trader",
        instructions=_INSTRUCTIONS.format(hard=HARD_STOP_PCT),
        model=model,
        tools=[search_news, get_stock_fund_flow, get_sector_data],
    )

    result = await asyncio.wait_for(
        Runner.run(agent, f"当前持仓与实时状态如下，请逐个决定：\n\n{context}",
                   max_turns=20),
        timeout=_DECISION_TIMEOUT,
    )
    return parse_decisions(result.final_output or "")


def parse_decisions(output: str) -> list[dict]:
    """Pull the DECISIONS block out of the agent's report.

    A missing or malformed block returns nothing, and nothing means hold.
    An exit that fires because the model wrote unparseable JSON is worse
    than an exit that never fires: the hard stop is still underneath.
    """
    m = _JSON_BLOCK.search(output)
    if not m:
        logger.warning("Exit agent returned no DECISIONS block — holding all")
        return []
    try:
        items = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        logger.warning("Exit DECISIONS JSON malformed (%s) — holding all", e)
        return []
    if not isinstance(items, list):
        return []

    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code", "")).strip()
        action = str(item.get("action", "")).strip().lower()
        reason = str(item.get("reason", "")).strip()
        if not code or action not in VALID_ACTIONS:
            continue
        if action != "hold" and not reason:
            # An unexplained sell teaches the review nothing, and the
            # whole point of moving this decision to the agent was to get
            # a reason worth grading.
            logger.warning("Exit decision for %s has no reason — ignoring", code)
            continue
        out.append({"code": code, "action": action, "reason": reason,
                    "confidence": str(item.get("confidence", "")).strip()})
    return out


def apply(decisions: list[dict], positions: list[dict],
          price_map: dict[str, float]) -> list[dict]:
    """Execute the sells. Returns alerts in check_positions' shape."""
    by_code = {p["code"]: p for p in positions}
    alerts = []

    for d in decisions:
        if d["action"] == "hold":
            continue
        pos = by_code.get(d["code"])
        price = price_map.get(d["code"])
        if not pos or not price:
            logger.warning("Exit decision for unknown/unpriced %s — skipped",
                           d["code"])
            continue

        # 'trim' has no partial-close path in the portfolio yet, so it is
        # recorded as a full exit with its reason intact rather than
        # silently dropped. Halving a position needs a shares split in
        # close_position; until then the agent's intent to reduce is
        # honoured as an exit, which is the conservative reading.
        reason = f"agent{'减仓' if d['action'] == 'trim' else '卖出'}: {d['reason']}"[:200]
        if close_position(pos["id"], close_price=price, close_reason=reason):
            logger.info("Agent exit: %s %s @ %.2f — %s",
                        d["code"], pos.get("name", ""), price, d["reason"])
            alerts.append({
                "type": "agent_exit", "code": d["code"],
                "name": pos.get("name", ""), "reason": reason,
                "close_price": price, "confidence": d.get("confidence", ""),
            })
    return alerts


async def run(price_map: dict[str, float], signals: list[dict]) -> list[dict]:
    """One exit-decision pass over the open positions.

    Every failure path holds rather than sells: no positions, no prices, a
    timeout, a bad response. The hard stop runs regardless, so holding on
    error cannot run the account down.
    """
    positions = [p for p in get_open_positions() if price_map.get(p["code"])]
    if not positions:
        return []

    context = build_context(positions, price_map, signals)
    try:
        decisions = await decide(context)
    except asyncio.TimeoutError:
        logger.warning("Exit decision timed out after %ds — holding all",
                       _DECISION_TIMEOUT)
        return []
    except Exception as e:
        logger.warning("Exit decision failed (%s) — holding all", e)
        return []

    if not decisions:
        return []
    logger.info("Exit decisions: %s",
                ", ".join(f"{d['code']}={d['action']}" for d in decisions))
    return apply(decisions, positions, price_map)
