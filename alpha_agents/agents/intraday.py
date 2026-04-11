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
    get_lhb_detail, get_institutional_position, get_sector_best_stocks,
    web_search,
)

logger = logging.getLogger(__name__)

INTRADAY_TOOLS = [
    get_sector_data, get_sector_ranking, get_anomaly_stocks,
    get_stock_quotes, get_stock_fund_flow, get_north_flow,
    get_lhb_detail, get_institutional_position, get_sector_best_stocks,
    web_search,
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
        f"{context}\n\n"
        f"以上异动已经被检测到，请立即进行追因分析：\n"
        f"1. 调用工具查明异动原因（新闻催化、资金来源等）\n"
        f"2. 判断是一日游还是持续行情\n"
        f"3. 按格式输出盘中提醒"
    )

    logger.info("Intraday agent starting...")
    if hooks is None:
        from alpha_agents.agents.hooks import ToolEventHooks
        hooks = ToolEventHooks(callback=None, agent_label="intraday")
    try:
        result = await asyncio.wait_for(
            Runner.run(agent, user_message, hooks=hooks, max_turns=30),
            timeout=180,
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
