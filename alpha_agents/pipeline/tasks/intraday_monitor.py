"""Intraday monitoring task — runs every 15 minutes during trading hours.

Detects anomalies in active theme stocks and sector fund flows,
then traces causes and generates alerts.
"""

import json
import logging
from datetime import datetime

from alpha_agents.data.memory_store import get_active_themes
from alpha_agents.agents.intraday import run_intraday_analysis
from alpha_agents.notify import notify_all

logger = logging.getLogger(__name__)


def _format_themes_for_monitoring(themes: list[dict]) -> str:
    """Format themes into monitoring context."""
    if not themes:
        return "无活跃主线"
    lines = []
    for t in themes:
        stocks = json.loads(t["core_stocks"]) if t["core_stocks"] else []
        stock_list = ", ".join(f"{s['code']} {s['name']}" for s in stocks[:10])
        lines.append(
            f"主线: {t['name']}（强度 {t['strength']}/10, {t['status']}）\n"
            f"  龙头: {t.get('leader_code', '无')}\n"
            f"  监控标的: {stock_list}"
        )
    return "\n".join(lines)


async def run_intraday_monitor() -> str | None:
    """Execute one intraday monitoring cycle.

    1. Get active theme lines and core stocks
    2. Run intraday agent to detect anomalies
    3. If anomaly found, push notification

    Returns alert text if anomaly found, None otherwise.
    """
    import asyncio

    themes = get_active_themes()
    if not themes:
        logger.debug("Intraday monitor: no active themes")
        return None

    context = _format_themes_for_monitoring(themes)
    logger.info("Intraday monitor: watching %d themes", len(themes))

    output = await run_intraday_analysis(context)

    if output and output.strip() != "无异动":
        logger.info("Intraday anomaly detected!")
        print(output)

        # Push notification
        try:
            now = datetime.now().strftime("%H:%M")
            await asyncio.to_thread(
                notify_all,
                f"AlphaAgents 盘中提醒 | {now}",
                output[:500],
            )
        except Exception as e:
            logger.debug("Intraday notification failed: %s", e)

        return output

    logger.debug("Intraday monitor: no anomaly")
    return None
