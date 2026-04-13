"""Market sentiment cycle detection — 6-phase emotion model.

Phases: 冰点 → 修复 → 升温 → 狂热 → 分歧 → 退潮 → 冰点

Based on 3-day trends in:
  1. Limit-up stock count (赚钱效应)
  2. Broken-limit rate (炸板率, 分歧度)
  3. Max consecutive board height (情绪天花板)
  4. Advance-decline ratio (验证信号)
"""

import json
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Phase definitions with strategy parameters
PHASES = {
    "冰点": {
        "en": "freezing",
        "max_exposure_pct": 20,
        "trailing_stop_pct": 5,
        "theme_exit_threshold": 3,  # default, don't rush to sell
        "buy_style": "只低吸强主线龙头",
        "sell_style": "不急卖，主线没死就拿着",
    },
    "修复": {
        "en": "recovery",
        "max_exposure_pct": 40,
        "trailing_stop_pct": 5,
        "theme_exit_threshold": 3,
        "buy_style": "低吸为主，开始关注新方向",
        "sell_style": "正常止损",
    },
    "升温": {
        "en": "warming",
        "max_exposure_pct": 60,
        "trailing_stop_pct": 5,
        "theme_exit_threshold": 3,
        "buy_style": "可追强势，高beta优先",
        "sell_style": "放宽移动止损（给空间跑）",
    },
    "狂热": {
        "en": "frenzy",
        "max_exposure_pct": 50,
        "trailing_stop_pct": 3,
        "theme_exit_threshold": 3,
        "buy_style": "只持有不新开仓，准备跑",
        "sell_style": "收紧移动止损（锁利润）",
    },
    "分歧": {
        "en": "divergence",
        "max_exposure_pct": 30,
        "trailing_stop_pct": 3,
        "theme_exit_threshold": 7,  # aggressive: exit if theme < 7
        "buy_style": "不追高，只保留强主线持仓",
        "sell_style": "弱持仓主动减仓（主线强度<7清掉）",
    },
    "退潮": {
        "en": "retreat",
        "max_exposure_pct": 15,
        "trailing_stop_pct": 3,
        "theme_exit_threshold": 8,  # very aggressive: exit if theme < 8
        "buy_style": "几乎不买",
        "sell_style": "全面收紧（主线强度<8都走）",
    },
}


def get_phase_strategy(phase: str) -> dict:
    """Get strategy parameters for a given phase."""
    return PHASES.get(phase, PHASES["修复"]).copy()


