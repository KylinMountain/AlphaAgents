"""Daily market data archiving — snapshot all key data sources for future backtesting.

Called after the review task (15:30). Pure data collection, no LLM involved.
"""

import json
import logging
import time

from alpha_agents.data.memory_store import _get_conn, _write_lock, get_active_themes
from alpha_agents.tools.sector_ranking import get_sector_ranking_fn, get_concept_ranking_fn
from alpha_agents.tools.anomaly_detect import get_anomaly_stocks_fn
from alpha_agents.tools.market_breadth import get_market_breadth_fn
from alpha_agents.tools.fund_flow import (
    get_lhb_detail_fn, get_block_trade_fn,
    get_north_flow_fn, get_margin_data_fn,
    get_stock_fund_flow_fn,
)

logger = logging.getLogger(__name__)


def save_snapshot(date: str, data_type: str, data: dict) -> None:
    """Save a data snapshot. Upserts on (date, data_type)."""
    with _write_lock:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO daily_snapshots (date, data_type, data) VALUES (?, ?, ?) "
            "ON CONFLICT(date, data_type) DO UPDATE SET data = excluded.data, "
            "created_at = datetime('now')",
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


def run_daily_archive() -> int:
    """Archive all data sources for today. Returns count of successful archives."""
    today = time.strftime("%Y-%m-%d")
    archived = 0

    sources = [
        ("concept_fund_flow", lambda: get_concept_ranking_fn(top_n=50)),
        ("industry_fund_flow", lambda: get_sector_ranking_fn(top_n=30)),
        ("limit_up_pool", lambda: get_anomaly_stocks_fn()),
        ("market_breadth", lambda: get_market_breadth_fn()),
        ("lhb", lambda: get_lhb_detail_fn()),
        ("block_trade", lambda: get_block_trade_fn()),
        ("north_flow", lambda: get_north_flow_fn("today")),
        ("margin", lambda: get_margin_data_fn()),
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

    # Archive fund flow for active theme stocks
    try:
        themes = get_active_themes()
        theme_flows = {}
        for theme in themes:
            stocks = json.loads(theme["core_stocks"]) if theme.get("core_stocks") else []
            for stock in stocks[:5]:
                code = stock.get("code", "")
                if code and code not in theme_flows:
                    try:
                        raw = get_stock_fund_flow_fn(code)
                        theme_flows[code] = json.loads(raw)
                    except Exception:
                        pass
        if theme_flows:
            save_snapshot(today, "theme_fund_flow", theme_flows)
            archived += 1
            logger.info("Archived theme_fund_flow (%d stocks) for %s", len(theme_flows), today)
    except Exception as e:
        logger.warning("Failed to archive theme_fund_flow: %s", e)

    logger.info("Daily archive complete: %d/%d sources archived", archived, len(sources) + 1)
    return archived
