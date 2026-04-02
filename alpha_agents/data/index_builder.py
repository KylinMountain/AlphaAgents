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
        df = df[df["type"] == "1"]  # type 1 = stock
        df = df[df["status"] == "1"]  # status 1 = listed
        return df
    finally:
        bs.logout()


def _fetch_industry_baostock() -> pd.DataFrame:
    """Fetch industry classification via baostock (TCP)."""
    lg = bs.login()
    if lg.error_code != "0":
        raise RuntimeError(f"baostock login failed: {lg.error_msg}")

    try:
        rs = bs.query_stock_industry()
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
        return pd.DataFrame(rows, columns=rs.fields)
    finally:
        bs.logout()


def _fetch_concept_names_akshare() -> pd.DataFrame:
    """Fetch THS concept board names via akshare."""
    import akshare as ak
    with no_proxy():
        return ak.stock_board_concept_name_ths()


def _fetch_concept_constituents_akshare(symbol: str) -> pd.DataFrame:
    """Fetch constituents of a concept board via akshare (eastmoney).

    NOTE: This requires eastmoney.com to be accessible (may need Clash DIRECT rule).
    If it fails, concept names are still indexed but without stock mappings.
    """
    import akshare as ak
    try:
        with no_proxy():
            df = ak.stock_board_concept_cons_em(symbol=symbol)
        return df
    except Exception:
        return pd.DataFrame()


# Module-level aliases for easy mocking in tests
_fetch_stock_info = _fetch_stock_info_baostock
_fetch_concept_names = _fetch_concept_names_akshare
_fetch_concept_constituents = _fetch_concept_constituents_akshare


def build_index(db_path: Path) -> None:
    init_db(db_path)
    conn = get_connection(db_path)

    try:
        conn.execute("DELETE FROM concept_stocks")
        conn.execute("DELETE FROM concepts")
        conn.execute("DELETE FROM stocks")

        # 1. Stock basic info via baostock
        logger.info("Fetching stock info via baostock...")
        stock_info = _fetch_stock_info()
        for _, row in stock_info.iterrows():
            raw_code = str(row.get("code", ""))
            code = raw_code.split(".")[-1] if "." in raw_code else raw_code
            name = str(row.get("code_name", ""))
            is_st = 1 if "ST" in name or "st" in name else 0
            conn.execute(
                "INSERT OR REPLACE INTO stocks (code, name, market_cap, industry, is_st, is_suspended) "
                "VALUES (?, ?, NULL, NULL, ?, 0)",
                (code, name, is_st),
            )

        # 2. Industry classification via baostock (always works, TCP)
        logger.info("Fetching industry classification via baostock...")
        industry_df = _fetch_industry_baostock()
        for _, row in industry_df.iterrows():
            raw_code = str(row.get("code", ""))
            code = raw_code.split(".")[-1] if "." in raw_code else raw_code
            industry = str(row.get("industry", ""))
            if industry:
                conn.execute(
                    "UPDATE stocks SET industry = ? WHERE code = ?",
                    (industry, code),
                )

        # 3. THS concept names (works via 10jqka.com)
        logger.info("Fetching concept names via akshare (THS)...")
        concept_names_df = _fetch_concept_names()
        name_col = "name" if "name" in concept_names_df.columns else "概念名称"

        concept_success = 0
        concept_fail = 0
        for _, row in concept_names_df.iterrows():
            concept_name = str(row[name_col])
            conn.execute(
                "INSERT OR REPLACE INTO concepts (name, source) VALUES (?, 'ths')",
                (concept_name,),
            )
            concept_id = conn.execute(
                "SELECT id FROM concepts WHERE name = ?", (concept_name,)
            ).fetchone()["id"]

            # 4. Concept constituents via eastmoney (may fail if proxy blocks it)
            cons_df = _fetch_concept_constituents(concept_name)
            if cons_df.empty:
                concept_fail += 1
                continue

            concept_success += 1
            code_col = "代码" if "代码" in cons_df.columns else "code"
            for _, stock_row in cons_df.iterrows():
                stock_code = str(stock_row[code_col])
                conn.execute(
                    "INSERT OR IGNORE INTO concept_stocks (concept_id, stock_code) VALUES (?, ?)",
                    (concept_id, stock_code),
                )

        conn.commit()

        total = concept_success + concept_fail
        logger.info("Index build complete.")
        logger.info(
            "Stocks: %d, Concepts: %d, Constituents mapped: %d/%d",
            len(stock_info), total, concept_success, total,
        )
        if concept_fail > 0 and concept_success == 0:
            logger.warning(
                "No concept constituents were fetched. "
                "If you're behind a proxy, add DIRECT rule for eastmoney.com in Clash, "
                "then re-run build-index."
            )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
