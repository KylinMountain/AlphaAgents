"""Stock-choice stage for sector-first B-arm experiments.

This agent chooses *which* panel names deserve an order plan. It does not set
entry, stop, target or size. B and C therefore share the same downstream trade
planner; C only replaces this stock-choice stage with a transparent rule.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path

from agents import Agent, Runner
from agents.exceptions import MaxTurnsExceeded

from alpha_agents.agents import t1_decider
from alpha_agents.agents.json_reply import picks_line, json_body, match_offered
from alpha_agents.config import PROMPTS_DIR


PROMPT_FILE = "sector_stock_select.md"
DEFAULT_MAX_TURNS = 18

SYSTEM_INSTRUCTIONS = (
    "你是一名A股个股研究交易员。方向已经由上游冻结。"
    "你只负责在给出的股票面板里选择值得进入交易计划阶段的股票，"
    "不负责决定价格、止损、目标或仓位。只能使用当前消息和工具事实。"
    "输出必须是单个JSON对象。"
)

class StockSelectorError(RuntimeError):
    pass


def load_prompt(path: Path | None = None) -> str:
    return (path or (PROMPTS_DIR / PROMPT_FILE)).read_text(encoding="utf-8")


def build_message(*, day: str, prev_day: str, panel: list[dict],
                  news_window: str = "",
                  news: list[dict], market: dict, book: str,
                  knowledge: str, trader_note: str, picks: int | None,
                  template: str | None = None) -> str:
    fields = {
        "day": day,
        "prev_day": prev_day,
        # Named by the caller, which is the code that computed the bounds.
        # A default here would be the same guess the template used to make.
        "news_window": news_window or f"{prev_day} 15:00 → {day} 09:00",
        "panel": t1_decider.format_panel(panel),
        "news": t1_decider.format_news(news),
        "market": t1_decider.format_market(market),
        "book": book or "（空仓）",
        "knowledge": knowledge or "（还没有任何经验在生效）",
        "trader_note": trader_note or "",
        "picks": picks_line(picks),
    }
    prompt = template if template is not None else load_prompt()
    try:
        text = prompt.format(**fields)
    except (KeyError, IndexError) as exc:
        raise StockSelectorError(
            f"stock-selector prompt and renderer disagree: {exc}") from exc
    leftover = re.search(r"\{[A-Za-z_]{3,}\}", text)
    if leftover:
        raise StockSelectorError(
            f"stock-selector prompt still contains {leftover.group(0)}")
    return text


def parse(text: str, offered: set[str], *, picks: int | None,
          offered_themes: dict[str, list[str]] | None = None) -> dict:
    body = json_body(text)
    if not body:
        return {"stocks": [], "refused": [], "parse_error": "empty reply"}
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        return {
            "stocks": [], "refused": [],
            "parse_error": f"{type(exc).__name__}: {exc}"}
    rows = payload.get("stocks") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return {
            "stocks": [], "refused": [],
            "parse_error": "reply is not an object with a stocks list"}

    selected = []
    refused = []
    seen = set()
    for raw in rows:
        if not isinstance(raw, dict):
            refused.append({"code": "", "why": "not_an_object", "detail": ""})
            continue
        code = str(raw.get("code") or "").strip()
        if code not in offered:
            refused.append({"code": code, "why": "outside_panel", "detail": ""})
            continue
        if code in seen:
            refused.append({"code": code, "why": "duplicate", "detail": ""})
            continue
        if picks is not None and len(selected) >= picks:
            refused.append({
                "code": code, "why": "too_many", "detail": f"max={picks}"})
            continue
        primary_theme = str(raw.get("primary_theme") or "").strip()
        if offered_themes is not None:
            eligible = [
                str(value).strip()
                for value in (offered_themes.get(code) or [])
                if str(value).strip()
            ]
            if not primary_theme:
                refused.append({
                    "code": code, "why": "missing_primary_theme", "detail": ""})
                continue
            # Match on the name with its whitespace removed. THS concept
            # names carry spaces a model reliably drops — measured
            # 2026-01-06, the selector answered "中国AI50" for the offered
            # "中国AI 50" and both of its picks were refused, so a day whose
            # direction, stocks and reasoning were all correct placed no
            # orders at all. This does not loosen the check: only the exact
            # offered names are accepted, and the one that matches is the one
            # written down, so nothing downstream sees the model's spelling.
            match = match_offered(primary_theme, eligible)
            if match is None:
                refused.append({
                    "code": code,
                    "why": "invalid_primary_theme",
                    "detail": f"{primary_theme!r} not in {eligible}",
                })
                continue
            primary_theme = match
        seen.add(code)
        row = {
            "code": code,
            "reason": str(raw.get("reason") or "").strip(),
            "counterevidence": str(
                raw.get("counterevidence") or "").strip()[:400],
        }
        if primary_theme:
            row["primary_theme"] = primary_theme
        selected.append(row)
    return {"stocks": selected, "refused": refused, "parse_error": None}


async def propose(*, day: str, prev_day: str, panel: list[dict],
                  news_window: str = "",
                  news: list[dict], market: dict, book: str = "",
                  knowledge: str = "", trader_note: str = "", picks: int | None = None,
                  model=None, tools: list | None = None,
                  research_budget=None, loop=None,
                  max_turns: int | None = None) -> dict:
    del loop  # the async entrypoint does not own event-loop lifecycle
    if model is None:
        from alpha_agents.model_factory import create_model
        model = create_model()
    message = build_message(
        day=day, prev_day=prev_day, panel=panel, news=news, market=market,
        news_window=news_window,
        book=book, knowledge=knowledge, trader_note=trader_note, picks=picks)

    # A tool-less stage may still carry the shared decision budget.
    from alpha_agents.tools.budget import ResearchBudget, use_research_budget

    budget = research_budget
    if tools:
        budget = budget or ResearchBudget()
        message += "\n\n" + budget.prompt_hint()

    agent = Agent(
        name="sector_stock_selector_v0",
        instructions=SYSTEM_INSTRUCTIONS,
        model=model,
        tools=list(tools) if tools else [],
    )
    turns = max_turns or DEFAULT_MAX_TURNS
    started = time.monotonic()
    try:
        if budget is None:
            result = await Runner.run(agent, message, max_turns=turns)
        else:
            with use_research_budget(budget):
                result = await Runner.run(agent, message, max_turns=turns)
    except MaxTurnsExceeded as exc:
        return {
            "stocks": [], "refused": [], "raw": "",
            "parse_error": f"MaxTurnsExceeded after {turns} turns ({exc})",
            "research_budget": budget.summary() if budget else None,
            "research_trace": budget.trace() if budget else [],
            "model_elapsed_ms": int((time.monotonic() - started) * 1000),
        }

    raw = result.final_output or ""
    offered_themes = {
        str(row["code"]): list(
            row.get("eligible_themes")
            or [row.get("primary_theme"), *(row.get("supporting_themes") or [])]
        )
        for row in panel
    }
    parsed = parse(
        raw, {str(row["code"]) for row in panel}, picks=picks,
        offered_themes=offered_themes)
    parsed["raw"] = raw
    parsed["research_budget"] = budget.summary() if budget else None
    parsed["research_trace"] = budget.trace() if budget else []
    parsed["model_elapsed_ms"] = int((time.monotonic() - started) * 1000)
    return parsed


def propose_sync(*, loop: asyncio.AbstractEventLoop | None = None,
                 **kwargs) -> dict:
    if loop is None:
        return asyncio.run(propose(**kwargs))
    return loop.run_until_complete(propose(**kwargs))


def simple(panel: list[dict], *, picks: int | None) -> dict:
    """Preregistered C-arm selector: take the first panel rows as frozen.

    ``None`` takes the whole panel. This arm is a fixed rule with no
    judgement to exercise, so "how many" has to come from somewhere; the
    panel itself is the honest answer once the cap is gone.
    """
    chosen = [
        {
            "code": str(row["code"]),
            "reason": "transparent_panel_order",
            "counterevidence": "",
            "primary_theme": str(row.get("primary_theme") or ""),
        }
        for row in (panel if picks is None else panel[:max(0, int(picks))])
    ]
    return {
        "stocks": chosen,
        "refused": [],
        "raw": "",
        "parse_error": None,
        "research_budget": None,
        "research_trace": [],
    }