def detect_phase(
    limit_up_trend: list[int],
    broken_rates: list[float],
    max_boards: list[int],
    ad_ratios: list[float],
    previous_phase: str = "",
) -> dict:
    """Detect current sentiment phase from indicator trends.

    Args:
        limit_up_trend: Last 3 days limit-up counts [oldest, ..., newest]
        broken_rates: Last 3 days broken-limit rates (0-1) [oldest, ..., newest]
        max_boards: Last 3 days max consecutive board heights [oldest, ..., newest]
        ad_ratios: Last 3 days advance-decline ratios [oldest, ..., newest]
        previous_phase: Yesterday's phase for inertia

    Returns:
        {"phase": "升温", "phase_en": "warming", "confidence": 0.8, "indicators": {...}, "strategy": {...}}
    """
    # Use latest values as primary, trends for direction
    lu = limit_up_trend[-1] if limit_up_trend else 0
    br = broken_rates[-1] if broken_rates else 0
    mb = max_boards[-1] if max_boards else 0
    ar = ad_ratios[-1] if ad_ratios else 1.0

    # Trends (increasing/decreasing over last 2-3 days)
    lu_increasing = len(limit_up_trend) >= 2 and limit_up_trend[-1] > limit_up_trend[-2]
    lu_decreasing = len(limit_up_trend) >= 2 and limit_up_trend[-1] < limit_up_trend[-2]
    lu_2d_decrease = len(limit_up_trend) >= 3 and limit_up_trend[-1] < limit_up_trend[-2] < limit_up_trend[-3]
    lu_2d_increase = len(limit_up_trend) >= 3 and limit_up_trend[-1] > limit_up_trend[-2] > limit_up_trend[-3]
    mb_declining = len(max_boards) >= 2 and max_boards[-1] < max_boards[-2]
    br_rising = len(broken_rates) >= 2 and broken_rates[-1] > broken_rates[-2]

    # Score each phase
    scores = {}

    # 冰点: very few limit-ups, low boards, bearish
    scores["冰点"] = 0
    if lu < 20:
        scores["冰点"] += 40
    if mb <= 2:
        scores["冰点"] += 25
    if ar < 0.5:
        scores["冰点"] += 25
    if len(limit_up_trend) >= 2 and all(x < 20 for x in limit_up_trend[-2:]):
        scores["冰点"] += 10

    # 修复: recovering from freezing
    scores["修复"] = 0
    if 20 <= lu <= 50:
        scores["修复"] += 35
    if br < 0.20:
        scores["修复"] += 15
    if 2 <= mb <= 3:
        scores["修复"] += 20
    if 0.5 <= ar <= 1.5:
        scores["修复"] += 20
    if lu_increasing and lu < 50:
        scores["修复"] += 10

    # 升温: increasing limit-ups above 50, but NOT if broken rate is high
    scores["升温"] = 0
    if lu > 50:
        scores["升温"] += 30
    if lu_increasing or lu_2d_increase:
        scores["升温"] += 20
    if br < 0.15:
        scores["升温"] += 15
    elif br > 0.25:
        scores["升温"] -= 30  # High broken rate kills warming signal
    if 3 <= mb <= 5:
        scores["升温"] += 15
    if ar > 1.5:
        scores["升温"] += 20

    # 狂热: very high limit-ups, low broken rate, high boards
    scores["狂热"] = 0
    if lu > 100:
        scores["狂热"] += 35
    if br < 0.10:
        scores["狂热"] += 20
    elif br > 0.20:
        scores["狂热"] -= 20  # Broken rate rising → not frenzy, it's divergence
    if mb >= 5:
        scores["狂热"] += 20
    if ar > 3:
        scores["狂热"] += 25

    # 分歧: high broken rate + still many limit-ups (key: market is fighting itself)
    scores["分歧"] = 0
    if lu > 40 and br > 0.25:
        scores["分歧"] += 40  # Many limit-ups but many breaking = classic divergence
    elif lu > 30 and br > 0.30:
        scores["分歧"] += 35
    if br_rising and br > 0.20:
        scores["分歧"] += 20  # Broken rate rising is THE divergence signal
    if mb_declining:
        scores["分歧"] += 15
    if 0.5 <= ar <= 3:
        scores["分歧"] += 10

    # 退潮: decreasing limit-ups, high broken rate, declining boards
    scores["退潮"] = 0
    if lu_decreasing:
        scores["退潮"] += 25
    if lu_2d_decrease:
        scores["退潮"] += 20
    if br > 0.30:
        scores["退潮"] += 25
    elif br > 0.20:
        scores["退潮"] += 10
    if mb_declining:
        scores["退潮"] += 15
    if ar < 1:
        scores["退潮"] += 25

    # Apply inertia: boost previous phase by 10 points
    if previous_phase and previous_phase in scores:
        scores[previous_phase] += 10

    # Pick highest scoring phase
    phase = max(scores, key=scores.get)
    max_score = scores[phase]
    total_possible = 100
    confidence = round(min(max_score / total_possible, 1.0), 2)

    strategy = get_phase_strategy(phase)

    return {
        "phase": phase,
        "phase_en": PHASES[phase]["en"],
        "confidence": confidence,
        "scores": scores,
        "indicators": {
            "limit_up_trend": limit_up_trend,
            "broken_rate": br,
            "max_consecutive": mb,
            "ad_ratio_trend": ad_ratios,
        },
        "strategy": strategy,
    }


def _extract_indicators_from_snapshot(snapshot: dict) -> dict:
    """Extract sentiment indicators from a daily_snapshots limit_up_pool entry."""
    summary = snapshot.get("summary", {})
    limit_up_count = summary.get("limit_up_count", 0) or 0
    broken_count = summary.get("broken_limit_count", 0) or 0
    broken_rate = broken_count / limit_up_count if limit_up_count > 0 else 0

    consecutive = summary.get("consecutive_limit_stocks", [])
    max_board = 0
    if consecutive:
        max_board = max((s.get("consecutive_limits", 0) for s in consecutive), default=0)

    return {
        "limit_up_count": limit_up_count,
        "broken_rate": round(broken_rate, 3),
        "max_board": max_board,
    }


