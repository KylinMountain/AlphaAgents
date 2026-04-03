"""Futures market analysis agent.

Analyzes events for impact on commodity futures, index futures, and bond futures.
Uses the same OpenAI Agents SDK pattern as the stock strategist.
"""

import logging
from datetime import datetime

from agents import Agent, Runner
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from openai import AsyncOpenAI

from alpha_agents.config import (
    PROMPTS_DIR,
    AGENT_API_KEY, AGENT_BASE_URL, AGENT_MODEL,
)
from alpha_agents.agents.geopolitical import create_geopolitical_agent

logger = logging.getLogger(__name__)

# Futures agent uses a subset of tools — no stock-specific tools
FUTURES_TOOL_NAMES = {
    "get_news", "get_world_news", "get_cls_telegraph",
    "get_wallstreetcn", "get_whitehouse", "get_pboc_news",
    "get_jin10", "get_fed_news", "get_sec_news",
    "get_social_media", "get_eastmoney_live",
    "web_search", "web_fetch",
}


def _create_model() -> OpenAIChatCompletionsModel:
    client = AsyncOpenAI(
        api_key=AGENT_API_KEY,
        base_url=AGENT_BASE_URL,
    )
    model_name = AGENT_MODEL or "qwen-plus"
    return OpenAIChatCompletionsModel(model=model_name, openai_client=client)


def _get_futures_tools() -> list:
    """Get tools relevant for futures analysis (exclude stock-specific tools)."""
    from alpha_agents.tools.server import ALL_TOOLS
    return [t for t in ALL_TOOLS if getattr(t, "name", getattr(t, "__name__", "")) in FUTURES_TOOL_NAMES]


def _create_futures_agent() -> Agent:
    """Create the futures analysis agent."""
    model = _create_model()
    system_prompt = (PROMPTS_DIR / "futures.md").read_text(encoding="utf-8")
    geopolitical = create_geopolitical_agent(model)

    tools = _get_futures_tools()
    logger.debug("Futures agent loaded with %d tools", len(tools))

    return Agent(
        name="futures_strategist",
        instructions=system_prompt,
        model=model,
        tools=tools,
        handoffs=[geopolitical],
    )


async def run_futures_analysis(prompt: str) -> str:
    """Run futures market analysis and return the final output."""
    agent = _create_futures_agent()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S %A")
    user_message = f"[当前时间: {now}]\n\n{prompt}"
    result = await Runner.run(agent, user_message)
    return result.final_output
