"""Reflection Agent — validates strategist reports against actual market data.

After any strategist produces a report, this agent:
1. Extracts all bullish/bearish judgments
2. Queries actual market data (quotes, fund flows, basis)
3. Identifies contradictions with data evidence
4. Returns corrections to the strategist for a revised final report
"""

import asyncio
import logging
from datetime import datetime

from agents import Agent, Runner

from alpha_agents.config import PROMPTS_DIR
from alpha_agents.tools.registry import (
    get_sector_data, filter_stocks, get_watchlist,
    get_futures_quotes, get_futures_inventory, get_futures_basis,
    get_stock_quotes, get_financial_data, get_market_breadth, get_earnings_calendar,
    get_lhb_detail, get_north_flow, get_margin_data, get_stock_fund_flow,
    get_sector_ranking,
)

logger = logging.getLogger(__name__)


def create_reflection_agent(model) -> Agent:
    """Create the reflection/verification agent."""
    prompt = (PROMPTS_DIR / "reflection.md").read_text(encoding="utf-8")

    # Only market data tools — no web_search, no news tools
    verify_tools = [
        get_sector_data, filter_stocks, get_watchlist,
        get_futures_quotes, get_futures_inventory, get_futures_basis,
        get_stock_quotes, get_financial_data, get_market_breadth, get_earnings_calendar,
        get_lhb_detail, get_north_flow, get_margin_data, get_stock_fund_flow,
        get_sector_ranking,
    ]

    return Agent(
        name="reflection",
        instructions=prompt,
        model=model,
        tools=verify_tools,
    )


def _create_revise_agent(model) -> Agent:
    """Create a lightweight agent that revises the report based on reflection."""
    return Agent(
        name="reviser",
        instructions=(
            "你是报告修订助手。你会收到一份原始分析报告和一份反思验证报告。\n\n"
            "你的任务是：\n"
            "1. 根据反思中标记为 ❌ 的修正意见，直接修改原报告中对应的判断\n"
            "2. 保留反思中 ✅ 确认的判断不变\n"
            "3. 输出修订后的完整报告，格式与原报告完全一致\n"
            "4. 在报告末尾追加一个简短的【修订说明】段落，列出修改了哪些判断\n\n"
            "不要添加新的分析，只做修正。不要调用任何工具。"
        ),
        model=model,
        tools=[],
    )


async def run_reflection(report: str, model, hooks=None) -> str:
    """Run reflection verification, then revise the original report.

    Flow: original report → reflection (with data) → reviser → final report

    Args:
        report: The original analysis report text to verify.
        model: The LLM model to use.
        hooks: Optional agent hooks.

    Returns:
        Revised report incorporating reflection corrections.
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S %A")

    # Step 1: Reflection — verify with actual market data (3 min timeout)
    logger.info("Reflection step 1: verifying report (timeout=180s, max_turns=40)...")
    try:
        reflection = create_reflection_agent(model)
        verify_prompt = (
            f"[当前时间: {now}]\n\n"
            f"请验证以下分析报告，对每个多空判断都必须调用工具查询实际数据来验证。\n\n"
            f"--- 原始报告 ---\n{report}"
        )
        verify_result = await asyncio.wait_for(
            Runner.run(reflection, verify_prompt, hooks=hooks, max_turns=40),
            timeout=180,
        )
        reflection_output = verify_result.final_output
        logger.info("Reflection step 1 done, output length=%d", len(reflection_output))
    except asyncio.TimeoutError:
        logger.warning("Reflection timed out after 180s, returning original report")
        return report
    except Exception as e:
        logger.warning("Reflection verification failed (non-fatal): %s", e)
        return report

    # Step 2: Check if there are corrections needed
    if "全部验证通过" in reflection_output:
        logger.info("Reflection: all judgments verified, no revision needed")
        return report

    # Step 3: Revise — feed both reports to reviser agent
    logger.info("Reflection step 3: revising report (timeout=60s)...")
    try:
        reviser = _create_revise_agent(model)
        revise_prompt = (
            f"请根据反思验证的修正意见，修订原始报告。\n\n"
            f"--- 原始报告 ---\n{report}\n\n"
            f"--- 反思验证 ---\n{reflection_output}"
        )
        revise_result = await asyncio.wait_for(
            Runner.run(reviser, revise_prompt, hooks=hooks, max_turns=5),
            timeout=60,
        )
        revised = revise_result.final_output

        if revised and len(revised) > len(report) * 0.3:
            logger.info("Reflection step 3 done: revised with %d corrections",
                        reflection_output.count("❌"))
            return revised
        else:
            logger.warning("Reviser output too short, using original + reflection")
            return f"{report}\n\n{'=' * 50}\n{reflection_output}"

    except asyncio.TimeoutError:
        logger.warning("Reviser timed out after 60s, appending reflection")
        return f"{report}\n\n{'=' * 50}\n{reflection_output}"
    except Exception as e:
        logger.warning("Report revision failed, appending reflection: %s", e)
        return f"{report}\n\n{'=' * 50}\n{reflection_output}"
