"""Morning scan agent — pre-market analysis and daily briefing."""

import asyncio
import json
import logging
import os
import re
from datetime import datetime

from agents import Agent, Runner
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from openai import AsyncOpenAI

from alpha_agents.config import (
    PROMPTS_DIR, AGENT_API_KEY, AGENT_BASE_URL, AGENT_MODEL,
)
from alpha_agents.tools.registry import (
    filter_stocks, get_anomaly_stocks, get_block_trade, get_bond_yields,
    get_concept_ranking, get_earnings_calendar, get_financial_data,
    get_global_overview, get_institutional_position, get_lhb_detail,
    get_margin_data, get_market_breadth, get_north_flow, get_pizzint,
    get_price_levels, get_sector_best_stocks, get_sector_data,
    get_sector_ranking, get_sentiment_phase, get_stock_fund_flow,
    get_stock_quotes, get_us_market, search_news, search_stocks, web_search,
)

logger = logging.getLogger(__name__)

# Morning agent gets analysis tools but NOT news tools (news is pre-fetched)
# Grouped by the question each one answers, because a flat list of 24
# names is what makes an agent pick by the shape of the word rather than
# by what it needs to know.
#
# The fund-behaviour block is the one that was missing. The premise of
# this whole system is 资金行为优先于新闻叙事, and the agent that picks
# the stocks had no stock-level flow tool at all — only sector rankings.
# It was being asked to follow the money with no way to see it.
MORNING_TOOLS = [
    # 选标的
    search_stocks, filter_stocks, get_sector_best_stocks, get_stock_quotes,
    # 定价 — 买在哪里、止损放哪里，是判断不是常数
    get_price_levels,
    # 板块与主线
    get_sector_data, get_sector_ranking, get_concept_ranking,
    # 资金行为 — 谁在买卖，机构还是游资
    get_lhb_detail, get_north_flow, get_margin_data, get_stock_fund_flow,
    get_institutional_position, get_block_trade,
    # 市场状态 — 今天该不该出手，仓位给多大
    get_market_breadth, get_sentiment_phase, get_anomaly_stocks,
    # 排雷 — 5 天持仓期内的可预防损失
    get_earnings_calendar, get_financial_data,
    # 外盘与消息
    get_us_market, get_bond_yields, get_global_overview, get_pizzint,
    search_news, web_search,
]


from alpha_agents.model_factory import (
    create_model as _create_model,
    create_model_settings,
)


def _create_morning_agent(trader=None) -> Agent:
    """The scanning agent, optionally wearing one trader's instructions.

    The prompt file is the trader's — that is what makes adding a strategy
    a matter of writing a file. The vocabulary check below still applies to
    whichever file it is, so a custom prompt cannot quietly ship a
    placeholder the code never fills.
    """
    if trader is not None:
        prompt = trader.prompt()
    else:
        prompt = (PROMPTS_DIR / "morning_scan.md").read_text(encoding="utf-8")
    # The invalidation vocabulary is rendered from the same table the
    # evaluator reads, so the prompt cannot offer a condition the checker
    # would drop, and a kind added to the checker is offered immediately.
    from alpha_agents.data.thesis import prompt_vocabulary
    prompt = prompt.replace("{VOCAB}", prompt_vocabulary())

    # Prompt files are read from disk on every agent creation; Python is
    # not. A long-running scheduler therefore picks up an edited prompt
    # while still executing the code it started with — which is worse than
    # either being stale on its own. It happened: the prompt gained a
    # {VOCAB} placeholder before the process had the line that fills it,
    # so the agent was shown the literal braces, invented three condition
    # kinds that do not exist, and every one of them was silently dropped
    # by the validator. Fail loudly instead of shipping a prompt with a
    # hole in it.
    if "{VOCAB}" in prompt or re.search(r"\{[A-Z_]{3,}\}", prompt):
        raise RuntimeError(
            f"{getattr(trader, 'prompt_file', 'morning_scan.md')} "
            "有未替换的占位符——本进程的代码比 prompt 旧。"
            "重启调度器：prompt 从磁盘热加载，Python 不会。")
    return Agent(
        name=f"morning_analyst:{getattr(trader, 'id', 'default')}",
        instructions=prompt,
        model=_create_model(),
        model_settings=create_model_settings(),
        tools=MORNING_TOOLS,
    )


async def run_morning_analysis(
    events_summary: str,
    themes_context: str,
    stats_context: str,
    hooks=None,
    trader=None,
) -> str:
    """Run morning scan analysis with memory context.

    Args:
        events_summary: Pre-digested news events as text.
        themes_context: Active theme lines summary.
        stats_context: Prediction hit rate stats.
        hooks: Optional event hooks.
        trader: Which trader is scanning; None uses the base prompt.

    Returns:
        Morning briefing report text.
    """
    agent = _create_morning_agent(trader)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S %A")

    user_message = (
        f"[当前时间: {now}]\n\n"
        f"【活跃主线】\n{themes_context}\n\n"
        f"【近期预测表现】\n{stats_context}\n\n"
        f"【隔夜新闻事件】\n{events_summary}\n\n"
        f"请生成今日晨报。"
    )

    logger.info("Morning agent starting...")
    if hooks is None:
        from alpha_agents.agents.hooks import ToolEventHooks
        hooks = ToolEventHooks(
            callback=None,
            agent_label=f"morning:{trader.id}" if trader else "morning")
    try:
        result = await asyncio.wait_for(
            Runner.run(agent, user_message, hooks=hooks, max_turns=40),
            # 24 tools and a per-call ceiling of TOOL_TIMEOUT means the
            # worst case is bounded now, but the scan legitimately needs
            # more wall time than it did with 15 tools — it reads the
            # money before it picks, which is more calls by design.
            timeout=int(os.environ.get("MORNING_TIMEOUT", "420")),
        )
        logger.info("Morning agent finished, length=%d", len(result.final_output))
        return result.final_output
    except asyncio.TimeoutError:
        logger.error("Morning agent timed out after %ss",
                     os.environ.get("MORNING_TIMEOUT", "420"))
        return "[晨扫超时，未生成报告]"
    except Exception as e:
        logger.error("Morning agent failed: %s", e)
        return f"[晨扫失败: {e}]"
