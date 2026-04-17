"""L2 Lessons — extract structured lessons from review report, consolidate into principles."""

from __future__ import annotations

import json
import logging
import re

from alpha_agents.data.memory_store import (
    insert_daily_lesson,
    get_recent_daily_lessons,
    get_all_principles_including_weakened,
    create_trading_principle,
    reinforce_trading_principle,
    set_principle_status,
)

logger = logging.getLogger(__name__)

# Parallel to vpa.py's <!-- VERDICT: {...} --> pattern.
_LESSONS_TAG_RE = re.compile(r"<!--\s*LESSONS:\s*(\[.*?\])\s*-->", re.DOTALL)


def extract_daily_lessons(report: str, today: str) -> int:
    """Parse the <!-- LESSONS: [...] --> tag from a review report and persist rows.

    Returns the count of lessons successfully inserted. Unparseable/missing
    tag returns 0 (warn-logs only — don't raise, the review itself succeeded).
    """
    m = _LESSONS_TAG_RE.search(report)
    if not m:
        logger.info("No LESSONS tag in review report; skipping extraction")
        return 0

    raw = m.group(1).strip()
    try:
        items = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning("LESSONS JSON malformed: %s", e)
        return 0

    if not isinstance(items, list):
        logger.warning("LESSONS payload is not a list")
        return 0

    count = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        content = (item.get("content") or "").strip()
        lesson_type = (item.get("type") or "").strip()
        if not content or lesson_type not in ("success", "failure", "insight"):
            continue
        try:
            insert_daily_lesson(
                date=today,
                lesson_type=lesson_type,
                theme=(item.get("theme") or None) or None,
                content=content,
                tags=(item.get("tags") or ""),
            )
            count += 1
        except Exception as e:
            logger.warning("Failed to insert lesson %s: %s", content[:40], e)
    return count


_CONSOLIDATION_SYSTEM_PROMPT = """你是交易经验沉淀师。
读今天新增的 daily_lessons（实盘观察）+ 已有 trading_principles（历史沉淀的量价经验，风格类似 Anna Coulling《量价分析》的法则），判断是否：
- CREATE: 今天的 lesson 揭示了新的可复用模式，创建新 principle（必须带具体 pattern_description + action_guidance + 至少1条 evidence）
- REINFORCE: 今天的 lesson 是已有 principle 的新一个佐证案例
- WEAKEN: 今天的 lesson 反驳了某条 principle（或 principle 的 evidence 胜率已低于40%）

输出**只能**是这个 JSON（不要其他内容）：
{"operations": [
  {"op": "create", "principle": "...", "pattern_description": "...", "category": "vpa_signal|theme_timing|entry|exit|risk", "action_guidance": "...", "evidence": [{"code":"...", "date":"...", "outcome":"..."}]},
  {"op": "reinforce", "principle_id": 123, "new_case": {"code":"...", "date":"...", "outcome":"..."}},
  {"op": "weaken", "principle_id": 456, "reason": "..."}
]}

原则:
- principle 要具体到**量价形态+位置+量能配合**，不要空话
- 每条 principle 必须有证据支撑
- 若今天没值得沉淀的东西，输出 {"operations": []}"""


def _call_consolidation_llm(lessons: list[dict], principles: list[dict]) -> dict:
    """Call the consolidation LLM. Returns parsed JSON dict with 'operations' list."""
    from openai import OpenAI
    from alpha_agents.config import AGENT_API_KEY, AGENT_BASE_URL, AGENT_MODEL

    user_content = (
        "【今天新 lessons】\n" + json.dumps(lessons, ensure_ascii=False, indent=2)
        + "\n\n【已有 principles】\n" + json.dumps(
            [{"id": p["id"], "principle": p["principle"], "status": p["status"],
              "evidence_count": p.get("evidence_count", 0)} for p in principles],
            ensure_ascii=False, indent=2,
        )
    )
    client = OpenAI(api_key=AGENT_API_KEY, base_url=AGENT_BASE_URL)
    resp = client.chat.completions.create(
        model=AGENT_MODEL or "qwen-plus",
        messages=[
            {"role": "system", "content": _CONSOLIDATION_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        max_tokens=3000,
        timeout=60,
    )
    content = (resp.choices[0].message.content or "").strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        m = re.search(r'\{.*"operations".*\}', content, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
        logger.warning("Consolidation LLM returned unparseable content: %s",
                       content[:200])
        return {"operations": []}


def consolidate_principles(today: str) -> dict:
    """Run the daily consolidation: LLM decides create/reinforce/weaken.

    Returns dict {created, reinforced, weakened} counts.
    """
    lessons = get_recent_daily_lessons(days=1)  # only today's
    if not lessons:
        return {"created": 0, "reinforced": 0, "weakened": 0}

    principles = get_all_principles_including_weakened()

    try:
        result = _call_consolidation_llm(lessons, principles)
    except Exception as e:
        logger.warning("Consolidation LLM call failed: %s", e)
        return {"created": 0, "reinforced": 0, "weakened": 0}

    ops = result.get("operations", []) if isinstance(result, dict) else []
    counts = {"created": 0, "reinforced": 0, "weakened": 0}
    for op in ops:
        if not isinstance(op, dict):
            continue
        kind = op.get("op")
        try:
            if kind == "create":
                create_trading_principle(
                    principle=op["principle"],
                    pattern_description=op["pattern_description"],
                    category=op.get("category", "insight"),
                    action_guidance=op["action_guidance"],
                    evidence=op.get("evidence", []),
                    today=today,
                )
                counts["created"] += 1
            elif kind == "reinforce":
                reinforce_trading_principle(
                    op["principle_id"], today=today,
                    new_case=op.get("new_case"),
                )
                counts["reinforced"] += 1
            elif kind == "weaken":
                set_principle_status(op["principle_id"], "weakened")
                counts["weakened"] += 1
        except Exception as e:
            logger.warning("Consolidation op %s failed: %s", op, e)
    return counts


async def post_review(today: str, review_report: str) -> str:
    """Phase 2 entry point: extract lessons, consolidate into principles.

    Called from review.py after the review report is generated. Returns a
    short summary string to append to the report (or empty string if nothing
    happened).
    """
    import asyncio

    # Step ①: extract structured lessons from the report's LESSONS tag
    lesson_count = await asyncio.to_thread(extract_daily_lessons, review_report, today)

    # Step ②: LLM consolidation (only if we got new lessons today)
    if lesson_count > 0:
        counts = await asyncio.to_thread(consolidate_principles, today)
    else:
        counts = {"created": 0, "reinforced": 0, "weakened": 0}

    if lesson_count == 0 and sum(counts.values()) == 0:
        return ""

    lines = ["【经验沉淀】"]
    if lesson_count > 0:
        lines.append(f"• 今日提取 {lesson_count} 条 lessons")
    if counts["created"]:
        lines.append(f"• 新增 {counts['created']} 条 principles")
    if counts["reinforced"]:
        lines.append(f"• 强化 {counts['reinforced']} 条 principles")
    if counts["weakened"]:
        lines.append(f"• 减弱 {counts['weakened']} 条 principles")
    return "\n".join(lines)
