"""Intraday monitoring agent — detects anomalies and traces causes."""

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
    get_sector_data, get_sector_ranking, get_anomaly_stocks,
    get_stock_quotes, get_stock_fund_flow, get_north_flow,
    get_lhb_detail, web_search,
)

logger = logging.getLogger(__name__)

INTRADAY_TOOLS = [
    get_sector_data, get_sector_ranking, get_anomaly_stocks,
    get_stock_quotes, get_stock_fund_flow, get_north_flow,
    get_lhb_detail, web_search,
]


def _create_model() -> OpenAIChatCompletionsModel:
    client = AsyncOpenAI(api_key=AGENT_API_KEY, base_url=AGENT_BASE_URL)
    return OpenAIChatCompletionsModel(model=AGENT_MODEL or "qwen-plus", openai_client=client)


def _create_intraday_agent() -> Agent:
    prompt = (PROMPTS_DIR / "intraday.md").read_text(encoding="utf-8")
    return Agent(
        name="intraday_analyst",
        instructions=prompt,
        model=_create_model(),
        tools=INTRADAY_TOOLS,
    )


async def run_intraday_analysis(context: str, hooks=None) -> str:
    """Run intraday anomaly detection and cause-tracing.

    Args:
        context: Active themes and their core stocks as text.
        hooks: Optional event hooks.

    Returns:
        Alert text if anomaly found, "无异动" otherwise.
    """
    agent = _create_intraday_agent()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    user_message = (
        f"[当前时间: {now}]\n\n"
        f"【当前活跃主线和监控标的】\n{context}\n\n"
        f"请检查是否有异动。如果无异动，直接输出\u201c无异动\u201d。"
    )

    logger.info("Intraday agent starting...")
    try:
        result = await asyncio.wait_for(
            Runner.run(agent, user_message, hooks=hooks, max_turns=20),
            timeout=120,
        )
        output = result.final_output
        logger.info("Intraday agent finished, length=%d", len(output))
        return output
    except asyncio.TimeoutError:
        logger.warning("Intraday agent timed out")
        return "无异动"
    except Exception as e:
        logger.warning("Intraday agent failed: %s", e)
        return "无异动"
