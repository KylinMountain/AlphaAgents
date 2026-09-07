import logging
from datetime import datetime

from agents import Agent, Runner
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from openai import AsyncOpenAI

from alpha_agents.config import (
    PROMPTS_DIR,
    AGENT_API_KEY, AGENT_BASE_URL, AGENT_MODEL,
)
from alpha_agents.tools.registry import STOCK_TOOLS
from alpha_agents.agents.geopolitical import create_geopolitical_agent
from alpha_agents.agents.reflection import run_reflection

logger = logging.getLogger(__name__)


from alpha_agents.agents.model_factory import (
    create_model as _create_model,
    create_model_settings,
)


def _create_strategist() -> Agent:
    """Create the main strategist agent with all tools and sub-agents."""
    model = _create_model()
    system_prompt = (PROMPTS_DIR / "strategist.md").read_text(encoding="utf-8")

    geopolitical = create_geopolitical_agent(model)

    return Agent(
        name="strategist",
        instructions=system_prompt,
        model=model,
        model_settings=create_model_settings(),
        tools=STOCK_TOOLS,
        handoffs=[geopolitical],
    )


async def run_analysis(prompt: str, hooks=None) -> str:
    """Run a full analysis cycle with reflection verification."""
    import asyncio
    agent = _create_strategist()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S %A")
    user_message = f"[当前时间: {now}]\n\n{prompt}"

    logger.info("Stock strategist starting (max_turns=100)...")
    try:
        result = await asyncio.wait_for(
            Runner.run(agent, user_message, hooks=hooks, max_turns=100),
            timeout=600,
        )
        report = result.final_output
        logger.info("Stock strategist finished, report length=%d", len(report))
    except asyncio.TimeoutError:
        logger.error("Stock strategist timed out after 300s")
        return "[股票策略师超时，未生成报告]"

    # Reflection: verify with actual market data
    logger.info("Stock reflection starting...")
    result = await run_reflection(report, _create_model(), hooks=hooks)
    logger.info("Stock reflection finished, final length=%d", len(result))
    return result
