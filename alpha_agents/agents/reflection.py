"""Reflection Agent — validates strategist reports against actual market data.

After the strategist produces a report, this agent:
1. Extracts all bullish/bearish judgments
2. Queries actual market data (quotes, fund flows, basis)
3. Identifies contradictions (e.g. "bullish" but price is falling)
4. Analyzes why the divergence happened
5. Outputs corrections
"""

import logging

from agents import Agent

from alpha_agents.config import PROMPTS_DIR
from alpha_agents.tools.registry import STOCK_TOOLS, FUTURES_TOOLS

logger = logging.getLogger(__name__)


def create_reflection_agent(model) -> Agent:
    """Create the reflection/verification agent."""
    prompt = (PROMPTS_DIR / "reflection.md").read_text(encoding="utf-8")

    # Needs both stock and futures tools to verify all judgments
    verify_tools = list({*STOCK_TOOLS, *FUTURES_TOOLS})

    return Agent(
        name="reflection",
        instructions=prompt,
        model=model,
        tools=verify_tools,
    )
