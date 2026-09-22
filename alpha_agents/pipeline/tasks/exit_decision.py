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
# A-share board lot. Imported from the module that already owns the constant
# rather than re-declared, so a market-rule change moves one place.
from alpha_agents.data.portfolio_exit import LOT_SIZE

logger = logging.getLogger(__name__)

# How much of the flash corpus to put in front of the agent per theme.
# Two is enough to establish whether the story changed; more and the
# position rows drown in news.
_NEWS_PER_THEME = 2
_NEWS_LOOKBACK_HOURS = 8

_DECISION_TIMEOUT = int(os.environ.get("EXIT_DECISION_TIMEOUT", "120"))

_JSON_BLOCK = re.compile(r"<!--\s*DECISIONS:\s*(\[.*?\])\s*-->", re.DOTALL)

#: A ```json (or plain ```) fenced body. Both models this repository runs
#: answer in this shape, so it is the common case rather than the fallback.
_FENCED = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

# The five things a trader can do with a live position. Previously three,
# and one of those was a lie: "trim" had no partial-close path so it was
# booked as a full exit. A system that can only hold or close cannot learn
# 加仓 or 减仓, which is where a good trader and an adequate one differ.
VALID_ACTIONS = ("sell", "trim", "add", "hold")

# Used only when a trim says nothing about how much. The agent may state
# `fraction` (of the position, for a trim) or `size_pct` (of the book, for
# an add) and both are its call — sizing is the decision, not a constant.
DEFAULT_TRIM_FRACTION = 0.5


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


def _news_for_theme(theme_name: str) -> list[str] | None:
    """Recent flashes for a theme, or ``None`` when the index is unreadable.

    The distinction is the point. An empty list means "searched, nothing
    there", which the agent is entitled to read as calm. ``None`` means the
    search did not happen, and the prompt must say so rather than describe
    a quiet tape that nobody looked at.
    """
    if not theme_name:
        return []
    try:
        from alpha_agents.data.news_index import search_news
        hits = search_news(theme_name, hours=_NEWS_LOOKBACK_HOURS,
                           top_k=_NEWS_PER_THEME)
    except Exception as e:
        # warning, not debug: this ran at debug for three and a half hours
        # of live trading while every theme reported 无新消息.
        logger.warning("Exit news lookup unavailable for %s: %s", theme_name, e)
        return None
    return [f"[{(h.get('time') or '')[11:16]} {h.get('source', '')}] "
            f"{h.get('title', '')[:70]}" for h in hits]


