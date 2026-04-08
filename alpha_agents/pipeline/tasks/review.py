"""Post-market review task — runs at 15:30 after market close.

Verifies today's predictions, updates theme line strengths,
updates market cognition, and generates review report.
"""

import json
import logging
from datetime import datetime

from alpha_agents.data.memory_store import (
    get_active_themes, get_pending_predictions, update_prediction_result,
    get_prediction_stats, upsert_cognition,
)
from alpha_agents.pipeline.theme_manager import (
    evaluate_theme_signals, update_theme_strength, maybe_discover_theme,
    retire_stale_themes,
)
from alpha_agents.tools.sector_ranking import get_sector_ranking_fn, get_concept_ranking_fn
from alpha_agents.tools.market_breadth import get_market_breadth_fn
from alpha_agents.agents.review_agent import run_review_analysis
from alpha_agents.notify import notify_all

logger = logging.getLogger(__name__)


def _update_themes_from_market_data(existing_themes: list[dict]) -> None:
    """Use real sector ranking data to discover new themes and update existing ones.

    This runs synchronously (called via to_thread).
    """
    try:
        # Get market breadth for context
        breadth = json.loads(get_market_breadth_fn())
        market_change = 0.0  # Approximate from breadth
        ad_ratio = breadth.get("advance_decline_ratio", 1)

        # Get concept ranking (matches DB concept names)
        ranking = json.loads(get_concept_ranking_fn(top_n=10))
        all_concepts = ranking.get("gainers", []) + ranking.get("losers", [])

        # Build lookup by concept name
        concept_lookup = {c.get("concept", ""): c for c in all_concepts}

        # Update existing themes
        existing_names = {t["name"] for t in existing_themes}
        for theme in existing_themes:
            if theme["name"] in concept_lookup:
                c = concept_lookup[theme["name"]]
                signals = evaluate_theme_signals(
                    sector_name=theme["name"],
                    sector_change_pct=c.get("change_pct", 0),
                    sector_fund_flow=c.get("net_flow_yi", 0) * 1e8,
                    market_change_pct=market_change,
                )
                update_theme_strength(theme["name"], signals)

        # Discover new themes from top-performing concepts
        for gainer in ranking.get("gainers", [])[:8]:
            concept = gainer.get("concept", "")
            if not concept or concept in existing_names:
                continue
            signals = evaluate_theme_signals(
                sector_name=concept,
                sector_change_pct=gainer.get("change_pct", 0),
                sector_fund_flow=gainer.get("net_flow_yi", 0) * 1e8,
                market_change_pct=market_change,
            )
            maybe_discover_theme(
                concept,
                signals,
                catalyst=f"概念涨{gainer.get('change_pct', 0):.1f}%, 净流入{gainer.get('net_flow_yi', 0):.1f}亿, 领涨{gainer.get('leader', '')}",
            )

        logger.info("Theme update: %d existing updated, checked top 5 for discovery",
                     len(existing_themes))
    except Exception as e:
        logger.warning("Theme update from market data failed: %s", e)


def _format_predictions(predictions: list[dict]) -> str:
    """Format pending predictions for the review agent."""
    if not predictions:
        return "今日无待验证预测"
    lines = []
    for p in predictions:
        lines.append(
            f"- {p['code']} {p.get('name', '?')} | 方向: {p['direction']} | "
            f"信心: {p.get('confidence', '?')} | 推荐价: {p.get('entry_price', '?')} | "
            f"主线: {p.get('theme_line', '?')} | 理由: {p.get('reason', '')[:50]}"
        )
    return "\n".join(lines)


def _format_themes(themes: list[dict]) -> str:
    """Format active themes for the review agent."""
    if not themes:
        return "无活跃主线"
    lines = []
    for t in themes:
        stocks = json.loads(t["core_stocks"]) if t["core_stocks"] else []
        leader = next((s["name"] for s in stocks if s.get("role") == "龙头"), "无")
        lines.append(
            f"- {t['name']}（强度 {t['strength']}/10, {t['status']}）\n"
            f"  龙头: {leader} ({t.get('leader_code', '?')})"
        )
    return "\n".join(lines)


def _format_stats(stats: dict) -> str:
    """Format prediction stats."""
    total = stats.get("total", 0)
    if total == 0:
        return "暂无预测记录"
    return f"近7天命中率: {stats.get('hit_rate', 0):.1f}% ({stats.get('hits', 0)}/{total})"


async def run_review() -> str | None:
    """Execute the post-market review task.

    1. Get pending predictions and format for agent
    2. Get active themes and format for agent
    3. Run review agent to verify and analyze
    4. Retire stale themes
    5. Push notification

    Returns the review report text.
    """
    import asyncio

    today = datetime.now().strftime("%Y-%m-%d")
    logger.info("Review starting for %s...", today)

    # 1. Get data
    pending = get_pending_predictions(today)
    themes = get_active_themes()
    stats = get_prediction_stats(days=7)

    logger.info("Review: %d predictions, %d themes", len(pending), len(themes))

    # 2. Auto-discover/update themes from real sector data
    await asyncio.to_thread(_update_themes_from_market_data, themes)

    # Re-read themes after update
    themes = get_active_themes()

    # 3. Format contexts
    pred_ctx = _format_predictions(pending)
    themes_ctx = _format_themes(themes)
    stats_ctx = _format_stats(stats)

    # 4. Run review agent
    report = await run_review_analysis(pred_ctx, themes_ctx, stats_ctx)

    # 5. Retire stale themes
    retired = retire_stale_themes()
    if retired:
        logger.info("Review: retired themes: %s", retired)

    # 5. Push notification
    if report and not report.startswith("["):
        try:
            await asyncio.to_thread(
                notify_all,
                f"AlphaAgents 复盘 | {today}",
                report[:500],
            )
        except Exception as e:
            logger.debug("Review notification failed: %s", e)

    print(report)
    return report
