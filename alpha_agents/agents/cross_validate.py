"""Cross-validation agent — 4-dimension verification before recommending.

Called by morning/intraday agents to validate stock candidates.
Not scheduled independently.
"""

import asyncio
import logging

from agents import Agent, Runner
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from openai import AsyncOpenAI

from alpha_agents.config import (
    PROMPTS_DIR, AGENT_API_KEY, AGENT_BASE_URL, AGENT_MODEL,
)
from alpha_agents.tools.registry import (
    get_stock_quotes, get_financial_data, get_market_breadth,
    get_earnings_calendar, get_stock_fund_flow, get_north_flow,
    get_margin_data, get_lhb_detail,
)

logger = logging.getLogger(__name__)


from alpha_agents.agents.model_factory import (
    create_model as _create_model,
    create_model_settings,
)


def _create_validator() -> Agent:
    prompt = (PROMPTS_DIR / "cross_validate.md").read_text(encoding="utf-8")
    tools = [
        get_stock_quotes, get_financial_data, get_market_breadth,
        get_earnings_calendar, get_stock_fund_flow, get_north_flow,
        get_margin_data, get_lhb_detail,
    ]
    return Agent(
        name="cross_validator",
        instructions=prompt,
        model=_create_model(),
        model_settings=create_model_settings(),
        tools=tools,
    )


async def run_cross_validation(candidates: str, hooks=None) -> str:
    """Validate stock candidates across 4 dimensions.

    Args:
        candidates: Text listing candidate stocks with codes and reasons.
        hooks: Optional agent hooks for event broadcasting.

    Returns:
        Validation result with confidence levels.
    """
    logger.info("Cross-validation starting...")
    agent = _create_validator()
    prompt = f"请对以下推荐候选股进行4维交叉验证：\n\n{candidates}"

    try:
        result = await asyncio.wait_for(
            Runner.run(agent, prompt, hooks=hooks, max_turns=30),
            timeout=180,
        )
        logger.info("Cross-validation complete, length=%d", len(result.final_output))
        return result.final_output
    except asyncio.TimeoutError:
        logger.warning("Cross-validation timed out")
        raise RuntimeError("Cross-validation timed out")
    except Exception as e:
        logger.warning("Cross-validation failed: %s", e)
        raise
