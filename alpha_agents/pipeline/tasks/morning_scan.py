"""Morning scan task — runs at 06:30 before market open.

Reads: overnight news, foreign markets, active theme lines
Outputs: morning briefing report
"""

import json
import logging
import time

from alpha_agents.data.memory_store import (
    get_active_themes, get_prediction_stats, get_all_cognition_latest,
)
from alpha_agents.pipeline.digest import digest_news
from alpha_agents.pipeline.monitor import NEWS_SOURCES

logger = logging.getLogger(__name__)


async def run_morning_scan() -> str | None:
    """Execute the morning scan task.

    1. Fetch overnight news from all sources
    2. Read active theme lines and market cognition
    3. Digest news with theme context
    4. Generate morning briefing

    Returns the morning report text, or None if nothing significant.
    """
    import asyncio

    logger.info("Morning scan starting...")

    # 1. Fetch news from all sources (reuse existing infrastructure)
    news_items = []
    for source_id, name, fetch_fn_factory in NEWS_SOURCES:
        try:
            raw = await asyncio.to_thread(fetch_fn_factory)
            data = json.loads(raw)
            items = data.get("news", [])
            news_items.extend(items)
            logger.debug("Morning scan: %d items from %s", len(items), name)
        except Exception as e:
            logger.debug("Morning scan: %s unavailable: %s", name, e)

    if not news_items:
        logger.info("Morning scan: no news items")
        return None

    # 2. Read memory for context
    themes = get_active_themes()
    stats = get_prediction_stats(days=7)
    cognition = get_all_cognition_latest()

    logger.info("Morning scan: %d news items, %d active themes, hit rate=%.1f%%",
                len(news_items), len(themes), stats.get("hit_rate", 0))

    # 3. Digest news (reuse existing cheap LLM filtering)
    events = await digest_news(news_items)

    if not events:
        logger.info("Morning scan: no significant events after digest")
        return None

    # 4. Generate morning report
    # TODO Phase 3: Replace with morning_scan Agent that uses memory context
    report_lines = [
        f"=== AlphaAgents 晨报 | {time.strftime('%Y-%m-%d')} ===",
        "",
        "【活跃主线】",
    ]
    for t in themes[:5]:
        stocks = json.loads(t["core_stocks"]) if t["core_stocks"] else []
        leader = next((s["name"] for s in stocks if s.get("role") == "龙头"), "无")
        report_lines.append(f"  {t['name']}（强度 {t['strength']}/10, {t['status']}）— 龙头: {leader}")

    report_lines.append("")
    report_lines.append(f"【预测命中率（近7天）】{stats.get('hit_rate', 0):.1f}% ({stats.get('hits', 0)}/{stats.get('total', 0)})")

    report_lines.append("")
    report_lines.append("【今日事件】")
    for e in events[:5]:
        report_lines.append(f"  [{e.get('category', '?')}] {e.get('event', '?')} — 重要性 {e.get('importance', 0)}/5")

    report = "\n".join(report_lines)
    logger.info("Morning scan complete: %d events, report length=%d", len(events), len(report))

    return report