def build_context(positions: list[dict], price_map: dict[str, float],
                  signals: list[dict],
                  news_by_theme: dict[str, list[str]] | None = None,
                  mechanical_stops: bool = True,
                  phase: str = "open") -> str:
    """Everything the agent needs to decide, as one block of text.

    ``news_by_theme`` overrides the live lookup. That override is not a
    convenience: ``_news_for_theme`` reads the vector store by wall-clock
    offset and does not respect ``replay_mode``, so a replay that let it
    through would decide with flashes from after its own day. A replay passes
    its own already-windowed news; a live caller passes nothing and gets the
    live lookup.
    """
    by_code: dict[str, list[str]] = {}
    for s in signals:
        if s.get("type") == "signal":
            by_code.setdefault(s["code"], []).append(s["reason"])

    # Which moment this is. The agent must not be told it is 09:00 when the
    # session is over, or it will reason about a day it already knows and
    # answer as though the outcome were still open.
    if phase == "close":
        lines = ["【现在是收盘前】今天的开盘、最高、最低、收盘、成交量你**都已经"
                 "看到**，下面给的是今日收盘价。你的卖出以今日收盘价成交。", ""]
    else:
        lines = ["【现在是开盘前】你能看到的最后一根日线是**昨日**收盘，"
                 "今日的开盘、最高、最低、收盘、成交量**你还不知道**。"
                 "你的卖出以**今日开盘价**成交。", ""]
    if mechanical_stops:
        lines.append(f"【风控硬线】亏损达 -{HARD_STOP_PCT:.0f}% 或主线归档时系统强制平仓，"
                     f"你的判断不能覆盖这两条。以下持仓都还没触及硬线。")
    else:
        # The experiment arm, and it must be stated rather than implied: if
        # the agent believes a stop sits underneath, it will answer as
        # though something else will save it, and the answer stops being a
        # judgement about selling.
        lines.append("【没有安全网】本次运行**关闭了机械止损与止盈**："
                     "系统不会替你平仓，不会在 -"
                     f"{HARD_STOP_PCT:.0f}% 兜底。**卖不卖完全由你决定**，"
                     "如果你不卖，这个仓位会一直持有到窗口结束或被你自己卖掉。")
    lines.append("")

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
        if news_by_theme is not None:
            news = news_by_theme.get(pos.get("theme", "")) or []
            source = "本日窗口内快讯"
        else:
            news = _news_for_theme(pos.get("theme", ""))
            source = "近期快讯"
        if news:
            for n in news:
                lines.append(f"  {source}: {n}")
        elif news_by_theme is not None:
            # Distinguished from "the live lookup found nothing": a replay
            # was not shown any news, and saying "无" would read as a market
            # fact rather than as a missing input.
            lines.append(f"  {source}: 未提供（回放未接入新闻窗口，不等于没有消息）")
        elif news is None:
            # The search did not happen. The replay arm has always said so;
            # the live arm said 无新消息 instead, which is a claim about the
            # market made from an outage of ours.
            lines.append(f"  {source}: **检索不可用**（新闻索引读取失败，"
                         f"不等于没有消息——不要据此判断主线平静）")
        else:
            lines.append(f"  {source}: 无（主线无新消息，不等于逻辑破坏）")
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

action 与配套字段：
- **sell** 清仓
- **trim** 减仓，可带 `"fraction": 0.5` 表示卖掉一半（0.1–0.9，不写默认一半）
- **add** 加仓，可带 `"size_pct": 0.02` 表示再买入总资金的 2%
- **hold** 什么都不做

**仓位大小是你的判断，不是系统算好的。** 加多少、减多少、什么时候加，
这些是这套系统里最难也最值钱的决策：
- 涨了加仓是顺势而为，跌了加仓是摊薄成本还是**加倍下注一个已经错了的判断**？
- 浮盈回吐一半才减，和冲高就减一半，是两种完全不同的性格
- 全清和留底仓，对一个还没走完的主线是两种不同的表达

