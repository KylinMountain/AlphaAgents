"""Financial data tool — fundamental metrics for A-share stocks."""

import json
import logging

from alpha_agents.data.market_data import get_financial_indicator

logger = logging.getLogger(__name__)


def get_financial_data_fn(code: str) -> str:
    """Fetch fundamental financial metrics for an A-share stock.

    Args:
        code: Stock code, e.g. "000858"
    """
    try:
        df = get_financial_indicator(code)

        if df is None or df.empty:
            return json.dumps({"code": code, "error": "no financial data"}, ensure_ascii=False)

        latest = df.iloc[-1]

        def safe_float(val):
            try:
                v = float(val)
                return v if v == v else None
            except (ValueError, TypeError):
                return None

        result = {
            "code": code,
            "report_date": str(latest.get("日期", "")),
            "eps": safe_float(latest.get("摊薄每股收益(元)")),
            "bvps": safe_float(latest.get("每股净资产_调整后(元)")),
            "roe_pct": safe_float(latest.get("净资产收益率(%)")),
            "roa_pct": safe_float(latest.get("总资产利润率(%)")),
            "gross_margin_pct": safe_float(latest.get("销售毛利率(%)")),
            "net_margin_pct": safe_float(latest.get("销售净利率(%)")),
            "debt_to_equity_pct": safe_float(latest.get("负债与所有者权益比率(%)")),
            "current_ratio": safe_float(latest.get("流动比率")),
            "revenue_growth_pct": safe_float(latest.get("主营业务收入增长率(%)")),
            "net_profit_growth_pct": safe_float(latest.get("净利润增长率(%)")),
            "ocf_per_share": safe_float(latest.get("每股经营性现金流(元)")),
            "dividend_payout_pct": safe_float(latest.get("股息发放率(%)")),
            "error": None,
        }

        roe = result["roe_pct"]
        debt = result["debt_to_equity_pct"]
        if roe is not None and roe > 15 and (debt is None or debt < 100):
            result["quality_flag"] = "优质"
        elif roe is not None and roe < 5:
            result["quality_flag"] = "盈利能力弱"
        else:
            result["quality_flag"] = "一般"

        return json.dumps(result, ensure_ascii=False)

    except Exception as e:
        logger.error("get_financial_data failed for %s: %s", code, e)
        return json.dumps({"code": code, "error": str(e)}, ensure_ascii=False)
