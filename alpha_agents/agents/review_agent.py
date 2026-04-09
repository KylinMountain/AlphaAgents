"""Post-market review agent — verifies predictions and updates theme lines."""

import asyncio
import logging
from datetime import datetime

from agents import Agent, Runner
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from openai import AsyncOpenAI

from alpha_agents.config import (
    PROMPTS_DIR, AGENT_API_KEY, AGENT_BASE_URL, AGENT_MODEL,
)
from alpha_agents.tools.registry import (
    get_stock_quotes, get_sector_data, get_sector_ranking,
    get_lhb_detail, get_north_flow, get_market_breadth,
    get_stock_fund_flow, get_block_trade,
)

logger = logging.getLogger(__name__)

REVIEW_TOOLS = [
    get_stock_quotes, get_sector_data, get_sector_ranking,
    get_lhb_detail, get_north_flow, get_market_breadth,
    get_stock_fund_flow, get_block_trade,
]


def _create_model() -> OpenAIChatCompletionsModel:
    client = AsyncOpenAI(api_key=AGENT_API_KEY, base_url=AGENT_BASE_URL)
    return OpenAIChatCompletionsModel(model=AGENT_MODEL or "qwen-plus", openai_client=client)


def _create_review_agent() -> Agent:
    prompt = (PROMPTS_DIR / "review.md").read_text(encoding="utf-8")
    return Agent(
        name="review_analyst",
        instructions=prompt,
        model=_create_model(),
        tools=REVIEW_TOOLS,
    )


async def run_review_analysis(
    predictions_context: str,
    themes_context: str,
    stats_context: str,
    lessons_context: str = "暂无历史经验",
    hooks=None,
) -> str:
    """Run post-market review analysis.

    Args:
        predictions_context: Today's predictions to verify.
        themes_context: Active theme lines to evaluate.
        stats_context: Recent prediction stats.
        lessons_context: Historical lessons for reference.
        hooks: Optional event hooks.

    Returns:
        Review report text.
    """
    agent = _create_review_agent()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    user_message = (
        f"[当前时间: {now}]\n\n"
        f"【待验证预测】\n{predictions_context}\n\n"
        f"【活跃主线】\n{themes_context}\n\n"
        f"【近期预测表现】\n{stats_context}\n\n"
        f"【历史经验教训】\n{lessons_context}\n\n"
        f"请执行收盘复盘分析。"
    )

    logger.info("Review agent starting...")
    try:
        result = await asyncio.wait_for(
            Runner.run(agent, user_message, hooks=hooks, max_turns=40),
            timeout=300,
        )
        logger.info("Review agent finished, length=%d", len(result.final_output))
        return result.final_output
    except asyncio.TimeoutError:
        logger.error("Review agent timed out after 300s")
        return "[复盘超时，未生成报告]"
    except Exception as e:
        logger.error("Review agent failed: %s", e)
        return f"[复盘失败: {e}]"
