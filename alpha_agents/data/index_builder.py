import logging
import threading
import time
from pathlib import Path

import baostock as bs
import pandas as pd
import py_mini_racer
from bs4 import BeautifulSoup

from akshare.datasets import get_ths_js

from alpha_agents.config import no_proxy
from alpha_agents.http_client import fetch as http_fetch, get_headers
from alpha_agents.data.db import get_connection, init_db

logger = logging.getLogger(__name__)

# py_mini_racer is not thread-safe — serialize all THS auth calls
_ths_lock = threading.Lock()


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


def _get_ths_headers() -> dict:
    """Generate THS auth headers with v cookie (same method akshare uses).

    Thread-locked because py_mini_racer crashes on concurrent access.
    """
    with _ths_lock:
        js_code = py_mini_racer.MiniRacer()
        with open(get_ths_js("ths.js"), encoding="utf-8") as f:
            js_content = f.read()
        js_code.eval(js_content)
        v_code = js_code.call("v")
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Cookie": f"v={v_code}",
        "Referer": "https://q.10jqka.com.cn/gn/",
    }


def _fetch_concept_names_ths() -> pd.DataFrame:
    """Fetch THS concept board names via akshare (10jqka.com, works through proxy)."""
    import akshare as ak
    with no_proxy():
        return ak.stock_board_concept_name_ths()


def _fetch_concept_constituents_ths(concept_code: str) -> list[dict]:
    """Scrape concept constituents from THS detail page (10jqka.com).

    Returns top stocks from the first page (usually 10-20).
    THS blocks ajax pagination but the first page with auth cookie works.
    """
    ths_headers = _get_ths_headers()
    url = f"https://q.10jqka.com.cn/gn/detail/code/{concept_code}/"

    try:
        with no_proxy():
            r = requests.get(url, headers=headers, timeout=15)
    except Exception:
        return []

    if r.status_code != 200:
        return []

    soup = BeautifulSoup(r.text, "lxml")
    table = soup.find("table", class_="m-table")
    if not table:
        return []

    stocks = []
    for row in table.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) >= 3:
            stocks.append({
                "code": cells[1].text.strip(),
                "name": cells[2].text.strip(),
            })
    return stocks


# Module-level aliases for easy mocking in tests
_fetch_stock_info = _fetch_stock_info_baostock
_fetch_concept_names = _fetch_concept_names_ths
_fetch_concept_constituents = _fetch_concept_constituents_ths


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

        # 3. THS concept names + constituents (all via 10jqka.com, no eastmoney)
        logger.info("Fetching concept names via THS...")
        concept_names_df = _fetch_concept_names()
        name_col = "name" if "name" in concept_names_df.columns else "概念名称"
        code_col = "code" if "code" in concept_names_df.columns else "概念代码"

        concept_success = 0
        concept_fail = 0
        total = len(concept_names_df)

        for i, (_, row) in enumerate(concept_names_df.iterrows()):
            concept_name = str(row[name_col])
            concept_code = str(row[code_col])

            conn.execute(
                "INSERT OR REPLACE INTO concepts (name, source) VALUES (?, 'ths')",
                (concept_name,),
            )
            concept_id = conn.execute(
                "SELECT id FROM concepts WHERE name = ?", (concept_name,)
            ).fetchone()["id"]

            # Scrape constituents from THS detail page
            stocks = _fetch_concept_constituents(concept_code)
            if not stocks:
                concept_fail += 1
            else:
                concept_success += 1
                for stock in stocks:
                    # Only insert mapping if stock exists in stocks table
                    exists = conn.execute(
                        "SELECT 1 FROM stocks WHERE code = ?", (stock["code"],)
                    ).fetchone()
                    if exists:
                        conn.execute(
                            "INSERT OR IGNORE INTO concept_stocks (concept_id, stock_code) VALUES (?, ?)",
                            (concept_id, stock["code"]),
                        )

            if (i + 1) % 50 == 0:
                logger.info("Progress: %d/%d concepts processed", i + 1, total)
                conn.commit()  # Intermediate commit

            time.sleep(0.3)  # Be nice to THS servers

        conn.commit()

        logger.info("Index build complete.")
        logger.info(
            "Stocks: %d, Concepts: %d, Constituents mapped: %d/%d",
            len(stock_info), total, concept_success, total,
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
