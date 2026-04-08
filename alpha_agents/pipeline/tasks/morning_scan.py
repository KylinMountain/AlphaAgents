"""Morning scan task — runs at 06:30 before market open.

Fetches overnight news, reads memory context, and runs the morning
analyst agent to produce a daily briefing.
"""

import json
import logging
import time

from alpha_agents.data.memory_store import (
    get_active_themes, get_prediction_stats, get_all_cognition_latest,
)
from alpha_agents.pipeline.digest import digest_news
from alpha_agents.pipeline.monitor import NEWS_SOURCES
from alpha_agents.pipeline.theme_manager import evaluate_theme_signals, maybe_discover_theme
from alpha_agents.tools.sector_ranking import get_sector_ranking_fn
from alpha_agents.tools.stock_search import search_stocks_fn
from alpha_agents.tools.stock_filter import filter_stocks_fn
from alpha_agents.tools.global_market import get_global_overview_fn
from alpha_agents.data.memory_store import upsert_theme
from alpha_agents.agents.morning import run_morning_analysis
from alpha_agents.notify import notify_all

logger = logging.getLogger(__name__)


def _fill_theme_stocks(theme_name: str) -> None:
    """Search for core stocks for a newly discovered theme and update it."""
    try:
        result = json.loads(search_stocks_fn(keyword=theme_name))
        all_codes = []
        for concept in result.get("concepts", []):
            for stock in concept.get("stocks", [])[:5]:
                all_codes.append(stock["code"])

        if not all_codes:
            return

        # Filter out ST/suspended
        filtered = json.loads(filter_stocks_fn(stock_codes=all_codes[:20]))
        kept = filtered.get("stocks", [])
        if not kept:
            return

        # First stock as leader, rest as core
        core_stocks = []
        for i, s in enumerate(kept[:10]):
            core_stocks.append({
                "code": s["code"],
                "name": s["name"],
                "role": "龙头" if i == 0 else "核心",
            })

        leader = core_stocks[0]["code"] if core_stocks else None
        upsert_theme(theme_name, core_stocks=core_stocks, leader_code=leader)
        logger.info("  Filled %d stocks for '%s', leader=%s",
                     len(core_stocks), theme_name, core_stocks[0]["name"] if core_stocks else "无")
    except Exception as e:
        logger.debug("Failed to fill stocks for '%s': %s", theme_name, e)


def _format_themes(themes: list[dict]) -> str:
    """Format active themes into a readable context string."""
    if not themes:
        return "无活跃主线"
    lines = []
    for t in themes:
        stocks = json.loads(t["core_stocks"]) if t["core_stocks"] else []
        leader = next((s["name"] for s in stocks if s.get("role") == "龙头"), "无")
        stock_names = ", ".join(s["name"] for s in stocks[:5])
        lines.append(
            f"- {t['name']}（强度 {t['strength']}/10, {t['status']}）\n"
            f"  龙头: {leader} | 标的: {stock_names}\n"
            f"  催化: {t.get('catalyst', '无')}"
        )
    return "\n".join(lines)


def _format_stats(stats: dict) -> str:
    """Format prediction stats into readable text."""
    total = stats.get("total", 0)
    if total == 0:
        return "暂无预测记录"
    hit_rate = stats.get("hit_rate", 0)
    hits = stats.get("hits", 0)
    by_conf = stats.get("by_confidence", {})
    lines = [f"近7天命中率: {hit_rate:.1f}% ({hits}/{total})"]
    for conf, data in by_conf.items():
        lines.append(f"  {conf}信心: {data['hit_rate']:.1f}% ({data['hits']}/{data['total']})")
    return "\n".join(lines)


def _format_events(events: list[dict]) -> str:
    """Format digested events into readable text."""
    if not events:
        return "无重要事件"
    lines = []
    for e in events:
        lines.append(
            f"- [{e.get('category', '?')}] {e.get('event', '?')} "
            f"(重要性 {e.get('importance', 0)}/5, {e.get('credibility', '?')})\n"
            f"  摘要: {e.get('summary', '')[:100]}"
        )
    return "\n".join(lines)


