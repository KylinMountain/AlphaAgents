"""Post-market review task — runs at 15:30 after market close.

Verifies today's predictions, updates theme line strengths,
updates market cognition, and generates review report.
"""

import json
import logging
import re
from datetime import datetime

from alpha_agents.data.memory_store import (
    get_active_themes, get_pending_predictions, update_prediction_result,
    get_prediction_stats, upsert_cognition, save_lesson, format_lessons_context,
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


def _extract_lessons(report: str) -> list[dict]:
    """Extract structured lessons from <!--LESSONS ... LESSONS--> block."""
    match = re.search(r"<!--LESSONS\s*(.*?)\s*LESSONS-->", report, re.DOTALL)
    if not match:
        return []
    try:
        from json_repair import repair_json
        data = repair_json(match.group(1), return_objects=True)
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return []


def _save_lessons(report: str, today: str) -> int:
    """Extract lessons from review report and persist them. Returns count saved."""
    lessons = _extract_lessons(report)
    saved = 0
    for l in lessons:
        lesson_type = l.get("type", "")
        if lesson_type not in ("success", "mistake", "insight"):
            continue
        description = l.get("description", "")
        if not description or len(description) < 5:
            continue
        try:
            save_lesson(
                date=today,
                type=lesson_type,
                description=description,
                category=l.get("category"),
                theme_line=l.get("theme") or None,
                market_context=l.get("market_context"),
                actionable=l.get("actionable"),
            )
            saved += 1
            logger.info("  Saved lesson [%s]: %s", lesson_type, description[:60])
        except Exception as e:
            logger.debug("Failed to save lesson: %s", e)
    return saved


async def run_review() -> str | None:
    """Execute the post-market review task.

    1. Get pending predictions and format for agent
    2. Get active themes and format for agent
    3. Run review agent to verify and analyze (with historical lessons)
    4. Extract and save structured lessons
    5. Retire stale themes
    6. Push notification

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

    # 3. Format contexts (include historical lessons)
    pred_ctx = _format_predictions(pending)
    themes_ctx = _format_themes(themes)
    stats_ctx = _format_stats(stats)
    lessons_ctx = format_lessons_context(limit=10)

    # 4. Run review agent with lessons context
    report = await run_review_analysis(pred_ctx, themes_ctx, stats_ctx, lessons_ctx)

    # 5. Extract and save structured lessons from report
    if report and not report.startswith("["):
        saved = _save_lessons(report, today)
        if saved:
            logger.info("Review: saved %d lessons", saved)

    # 6. Retire stale themes
    retired = retire_stale_themes()
    if retired:
        logger.info("Review: retired themes: %s", retired)

    # 7. Push notification
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
