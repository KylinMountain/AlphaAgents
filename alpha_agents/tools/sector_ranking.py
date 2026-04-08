"""Sector ranking tool — industry and concept fund flow rankings.

Two ranking dimensions:
- Industry (行业): 90 sectors — broad classification (半导体, 银行, 白酒...)
- Concept (概念): 387 themes — hot topics (华为昇腾, AI算力, 低空经济...)

Both from 同花顺 via data.10jqka.com.cn (reliable, no push2 issues).
"""

import json
import logging

from alpha_agents.data.market_data import get_industry_fund_flow, get_concept_fund_flow

logger = logging.getLogger(__name__)


def get_sector_ranking_fn(top_n: int = 20) -> str:
    """Get industry sector ranking by fund flow — shows sector rotation direction.

    Returns top gaining and losing industries by net fund flow.
    Use this to detect which sectors money is flowing INTO and OUT OF.

    Args:
        top_n: Number of top/bottom sectors to return. Default 20.
    """
    try:
        df = get_industry_fund_flow()

        if df is None or df.empty:
            return json.dumps({"error": "no sector data", "gainers": [], "losers": []}, ensure_ascii=False)

        gainers = []
        losers = []
        for _, row in df.iterrows():
            name = str(row.get("行业", ""))
            change_pct = float(row.get("行业-涨跌幅", 0) or 0)
            net_flow = float(row.get("净额", 0) or 0)
            leader = str(row.get("领涨股", ""))
            leader_change = float(row.get("领涨股-涨跌幅", 0) or 0)

            entry = {
                "sector": name,
                "change_pct": change_pct,
                "net_flow_yi": round(net_flow, 2),
                "leader": leader,
                "leader_change_pct": leader_change,
                "company_count": int(row.get("公司家数", 0) or 0),
            }

            if net_flow > 0:
                gainers.append(entry)
            else:
                losers.append(entry)

        gainers.sort(key=lambda x: x["net_flow_yi"], reverse=True)
        losers.sort(key=lambda x: x["net_flow_yi"])

        return json.dumps({
            "total_sectors": len(gainers) + len(losers),
            "inflow_sectors": len(gainers),
            "outflow_sectors": len(losers),
            "gainers": gainers[:top_n],
            "losers": losers[:top_n],
            "rotation_signal": "资金集中流入少数行业" if len(gainers) < len(losers) else "普涨格局",
        }, ensure_ascii=False)

    except Exception as e:
        logger.error("get_sector_ranking failed: %s", e)
        return json.dumps({"error": str(e), "gainers": [], "losers": []}, ensure_ascii=False)


def get_concept_ranking_fn(top_n: int = 20) -> str:
    """Get concept board ranking by fund flow — shows hot theme rotation.

    Returns top gaining and losing concept themes by net fund flow.
    Concept names match the DB concept table (同花顺 concepts).
    Use this to discover new investment themes and track existing ones.

    Args:
        top_n: Number of top/bottom concepts to return. Default 20.
    """
    try:
        df = get_concept_fund_flow()

        if df is None or df.empty:
            return json.dumps({"error": "no concept data", "gainers": [], "losers": []}, ensure_ascii=False)

        name_col = "行业" if "行业" in df.columns else "名称"

        gainers = []
        losers = []
        for _, row in df.iterrows():
            name = str(row.get(name_col, ""))
            change_pct = float(row.get("行业-涨跌幅", 0) or 0)
            net_flow = float(row.get("净额", 0) or 0)
            leader = str(row.get("领涨股", ""))
            leader_change = float(row.get("领涨股-涨跌幅", 0) or 0)

            entry = {
                "concept": name,
                "change_pct": change_pct,
                "net_flow_yi": round(net_flow, 2),
                "leader": leader,
                "leader_change_pct": leader_change,
                "company_count": int(row.get("公司家数", 0) or 0),
            }

            if net_flow > 0:
                gainers.append(entry)
            else:
                losers.append(entry)

        gainers.sort(key=lambda x: x["net_flow_yi"], reverse=True)
        losers.sort(key=lambda x: x["net_flow_yi"])

        return json.dumps({
            "total_concepts": len(gainers) + len(losers),
            "inflow_concepts": len(gainers),
            "outflow_concepts": len(losers),
            "gainers": gainers[:top_n],
            "losers": losers[:top_n],
        }, ensure_ascii=False)

    except Exception as e:
        logger.error("get_concept_ranking failed: %s", e)
        return json.dumps({"error": str(e), "gainers": [], "losers": []}, ensure_ascii=False)