def _get_recent_trading_dates(n: int = 5) -> list[str]:
    """Get the last N trading dates (or calendar dates as approximation)."""
    dates = []
    d = datetime.now()
    while len(dates) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:  # Skip weekends
            dates.append(d.strftime("%Y-%m-%d"))
    dates.reverse()
    return dates


def backfill_snapshots(days: int = 10) -> int:
    """Backfill daily_snapshots with historical limit-up pool data.

    Tries local market_history.db first (fast, covers months).
    Falls back to akshare API (slow, covers ~1 month).
    """
    # Try local market history first (fast, covers months of data)
    try:
        from alpha_agents.data.market_history import backfill_sentiment_snapshots
        filled = backfill_sentiment_snapshots(days=days)
        if filled > 0:
            logger.info("Backfilled %d days from local market history", filled)
            return filled
    except Exception as e:
        logger.debug("Local backfill unavailable, using akshare: %s", e)

    from alpha_agents.data.daily_archive import save_snapshot, get_snapshot
    from alpha_agents.data.market_data import _ak_call

    filled = 0
    dates = _get_recent_trading_dates(days)

    for date_str in dates:
        # Skip if already have data
        existing = get_snapshot(date_str, "limit_up_pool")
        if existing and existing.get("summary", {}).get("limit_up_count", 0) > 0:
            continue

        date_fmt = date_str.replace("-", "")
        try:
            import akshare as ak

            # Limit up pool
            df_zt = _ak_call(ak.stock_zt_pool_em, date=date_fmt)
            df_zb = _ak_call(ak.stock_zt_pool_zbgc_em, date=date_fmt)

            limit_up_count = len(df_zt) if df_zt is not None else 0
            broken_count = len(df_zb) if df_zb is not None else 0

            # Extract consecutive board info
            consecutive = []
            if df_zt is not None and "连板数" in df_zt.columns:
                for _, row in df_zt.iterrows():
                    boards = int(row.get("连板数", 1) or 1)
                    if boards >= 2:
                        consecutive.append({
                            "code": str(row.get("代码", "")),
                            "name": str(row.get("名称", "")),
                            "consecutive_limits": boards,
                        })
                consecutive.sort(key=lambda x: x["consecutive_limits"], reverse=True)

            snapshot_data = {
                "summary": {
                    "limit_up_count": limit_up_count,
                    "broken_limit_count": broken_count,
                    "consecutive_limit_stocks": consecutive[:10],
                },
            }
            save_snapshot(date_str, "limit_up_pool", snapshot_data)
            filled += 1
            logger.info("Backfilled limit_up_pool for %s: %d limit-up, %d broken",
                        date_str, limit_up_count, broken_count)
        except Exception as e:
            logger.debug("Backfill failed for %s: %s", date_str, e)

    logger.info("Backfill complete: %d days filled", filled)
    return filled


def _save_phase_to_db(date: str, result: dict) -> None:
    """Save computed sentiment phase to DB for the given date."""
    from alpha_agents.data.memory_store import _get_conn, _write_lock
    with _write_lock:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO sentiment_phase (date, phase, phase_en, confidence, indicators, strategy) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(date) DO UPDATE SET "
            "phase=excluded.phase, phase_en=excluded.phase_en, confidence=excluded.confidence, "
            "indicators=excluded.indicators, strategy=excluded.strategy, created_at=datetime('now')",
            (date, result["phase"], result["phase_en"], result["confidence"],
             json.dumps(result.get("indicators", {}), ensure_ascii=False),
             json.dumps(result.get("strategy", {}), ensure_ascii=False)),
        )
        conn.commit()
    logger.info("Saved sentiment phase for %s: %s (confidence %.0f%%)",
                date, result["phase"], result["confidence"] * 100)


def _load_phase_from_db(date: str) -> dict | None:
    """Load saved sentiment phase from DB."""
    from alpha_agents.data.memory_store import _get_conn
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM sentiment_phase WHERE date = ?", (date,)
    ).fetchone()
    if not row:
        return None
    return {
        "phase": row["phase"],
        "phase_en": row["phase_en"],
        "confidence": row["confidence"],
        "indicators": json.loads(row["indicators"]) if row["indicators"] else {},
        "strategy": get_phase_strategy(row["phase"]),
    }


