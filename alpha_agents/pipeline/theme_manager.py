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
from alpha_agents.tools.sector_ranking import (
    get_concept_ranking_fn as _concept_ranking_fn,
    get_sector_ranking_fn as _sector_ranking_fn,
)

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


def update_theme_strength(name: str, signals: dict, today: str | None = None) -> None:
    """Record today's score for a theme, and age its strength once per day.

    Two numbers come out of one evaluation:

    ``daily_score`` is ``bullish − bearish`` as measured right now, and it
    is rewritten on every call. It is the only one of the two that ranks
    lines against each other today.

    ``strength`` accumulates that score, but **at most once per calendar
    day**. It is a lifecycle position — roughly how many sessions this
    line has been confirmed — and it drives the status machine and the
    ≥4 gate on pending orders.

    The day guard is the whole point. ``_refresh_theme_strengths`` calls
    this every intraday cycle, 48 times between 09:30 and 15:00; without
    it a line with steady inflow hit the ceiling of 10 within half an hour
    and a weak one floored at 0, so the number tracked how many cycles had
    elapsed rather than how strong anything was. 锡业股份's order was
    cancelled for 金属铜 强度3 on a session that line ran +2.0% against
    the market on 55億 of net inflow.
    """
    theme = get_theme_by_name(name)
    if not theme or theme["status"] == "archived":
        return

    today = today or datetime.now().strftime("%Y-%m-%d")
    current = theme["strength"] or 0
    bull = signals["bullish_signals"]
    bear = signals["bearish_signals"]
    delta = bull - bear

    already_scored_today = (theme["last_scored_date"] or "") == today
    if already_scored_today:
        # Today's contribution is already in `strength`; only refresh the
        # live read. Status cannot change without a strength change, so
        # there is nothing else to recompute.
        upsert_theme(name, daily_score=delta)
        return

    new_strength = max(0, min(10, current + delta))

    status = theme["status"]
    if new_strength >= 7 and status in ("watching", "active"):
        status = "peak" if new_strength >= 9 else "active"
    elif new_strength >= 4 and status == "watching":
        status = "active"
    elif new_strength < 4 and status in ("active", "peak"):
        status = "declining"
    elif new_strength <= 1 and status in ("declining", "watching"):
        # 'watching' had no exit: a theme discovered weak and never
        # confirmed decayed to strength 0 and stayed forever, still
        # supplying picks with no thesis behind them.
        status = "archived"

    upsert_theme(name, status=status, strength=new_strength,
                 daily_score=delta, last_scored_date=today)
    logger.info("Theme '%s': strength %d→%d (今日 %+d), status=%s (%s)",
                name, current, new_strength, delta, status,
                "; ".join(signals["details"]))


def sectors_from_events(events: list[dict], min_importance: int = 4) -> dict[str, dict]:
    """Sectors the digest named as bullish, keyed by sector name.

    ``evaluate_theme_signals`` has always accepted ``has_news_catalyst``
    and nothing ever passed it True: every discovery path fed the concept
    ranking alone. So a system whose whole premise is 资金 + 新闻归因 ran
    its theme lifecycle on price and flow only — the digest could rate
    "沙特能源设施遭袭" importance 5/5 and name 石油石化 / 天然气, and no
    theme moved.

    Returns {sector: {"importance": int, "event": str}} keeping the
    highest-importance event per sector.
    """
    out: dict[str, dict] = {}
    for e in events or []:
        importance = e.get("importance", 0) or 0
        if importance < min_importance:
            continue
        impact = (e.get("market_impact") or {}).get("a_share") or {}
        if impact.get("direction") == "bearish":
            continue
        for sector in impact.get("sectors_bullish") or []:
            sector = (sector or "").strip()
            if not sector:
                continue
            if sector not in out or importance > out[sector]["importance"]:
                out[sector] = {"importance": importance,
                               "event": e.get("event", "")}
    return out


