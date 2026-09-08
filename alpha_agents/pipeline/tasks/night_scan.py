"""Night scan task — runs at 20:00 every day.

Focuses on US market open, overnight futures, and impact on next-day A-shares.
Unlike morning scan, this does NOT re-fetch all 12 news sources — only
overseas sources and global market data.
"""

import json
import logging
import time

from agents import Agent, Runner
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from openai import AsyncOpenAI

from alpha_agents.config import (
    PROMPTS_DIR, AGENT_API_KEY, AGENT_BASE_URL, AGENT_MODEL,
)
from alpha_agents.data.memory_store import get_active_themes, get_prediction_stats
from alpha_agents.tools.registry import (
    get_global_overview, get_us_market, get_bond_yields,
    get_pizzint, web_search,
)
from alpha_agents.tools.global_market import get_global_overview_fn
from alpha_agents.notify import notify_all

logger = logging.getLogger(__name__)

NIGHT_TOOLS = [get_global_overview, get_us_market, get_bond_yields, get_pizzint, web_search]


from alpha_agents.model_factory import (
    create_model as _create_model,
    create_model_settings,
)


def _format_themes(themes: list[dict]) -> str:
    if not themes:
        return "无活跃主线"
    lines = []
    for t in themes:
        stocks = json.loads(t["core_stocks"]) if t["core_stocks"] else []
        leader = next((s["name"] for s in stocks if s.get("role") == "龙头"), "无")
        lines.append(f"- {t['name']}（累计强度 {t['strength']}/10, 今日 {t.get('daily_score', 0):+d}, {t['status']}）龙头: {leader}")
    return "\n".join(lines)


async def run_night_scan() -> str | None:
    """Execute the night scan task.

    1. Pre-fetch global market overview
    2. Read active themes
    3. Run night agent to analyze impact on tomorrow
    4. Push notification

    Returns the night report text, or None if nothing significant.
    """
    import asyncio
    from datetime import datetime

    logger.info("Night scan starting...")

    # 1. Pre-fetch global market data
    global_ctx = ""
    overview = {}
    try:
        overview = json.loads(await asyncio.to_thread(get_global_overview_fn))
        lines = []
        for idx in overview.get("us_indices", []):
            lines.append(f"  {idx['name']}: {idx['close']} ({idx['change_pct']:+.2f}%)")
        bonds = overview.get("bond_yields", {})
        if bonds.get("us_10y"):
            lines.append(f"  美债10Y: {bonds['us_10y']}%")
        if bonds.get("cn_us_spread"):
            lines.append(f"  中美利差: {bonds['cn_us_spread']}%")
        for sig in overview.get("signals", []):
            lines.append(f"  信号: {sig}")
        global_ctx = "【当前外盘数据】\n" + "\n".join(lines)
    except Exception as e:
        logger.debug("Night scan: global overview failed: %s", e)
        global_ctx = "【当前外盘数据】获取失败"

    # 2. Read active themes
    themes = get_active_themes()
    themes_ctx = _format_themes(themes)

    # 2b. Prediction stats (recent 7 days)
    pred_ctx = ""
    try:
        stats = get_prediction_stats(days=7)
        if stats.get("total", 0) > 0:
            lines = [
                f"总推荐: {stats['total']}次, 命中: {stats['hits']}次, "
                f"命中率: {stats.get('hit_rate', 0):.1f}%"
            ]
            for conf, data in stats.get("by_confidence", {}).items():
                lines.append(f"  {conf}信心: {data['hit_rate']:.1f}% ({data['hits']}/{data['total']})")
            pred_ctx = "【近7日预测统计】\n" + "\n".join(lines)
    except Exception as e:
        logger.debug("Night scan: prediction stats failed: %s", e)

    # 3. Run night agent
    prompt_text = (PROMPTS_DIR / "night_scan.md").read_text(encoding="utf-8")
    agent = Agent(
        name="night_analyst",
        instructions=prompt_text,
        model=_create_model(),
        model_settings=create_model_settings(),
        tools=NIGHT_TOOLS,
    )

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    context_parts = [
        f"[当前时间: {now}]",
        global_ctx,
        f"【当前活跃主线】\n{themes_ctx}",
    ]
    if pred_ctx:
        context_parts.append(pred_ctx)
    context_parts.append("请生成今日夜报，分析外盘对明日A股的影响。")
    user_message = "\n\n".join(context_parts)

    logger.info("Night agent starting...")
    try:
        result = await asyncio.wait_for(
            Runner.run(agent, user_message, max_turns=20),
            timeout=120,
        )
        report = result.final_output
        logger.info("Night agent finished, length=%d", len(report))
    except asyncio.TimeoutError:
        logger.warning("Night agent timed out")
        return None
    except Exception as e:
        logger.error("Night agent failed: %s", e)
        return None

    # 4. Conditional push — only push if market moves are significant
    if report:
        # Determine significance: check US index moves or keywords in report
        significant = False
        try:
            for idx in overview.get("us_indices", []):
                if abs(idx.get("change_pct", 0)) >= 1.5:
                    significant = True
                    break
        except Exception as e:
            logger.warning("Significance check failed, defaulting to significant: %s", e)
            significant = True
        # Also check report text for high-impact keywords
        high_impact_kw = ("重大", "剧烈", "暴跌", "暴涨", "熔断", "危机")
        low_impact_kw = ("影响有限", "平稳", "波澜不惊")
        if any(kw in report for kw in high_impact_kw):
            significant = True
        if not significant and any(kw in report for kw in low_impact_kw):
            significant = False  # explicitly skip

        if significant:
            try:
                await asyncio.to_thread(
                    notify_all,
                    f"AlphaAgents 夜报 | {time.strftime('%m-%d')}",
                    report[:500],
                )
            except Exception as e:
                logger.error("Night scan notification failed: %s", e)
        else:
            logger.info("Night scan: no significant moves, skipping push notification")

    return report
