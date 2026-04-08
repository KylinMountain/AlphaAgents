"""Build stock concept index from baostock (stock info) + eastmoney (concept boards).

Run once via `python main.py build-index`. Data is cached in SQLite.
Subsequent runs skip if index already exists (use --force to rebuild).
"""

import json
import logging
import threading
import time
from pathlib import Path

import baostock as bs
import pandas as pd

from alpha_agents.config import no_proxy, DATA_DIR
from alpha_agents.data.db import get_connection, init_db

logger = logging.getLogger(__name__)

# Cache file for concept constituents (avoid re-fetching on retry)
_CONCEPT_CACHE_PATH = DATA_DIR / "concept_cache.json"


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


def _fetch_concept_names_em() -> pd.DataFrame:
    """Fetch eastmoney concept board names.

    Uses stock_fund_flow_concept (data.eastmoney.com, reliable) to get
    the concept list with fund flow data. Falls back to
    stock_board_concept_name_em (push2.eastmoney.com) if available.
    """
    import akshare as ak
    with no_proxy():
        # Primary: fund flow concept list (always works, 387 concepts)
        df = ak.stock_fund_flow_concept()

    name_col = "行业" if "行业" in df.columns else "名称"
    result = pd.DataFrame({
        "concept_name": df[name_col],
    })
    return result


def _fetch_concept_constituents_em(concept_name: str, max_retries: int = 3) -> list[dict]:
    """Fetch constituents for a single EM concept board.

    Uses stock_board_concept_cons_em (push2.eastmoney.com).
    Retries on failure. Returns empty list if all retries fail.
    """
    import akshare as ak

    for attempt in range(max_retries):
        try:
            with no_proxy():
                df = ak.stock_board_concept_cons_em(symbol=concept_name)
            if df is None or df.empty:
                return []

            stocks = []
            for _, row in df.iterrows():
                code = str(row.get("代码", ""))
                name = str(row.get("名称", ""))
                if code and name:
                    stocks.append({"code": code, "name": name})
            return stocks

        except Exception as e:
            if attempt < max_retries - 1:
                delay = 2 ** attempt + 1
                logger.debug("Retry %d for concept '%s': %s (wait %ds)",
                             attempt + 1, concept_name, type(e).__name__, delay)
                time.sleep(delay)
            else:
                logger.debug("Failed to fetch constituents for '%s' after %d retries: %s",
                             concept_name, max_retries, e)
                return []


def _load_concept_cache() -> dict:
    """Load cached concept constituents from disk."""
    if _CONCEPT_CACHE_PATH.exists():
        try:
            with open(_CONCEPT_CACHE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_concept_cache(cache: dict) -> None:
    """Save concept constituents cache to disk."""
    _CONCEPT_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_CONCEPT_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)


# Module-level aliases for easy mocking in tests
_fetch_stock_info = _fetch_stock_info_baostock
_fetch_concept_names = _fetch_concept_names_em
_fetch_concept_constituents = _fetch_concept_constituents_em


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

        # 3. Eastmoney concept names + constituents
        logger.info("Fetching concept names via eastmoney...")
        concept_names_df = _fetch_concept_names()
        total = len(concept_names_df)
        logger.info("Found %d concept boards", total)

        # Load cache to avoid re-fetching already successful concepts
        cache = _load_concept_cache()
        concept_success = 0
        concept_fail = 0

        for i, (_, row) in enumerate(concept_names_df.iterrows()):
            concept_name = str(row["concept_name"])

            conn.execute(
                "INSERT OR REPLACE INTO concepts (name, source) VALUES (?, 'em')",
                (concept_name,),
            )
            concept_id = conn.execute(
                "SELECT id FROM concepts WHERE name = ?", (concept_name,)
            ).fetchone()["id"]

            # Check cache first
            if concept_name in cache:
                stocks = cache[concept_name]
            else:
                stocks = _fetch_concept_constituents(concept_name)
                if stocks:
                    cache[concept_name] = stocks

            if not stocks:
                concept_fail += 1
            else:
                concept_success += 1
                for stock in stocks:
                    exists = conn.execute(
                        "SELECT 1 FROM stocks WHERE code = ?", (stock["code"],)
                    ).fetchone()
                    if exists:
                        conn.execute(
                            "INSERT OR IGNORE INTO concept_stocks (concept_id, stock_code) VALUES (?, ?)",
                            (concept_id, stock["code"]),
                        )

            if (i + 1) % 20 == 0:
                logger.info("Progress: %d/%d concepts (%d success, %d fail)",
                            i + 1, total, concept_success, concept_fail)
                conn.commit()
                _save_concept_cache(cache)

            time.sleep(0.5)  # Rate limit

        conn.commit()
        _save_concept_cache(cache)

        logger.info("Index build complete.")
        logger.info(
            "Stocks: %d, Concepts: %d (success: %d, fail: %d)",
            len(stock_info), total, concept_success, concept_fail,
        )

        # 4. Build concept embeddings for semantic search
        logger.info("Building concept embeddings...")
        try:
            from alpha_agents.data.embeddings import build_concept_embeddings
            n = build_concept_embeddings(conn)
            logger.info("Embedded %d concepts for semantic search", n)
        except Exception as e:
            logger.warning("Failed to build embeddings (non-fatal): %s", e)

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
