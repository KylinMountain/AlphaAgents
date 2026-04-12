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

    # 升温: increasing limit-ups above 50
    scores["升温"] = 0
    if lu > 50:
        scores["升温"] += 30
    if lu_increasing or lu_2d_increase:
        scores["升温"] += 20
    if br < 0.15:
        scores["升温"] += 15
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
    if mb >= 5:
        scores["狂热"] += 20
    if ar > 3:
        scores["狂热"] += 25

    # 分歧: still many limit-ups but broken rate rising
    scores["分歧"] = 0
    if lu > 40 and br > 0.25:
        scores["分歧"] += 40
    if br_rising:
        scores["分歧"] += 15
    if mb_declining:
        scores["分歧"] += 15
    if 1 <= ar <= 3:
        scores["分歧"] += 15
    if lu > 60 and br > 0.20:
        scores["分歧"] += 15

    # 退潮: decreasing limit-ups, high broken rate
    scores["退潮"] = 0
    if lu_decreasing:
        scores["退潮"] += 20
    if lu_2d_decrease:
        scores["退潮"] += 15
    if br > 0.30:
        scores["退潮"] += 25
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

    Uses akshare to fetch historical data for dates not yet in the DB.
    """
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


def get_sentiment_cycle(use_cache: bool = True) -> dict:
    """Get current market sentiment cycle phase.

    Reads last 3 days from daily_snapshots + today's realtime data.
    Runs backfill if insufficient historical data.

    Returns:
        {"phase": ..., "confidence": ..., "indicators": ..., "strategy": ...}
    """
    from alpha_agents.data.daily_archive import get_snapshot

    # Collect last 3 days of data
    dates = _get_recent_trading_dates(5)  # Get 5 to have buffer
    limit_up_trend = []
    broken_rates = []
    max_boards = []
    ad_ratios = []

    for date_str in dates[-3:]:
        # Limit up data
        snap = get_snapshot(date_str, "limit_up_pool")
        if snap:
            ind = _extract_indicators_from_snapshot(snap)
            limit_up_trend.append(ind["limit_up_count"])
            broken_rates.append(ind["broken_rate"])
            max_boards.append(ind["max_board"])

        # Market breadth
        breadth = get_snapshot(date_str, "market_breadth")
        if breadth:
            ad_ratios.append(breadth.get("advance_decline_ratio", 1.0))

    # If not enough historical data, try backfill
    if len(limit_up_trend) < 2:
        logger.info("Insufficient snapshot data (%d days), attempting backfill...", len(limit_up_trend))
        backfill_snapshots(days=10)
        # Retry after backfill
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

    # Add today's realtime data
    try:
        from alpha_agents.tools.anomaly_detect import get_anomaly_stocks_fn
        from alpha_agents.tools.market_breadth import get_market_breadth_fn
        import json as _j

        anomaly = _j.loads(get_anomaly_stocks_fn())
        summary = anomaly.get("summary", {})
        today_lu = summary.get("limit_up_count", 0) or 0
        today_broken = summary.get("broken_limit_count", 0) or 0
        today_br = today_broken / today_lu if today_lu > 0 else 0
        consec = summary.get("consecutive_limit_stocks", [])
        today_mb = max((s.get("consecutive_limits", 0) for s in consec), default=0) if consec else 0

        limit_up_trend.append(today_lu)
        broken_rates.append(round(today_br, 3))
        max_boards.append(today_mb)

        breadth = _j.loads(get_market_breadth_fn())
        ad_ratios.append(breadth.get("advance_decline_ratio", 1.0))
    except Exception as e:
        logger.warning("Failed to fetch today's realtime sentiment data: %s", e)

    # Fallback if still not enough data
    if not limit_up_trend:
        logger.warning("No sentiment data available, defaulting to 修复")
        return {
            "phase": "修复",
            "phase_en": "recovery",
            "confidence": 0.0,
            "indicators": {},
            "strategy": get_phase_strategy("修复"),
        }

    return detect_phase(
        limit_up_trend=limit_up_trend,
        broken_rates=broken_rates,
        max_boards=max_boards,
        ad_ratios=ad_ratios,
    )


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
