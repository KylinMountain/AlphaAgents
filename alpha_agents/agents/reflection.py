"""Reflection Agent — validates strategist reports against actual market data.

After any strategist produces a report, this agent:
1. Extracts all bullish/bearish judgments
2. Queries actual market data (quotes, fund flows, basis)
3. Identifies contradictions (e.g. "bullish" but price is falling)
4. Analyzes why the divergence happened
5. Outputs corrections
"""

import logging
from datetime import datetime

from agents import Agent, Runner

from alpha_agents.config import PROMPTS_DIR
from alpha_agents.tools.registry import STOCK_TOOLS, FUTURES_TOOLS

logger = logging.getLogger(__name__)


def create_reflection_agent(model) -> Agent:
    """Create the reflection/verification agent."""
    prompt = (PROMPTS_DIR / "reflection.md").read_text(encoding="utf-8")

    # Merge stock + futures tools, deduplicate by id
    seen = set()
    verify_tools = []
    for t in [*STOCK_TOOLS, *FUTURES_TOOLS]:
        if id(t) not in seen:
            seen.add(id(t))
            verify_tools.append(t)

    return Agent(
        name="reflection",
        instructions=prompt,
        model=model,
        tools=verify_tools,
    )


async def run_reflection(report: str, model, hooks=None) -> str:
    """Run reflection verification on any analysis report.

    Args:
        report: The original analysis report text to verify.
        model: The LLM model to use.
        hooks: Optional agent hooks.

    Returns:
        Combined report: original + reflection verification.
    """
    try:
        reflection = create_reflection_agent(model)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S %A")
        verify_prompt = (
            f"[当前时间: {now}]\n\n"
            f"请验证以下分析报告，用实际行情数据检验每个多空判断是否与市场走势一致。"
            f"如果发现矛盾，分析原因并给出修正意见。\n\n"
            f"--- 原始报告 ---\n{report}"
        )
        verify_result = await Runner.run(reflection, verify_prompt, hooks=hooks, max_turns=15)
        return f"{report}\n\n{'=' * 50}\n{verify_result.final_output}"
    except Exception as e:
        logger.warning("Reflection verification failed (non-fatal): %s", e)
        return report
