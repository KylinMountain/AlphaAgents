import logging
from pathlib import Path

import akshare as ak
import pandas as pd

from alpha_agents.data.db import get_connection, init_db

logger = logging.getLogger(__name__)


def _fetch_concept_names() -> pd.DataFrame:
    return ak.stock_board_concept_name_ths()


def _fetch_concept_constituents(symbol: str) -> pd.DataFrame:
    try:
        return ak.stock_board_concept_cons_ths(symbol=symbol)
    except Exception:
        logger.warning("Failed to fetch constituents for concept: %s", symbol)
        return pd.DataFrame({"代码": [], "名称": []})


def _fetch_stock_info() -> pd.DataFrame:
    return ak.stock_zh_a_spot_em()


def build_index(db_path: Path) -> None:
    init_db(db_path)
    conn = get_connection(db_path)

    try:
        # Clear existing data for idempotent rebuild
        conn.execute("DELETE FROM concept_stocks")
        conn.execute("DELETE FROM concepts")
        conn.execute("DELETE FROM stocks")

        # 1. Fetch and insert stock info
        logger.info("Fetching stock info...")
        stock_info = _fetch_stock_info()
        for _, row in stock_info.iterrows():
            code = str(row.get("代码", ""))
            name = str(row.get("名称", ""))
            market_cap = float(row["总市值"]) if pd.notna(row.get("总市值")) else None
            industry = str(row.get("行业", "")) if pd.notna(row.get("行业")) else None
            is_st = 1 if "ST" in name or "st" in name else 0
            conn.execute(
                "INSERT OR REPLACE INTO stocks (code, name, market_cap, industry, is_st, is_suspended) "
                "VALUES (?, ?, ?, ?, ?, 0)",
                (code, name, market_cap, industry, is_st),
            )

        # 2. Fetch concept names
        logger.info("Fetching concept names...")
        concept_names_df = _fetch_concept_names()

        for _, row in concept_names_df.iterrows():
            concept_name = str(row["概念名称"])
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
            for _, stock_row in cons_df.iterrows():
                stock_code = str(stock_row["代码"])
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
