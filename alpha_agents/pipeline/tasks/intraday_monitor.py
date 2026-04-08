"""Intraday monitoring task — runs every 15 minutes during trading hours (09:30-15:00).

Watches for anomalies in active theme line stocks and sector fund flows.
Triggers alerts only when significant changes detected.
"""

import json
import logging

from alpha_agents.data.memory_store import get_active_themes

logger = logging.getLogger(__name__)


async def run_intraday_monitor() -> str | None:
    """Execute one intraday monitoring cycle.

    1. Get active theme lines and their core stocks
    2. Check sector fund flows for anomalies
    3. Check theme leader stocks for significant moves
    4. If anomaly detected, trace the cause and generate alert

    Returns alert text if anomaly found, None otherwise.
    """
    themes = get_active_themes()
    if not themes:
        logger.debug("Intraday monitor: no active themes to watch")
        return None

    logger.info("Intraday monitor: watching %d themes", len(themes))

    # TODO Phase 2+3: Add sector fund flow checking, anomaly detection,
    # cause-tracing with news, and alert generation.
    for theme in themes:
        stocks = json.loads(theme["core_stocks"]) if theme["core_stocks"] else []
        leader = theme.get("leader_code", "?")
        logger.debug("  Watching '%s' (strength=%d, leader=%s, %d stocks)",
                     theme["name"], theme["strength"], leader, len(stocks))

    return None
