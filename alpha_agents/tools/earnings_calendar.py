"""Earnings calendar tool — upcoming earnings forecasts and disclosure dates."""

import json
import logging

from alpha_agents.data.market_data import get_earnings_forecast

logger = logging.getLogger(__name__)


def get_earnings_calendar_fn(codes: str = "") -> str:
    """Check earnings forecast (业绩预告) for specific stocks.

    Args:
        codes: Comma-separated stock codes to check, e.g. "000858,600519".
               If empty, returns recent notable earnings forecasts (预增/预减/首亏).
    """
    try:
        from datetime import datetime
        now = datetime.now()
        if now.month <= 4:
            date = f"{now.year - 1}1231"
        elif now.month <= 8:
            date = f"{now.year}0630"
        elif now.month <= 10:
            date = f"{now.year}0930"
        else:
            date = f"{now.year}0930"

        df = get_earnings_forecast(date=date)

        if df is None or df.empty:
            return json.dumps({"error": "no earnings data available", "forecasts": []}, ensure_ascii=False)

        code_list = [c.strip() for c in codes.split(",") if c.strip()] if codes else []

        if code_list:
            mask = df["股票代码"].isin(code_list)
            filtered = df[mask]
            if filtered.empty:
                return json.dumps({
                    "codes": code_list,
                    "forecasts": [],
                    "note": "这些股票暂无业绩预告数据",
                }, ensure_ascii=False)
            df = filtered
        else:
            notable = df[df["预告类型"].isin(["首亏", "预减", "大幅预减", "预增", "大幅预增"])]
            df = notable.head(20)

        results = []
        seen = set()
        for _, row in df.iterrows():
            code = str(row["股票代码"])
            if code in seen:
                continue
            seen.add(code)

            forecast_type = str(row.get("预告类型", ""))
            change_pct = row.get("业绩变动幅度")

            if forecast_type in ("首亏", "预减", "大幅预减"):
                risk = "high"
            elif forecast_type in ("预增", "大幅预增"):
                risk = "low"
            else:
                risk = "medium"

            results.append({
                "code": code,
                "name": str(row.get("股票简称", "")),
                "forecast_type": forecast_type,
                "change_pct": float(change_pct) if change_pct == change_pct and change_pct is not None else None,
                "disclosure_date": str(row.get("公告日期", "")),
                "earnings_risk": risk,
            })

        return json.dumps({
            "report_period": date,
            "count": len(results),
            "forecasts": results,
        }, ensure_ascii=False)

    except Exception as e:
        logger.error("get_earnings_calendar failed: %s", e)
        return json.dumps({"error": str(e), "forecasts": []}, ensure_ascii=False)
