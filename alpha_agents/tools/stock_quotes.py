"""Stock quote tool — fetch prices and basic metrics for A-share stocks.

Uses real-time data (akshare spot) during trading hours (09:30-15:00),
falls back to historical data (baostock) outside trading hours.
"""

import json
import logging
from datetime import datetime

from alpha_agents.data.market_data import get_stock_history, get_realtime_quotes

logger = logging.getLogger(__name__)


def _is_sina_available() -> bool:
    """Check if Sina realtime API can return today's data.

    Sina works during trading hours AND after market close (returns closing price).
    Only unavailable on weekends and before 09:25 on trading days.
    """
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    hour_min = now.hour * 100 + now.minute
    return hour_min >= 925  # Available from 09:25 through end of day


def get_stock_quotes_fn(codes: str) -> str:
    """Fetch recent quotes for a list of A-share stocks.

    During trading hours (09:25-15:05), returns real-time prices.
    Outside trading hours, returns latest historical close.

    Args:
        codes: Comma-separated stock codes, e.g. "000858,600519,002594"
    """
    import re
    raw_list = [c.strip() for c in codes.split(",") if c.strip()]
    # Filter: only keep valid 6-digit stock codes, skip names/garbage
    code_list = [c for c in raw_list if re.match(r"^\d{6}$", c)]
    if not code_list:
        return json.dumps({
            "error": f"无有效股票代码。请传入6位数字代码（如000858），不要传股票名称。收到: {raw_list[:5]}",
            "quotes": [],
        }, ensure_ascii=False)

    # Use Sina when available (trading hours + after close on weekdays)
    realtime = None
    if _is_sina_available():
        realtime = get_realtime_quotes(code_list[:10])

    if realtime:
        # Verify Sina returned today's data (not stale holiday data)
        today_str = datetime.now().strftime("%Y-%m-%d")
        sample = next(iter(realtime.values()), {})
        if sample.get("date") and sample["date"] != today_str:
            realtime = None  # Stale data from holiday/weekend

    results = []
    for code in code_list[:10]:
        # Prefer real-time data if available
        if realtime and code in realtime:
            rt = realtime[code]
            # Also get 5-day history for week stats
            history = get_stock_history(code, days=5)
            week_change_pct = 0
            week_high = rt["high"]
            week_low = rt["low"]
            if history and len(history) >= 2:
                first_close = history[0]["close"]
                week_change_pct = round((rt["price"] - first_close) / first_close * 100, 2) if first_close else 0
                week_high = max(week_high, max(d["high"] for d in history))
                week_low = min(week_low, min(d["low"] for d in history))

            results.append({
                "code": code,
                "name": rt.get("name", ""),
                "price": rt["price"],
                "change_pct": rt["change_pct"],
                "week_change_pct": week_change_pct,
                "week_high": week_high,
                "week_low": week_low,
                "week_data_available": bool(history and len(history) >= 2),
                "volume_ratio": rt.get("volume_ratio", 0),
                "turnover_rate": rt.get("turnover_rate", 0),
                "amount_yi": rt.get("amount_yi", 0),
                "prev_close": rt.get("prev_close", 0),
                "realtime": True,
                "date": datetime.now().strftime("%Y-%m-%d"),
            })
            continue

        # Fallback to historical data
        history = get_stock_history(code, days=5)
        if not history:
            results.append({"code": code, "error": "no data"})
            continue

        latest = history[-1]
        prev = history[-2] if len(history) > 1 else latest
        close = latest["close"]
        prev_close = prev["close"]
        change_pct = round((close - prev_close) / prev_close * 100, 2) if prev_close else 0

        first_close = history[0]["close"]
        week_change_pct = round((close - first_close) / first_close * 100, 2) if first_close else 0

        results.append({
            "code": code,
            "price": close,
            "change_pct": change_pct,
            "week_change_pct": week_change_pct,
            "week_high": max(d["high"] for d in history),
            "week_low": min(d["low"] for d in history),
            "week_data_available": len(history) >= 2,
            "volume": latest["volume"],
            "turnover_rate": latest["turnover_rate"],
            "realtime": False,
            "date": latest["date"],
        })

    return json.dumps({"count": len(results), "quotes": results}, ensure_ascii=False)