def compute_and_save_sentiment(target_date: str = "") -> dict:
    """Compute sentiment phase from historical data and save to DB.

    Called by the review task (15:30) to set tomorrow's sentiment.
    Uses the last 3 days of daily_snapshots (no realtime data needed).

    Args:
        target_date: The date this sentiment applies to (default: tomorrow for weekday, next Monday for Friday)
    """
    from alpha_agents.data.daily_archive import get_snapshot

    if not target_date:
        # Determine next trading day
        today = datetime.now()
        if today.weekday() == 4:  # Friday → next Monday
            next_day = today + timedelta(days=3)
        elif today.weekday() == 5:  # Saturday → next Monday
            next_day = today + timedelta(days=2)
        else:
            next_day = today + timedelta(days=1)
        target_date = next_day.strftime("%Y-%m-%d")

    # Collect last 3 days of data from snapshots
    dates = _get_recent_trading_dates(5)
    limit_up_trend = []
    broken_rates = []
    max_boards = []
    ad_ratios = []

    for date_str in dates[-3:]:
        snap = get_snapshot(date_str, "limit_up_pool")
        if snap:
            ind = _extract_indicators_from_snapshot(snap)
            limit_up_trend.append(ind["limit_up_count"])
            broken_rates.append(ind["broken_rate"])
            max_boards.append(ind["max_board"])
        breadth = get_snapshot(date_str, "market_breadth")
        if breadth:
            ad_ratios.append(breadth.get("advance_decline_ratio", 1.0))

    # Backfill if not enough data
    if len(limit_up_trend) < 2:
        backfill_snapshots(days=10)
        limit_up_trend, broken_rates, max_boards, ad_ratios = [], [], [], []
        for date_str in dates[-3:]:
            snap = get_snapshot(date_str, "limit_up_pool")
            if snap:
                ind = _extract_indicators_from_snapshot(snap)
                limit_up_trend.append(ind["limit_up_count"])
                broken_rates.append(ind["broken_rate"])
                max_boards.append(ind["max_board"])
            breadth = get_snapshot(date_str, "market_breadth")
            if breadth:
                ad_ratios.append(breadth.get("advance_decline_ratio", 1.0))

    if not limit_up_trend:
        result = {
            "phase": "修复", "phase_en": "recovery", "confidence": 0.0,
            "indicators": {}, "strategy": get_phase_strategy("修复"),
        }
    else:
        # Get previous phase for inertia
        today_str = datetime.now().strftime("%Y-%m-%d")
        prev = _load_phase_from_db(today_str)
        previous_phase = prev["phase"] if prev else ""

        result = detect_phase(
            limit_up_trend=limit_up_trend,
            broken_rates=broken_rates,
            max_boards=max_boards,
            ad_ratios=ad_ratios,
            previous_phase=previous_phase,
        )

    _save_phase_to_db(target_date, result)
    return result


def get_sentiment_cycle() -> dict:
    """Get today's sentiment cycle phase.

    Reads from DB (pre-computed by review task). If not found,
    falls back to computing from historical data.

    Returns:
        {"phase": ..., "confidence": ..., "indicators": ..., "strategy": ...}
    """
    today = datetime.now().strftime("%Y-%m-%d")

    # Try DB first (set by previous day's review)
    cached = _load_phase_from_db(today)
    if cached:
        return cached

    # Fallback: compute on-the-fly (first run or missed review)
    logger.info("No pre-computed sentiment for %s, computing on-the-fly...", today)
    return compute_and_save_sentiment(target_date=today)


def format_sentiment_cycle(result: dict) -> str:
    """Format sentiment cycle result for display."""
    phase = result.get("phase", "?")
    conf = result.get("confidence", 0)
    ind = result.get("indicators", {})
    strat = result.get("strategy", {})

    lines = [
        f"情绪阶段: {phase} ({result.get('phase_en', '')}) 置信度{conf:.0%}",
        f"涨停趋势: {ind.get('limit_up_trend', [])}",
        f"炸板率: {ind.get('broken_rate', 0):.1%}",
        f"连板高度: {ind.get('max_consecutive', 0)}板",
        f"涨跌比趋势: {[round(x, 2) for x in ind.get('ad_ratio_trend', [])]}",
        "",
        f"策略建议:",
        f"  仓位上限: {strat.get('max_exposure_pct', 50)}%",
        f"  买入: {strat.get('buy_style', '')}",
        f"  卖出: {strat.get('sell_style', '')}",
        f"  移动止损: {strat.get('trailing_stop_pct', 5)}%",
    ]
    return "\n".join(lines)
