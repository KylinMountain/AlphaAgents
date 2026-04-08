"""Stock quote tool — fetch real-time prices and basic metrics for A-share stocks."""

import json
import logging
from datetime import datetime, timedelta

import akshare as ak

from alpha_agents.config import no_proxy

logger = logging.getLogger(__name__)


def get_stock_quotes_fn(codes: str) -> str:
    """Fetch real-time quotes for a list of A-share stocks.

    Args:
        codes: Comma-separated stock codes, e.g. "000858,600519,002594"
    """
    code_list = [c.strip() for c in codes.split(",") if c.strip()]
    if not code_list:
        return json.dumps({"error": "no stock codes provided", "quotes": []}, ensure_ascii=False)

    results = []
    for code in code_list[:10]:  # limit to 10 stocks
        try:
            with no_proxy():
                info_df = ak.stock_individual_info_em(symbol=code)

            info = {}
            for _, row in info_df.iterrows():
                info[row["item"]] = row["value"]

            # Get recent 5-day history for trend
            end = datetime.now().strftime("%Y%m%d")
            start = (datetime.now() - timedelta(days=14)).strftime("%Y%m%d")
            try:
                with no_proxy():
                    hist_df = ak.stock_zh_a_hist(
                        symbol=code, period="daily",
                        start_date=start, end_date=end, adjust="qfq",
                    )
                if not hist_df.empty:
                    hist_df = hist_df.tail(5)
                    latest = hist_df.iloc[-1]
                    prev = hist_df.iloc[-2] if len(hist_df) > 1 else latest
                    change_pct = round(
                        (float(latest["收盘"]) - float(prev["收盘"])) / float(prev["收盘"]) * 100, 2
                    ) if float(prev["收盘"]) != 0 else 0
                    week_high = float(hist_df["最高"].max())
                    week_low = float(hist_df["最低"].min())
                else:
                    change_pct = 0
                    week_high = week_low = 0
            except Exception:
                change_pct = 0
                week_high = week_low = 0

            market_cap = info.get("总市值", 0)
            float_cap = info.get("流通市值", 0)

            results.append({
                "code": code,
                "name": info.get("股票简称", "").replace(" ", ""),
                "price": float(info.get("最新", 0)),
                "change_pct": change_pct,
                "market_cap_yi": round(float(market_cap) / 1e8, 1) if market_cap else None,
                "float_cap_yi": round(float(float_cap) / 1e8, 1) if float_cap else None,
                "industry": info.get("行业", ""),
                "pe_ttm": None,
                "week_high": week_high,
                "week_low": week_low,
            })
        except Exception as e:
            logger.debug("Failed to fetch quote for %s: %s", code, e)
            results.append({"code": code, "error": str(e)})

    return json.dumps({"count": len(results), "quotes": results}, ensure_ascii=False)