复盘会把这些决策连同结果一起回看。写理由时要让未来的自己看得懂**当时为什么
选这个比例**，而不只是选了什么。
reason 必须具体：说出是哪条逻辑成立或破坏，带上数据。写"技术面走弱"这种\
没有信息量的理由等于没写——它会被存进交易记录，复盘时要用它来判断你当时想对了没有。
confidence 是 high / medium / low。"""


async def decide(context: str, trader=None, *, model=None,
                 tools: list | None = None) -> list[dict]:
    """Ask the agent what to do with each position.

    Returns the parsed decision list; an empty list means "no decision",
    which the caller must treat as hold rather than as sell.

    ``trader`` appends that trader's own instructions. Selling is where a
    style shows most — a breakout trader that cuts the moment momentum
    stops and a pullback trader that gives a position room are two
    different books, and giving them one exit prompt would erase the
    difference the traders exist to measure.

    ``model`` and ``tools`` exist for the replay, and the defaults are the
    live ones. A replay must pass both:

    * the live path builds its own ``AsyncOpenAI`` here, which bypasses the
      LLM journal entirely — so exit calls were not recorded and a replay
      could not reproduce them;
    * the live tools (``search_news``, ``get_stock_fund_flow``,
      ``get_sector_data``) answer from *now*. A replay that let them through
      would decide a 2026-08 day using 2026-09 data. The buy side never had
      this problem because it passes no tools at all.
    """
    from agents import Agent, Runner

    if model is None:
        from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
        from openai import AsyncOpenAI

        from alpha_agents import llm_roles

        # Agent client — counted by the tracing hook, not wrapped here.
        api_key, base_url, agent_model = llm_roles.resolve(llm_roles.AGENT)
        client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        model = OpenAIChatCompletionsModel(
            model=agent_model or "qwen-plus", openai_client=client)
    if tools is None:
        from alpha_agents.tools.registry import (
            get_stock_fund_flow, get_sector_data, search_news,
        )
        tools = [search_news, get_stock_fund_flow, get_sector_data]

    instructions = _INSTRUCTIONS.format(hard=HARD_STOP_PCT)
    if trader is not None and getattr(trader, "extra_prompt", ""):
        instructions += f"\n\n## 你是谁\n\n{trader.extra_prompt.strip()}\n"
    agent = Agent(
        name=f"exit_trader:{getattr(trader, 'id', 'default')}",
        instructions=instructions,
        model=model,
        tools=list(tools),
    )

    result = await asyncio.wait_for(
        Runner.run(agent, f"当前持仓与实时状态如下，请逐个决定：\n\n{context}",
                   max_turns=20),
        timeout=_DECISION_TIMEOUT,
    )
    return parse_decisions(result.final_output or "")


def parse_decisions(output: str) -> list[dict]:
    """Pull the decisions out of the agent's report.

    Three shapes are accepted, and the reason is a measured defect rather
    than defensive coding:

    1. the documented ``<!-- DECISIONS: [...] -->`` marker;
    2. a ```json fenced array;
    3. a bare JSON array.

    Only shape 1 used to parse, and **neither model this repository runs
    emits it** — asked for the marker, both answer with a fenced array. So
    every exit decision was dropped and the agent held everything, silently:
    production has had ``AGENT_EXIT_DECISIONS=1`` set with not one
    ``Exit decisions:`` line in its logs, and the first replay that enabled
    the path produced 17 unreadable replies out of 17.

    That is the failure this repository keeps paying for — an answer that
    looks like a normal one. The prompt still asks for the marker, because
    the marker is the unambiguous form; the fallbacks exist because a parser
    that only reads the form the model does not produce is a parser for
    nothing.

    A reply that matches nothing still returns ``[]``, which means hold. The
    hard stop is underneath, so holding on an unreadable reply cannot run the
    account down.
    """
    text = output or ""
    payload = None

    m = _JSON_BLOCK.search(text)
    if m:
        payload = m.group(1)
    else:
        # The last fenced block wins: a model that thinks out loud often
        # shows an example early and its real answer at the end.
        fences = _FENCED.findall(text)
        for body in reversed(fences):
            if body.lstrip().startswith("["):
                payload = body
                break
        if payload is None:
            # An array with no fence around it. Anchored on the last `[` that
            # closes at the end of the text, so an example array quoted in the
            # prose is not mistaken for the answer.
            start = text.rfind("[")
            while start != -1:
                candidate = text[start:]
                if candidate.rstrip().endswith("]"):
                    payload = candidate[: candidate.rfind("]") + 1]
                    break
                start = text.rfind("[", 0, start)
    if payload is None:
        logger.warning("Exit agent returned no readable decision list — "
                       "holding all")
        return []
    try:
        items = json.loads(payload)
    except json.JSONDecodeError as e:
        logger.warning("Exit decisions malformed/unparseable (%s) — holding all", e)
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
                    "confidence": str(item.get("confidence", "")).strip(),
                    "fraction": item.get("fraction"),
                    "size_pct": item.get("size_pct")})
    return out


def apply(decisions: list[dict], positions: list[dict],
          price_map: dict[str, float]) -> list[dict]:
    """Execute the decisions. Returns alerts in check_positions' shape."""
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

        if d["action"] == "add":
            alert = _apply_add(pos, price, d)
        elif d["action"] == "trim":
            alert = _apply_trim(pos, price, d)
        else:
            alert = _apply_sell(pos, price, d)
        if alert:
            alerts.append(alert)
    return alerts


def _apply_sell(pos: dict, price: float, d: dict) -> dict | None:
    # Whole: this is close_reason, and close_reason is exactly what the
    # review scores. Cutting it means the review grades the truncation.
    reason = f"agent卖出: {d['reason']}"
    if not close_position(pos["id"], close_price=price, close_reason=reason):
        return None
    logger.info("Agent exit: %s %s @ %.2f — %s",
                d["code"], pos.get("name", ""), price, d["reason"])
    return {"type": "agent_exit", "code": d["code"], "name": pos.get("name", ""),
            "reason": reason, "close_price": price,
            "confidence": d.get("confidence", "")}


def _apply_trim(pos: dict, price: float, d: dict) -> dict | None:
    """Sell part and keep the rest. A real partial exit, not a relabelled one."""
    held = pos.get("shares") or 0
    try:
        frac = min(0.9, max(0.1, float(d.get("fraction"))))
    except (TypeError, ValueError):
        frac = DEFAULT_TRIM_FRACTION
    # Lot-aligned, because A-shares trade in board lots and the exit path
    # refuses a partial that is not a whole lot. Without this, a trim of a
    # 7000-share position succeeds (3500) and the next one is refused
    # (1750) — measured on a real run, where the second trim was refused and
    # the decision was silently dropped.
    sell = int(held * frac) // LOT_SIZE * LOT_SIZE
    reason = f"agent减仓: {d['reason']}"
    if sell <= 0 or sell >= held:
        # Below one lot, or the whole position: neither is a trim. Holding is
        # the honest outcome for the first, and a full exit dressed as a trim
        # would misreport what the agent decided.
        logger.info("Trim of %s not actionable (%d of %d股) — holding",
                    d["code"], sell, held)
        return None
    if not close_position(pos["id"], close_price=price,
                          close_reason=reason, shares=sell):
        # The exit path refused for a reason it logged (T+1, a limit, a
        # closed position). Holding is the honest outcome, not a silent full
        # exit, which is what the old code did.
        logger.info("Trim of %s refused by the exit path (%d of %d股) — holding",
                    d["code"], sell, held)
        return None
    return {"type": "agent_trim", "code": d["code"], "name": pos.get("name", ""),
            "reason": reason, "close_price": price,
            "confidence": d.get("confidence", "")}


def _apply_add(pos: dict, price: float, d: dict) -> dict | None:
    """Buy the second tranche, if the thesis left one unspent."""
    from alpha_agents.data.portfolio import add_to_position

    try:
        size = float(d.get("size_pct")) or None
    except (TypeError, ValueError):
        size = None
    result = add_to_position(pos["id"], price=price, size_pct=size,
                             reason=f"agent加仓: {d['reason']}")
    if not result:
        return None
    return {"type": "agent_add", "code": d["code"], "name": pos.get("name", ""),
            "reason": d["reason"], "add_shares": result["shares"],
            "add_price": price, "new_avg_price": result["avg_price"],
            "total_shares": result["total_shares"],
            "confidence": d.get("confidence", "")}


async def run(price_map: dict[str, float], signals: list[dict],
              narrative_due: list | None = None,
              trader_id: str | None = None) -> list[dict]:
    """Call a model only where one is actually needed.

    Three situations qualify. A thesis carrying a ``narrative`` condition
    has reached its review slot — the agent wrote something no rule can
    check and has to re-read it itself. A thesis whose **stated
    invalidation just fired**, which is the commitment coming due. Or a
    rule signal on a position that has no thesis at all, which is every
    position opened before this design existed and any pick whose
    recommendation omitted its invalidations.

    Everything else was already settled in code by ``thesis_monitor``, for
    free and identically every cycle. A quiet market now costs zero model
    calls instead of 48.

    Every failure path holds rather than sells: no candidates, no prices, a
    timeout, a bad response. The hard stop runs regardless, so holding on
    error cannot run the account down.
    """
    from alpha_agents.data.thesis import get_active

    signal_codes = {s["code"] for s in signals if s.get("type") == "signal"}
    # A thesis whose own stated invalidation just fired. This is the one
    # case that *must* reach the model: the agent committed in advance to
    # what would prove it wrong, the line has been crossed, and the whole
    # point is to hear its answer. The old wiring excluded exactly these
    # positions — reasonably, back when thesis_monitor closed them in code
    # — so leaving the exclusion in place would have made the wake-up
    # signal arrive nowhere.
    woken_codes = {s["code"] for s in signals
                   if s.get("type") == "signal" and s.get("thesis_id")}
    narrative_codes = {th.code for th, _ in (narrative_due or [])}

    candidates = []
    for pos in get_open_positions(trader_id):
        code = pos["code"]
        if not price_map.get(code):
            continue
        if code in narrative_codes or code in woken_codes:
            candidates.append(pos)
        elif code in signal_codes and not get_active(code=code,
                                                     trader_id=trader_id):
            # A rule wanted out and there is no plan on file to consult.
            candidates.append(pos)

    positions = candidates
    if not positions:
        return []

    context = build_context(positions, price_map, signals)
    trader = None
    if trader_id:
        from alpha_agents.data.trader import get_trader
        trader = get_trader(trader_id)
    try:
        decisions = await decide(context, trader)
    except asyncio.TimeoutError:
        logger.warning("Exit decision timed out after %ds — holding all",
                       _DECISION_TIMEOUT)
        note_unanswered(signals, [])
        return []
    except Exception as e:
        logger.warning("Exit decision failed (%s) — holding all", e)
        note_unanswered(signals, [])
        return []

    note_unanswered(signals, decisions)
    if not decisions:
        return []
    logger.info("Exit decisions: %s",
                ", ".join(f"{d['code']}={d['action']}" for d in decisions))
    return apply(decisions, positions, price_map)


def note_unanswered(signals: list[dict] | None,
                    decisions: list[dict] | None) -> list[str]:
    """Woken positions the agent's reply never mentions.

    A wake-up asks "还持有吗？". An answer of ``hold`` is a decision; no
    answer at all is a different thing, and the book cannot tell them apart
    — both leave the position open. That matters more here than anywhere
    else, because the whole reason a crossed invalidation now wakes the
    agent instead of closing the position is to find out whether it honours
    its own line. Silence scored as "held on purpose" would put the answer
    into the data without the agent ever giving it.

    Seen on 2026-01-15: 600276 filled, its stated invalidation fired the
    same session, the wake-up was handed over, and the reply covered only
    002555. The next day it answered ``sell``.

    Writes a checkpoint so the review can count it, and returns the codes.
    """
    woken = {s["code"]: s for s in (signals or [])
             if s.get("type") == "signal" and s.get("thesis_id")}
    if not woken:
        return []
    answered = {d.get("code") for d in (decisions or [])}
    missing = [code for code in woken if code not in answered]
    for code in missing:
        signal = woken[code]
        logger.warning("%s was woken by its own %s and the reply did not "
                       "mention it — recorded as unanswered, not as hold",
                       code, signal.get("kind") or "invalidation")
        try:
            from alpha_agents.data import thesis as T
            T.add_checkpoint(int(signal["thesis_id"]),
                             "唤醒后 agent 未作答（仓位因此留存，但这不是它的决定）",
                             T.TRIGGERED, kind=signal.get("kind", ""))
        except Exception as exc:                      # noqa: BLE001
            logger.debug("Could not record the unanswered wake for %s: %s",
                         code, exc)
    return missing


async def decide_for_replay(positions: list[dict], price_map: dict[str, float],
                            *, day: str, news_by_theme: dict[str, list[str]]
                            | None = None, trader=None,
                            model=None, mechanical_stops: bool = True,
                            phase: str = "open",
                            signals: list[dict] | None = None) -> list[dict]:
    """The sell side, for a historical replay.

    A separate entry point rather than a flag on :func:`run`, because the two
    differ in what they may read, and that difference is the whole risk:

    * ``run`` decides *which* positions are worth a model call, using theses
      and rule signals. A replay asks about every open position, every day,
      and passes its own ``signals`` — which since the autonomous arm do
      include theses: a crossed invalidation arrives here as a signal
      rather than having already closed the position, so the agent answers
      for its own stated commitment instead of being overruled by it.
    * ``run`` builds its context with ``_news_for_theme``, which calls
      ``news_index.search_news(hours=...)``. That function searches the whole
      vector store by wall-clock offset and does **not** consult
      ``replay_mode`` (verified: zero references). Using it in a replay would
      read flashes published after the replay day — the future leak this
      runner exists to prevent.

    So the caller supplies the news, already windowed to its own day, as
    ``news_by_theme``. Passing ``None`` means "no news", which is honest and
    not the same as "searched and found nothing" — the block says so.

    The mechanical hard stop is **not** bypassed: ``_settle_exits`` runs
    first in the runner, so a position that gapped through its stop is
    already closed by the time this is asked. This function cannot reopen
    one, and it must not be called before that settlement.
    """
    if not positions:
        return []

    if phase not in ("open", "close"):
        raise ValueError(f"phase must be 'open' or 'close', not {phase!r}")
    context = build_context(positions, price_map, signals=signals or [],
                           news_by_theme=news_by_theme,
                           mechanical_stops=mechanical_stops,
                           phase=phase)
    # Every exit from here notes the wake-ups that got no answer, including
    # the failure paths — those are the cases where *nothing* was answered,
    # and "the model timed out" must not settle into the book as "the agent
    # decided to hold".
    decisions: list[dict] = []
    try:
        decisions = await decide(context, trader, model=model, tools=[])
    except asyncio.TimeoutError:
        logger.warning("Replay exit decision timed out after %ds — holding all",
                       _DECISION_TIMEOUT)
    except Exception as e:
        # Every failure path holds. Selling on an unreadable reply is how a
        # model outage becomes a liquidation.
        logger.warning("Replay exit decision failed (%s) — holding all", e)
    if decisions:
        logger.info("%s: exit decisions %s", day,
                    ", ".join(f"{d['code']}={d['action']}" for d in decisions))
    note_unanswered(signals, decisions)
    return decisions


def morning_calls_key(trader_id: str | None = None) -> str:
    """Where one trader's morning position calls are parked.

    The default trader keeps the original key so a call written before
    traders existed is still found and applied.
    """
    from alpha_agents.data.trader import DEFAULT_TRADER
    return ("morning_position_calls"
            if not trader_id or trader_id == DEFAULT_TRADER
            else f"morning_position_calls:{trader_id}")


def pending_morning_calls(today: str,
                          trader_id: str | None = None) -> list[dict]:
    """The morning scan's decisions on open positions, once and once only.

    Consumed on read: the first intraday cycle after the open applies them
    against real prices, and later cycles must not re-run a trim the
    market has already moved past. Per trader, so one book consuming its
    calls cannot swallow another's.
    """
    from alpha_agents.data.memory_store import _get_conn, _write_lock

    key = morning_calls_key(trader_id)
    try:
        conn = _get_conn()
        row = conn.execute(
            "SELECT data FROM daily_snapshots WHERE date = ? AND "
            "data_type = ?", (today, key)).fetchone()
        if not row:
            return []
        calls = json.loads(row["data"])
        with _write_lock:
            conn.execute("DELETE FROM daily_snapshots WHERE date = ? AND "
                         "data_type = ?", (today, key))
            conn.commit()
    except Exception as e:
        logger.warning("Morning position calls unreadable: %s", e)
        return []

    if calls:
        logger.info("Applying %d morning position calls at the open", len(calls))
    return calls
