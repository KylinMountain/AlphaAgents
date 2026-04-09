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
from alpha_agents.data.market_data import get_stock_history

logger = logging.getLogger(__name__)

MAX_ACTIVE_THEMES = 8
MAX_PER_CATEGORY = 3  # Max themes from the same broad category

STATUS_ORDER = ["watching", "active", "peak", "declining", "archived"]

# Broad category mapping — prevents all 8 themes being tech
CATEGORY_KEYWORDS = {
    "科技": ["芯片", "半导体", "AI", "人工智能", "数据中心", "算力", "5G", "光模块", "CPO",
             "华为", "DeepSeek", "Sora", "ChatGPT", "通信", "软件", "云计算", "物联网",
             "信创", "操作系统", "数据库", "网络安全", "量子", "存储"],
    "消费": ["白酒", "食品", "零售", "电商", "消费电子", "家电", "旅游", "酒店", "免税",
             "预制菜", "医美", "服装", "汽车", "新能源汽车"],
    "制造": ["机器人", "无人机", "低空经济", "3D打印", "工业母机", "新材料", "军工",
             "航天", "船舶", "先进封装"],
    "金融": ["银行", "保险", "券商", "证券", "金融科技", "数字货币"],
    "资源": ["黄金", "贵金属", "石油", "天然气", "煤炭", "有色", "稀土", "锂电",
             "光伏", "风电", "新能源", "储能", "氢能"],
    "医药": ["医药", "生物", "疫苗", "中药", "医疗器械", "创新药", "CXO"],
    "基建": ["地产", "基建", "水泥", "钢铁", "建材", "交通", "港口", "航运"],
}


def _get_theme_category(name: str) -> str:
    """Classify a theme into a broad category."""
    for cat, keywords in CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if kw in name:
                return cat
    return "其他"


# Concepts that are too broad or not real investment themes — skip these
NOISE_CONCEPTS = {
    "融资融券", "深股通", "沪股通", "国企改革", "人民币贬值受益",
    "人民币升值受益", "标准普尔", "MSCI概念", "富时罗素概念",
    "基金重仓", "社保重仓", "险资重仓", "送转预期",
    "年报预增", "2025年报预增", "2024年报预增",
    "ST股", "B股", "AH股", "注册制次新股",
}


def check_leader_health(theme: dict) -> bool:
    """Check if the leader stock is breaking down (price below 5-day MA).

    Args:
        theme: A theme dict from the DB (must have 'leader_code').

    Returns:
        True if the leader is breaking down (bearish), False otherwise.
    """
    leader_code = theme.get("leader_code")
    if not leader_code:
        return False
    try:
        history = get_stock_history(leader_code, days=7)
        if not history or len(history) < 5:
            return False
        # Calculate 5-day moving average
        recent_5 = history[-5:]
        ma5 = sum(d["close"] for d in recent_5) / 5
        latest_close = history[-1]["close"]
        if latest_close < ma5:
            logger.debug("Leader %s breaking down: close %.2f < MA5 %.2f",
                         leader_code, latest_close, ma5)
            return True
    except Exception as e:
        logger.debug("check_leader_health(%s) failed: %s", leader_code, e)
    return False


def evaluate_theme_signals(
    sector_name: str,
    sector_change_pct: float,
    sector_fund_flow: float,
    market_change_pct: float,
    has_news_catalyst: bool = False,
    leader_hit_limit: bool = False,
    consecutive_inflow_days: int = 0,
    consecutive_outflow_days: int = 0,
    leader_breaking_down: bool = False,
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
    elif consecutive_outflow_days >= 2:
        bearish += 1
        details.append(f"连续{consecutive_outflow_days}天资金流出")
    if leader_breaking_down:
        bearish += 1
        details.append("龙头破位(跌破5日均线)")

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

    # Check category diversity — max 3 themes per broad category
    new_cat = _get_theme_category(sector_name)
    active = get_active_themes()
    same_cat_count = sum(1 for t in active if _get_theme_category(t["name"]) == new_cat)
    if same_cat_count >= MAX_PER_CATEGORY:
        logger.debug("Skipping '%s': category '%s' already has %d themes",
                      sector_name, new_cat, same_cat_count)
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
