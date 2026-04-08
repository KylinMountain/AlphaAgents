"""Post-market review task — runs at 15:30 after market close.

Reviews today's predictions, updates theme line strengths,
updates market cognition, generates review report.
"""

import json
import logging
import time
from datetime import datetime

from alpha_agents.data.memory_store import (
    get_active_themes, get_pending_predictions, update_prediction_result,
    get_prediction_stats, upsert_cognition,
)
from alpha_agents.pipeline.theme_manager import (
    evaluate_theme_signals, update_theme_strength, retire_stale_themes,
)

logger = logging.getLogger(__name__)


async def run_review() -> str | None:
    """Execute the post-market review task.

    1. Check today's prediction results (were we right?)
    2. Update theme line strengths based on today's market data
    3. Update market cognition for tracked sectors
    4. Generate review report

    Returns the review report text.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    logger.info("Review starting for %s...", today)

    # 1. Check pending predictions
    pending = get_pending_predictions(today)
    logger.info("Review: %d pending predictions to verify", len(pending))

    # TODO Phase 3: Use get_stock_quotes to check actual returns
    # and call update_prediction_result for each

    # 2. Update theme strengths
    themes = get_active_themes()
    logger.info("Review: evaluating %d active themes", len(themes))

    # TODO Phase 2+3: Fetch actual sector data for each theme
    # and call update_theme_strength with real signals

    # 3. Retire stale themes
    retired = retire_stale_themes()
    if retired:
        logger.info("Review: retired themes: %s", retired)

    # 4. Generate review report
    stats = get_prediction_stats(days=7)
    report_lines = [
        f"=== AlphaAgents 复盘 | {today} ===",
        "",
        f"【预测验证】待验证: {len(pending)}条 | 近7天命中率: {stats.get('hit_rate', 0):.1f}%",
        "",
        "【主线状态】",
    ]
    for t in themes:
        report_lines.append(f"  {t['name']}（强度 {t['strength']}/10, {t['status']}）")
    if retired:
        report_lines.append(f"  已退出: {', '.join(retired)}")

    report = "\n".join(report_lines)
    logger.info("Review complete")

    return report
