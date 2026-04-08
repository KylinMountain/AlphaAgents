"""Sector ranking tool — industry-level fund flow ranking.

Complements the existing concept-level sector tool (sector.py) with
industry-level (行业) data. Shows which industries are strongest/weakest
by fund flow, helping detect sector rotation.
"""

import json
import logging

import akshare as ak

from alpha_agents.config import no_proxy

logger = logging.getLogger(__name__)


def get_sector_ranking_fn(top_n: int = 20) -> str:
    """Get industry sector ranking by fund flow — shows sector rotation direction.

    Returns top gaining and losing industries by net fund flow.
    Use this to detect which sectors money is flowing INTO and OUT OF.

    Args:
        top_n: Number of top/bottom sectors to return. Default 20.
    """
    try:
        with no_proxy():
            df = ak.stock_fund_flow_industry()

        if df.empty:
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
