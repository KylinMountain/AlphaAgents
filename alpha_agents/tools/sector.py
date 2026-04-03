import json
import logging

import akshare as ak
import pandas as pd

from alpha_agents.config import no_proxy

logger = logging.getLogger(__name__)


def _fetch_sector_fund_flow() -> pd.DataFrame:
    with no_proxy():
        return ak.stock_fund_flow_concept()


def get_sector_data_fn(sector_name: str) -> str:
    try:
        df = _fetch_sector_fund_flow()
        # Column names vary by akshare version
        name_col = "行业" if "行业" in df.columns else "名称"
        row = df[df[name_col] == sector_name]
        if row.empty:
            # Try fuzzy match
            row = df[df[name_col].str.contains(sector_name, na=False)]
        if row.empty:
            return json.dumps({"sector_name": sector_name, "error": f"板块 '{sector_name}' 未找到"}, ensure_ascii=False)

        r = row.iloc[0]
        change_col = "行业-涨跌幅" if "行业-涨跌幅" in df.columns else "今日涨跌幅"
        inflow_col = "净额" if "净额" in df.columns else "今日主力净流入-净额"
        return json.dumps({
            "sector_name": sector_name,
            "change_pct": float(r.get(change_col, 0)),
            "main_net_inflow": float(r.get(inflow_col, 0)),
            "error": None,
        }, ensure_ascii=False)
    except Exception as e:
        logger.error("Failed to fetch sector data: %s", e)
        return json.dumps({"sector_name": sector_name, "error": str(e)}, ensure_ascii=False)
