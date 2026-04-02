import json
import logging

import akshare as ak
import pandas as pd

from alpha_agents.config import no_proxy

logger = logging.getLogger(__name__)


def _fetch_sector_fund_flow() -> pd.DataFrame:
    with no_proxy():
        return ak.stock_board_concept_fund_flow_ths()


def get_sector_data_fn(sector_name: str) -> str:
    try:
        df = _fetch_sector_fund_flow()
        row = df[df["名称"] == sector_name]
        if row.empty:
            return json.dumps({"sector_name": sector_name, "error": f"板块 '{sector_name}' 未找到"}, ensure_ascii=False)

        r = row.iloc[0]
        return json.dumps({
            "sector_name": sector_name,
            "change_pct": float(r.get("今日涨跌幅", 0)),
            "main_net_inflow": float(r.get("今日主力净流入-净额", 0)),
            "main_net_inflow_pct": float(r.get("今日主力净流入-净占比", 0)),
            "error": None,
        }, ensure_ascii=False)
    except Exception as e:
        logger.error("Failed to fetch sector data: %s", e)
        return json.dumps({"sector_name": sector_name, "error": str(e)}, ensure_ascii=False)
