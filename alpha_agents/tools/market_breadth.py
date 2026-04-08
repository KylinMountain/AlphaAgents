"""Market breadth tool — overall A-share market sentiment indicators."""

import json
import logging
from datetime import datetime

from alpha_agents.data.market_data import get_market_activity

logger = logging.getLogger(__name__)


def get_market_breadth_fn() -> str:
    """Fetch A-share market breadth indicators.

    Returns advance/decline ratio, limit up/down counts, and market activity.
    Use this to assess whether the market is risk-on or risk-off before making recommendations.
    """
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
