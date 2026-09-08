"""Daily market data archiving — snapshot all key data sources for future backtesting.

Called after the review task (15:30). Pure data collection, no LLM involved.
"""

import json
import logging
import time

from alpha_agents.data.memory_store import _get_conn, _write_lock
from alpha_agents.tools.sector_ranking import get_sector_ranking_fn, get_concept_ranking_fn
from alpha_agents.tools.anomaly_detect import get_anomaly_stocks_fn
from alpha_agents.tools.market_breadth import get_market_breadth_fn
from alpha_agents.tools.fund_flow import get_block_trade_fn

logger = logging.getLogger(__name__)


def save_snapshot(date: str, data_type: str, data: dict) -> None:
    """Save a data snapshot. Upserts on (date, data_type)."""
    with _write_lock:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO daily_snapshots (date, data_type, data) VALUES (?, ?, ?) "
            "ON CONFLICT(date, data_type) DO UPDATE SET data = excluded.data, "
            "created_at = datetime('now','localtime')",
            (date, data_type, json.dumps(data, ensure_ascii=False)),
        )
        conn.commit()


def get_snapshot(date: str, data_type: str) -> dict | None:
    """Read a snapshot back. Returns parsed JSON or None."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT data FROM daily_snapshots WHERE date = ? AND data_type = ?",
        (date, data_type),
    ).fetchone()
    if row:
        return json.loads(row["data"])
    return None


def _tushare_archive(today: str) -> int:
    """Pull today's Tushare EOD data in bulk (one call per endpoint, whole
    market). Writes to the structured Tushare tables. Returns success count."""
    from alpha_agents.data.tushare_client import call_with_retry, is_permission_error
    from alpha_agents.data.tushare_store import (
        save_stock_fund_flow_daily, save_lhb_daily, save_lhb_inst_daily,
        save_hm_daily, save_north_flow_daily, save_margin_daily,
    )

    td = today.replace("-", "")
    jobs = [
        ("fund_flow",   "moneyflow_dc",     save_stock_fund_flow_daily),
        ("lhb",         "top_list",         save_lhb_daily),
        ("lhb_inst",    "top_inst",         save_lhb_inst_daily),
        ("hm",          "hm_detail",        save_hm_daily),
        ("north",       "moneyflow_hsgt",   save_north_flow_daily),
        ("margin",      "margin",           save_margin_daily),
    ]

    success = 0
    for name, api, save_fn in jobs:
        try:
            df = call_with_retry(api, trade_date=td)
            n = save_fn(df)
            if n:
                success += 1
                logger.info("Tushare %s: +%d rows for %s", name, n, today)
        except Exception as e:
            if is_permission_error(e):
                logger.warning("Tushare %s: permission denied (skipping)", name)
            else:
                logger.warning("Tushare %s failed: %s", name, str(e)[:100])
    return success


def run_daily_archive() -> int:
    """Archive all data sources for today. Returns count of successful archives.

    Two phases:
      1. Tushare bulk EOD pull (whole market in 6 API calls, ~10s total).
         Covers: stock fund flow (5900+), LHB, hm, north flow, margin.
      2. Legacy JSON-blob sources still in daily_snapshots for data Tushare
         doesn't have (concept/industry flow from THS, limit-up pool, breadth,
         block trade). These redundantly snapshot board-level state.
    """
    today = time.strftime("%Y-%m-%d")
    archived = _tushare_archive(today)

    # Legacy JSON-blob sources (THS/akshare-only, no Tushare equivalent we trust)
    sources = [
        ("concept_fund_flow", lambda: get_concept_ranking_fn(top_n=50)),
        ("industry_fund_flow", lambda: get_sector_ranking_fn(top_n=30)),
        ("limit_up_pool", lambda: get_anomaly_stocks_fn()),
        ("market_breadth", lambda: get_market_breadth_fn()),
        ("block_trade", lambda: get_block_trade_fn()),
    ]

    for data_type, fetch_fn in sources:
        try:
            raw = fetch_fn()
            data = json.loads(raw)
            save_snapshot(today, data_type, data)
            archived += 1
            logger.info("Archived %s for %s", data_type, today)
        except Exception as e:
            logger.warning("Failed to archive %s: %s", data_type, e)

    logger.info("Daily archive complete: %d sources archived", archived)
    return archived
