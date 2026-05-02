import json
import logging
import threading

import pandas as pd

from alpha_agents.data.market_data import get_concept_fund_flow, get_industry_fund_flow

logger = logging.getLogger(__name__)

# py_mini_racer (used internally by akshare THS functions) is not thread-safe
_ths_lock = threading.Lock()


def _fetch_sector_fund_flow() -> pd.DataFrame:
    """Backward-compatible concept fund-flow fetcher used by tests and callers."""
    return get_concept_fund_flow()


def _lookup_in_df(df: pd.DataFrame, query: str, name_col: str) -> pd.DataFrame:
    """Try exact → fuzzy contains → reverse contains → 2-char keyword split."""
    row = df[df[name_col] == query]
    if not row.empty:
        return row
    row = df[df[name_col].str.contains(query, na=False)]
    if not row.empty:
        return row
    row = df[df[name_col].apply(
        lambda x: x in query if isinstance(x, str) and len(x) >= 2 else False
    )]
    if not row.empty:
        return row
    for i in range(0, len(query) - 1, 2):
        sub = query[i:i + 2]
        row = df[df[name_col].str.contains(sub, na=False)]
        if not row.empty:
            return row
    return pd.DataFrame()


def _row_to_payload(row: pd.Series, df_cols: list, classification: str) -> dict:
    """Extract change_pct / net_inflow regardless of akshare column naming quirks."""
    change_col = "行业-涨跌幅" if "行业-涨跌幅" in df_cols else "今日涨跌幅"
    inflow_col = "净额" if "净额" in df_cols else "今日主力净流入-净额"
    return {
        "change_pct": float(row.get(change_col, 0) or 0),
        "main_net_inflow": float(row.get(inflow_col, 0) or 0),
        "classification": classification,  # 'concept' or 'industry'
    }


def get_sector_data_fn(sector_name: str) -> str:
    """Get fund flow data for a sector name. Automatically checks both concept
    and industry tables, since A-share has two parallel classifications:

    - **concept**（概念）: dynamic themes (CPO / 算力 / 固态电池 / 华为概念) — 491
    - **industry**（行业）: static Shenwan classification (半导体 / 乘用车 / 银行) — 492

    LLM can pass either type of name; we try concept first (primary signal
    channel), then industry (style context), and return whichever matches.
    ``classification`` in the response says which table the match came from.
    """
    try:
        with _ths_lock:
            df_c = _fetch_sector_fund_flow()

        # Concept table first (primary)
        if df_c is not None and not df_c.empty:
            name_col = "行业" if "行业" in df_c.columns else ("名称" if "名称" in df_c.columns else None)
            if name_col:
                row = _lookup_in_df(df_c, sector_name, name_col)
                if not row.empty:
                    payload = _row_to_payload(row.iloc[0], list(df_c.columns), "concept")
                    payload.update({"sector_name": sector_name,
                                    "matched_name": str(row.iloc[0][name_col]),
                                    "error": None})
                    return json.dumps(payload, ensure_ascii=False)

        # Industry fallback
        with _ths_lock:
            df_i = get_industry_fund_flow()
        if df_i is not None and not df_i.empty:
            name_col = "行业" if "行业" in df_i.columns else ("名称" if "名称" in df_i.columns else None)
            if name_col:
                row = _lookup_in_df(df_i, sector_name, name_col)
                if not row.empty:
                    payload = _row_to_payload(row.iloc[0], list(df_i.columns), "industry")
                    payload.update({"sector_name": sector_name,
                                    "matched_name": str(row.iloc[0][name_col]),
                                    "error": None})
                    return json.dumps(payload, ensure_ascii=False)

        return json.dumps({
            "sector_name": sector_name,
            "error": f"'{sector_name}' 在概念和行业两表中都未找到",
        }, ensure_ascii=False)
    except Exception as e:
        logger.error("Failed to fetch sector data: %s", e)
        return json.dumps({"sector_name": sector_name, "error": str(e)}, ensure_ascii=False)
