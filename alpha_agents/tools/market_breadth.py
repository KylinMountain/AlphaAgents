"""Market breadth tool — overall A-share market sentiment indicators."""

import json
import logging
from datetime import datetime

from alpha_agents.data.market_data import get_market_activity

logger = logging.getLogger(__name__)


def _reconstruct_breadth_from_kline(as_of: str) -> str:
    """Rebuild a breadth summary from daily_kline advances/declines for as_of."""
    from alpha_agents.data.market_history import _get_conn as _mh_conn
    row = _mh_conn().execute(
        "SELECT "
        "SUM(CASE WHEN change_pct > 0 THEN 1 ELSE 0 END) as advances, "
        "SUM(CASE WHEN change_pct < 0 THEN 1 ELSE 0 END) as declines, "
        "SUM(CASE WHEN change_pct = 0 THEN 1 ELSE 0 END) as flat, "
        "SUM(CASE WHEN change_pct >= 9.9 THEN 1 ELSE 0 END) as limit_up, "
        "SUM(CASE WHEN change_pct <= -9.9 THEN 1 ELSE 0 END) as limit_down "
        "FROM daily_kline WHERE date = ?",
        (as_of,),
    ).fetchone()
    if not row or not row["advances"]:
        return json.dumps({"error": f"no kline data for {as_of}"}, ensure_ascii=False)
    advances = int(row["advances"])
    declines = int(row["declines"])
    ad_ratio = round(advances / declines, 2) if declines > 0 else 999
    if ad_ratio > 2:
        sentiment = "乐观"
    elif ad_ratio > 1:
        sentiment = "偏多"
    elif ad_ratio > 0.5:
        sentiment = "偏空"
    else:
        sentiment = "悲观"
    return json.dumps({
        "timestamp": f"{as_of} (reconstructed)",
        "advances": advances,
        "declines": declines,
        "flat": int(row["flat"] or 0),
        "limit_up": int(row["limit_up"] or 0),
        "limit_down": int(row["limit_down"] or 0),
        "real_limit_up": int(row["limit_up"] or 0),
        "real_limit_down": int(row["limit_down"] or 0),
        "total": advances + declines + int(row["flat"] or 0),
        "advance_decline_ratio": ad_ratio,
        "sentiment": sentiment,
    }, ensure_ascii=False)


def get_market_breadth_fn() -> str:
    """Fetch A-share market breadth indicators.

    Returns advance/decline ratio, limit up/down counts, and market activity.
    Use this to assess whether the market is risk-on or risk-off before making recommendations.

    Replay mode: reads the latest daily_snapshots market_breadth entry at/before as_of.
    If none available, reconstructs a minimal breadth from daily_kline advances/declines.
    """
    # Replay mode
    try:
        from alpha_agents.evolution.replay_mode import get_replay_as_of
        as_of = get_replay_as_of()
    except Exception:
        as_of = None
    if as_of:
        from alpha_agents.data.memory_store import _get_conn
        row = _get_conn().execute(
            "SELECT date, data FROM daily_snapshots WHERE data_type='market_breadth' "
            "AND date <= ? ORDER BY date DESC LIMIT 1",
            (as_of,),
        ).fetchone()
        if row:
            return row["data"]  # already JSON string
        # Fallback: reconstruct from market_history daily_kline
        return _reconstruct_breadth_from_kline(as_of)

    try:
        df = get_market_activity()
        if df is None:
            return json.dumps({"error": "no market data"}, ensure_ascii=False)

        data = {}
        for _, row in df.iterrows():
            data[row["item"]] = row["value"]

        advances = int(float(data.get("上涨", 0)))
        declines = int(float(data.get("下跌", 0)))
        limit_up = int(float(data.get("涨停", 0)))
        limit_down = int(float(data.get("跌停", 0)))
        real_limit_up = int(float(data.get("真实涨停", 0)))
        real_limit_down = int(float(data.get("真实跌停", 0)))
        flat = int(float(data.get("平盘", 0)))
        total = advances + declines + flat

        ad_ratio = round(advances / declines, 2) if declines > 0 else 999

        if ad_ratio > 3 and real_limit_up > 50:
            sentiment = "极度乐观"
        elif ad_ratio > 2:
            sentiment = "乐观"
        elif ad_ratio > 1:
            sentiment = "偏多"
        elif ad_ratio > 0.5:
            sentiment = "偏空"
        elif ad_ratio > 0.3:
            sentiment = "悲观"
        else:
            sentiment = "极度悲观"

        result = {
            "timestamp": data.get("统计日期", datetime.now().strftime("%Y-%m-%d %H:%M")),
            "advances": advances,
            "declines": declines,
            "flat": flat,
            "total": total,
            "advance_decline_ratio": ad_ratio,
            "limit_up": limit_up,
            "real_limit_up": real_limit_up,
            "limit_down": limit_down,
            "real_limit_down": real_limit_down,
            "activity_pct": data.get("活跃度", ""),
            "sentiment": sentiment,
            "error": None,
        }

        return json.dumps(result, ensure_ascii=False)

    except Exception as e:
        logger.error("get_market_breadth failed: %s", e)
        return json.dumps({"error": str(e)}, ensure_ascii=False)
