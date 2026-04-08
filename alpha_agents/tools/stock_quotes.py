"""Stock quote tool — fetch prices and basic metrics for A-share stocks."""

import json
import logging

from alpha_agents.data.market_data import get_stock_history

logger = logging.getLogger(__name__)


def get_stock_quotes_fn(codes: str) -> str:
    """Fetch recent quotes for a list of A-share stocks.

    Args:
        codes: Comma-separated stock codes, e.g. "000858,600519,002594"
    """
    code_list = [c.strip() for c in codes.split(",") if c.strip()]
    if not code_list:
        return json.dumps({"error": "no stock codes provided", "quotes": []}, ensure_ascii=False)

    results = []
    for code in code_list[:10]:
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
            "volume": latest["volume"],
            "turnover_rate": latest["turnover_rate"],
            "date": latest["date"],
        })

    return json.dumps({"count": len(results), "quotes": results}, ensure_ascii=False)
