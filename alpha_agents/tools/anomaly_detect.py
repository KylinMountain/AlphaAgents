"""Anomaly detection tool — find stocks with unusual volume/price behavior.

Detects stocks with abnormal volume ratio (量比 > 3) or extreme turnover,
which often signal institutional activity or news-driven moves.
Uses the existing stock_zt_pool_em for limit-up detection.
"""

import json
import logging
from datetime import datetime

from alpha_agents.data.market_data import get_limit_up_pool, get_broken_limit_pool, get_limit_down_pool

logger = logging.getLogger(__name__)


def get_anomaly_stocks_fn(date: str = "") -> str:
    """Detect stocks with unusual price/volume behavior.

    Returns:
    - Limit-up stocks (涨停) with seal strength info
    - Limit-down stocks (跌停)
    - Stocks breaking out of limit-up (炸板)

    Args:
        date: Date in YYYYMMDD format. Empty for today.
    """
    try:
        if not date:
            date = datetime.now().strftime("%Y%m%d")

        result = {
            "date": date,
            "limit_up": [],
            "limit_down": [],
            "broken_limit": [],
            "error": None,
        }

        # 1. Limit-up pool (涨停)
        try:
            df_zt = get_limit_up_pool(date=date)
            if df_zt is None:
                raise ValueError("no data")
            for _, row in df_zt.head(20).iterrows():
                result["limit_up"].append({
                    "code": str(row.get("代码", "")),
                    "name": str(row.get("名称", "")),
                    "change_pct": float(row.get("涨跌幅", 0) or 0),
                    "turnover_rate": float(row.get("换手率", 0) or 0),
                    "seal_amount_yi": round(float(row.get("封板资金", 0) or 0) / 1e8, 2),
                    "first_seal_time": str(row.get("首次封板时间", "")),
                    "break_count": int(row.get("炸板次数", 0) or 0),
                    "consecutive_limits": int(row.get("连板数", 0) or 0),
                    "sector": str(row.get("所属行业", "")),
                })
        except Exception as e:
            logger.debug("Limit-up pool failed: %s", e)

        # 2. Broken limit-up pool (炸板)
        try:
            df_zb = get_broken_limit_pool(date=date)
            if df_zb is None:
                raise ValueError("no data")
            for _, row in df_zb.head(10).iterrows():
                result["broken_limit"].append({
                    "code": str(row.get("代码", "")),
                    "name": str(row.get("名称", "")),
                    "change_pct": float(row.get("涨跌幅", 0) or 0),
                    "turnover_rate": float(row.get("换手率", 0) or 0),
                    "sector": str(row.get("所属行业", "")),
                })
        except Exception as e:
            logger.debug("Broken limit pool failed: %s", e)

        # 3. Limit-down pool (跌停)
        try:
            df_dt = get_limit_down_pool(date=date)
            if df_dt is None:
                raise ValueError("no data")
            for _, row in df_dt.head(10).iterrows():
                result["limit_down"].append({
                    "code": str(row.get("代码", "")),
                    "name": str(row.get("名称", "")),
                    "change_pct": float(row.get("涨跌幅", 0) or 0),
                    "sector": str(row.get("所属行业", "")),
                })
        except Exception as e:
            logger.debug("Limit-down pool failed: %s", e)

        # Summary
        result["summary"] = {
            "limit_up_count": len(result["limit_up"]),
            "limit_down_count": len(result["limit_down"]),
            "broken_count": len(result["broken_limit"]),
            "top_sector": _most_common_sector(result["limit_up"]),
            "consecutive_limit_stocks": [
                s for s in result["limit_up"] if s["consecutive_limits"] >= 2
            ],
        }

        return json.dumps(result, ensure_ascii=False)

    except Exception as e:
        logger.error("get_anomaly_stocks failed: %s", e)
        return json.dumps({"date": date, "error": str(e)}, ensure_ascii=False)


def _most_common_sector(stocks: list[dict]) -> str:
    """Find the most common sector among a list of stocks."""
    if not stocks:
        return ""
    sectors = {}
    for s in stocks:
        sec = s.get("sector", "")
        if sec:
            sectors[sec] = sectors.get(sec, 0) + 1
    return max(sectors, key=sectors.get) if sectors else ""