def _match_board(sector: str, boards: dict[str, dict]) -> str | None:
    """Board matching a sector the news named.

    The digest writes its own vocabulary (石油石化, 航运) while the
    exchange's boards carry theirs (油气开采及服务, 航运港口). Exact and
    containment matching pair neither, and a hand-written synonym table
    would need editing every time a board is renamed — so the embeddings
    that already back concept search do the pairing.

    Exact and containment run first: they are free and cover most calls,
    leaving the embedding API for the pairs that actually need it.
    """
    if sector in boards:
        return sector
    for name in boards:
        if sector in name or name in sector:
            return name
    try:
        from alpha_agents.data.embeddings import match_label_semantic
        return match_label_semantic(sector, list(boards))
    except Exception as e:
        logger.debug("Semantic board match unavailable for %r: %s", sector, e)
        return None



def _full_boards() -> list[dict]:
    """Every concept and industry board, for matching a news label to one.

    News names a sector in the digest's own words ("石油石化"); the
    tradable line is a concept board. Matching across the whole list is
    the point — a board the news just made interesting is often nowhere
    near the top of the flow ranking *yet*, which is precisely the case
    this function exists to catch.

    Returns [] on failure: no board means no confirmation, and no
    confirmation means no theme. Falling silent here is the conservative
    direction.
    """
    out = []
    for fn, label in ((_concept_ranking_fn, "概念"), (_sector_ranking_fn, "行业")):
        try:
            out.append(json.loads(fn(top_n=999)))
        except Exception as e:
            logger.warning("%s板块全量获取失败，新闻驱动的主线发现本轮受限: %s",
                           label, e)
    return out


def discover_themes_from_events(events: list[dict], *rankings: dict) -> list[str]:
    """Create or strengthen themes the news named AND the money confirms.

    Both halves are required. News alone would let the model invent a
    theme out of a headline; flow alone is what the system already did,
    and it misses the sector that just moved because of an event. A
    sector is only promoted when it appears in the live concept ranking.

    **The board is fetched here, not taken on trust from the caller.**
    Three separate bugs in this codebase had the same shape: a holding
    was looked up in a truncated slice of the market, and a board drops
    out of the slice exactly when it starts to matter. Callers may still
    pass rankings they already have — those are merged in as a
    supplement — but the correctness of this function no longer depends
    on someone else having asked for enough rows.

    Returns the theme names touched.
    """
    named = sectors_from_events(events)
    if not named:
        return []

    # Concept and industry boards both: the digest's labels sit closer to
    # industry names, but the tradable theme is usually a concept.
    concepts: dict[str, dict] = {}
    for ranking in (*rankings, *_full_boards()):
        for row in (ranking.get("gainers") or []) + (ranking.get("losers") or []):
            name = row.get("concept") or row.get("sector") or ""
            if name:
                concepts.setdefault(name, row)
    touched = []
    for sector, info in named.items():
        concept_name = _match_board(sector, concepts)
        if concept_name is None:
            logger.debug("News named '%s' but no concept board matched it",
                         sector)
            continue
        board = concepts[concept_name]
        signals = evaluate_theme_signals(
            sector_name=concept_name,
            sector_change_pct=board.get("change_pct", 0),
            sector_fund_flow=board.get("net_flow_yi", 0) * 1e8,
            market_change_pct=0,
            has_news_catalyst=True,
        )
        catalyst = (f"新闻催化(重要性{info['importance']}/5): {info['event'][:60]}"
                    f" | 概念涨{board.get('change_pct', 0):.1f}%,"
                    f" 净流入{board.get('net_flow_yi', 0):.1f}亿")
        existing = get_theme_by_name(concept_name)
        if existing and existing["status"] != "archived":
            update_theme_strength(concept_name, signals)
            upsert_theme(concept_name, catalyst=catalyst)
            touched.append(concept_name)
        elif maybe_discover_theme(concept_name, signals, catalyst=catalyst):
            touched.append(concept_name)
    if touched:
        logger.info("News-driven themes touched: %s", ", ".join(touched))
    return touched


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