async def run_morning_scan() -> str | None:
    """Execute the morning scan task.

    1. Fetch overnight news from all sources
    2. Read active theme lines, stats, cognition from memory
    3. Digest news into events
    4. Run morning analyst agent with full context
    5. Push notification

    Returns the morning report text, or None if nothing significant.
    """
    import asyncio

    logger.info("Morning scan starting...")

    # 1. Fetch news from all sources
    news_items = []
    for source_id, name, fetch_fn_factory in NEWS_SOURCES:
        try:
            raw = await asyncio.to_thread(fetch_fn_factory)
            data = json.loads(raw)
            items = data.get("news", [])
            news_items.extend(items)
        except Exception as e:
            logger.debug("Morning scan: %s unavailable: %s", name, e)

    if not news_items:
        logger.info("Morning scan: no news items")
        return None

    # 2. Auto-discover themes from current sector data
    try:
        ranking = json.loads(await asyncio.to_thread(get_sector_ranking_fn, 5))
        for gainer in ranking.get("gainers", [])[:5]:
            signals = evaluate_theme_signals(
                sector_name=gainer["sector"],
                sector_change_pct=gainer.get("change_pct", 0),
                sector_fund_flow=gainer.get("net_flow_yi", 0) * 1e8,
                market_change_pct=0,
            )
            if maybe_discover_theme(
                gainer["sector"], signals,
                catalyst=f"板块涨{gainer.get('change_pct', 0):.1f}%, 领涨{gainer.get('leader', '')}",
            ):
                logger.info("Morning scan: discovered theme '%s', searching for stocks...", gainer["sector"])
                # Fill in core stocks for the new theme
                _fill_theme_stocks(gainer["sector"])
    except Exception as e:
        logger.debug("Morning scan theme discovery failed: %s", e)

    # 3. Read memory
    themes = get_active_themes()
    stats = get_prediction_stats(days=7)
    cognition = get_all_cognition_latest()

    logger.info("Morning scan: %d news items, %d active themes", len(news_items), len(themes))

    # 3. Digest news
    events = await digest_news(news_items)
    if not events:
        logger.info("Morning scan: no significant events")
        return None

    # 4. Pre-fetch global market data
    global_ctx = ""
    try:
        overview = json.loads(await asyncio.to_thread(get_global_overview_fn))
        lines = []
        for idx in overview.get("us_indices", []):
            lines.append(f"  {idx['name']}: {idx['close']} ({idx['change_pct']:+.2f}%)")
        bonds = overview.get("bond_yields", {})
        if bonds.get("us_10y"):
            lines.append(f"  美债10Y: {bonds['us_10y']}%")
        if bonds.get("cn_us_spread"):
            lines.append(f"  中美利差: {bonds['cn_us_spread']}%")
        for sig in overview.get("signals", []):
            lines.append(f"  信号: {sig}")
        global_ctx = "【全球市场】\n" + "\n".join(lines) if lines else ""
    except Exception as e:
        logger.debug("Morning scan: global overview failed: %s", e)

    # 5. Run morning agent with context
    themes_ctx = _format_themes(themes)
    stats_ctx = _format_stats(stats)
    events_ctx = _format_events(events)
    if global_ctx:
        events_ctx = global_ctx + "\n\n" + events_ctx

    report = await run_morning_analysis(events_ctx, themes_ctx, stats_ctx)

    # 5. Push notification
    if report and not report.startswith("["):
        try:
            await asyncio.to_thread(
                notify_all,
                f"AlphaAgents 晨报 | {time.strftime('%m-%d')}",
                report[:500],
            )
        except Exception as e:
            logger.debug("Morning notification failed: %s", e)

    print(report)
    return report
