"""Morning scan agent — pre-market analysis and daily briefing."""

import asyncio
import json
import logging
from datetime import datetime

from agents import Agent, Runner
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from openai import AsyncOpenAI

from alpha_agents.config import (
    PROMPTS_DIR, AGENT_API_KEY, AGENT_BASE_URL, AGENT_MODEL,
)
from alpha_agents.tools.registry import (
    search_stocks, get_sector_data, filter_stocks,
    get_stock_quotes, get_market_breadth, get_sector_ranking,
    get_anomaly_stocks, get_us_market, get_bond_yields, get_global_overview,
    get_institutional_position, get_sector_best_stocks, web_search, get_pizzint,
)

logger = logging.getLogger(__name__)

# Morning agent gets analysis tools but NOT news tools (news is pre-fetched)
MORNING_TOOLS = [
    search_stocks, get_sector_data, filter_stocks,
    get_stock_quotes, get_market_breadth, get_sector_ranking,
    get_anomaly_stocks, get_us_market, get_bond_yields, get_global_overview,
    get_institutional_position, get_sector_best_stocks, web_search, get_pizzint,
]


def _create_model() -> OpenAIChatCompletionsModel:
    client = AsyncOpenAI(api_key=AGENT_API_KEY, base_url=AGENT_BASE_URL)
    return OpenAIChatCompletionsModel(model=AGENT_MODEL or "qwen-plus", openai_client=client)


def _create_morning_agent() -> Agent:
    prompt = (PROMPTS_DIR / "morning_scan.md").read_text(encoding="utf-8")
    return Agent(
        name="morning_analyst",
        instructions=prompt,
        model=_create_model(),
        tools=MORNING_TOOLS,
    )


async def run_morning_analysis(
    events_summary: str,
    themes_context: str,
    stats_context: str,
    hooks=None,
) -> str:
    """Run morning scan analysis with memory context.

    Args:
        events_summary: Pre-digested news events as text.
        themes_context: Active theme lines summary.
        stats_context: Prediction hit rate stats.
        hooks: Optional event hooks.

    Returns:
        Morning briefing report text.
    """
    agent = _create_morning_agent()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S %A")

    user_message = (
        f"[当前时间: {now}]\n\n"
        f"【活跃主线】\n{themes_context}\n\n"
        f"【近期预测表现】\n{stats_context}\n\n"
        f"【隔夜新闻事件】\n{events_summary}\n\n"
        f"请生成今日晨报。"
    )

    logger.info("Morning agent starting...")
    if hooks is None:
        from alpha_agents.agents.hooks import ToolEventHooks
        hooks = ToolEventHooks(callback=None, agent_label="morning")
    try:
        result = await asyncio.wait_for(
            Runner.run(agent, user_message, hooks=hooks, max_turns=40),
            timeout=300,
        )
        logger.info("Morning agent finished, length=%d", len(result.final_output))
        return result.final_output
    except asyncio.TimeoutError:
        logger.error("Morning agent timed out after 300s")
        return "[晨扫超时，未生成报告]"
    except Exception as e:
        logger.error("Morning agent failed: %s", e)
        return f"[晨扫失败: {e}]"
