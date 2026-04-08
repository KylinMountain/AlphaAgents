"""Theme line lifecycle manager.

Handles auto-discovery of new market themes from sector data,
strength tracking, and automatic retirement of fading themes.

Theme lifecycle: watching → active → peak → declining → archived
"""

import json
import logging
from datetime import datetime

from alpha_agents.data.memory_store import (
    get_active_themes, get_theme_by_name, upsert_theme, archive_theme,
)

logger = logging.getLogger(__name__)

MAX_ACTIVE_THEMES = 8

STATUS_ORDER = ["watching", "active", "peak", "declining", "archived"]

# Concepts that are too broad or not real investment themes — skip these
NOISE_CONCEPTS = {
    "融资融券", "深股通", "沪股通", "国企改革", "人民币贬值受益",
    "人民币升值受益", "标准普尔", "MSCI概念", "富时罗素概念",
    "基金重仓", "社保重仓", "险资重仓", "送转预期",
    "年报预增", "2025年报预增", "2024年报预增",
    "ST股", "B股", "AH股", "注册制次新股",
}


def evaluate_theme_signals(
    sector_name: str,
    sector_change_pct: float,
    sector_fund_flow: float,
    market_change_pct: float,
    has_news_catalyst: bool = False,
    leader_hit_limit: bool = False,
    consecutive_inflow_days: int = 0,
    consecutive_outflow_days: int = 0,
) -> dict:
    """Evaluate bullish/bearish signals for a potential or existing theme.

    Returns:
        {"bullish_signals": int, "bearish_signals": int, "details": [...]}
    """
    bullish, bearish, details = 0, 0, []

    relative_strength = sector_change_pct - market_change_pct
    if relative_strength > 1.0:
        bullish += 1
        details.append(f"跑赢大盘{relative_strength:.1f}%")
    if sector_fund_flow > 0:
        bullish += 1
        details.append(f"资金净流入{sector_fund_flow/1e8:.1f}亿")
    if has_news_catalyst:
        bullish += 1
        details.append("有新闻催化")
    if leader_hit_limit:
        bullish += 1
        details.append("龙头涨停")
    if consecutive_inflow_days >= 2:
        bullish += 1
        details.append(f"连续{consecutive_inflow_days}天资金流入")

    if relative_strength < -1.0:
        bearish += 1
        details.append(f"跑输大盘{abs(relative_strength):.1f}%")
    if sector_fund_flow < 0:
        bearish += 1
        details.append(f"资金净流出{abs(sector_fund_flow)/1e8:.1f}亿")
    if consecutive_outflow_days >= 3:
        bearish += 2
        details.append(f"连续{consecutive_outflow_days}天资金流出")

    return {"bullish_signals": bullish, "bearish_signals": bearish, "details": details}


def update_theme_strength(name: str, signals: dict) -> None:
    """Update a theme's strength based on today's signals."""
    theme = get_theme_by_name(name)
    if not theme or theme["status"] == "archived":
        return

    current = theme["strength"] or 0
    bull = signals["bullish_signals"]
    bear = signals["bearish_signals"]

    delta = bull - bear
    new_strength = max(0, min(10, current + delta))

    status = theme["status"]
    if new_strength >= 7 and status in ("watching", "active"):
        status = "peak" if new_strength >= 9 else "active"
    elif new_strength >= 4 and status == "watching":
        status = "active"
    elif new_strength < 4 and status in ("active", "peak"):
        status = "declining"
    elif new_strength <= 1 and status == "declining":
        status = "archived"

    upsert_theme(name, status=status, strength=new_strength)
    logger.info("Theme '%s': strength %d→%d, status=%s (%s)",
                name, current, new_strength, status, "; ".join(signals["details"]))


def maybe_discover_theme(
    sector_name: str,
    signals: dict,
    catalyst: str = "",
) -> bool:
    """Check if signals warrant creating a new theme line.

    Requirements: 2+ bullish signals and no existing active theme with this name.
    Returns True if a new theme was created.
    """
    if signals["bullish_signals"] < 2:
        return False

    if sector_name in NOISE_CONCEPTS:
        return False

    existing = get_theme_by_name(sector_name)
    if existing and existing["status"] != "archived":
        return False

    active = get_active_themes()
    if len(active) >= MAX_ACTIVE_THEMES:
        weakest = min(active, key=lambda t: t["strength"])
        if weakest["strength"] < signals["bullish_signals"]:
            archive_theme(weakest["name"])
            logger.info("Archived weakest theme '%s' (strength=%d) to make room",
                        weakest["name"], weakest["strength"])
        else:
            return False

    upsert_theme(
        sector_name,
        status="watching",
        strength=signals["bullish_signals"],
        catalyst=catalyst or "; ".join(signals["details"]),
    )
    logger.info("Discovered new theme: '%s' (strength=%d, catalyst=%s)",
                sector_name, signals["bullish_signals"], catalyst)
    return True


def retire_stale_themes(max_age_days: int = 14) -> list[str]:
    """Archive themes that have been declining for too long."""
    archived = []
    for theme in get_active_themes():
        if theme["status"] == "declining":
            updated = datetime.fromisoformat(theme["updated_at"]) if theme["updated_at"] else datetime.now()
            age = (datetime.now() - updated).days
            if age > max_age_days:
                archive_theme(theme["name"])
                archived.append(theme["name"])
                logger.info("Retired stale theme '%s' (declining for %d days)", theme["name"], age)
    return archived
