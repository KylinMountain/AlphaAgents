"""Weekly report task — runs on weekends.

Summarizes the week: prediction accuracy, theme line changes,
lessons learned, and outlook for next week.
"""

import json
import logging
from datetime import datetime

from agents import Agent, Runner
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from openai import AsyncOpenAI

from alpha_agents.config import (
    PROMPTS_DIR, AGENT_API_KEY, AGENT_BASE_URL, AGENT_MODEL,
)
from alpha_agents.data.memory_store import (
    get_active_themes, get_prediction_stats, get_all_cognition_latest,
)
from alpha_agents.notify import notify_all
from alpha_agents.data.portfolio import get_portfolio_stats, format_portfolio_stats

logger = logging.getLogger(__name__)


def _create_model() -> OpenAIChatCompletionsModel:
    client = AsyncOpenAI(api_key=AGENT_API_KEY, base_url=AGENT_BASE_URL)
    return OpenAIChatCompletionsModel(model=AGENT_MODEL or "qwen-plus", openai_client=client)


async def run_weekly_report() -> str | None:
    """Generate the weekly summary report.

    Reads prediction stats and theme data from memory,
    then uses LLM to generate a structured weekly summary.
    """
    import asyncio

    logger.info("Weekly report starting...")

    # Gather context
    themes = get_active_themes()
    stats = get_prediction_stats(days=7)
    cognition = get_all_cognition_latest()

    # Format context
    themes_text = ""
    for t in themes:
        stocks = json.loads(t["core_stocks"]) if t["core_stocks"] else []
        themes_text += (
            f"- {t['name']}（强度 {t['strength']}/10, {t['status']}）\n"
            f"  创建: {t.get('created_at', '?')[:10]} | 催化: {t.get('catalyst', '无')}\n"
        )

    stats_text = (
        f"总推荐: {stats.get('total', 0)}次, 命中: {stats.get('hits', 0)}次, "
        f"命中率: {stats.get('hit_rate', 0):.1f}%\n"
    )
    for conf, data in stats.get("by_confidence", {}).items():
        stats_text += f"  {conf}信心: {data['hit_rate']:.1f}% ({data['hits']}/{data['total']})\n"

    # Portfolio performance
    portfolio_stats = get_portfolio_stats(days=7)
    portfolio_text = format_portfolio_stats(portfolio_stats)

    # Generate report via LLM
    prompt_text = (PROMPTS_DIR / "weekly_report.md").read_text(encoding="utf-8")
    agent = Agent(
        name="weekly_analyst",
        instructions=prompt_text,
        model=_create_model(),
        tools=[],  # No tools needed — purely summarization
    )

    now = datetime.now()
    user_message = (
        f"[当前时间: {now.strftime('%Y-%m-%d %H:%M')}]\n\n"
        f"【本周预测统计】\n{stats_text}\n"
        f"【本周策略表现（虚拟持仓）】\n{portfolio_text}\n"
        f"【活跃主线】\n{themes_text}\n"
        f"请生成本周周报。"
    )

    try:
        result = await asyncio.wait_for(
            Runner.run(agent, user_message, max_turns=5),
            timeout=60,
        )
        report = result.final_output
        logger.info("Weekly report finished, length=%d", len(report))

        # Push notification
        if report:
            try:
                import time as _time
                await asyncio.to_thread(
                    notify_all,
                    f"AlphaAgents 周报 | {_time.strftime('%m-%d')}",
                    report[:500],
                )
            except Exception as e:
                logger.debug("Weekly report notification failed: %s", e)

        # Calculate sector betas for all active themes (weekly refresh)
        try:
            from alpha_agents.data.beta_calculator import run_weekly_beta_calculation
            import asyncio as _asyncio
            beta_count = await _asyncio.to_thread(run_weekly_beta_calculation)
            logger.info("Weekly beta calculation: %d betas computed", beta_count)
        except Exception as e:
            logger.warning("Weekly beta calculation failed: %s", e)

        return report
    except asyncio.TimeoutError:
        logger.warning("Weekly report timed out")
        return None
    except Exception as e:
        logger.error("Weekly report failed: %s", e)
        return None
