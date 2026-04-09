"""Market snapshot tool — detect volume ratio and turnover rate anomalies.

Fetches real-time A-share spot data and filters for stocks with unusually
high volume ratio (量比) or turnover rate (换手率), which often signal
institutional activity or breaking news.
"""

import json
import logging

from alpha_agents.data.market_data import _ak_call

logger = logging.getLogger(__name__)


def get_market_snapshot_fn(min_volume_ratio: float = 3.0, min_turnover: float = 15.0) -> str:
    """获取量比/换手率异常个股

    Filters real-time A-share spot data for stocks with:
    - 量比 > min_volume_ratio (default 3.0)
    - 换手率 > min_turnover% (default 15.0%)

    Returns top 20 unusual stocks sorted by 量比 descending.

    Args:
        min_volume_ratio: Minimum volume ratio threshold (default 3.0).
        min_turnover: Minimum turnover rate percentage threshold (default 15.0).
    """
    try:
        import akshare as ak
        df = _ak_call(ak.stock_zh_a_spot_em)
        if df is None:
            return json.dumps({"error": "无法获取实时行情数据", "stocks": []}, ensure_ascii=False)

        # Normalize column names — akshare may return slightly different names
        col_map = {}
        for col in df.columns:
            if "量比" in col:
                col_map["volume_ratio"] = col
            elif "换手率" in col:
                col_map["turnover"] = col
            elif col == "代码":
                col_map["code"] = col
            elif col == "名称":
                col_map["name"] = col
            elif "最新价" in col:
                col_map["price"] = col
            elif "涨跌幅" in col:
                col_map["change_pct"] = col
            elif "成交额" in col:
                col_map["amount"] = col
            elif "总市值" in col:
                col_map["market_cap"] = col

        vr_col = col_map.get("volume_ratio")
        tr_col = col_map.get("turnover")

        if not vr_col or not tr_col:
            return json.dumps({"error": "数据列缺失（量比/换手率）", "stocks": []}, ensure_ascii=False)

        # Convert to numeric, coerce errors to NaN
        import pandas as pd
        df[vr_col] = pd.to_numeric(df[vr_col], errors="coerce")
        df[tr_col] = pd.to_numeric(df[tr_col], errors="coerce")

        # Filter: volume ratio > threshold OR turnover > threshold
        mask = (df[vr_col] > min_volume_ratio) | (df[tr_col] > min_turnover)
        filtered = df[mask].copy()

        if filtered.empty:
            return json.dumps({
                "count": 0,
                "stocks": [],
                "filters": {"min_volume_ratio": min_volume_ratio, "min_turnover": min_turnover},
            }, ensure_ascii=False)

        # Sort by volume ratio descending, take top 20
        filtered = filtered.sort_values(vr_col, ascending=False).head(20)

        stocks = []
        for _, row in filtered.iterrows():
            code = str(row.get(col_map.get("code", "代码"), ""))
            name = str(row.get(col_map.get("name", "名称"), ""))

            # Skip ST / *ST stocks
            if "ST" in name.upper():
                continue

            stocks.append({
                "code": code,
                "name": name,
                "price": float(row.get(col_map.get("price", ""), 0) or 0),
                "change_pct": round(float(row.get(col_map.get("change_pct", ""), 0) or 0), 2),
                "volume_ratio": round(float(row.get(vr_col, 0) or 0), 2),
                "turnover_rate": round(float(row.get(tr_col, 0) or 0), 2),
                "amount_yi": round(float(row.get(col_map.get("amount", ""), 0) or 0) / 1e8, 2),
                "market_cap_yi": round(float(row.get(col_map.get("market_cap", ""), 0) or 0) / 1e8, 1),
            })

        return json.dumps({
            "count": len(stocks),
            "stocks": stocks,
            "filters": {"min_volume_ratio": min_volume_ratio, "min_turnover": min_turnover},
        }, ensure_ascii=False)

    except Exception as e:
        logger.error("get_market_snapshot failed: %s", e)
        return json.dumps({"error": str(e), "stocks": []}, ensure_ascii=False)
