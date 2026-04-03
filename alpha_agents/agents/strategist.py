import logging

from agents import Agent, Runner
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from openai import AsyncOpenAI

from alpha_agents.config import (
    PROMPTS_DIR,
    AGENT_API_KEY, AGENT_BASE_URL, AGENT_MODEL,
)
from alpha_agents.tools.server import ALL_TOOLS
from alpha_agents.agents.geopolitical import create_geopolitical_agent

logger = logging.getLogger(__name__)


def _create_model() -> OpenAIChatCompletionsModel:
    """Create OpenAI-compatible model from config."""
    client = AsyncOpenAI(
        api_key=AGENT_API_KEY,
        base_url=AGENT_BASE_URL,
    )
    model_name = AGENT_MODEL or "qwen-plus"
    return OpenAIChatCompletionsModel(model=model_name, openai_client=client)


def _create_strategist() -> Agent:
    """Create the main strategist agent with all tools and sub-agents."""
    model = _create_model()
    system_prompt = (PROMPTS_DIR / "strategist.md").read_text(encoding="utf-8")

    geopolitical = create_geopolitical_agent(model)

    return Agent(
        name="strategist",
        instructions=system_prompt,
        model=model,
        tools=ALL_TOOLS,
        handoffs=[geopolitical],
    )


async def run_analysis(prompt: str) -> str:
    """Run a full analysis cycle and return the final output."""
    agent = _create_strategist()
    result = await Runner.run(agent, prompt)
    return result.final_output
