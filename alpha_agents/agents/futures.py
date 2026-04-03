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
from alpha_agents.tools.server import FUTURES_TOOLS
from alpha_agents.agents.geopolitical import create_geopolitical_agent

logger = logging.getLogger(__name__)


def _create_model() -> OpenAIChatCompletionsModel:
    client = AsyncOpenAI(
        api_key=AGENT_API_KEY,
        base_url=AGENT_BASE_URL,
    )
    model_name = AGENT_MODEL or "qwen-plus"
    return OpenAIChatCompletionsModel(model=model_name, openai_client=client)


def _create_futures_agent() -> Agent:
    """Create the futures analysis agent."""
    model = _create_model()
    system_prompt = (PROMPTS_DIR / "futures.md").read_text(encoding="utf-8")
    geopolitical = create_geopolitical_agent(model)

    return Agent(
        name="futures_strategist",
        instructions=system_prompt,
        model=model,
        tools=FUTURES_TOOLS,
        handoffs=[geopolitical],
    )


async def run_futures_analysis(prompt: str) -> str:
    """Run futures market analysis and return the final output."""
    agent = _create_futures_agent()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S %A")
    user_message = f"[当前时间: {now}]\n\n{prompt}"
    result = await Runner.run(agent, user_message)
    return result.final_output
