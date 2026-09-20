"""Sector-first direction selector.

This stage allocates research attention. It never places orders.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from agents import Agent, Runner
from agents.exceptions import MaxTurnsExceeded

from alpha_agents.agents.json_reply import json_body
from alpha_agents.config import PROMPTS_DIR


DECIDER_NAME = "sector_selector_v0"
PROMPT_FILE = "sector_select.md"
DEFAULT_MAX_TURNS = 10
MAX_SELECTED = 3

SYSTEM_INSTRUCTIONS = (
    "你是一名A股研究交易员。只使用用户消息和工具中的事实。"
    "你的任务是选择值得继续研究的方向，不是直接下单。"
    "输出必须是单个JSON对象，没有其它文字。"
)

class SectorSelectorError(RuntimeError):
    pass


def load_prompt(path: Path | None = None) -> str:
    return (path or (PROMPTS_DIR / PROMPT_FILE)).read_text(encoding="utf-8")


def _show(value, suffix="") -> str:
    return "-" if value is None else f"{value}{suffix}"


def format_sector_cards(rows: list[dict]) -> str:
    if not rows:
        return "（没有可评估方向）"
    lines = [
        "|排名|方向|5日相对中位收益|上涨广度|5日广度变化|剔除头1后5日中位|资金覆盖|净额合计|",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        fund = row.get("fund_flow") or {}
        covered = fund.get("covered")
        members = fund.get("member_count")
        coverage = "-" if covered is None or members is None else f"{covered}/{members}"
        lines.append(
            f"|{_show(row.get('rank'))}|{row.get('sector_id', '')}|"
            f"{_show((row.get('relative_returns_pct') or {}).get('5d'), '%')}|"
            f"{_show(row.get('advancers_pct'), '%')}|"
            f"{_show(row.get('breadth_improvement_5d_pp'), 'pp')}|"
            f"{_show(row.get('ex_top1_5d_median_pct'), '%')}|"
            f"{coverage}|{_show(fund.get('net_amount_sum'))}|")
    return "\n".join(lines)


def build_message(*, day: str, as_of_session: str, sectors: list[dict],
                  market: dict | None = None, news: list[dict] | None = None,
                  template: str | None = None) -> str:
    prompt = template if template is not None else load_prompt()
    fields = {
        "day": day,
        "as_of_session": as_of_session,
        "sector_cards": format_sector_cards(sectors),
        "market": json.dumps(market or {}, ensure_ascii=False, sort_keys=True),
        "news": "\n".join(
            f"• {row.get('time', '')} {row.get('title', '')}"
            for row in (news or [])) or "（无）",
        "max_selected": MAX_SELECTED,
    }
    try:
        text = prompt.format(**fields)
    except (KeyError, IndexError) as exc:
        raise SectorSelectorError(
            f"sector prompt and renderer disagree: {exc}") from exc
    leftover = re.search(r"\{[A-Za-z_]{3,}\}", text)
    if leftover:
        raise SectorSelectorError(
            f"sector prompt still contains {leftover.group(0)}")
    return text


def parse_selection(text: str, offered: set[str],
                    max_selected: int = MAX_SELECTED) -> dict:
    body = json_body(text)
    if not body:
        return {"themes": [], "refused": [], "parse_error": "empty reply"}
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        return {"themes": [], "refused": [],
                "parse_error": f"{type(exc).__name__}: {exc}"}
    if not isinstance(payload, dict) or not isinstance(payload.get("themes"), list):
        return {"themes": [], "refused": [],
                "parse_error": "reply is not an object with a themes list"}

    selected, refused, seen = [], [], set()
    for index, raw in enumerate(payload["themes"]):
        if not isinstance(raw, dict):
            refused.append({"sector_id": "", "why": "not_an_object",
                            "detail": str(index)})
            continue
        sector_id = str(raw.get("sector_id") or "").strip()
        if sector_id not in offered:
            refused.append({"sector_id": sector_id, "why": "outside_shortlist",
                            "detail": ""})
            continue
        if sector_id in seen:
            refused.append({"sector_id": sector_id, "why": "duplicate",
                            "detail": ""})
            continue
        if len(selected) >= max_selected:
            refused.append({"sector_id": sector_id, "why": "too_many",
                            "detail": f"max={max_selected}"})
            continue
        invalidations = raw.get("invalidations") or []
        if not isinstance(invalidations, list):
            refused.append({"sector_id": sector_id, "why": "bad_invalidations",
                            "detail": ""})
            continue
        seen.add(sector_id)
        selected.append({
            "sector_id": sector_id,
            "thesis": str(raw.get("thesis") or "").strip()[:500],
            "counterevidence": str(raw.get("counterevidence") or "").strip()[:500],
            "unknowns": str(raw.get("unknowns") or "").strip()[:500],
            "invalidations": [
                str(value).strip()[:200]
                for value in invalidations if str(value).strip()
            ][:5],
        })
    return {"themes": selected, "refused": refused, "parse_error": None}


async def propose(*, day: str, as_of_session: str, sectors: list[dict],
                  market: dict | None = None, news: list[dict] | None = None,
                  model=None, template: str | None = None, tools: list | None = None,
                  research_budget=None, max_turns: int | None = None) -> dict:
    if model is None:
        from alpha_agents.model_factory import create_model
        model = create_model()
    max_turns = max_turns or DEFAULT_MAX_TURNS
    message = build_message(
        day=day, as_of_session=as_of_session, sectors=sectors,
        market=market, news=news, template=template)

    # A tool-less stage may still carry the shared decision budget.
    from alpha_agents.tools.budget import ResearchBudget, use_research_budget

    budget = research_budget
    if tools:
        budget = budget or ResearchBudget()
        message += "\n\n" + budget.prompt_hint()

    agent = Agent(
        name=f"sector_selector:{DECIDER_NAME}",
        instructions=SYSTEM_INSTRUCTIONS,
        model=model,
        tools=list(tools) if tools else [],
    )
    try:
        if budget is None:
            result = await Runner.run(agent, message, max_turns=max_turns)
        else:
            with use_research_budget(budget):
                result = await Runner.run(agent, message, max_turns=max_turns)
    except MaxTurnsExceeded as exc:
        return {
            "themes": [], "refused": [], "raw": "",
            "parse_error": f"MaxTurnsExceeded after {max_turns} turns ({exc})",
            "research_budget": budget.summary() if budget else None,
        }

    raw = result.final_output or ""
    parsed = parse_selection(
        raw, {str(row.get("sector_id") or "") for row in sectors})
    parsed["raw"] = raw
    parsed["research_budget"] = budget.summary() if budget else None
    return parsed


def propose_sync(*, loop: asyncio.AbstractEventLoop | None = None, **kwargs) -> dict:
    if loop is None:
        return asyncio.run(propose(**kwargs))
    return loop.run_until_complete(propose(**kwargs))
