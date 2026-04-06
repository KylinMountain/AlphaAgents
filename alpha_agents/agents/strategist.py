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
from alpha_agents.agents.reflection import create_reflection_agent

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
    reflection = create_reflection_agent(model)

    return Agent(
        name="strategist",
        instructions=system_prompt,
        model=model,
        tools=STOCK_TOOLS,
        handoffs=[geopolitical, reflection],
    )


async def run_analysis(prompt: str, hooks=None) -> str:
    """Run a full analysis cycle with reflection verification.

    Flow: strategist analyzes → handoff to reflection → reflection verifies with real data
    """
    agent = _create_strategist()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S %A")
    user_message = f"[当前时间: {now}]\n\n{prompt}"

    # First pass: strategist analysis
    result = await Runner.run(agent, user_message, hooks=hooks, max_turns=25)
    report = result.final_output

    # Second pass: reflection verification
    try:
        model = _create_model()
        reflection = create_reflection_agent(model)
        verify_prompt = (
            f"[当前时间: {now}]\n\n"
            f"请验证以下策略师报告，用实际行情数据检验每个多空判断是否与市场走势一致。"
            f"如果发现矛盾，分析原因并给出修正意见。\n\n"
            f"--- 策略师原始报告 ---\n{report}"
        )
        verify_result = await Runner.run(reflection, verify_prompt, hooks=hooks, max_turns=15)
        reflection_report = verify_result.final_output

        return f"{report}\n\n{'=' * 50}\n{reflection_report}"
    except Exception as e:
        logger.warning("Reflection verification failed (non-fatal): %s", e)
        return report
