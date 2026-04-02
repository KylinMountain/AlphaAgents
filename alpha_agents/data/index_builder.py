import logging
from pathlib import Path

import baostock as bs
import pandas as pd

from alpha_agents.config import no_proxy
from alpha_agents.data.db import get_connection, init_db

logger = logging.getLogger(__name__)


def _fetch_stock_info_baostock() -> pd.DataFrame:
    """Fetch all A-share stock basic info via baostock (TCP, no HTTP proxy issues)."""
    lg = bs.login()
    if lg.error_code != "0":
        raise RuntimeError(f"baostock login failed: {lg.error_msg}")

    try:
        rs = bs.query_stock_basic(code_name="", code="")
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
        df = pd.DataFrame(rows, columns=rs.fields)
        # Filter to A-shares that are currently listed (status=1)
        df = df[df["type"] == "1"]  # type 1 = stock
        df = df[df["status"] == "1"]  # status 1 = listed
        return df
    finally:
        bs.logout()


def _fetch_concept_names_akshare() -> pd.DataFrame:
    """Fetch THS concept board names via akshare."""
    import akshare as ak
    with no_proxy():
        return ak.stock_board_concept_name_ths()


def _fetch_concept_constituents_akshare(symbol: str) -> pd.DataFrame:
    """Fetch constituents of a concept board via akshare (eastmoney)."""
    import akshare as ak
    try:
        with no_proxy():
            df = ak.stock_board_concept_cons_em(symbol=symbol)
        return df
    except Exception:
        logger.warning("Failed to fetch constituents for concept: %s", symbol)
        return pd.DataFrame({"代码": [], "名称": []})


# Module-level aliases for easy mocking in tests
_fetch_stock_info = _fetch_stock_info_baostock
_fetch_concept_names = _fetch_concept_names_akshare
_fetch_concept_constituents = _fetch_concept_constituents_akshare


def build_index(db_path: Path) -> None:
    init_db(db_path)
    conn = get_connection(db_path)

    try:
        # Clear existing data for idempotent rebuild
        conn.execute("DELETE FROM concept_stocks")
        conn.execute("DELETE FROM concepts")
        conn.execute("DELETE FROM stocks")

        # 1. Fetch and insert stock info via baostock
        logger.info("Fetching stock info via baostock...")
        stock_info = _fetch_stock_info()
        for _, row in stock_info.iterrows():
            # baostock code format: sh.600519 / sz.000001
            raw_code = str(row.get("code", ""))
            code = raw_code.split(".")[-1] if "." in raw_code else raw_code
            name = str(row.get("code_name", ""))
            is_st = 1 if "ST" in name or "st" in name else 0
            conn.execute(
                "INSERT OR REPLACE INTO stocks (code, name, market_cap, industry, is_st, is_suspended) "
                "VALUES (?, ?, NULL, NULL, ?, 0)",
                (code, name, is_st),
            )

        # 2. Fetch concept names via akshare
        logger.info("Fetching concept names via akshare...")
        concept_names_df = _fetch_concept_names()

        # Column name varies by akshare version: "概念名称" or "name"
        name_col = "name" if "name" in concept_names_df.columns else "概念名称"
        for _, row in concept_names_df.iterrows():
            concept_name = str(row[name_col])
            conn.execute(
                "INSERT OR REPLACE INTO concepts (name, source) VALUES (?, 'ths')",
                (concept_name,),
            )
            concept_id = conn.execute(
                "SELECT id FROM concepts WHERE name = ?", (concept_name,)
            ).fetchone()["id"]

            # 3. Fetch constituents for each concept
            logger.info("Fetching constituents for: %s", concept_name)
            cons_df = _fetch_concept_constituents(concept_name)
            code_col = "代码" if "代码" in cons_df.columns else "code"
            for _, stock_row in cons_df.iterrows():
                stock_code = str(stock_row[code_col])
                conn.execute(
                    "INSERT OR IGNORE INTO concept_stocks (concept_id, stock_code) VALUES (?, ?)",
                    (concept_id, stock_code),
                )

        conn.commit()
        logger.info("Index build complete.")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
