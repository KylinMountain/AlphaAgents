"""Backfill historical sector/concept fund flow via eastmoney push2his API.

Direct akshare calls fail with ConnectionError in this environment (eastmoney
seems to block the SSL fingerprint from requests), but the project's CF Worker
proxy at CF_WORKER_URL reaches push2.eastmoney.com / push2his.eastmoney.com
fine. We replicate akshare's logic using the CF-routed http_client.

Data saved into `daily_snapshots` table:
- concept_fund_flow_hist: {date: {bk_code: {name, main_net, zl_pct, zl_pct_abs,
                                             big_net, ...}}}
- industry_fund_flow_hist: same structure keyed by industry BK code

Then for each trading day, we can synthesize the "ranking" snapshot that
intraday_monitor needs.

Usage:
    uv run python scripts/backfill_sector_flow.py
    # Optional:
    uv run python scripts/backfill_sector_flow.py --since 2025-10-20
"""

from __future__ import annotations

# Load .env BEFORE http_client so CF_WORKER_URL is picked up.
from dotenv import load_dotenv
load_dotenv()

import argparse
import json
import logging
import time
from datetime import datetime
from typing import Iterable

from alpha_agents.http_client import (
    fetch, _fetch_via_worker, get_headers, cf_worker_available,
)


def _fetch_via_worker_url(url: str, timeout: int = 20):
    """Force route through CF Worker regardless of domestic classification.
    The direct eastmoney SSL fingerprint is blocked in this environment,
    but push2his/push2 are reachable via the Worker proxy.
    """
    headers = get_headers()
    return _fetch_via_worker(url, "GET", headers, timeout)
from alpha_agents.data.daily_archive import save_snapshot, get_snapshot

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("backfill_sector")


_NAME_LIST_URL = "https://push2.eastmoney.com/api/qt/clist/get"
_HIST_URL = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"


def _fetch_name_code_map(board_type: int) -> list[dict]:
    """board_type: 2 = 行业, 3 = 概念. Returns list of {code, name}."""
    all_rows = []
    for page in range(1, 10):  # max 10 pages * 100 = 1000 (plenty)
        params = {
            "fid": "f62",
            "po": "1",
            "pz": "100",
            "pn": str(page),
            "np": "1",
            "fltt": "2",
            "invt": "2",
            "ut": "8dec03ba335b81bf4ebdf7b29ec27d15",
            "fs": f"m:90 t:{board_type}",
            "fields": "f12,f14",
        }
        from urllib.parse import urlencode
        url = f"{_NAME_LIST_URL}?{urlencode(params)}"
        resp = _fetch_via_worker_url(url)
        resp.raise_for_status()
        body = resp.json()
        rows = body.get("data", {}).get("diff", []) or []
        if not rows:
            break
        all_rows.extend({"code": r["f12"], "name": r["f14"]} for r in rows)
        if len(rows) < 100:
            break  # last page
    return all_rows


def _fetch_hist_for_sector(bk_code: str, lmt: int = 0) -> list[list[str]]:
    """lmt=0 means all available history. Returns klines as list of comma-split values."""
    params = {
        "lmt": str(lmt),
        "klt": "101",
        "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
        "secid": f"90.{bk_code}",
    }
    from urllib.parse import urlencode
    url = f"{_HIST_URL}?{urlencode(params)}"
    resp = _fetch_via_worker_url(url)
    resp.raise_for_status()
    body = resp.json()
    klines = body.get("data", {}).get("klines", []) or []
    return [k.split(",") for k in klines]


# Column mapping per akshare stock_sector_fund_flow_hist:
# [date, 主力净流入-净额, 小单净流入-净额, 中单净流入-净额, 大单净流入-净额,
#  超大单净流入-净额, 主力净流入-净占比, 小单净流入-净占比, 中单净流入-净占比,
#  大单净流入-净占比, 超大单净流入-净占比, 收盘价?, 涨跌幅, ?, ?]
def _parse_kline_row(row: list[str]) -> dict:
    return {
        "date": row[0],
        "main_net": _tofloat(row[1]),
        "small_net": _tofloat(row[2]),
        "mid_net": _tofloat(row[3]),
        "big_net": _tofloat(row[4]),
        "xlarge_net": _tofloat(row[5]),
        "main_pct": _tofloat(row[6]),
        "small_pct": _tofloat(row[7]),
        "mid_pct": _tofloat(row[8]),
        "big_pct": _tofloat(row[9]),
        "xlarge_pct": _tofloat(row[10]),
        "close_pct_change": _tofloat(row[12]),  # index 12 is 涨跌幅
    }


def _tofloat(s: str) -> float:
    try:
        return float(s)
    except (TypeError, ValueError):
        return 0.0


def backfill_sector_fund_flow_hist(board_type: int, snapshot_key: str,
                                    since: str | None = None,
                                    save_every: int = 25) -> dict:
    """Fetch each sector's full history, accumulate in memory, and incrementally
    save to daily_snapshots every ``save_every`` sectors. Survives crashes/
    interruptions — already-saved dates just get merged on next run.

    Returns: dict of stats.
    """
    label = "行业" if board_type == 2 else "概念"
    logger.info("Fetching %s name→BK code map...", label)
    sectors = _fetch_name_code_map(board_type)
    logger.info("  Got %d %s sectors", len(sectors), label)

    # Transpose: for each date, accumulate sector rows
    by_date: dict[str, list[dict]] = {}
    errors = 0
    processed = 0

    def _flush():
        """Merge current by_date into existing snapshots (union of old + new rows)."""
        for date, new_rows in by_date.items():
            existing = get_snapshot(date, snapshot_key)
            # Union on (code): keep old rows that aren't in new_rows
            new_codes = {r["code"] for r in new_rows}
            merged = list(new_rows)
            if existing:
                for old in existing.get("sectors", []):
                    if old.get("code") not in new_codes:
                        merged.append(old)
            merged_sorted = sorted(merged, key=lambda r: r["main_net"], reverse=True)
            for rank, r in enumerate(merged_sorted, 1):
                r["rank"] = rank
            save_snapshot(date, snapshot_key, {
                "count": len(merged_sorted),
                "sectors": merged_sorted,
            })

    for i, sec in enumerate(sectors):
        try:
            rows = _fetch_hist_for_sector(sec["code"])
        except Exception as e:
            logger.debug("Failed to fetch %s %s: %s", sec["code"], sec["name"], e)
            errors += 1
            continue
        for raw in rows:
            if len(raw) < 13:
                continue
            rec = _parse_kline_row(raw)
            date = rec["date"]
            if since and date < since:
                continue
            by_date.setdefault(date, []).append({
                "code": sec["code"],
                "name": sec["name"],
                **rec,
            })
        processed += 1
        if processed % save_every == 0:
            logger.info("  Progress: %d/%d sectors (flushing %d dates to DB)",
                        processed, len(sectors), len(by_date))
            _flush()
            # Reset by_date — future flushes will merge on top
            by_date = {}
        time.sleep(0.15)  # be polite to CF worker

    # Final flush
    if by_date:
        logger.info("  Final flush: %d sectors processed, %d dates pending",
                    processed, len(by_date))
        _flush()

    return {"sectors_processed": processed, "total_sectors": len(sectors),
            "errors": errors}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", type=str, default="2026-01-01",
                        help="Only save dates >= this (YYYY-MM-DD)")
    parser.add_argument("--sources", default="industry,concept",
                        help="Comma list: industry,concept")
    args = parser.parse_args()

    sources = [s.strip() for s in args.sources.split(",")]

    if "industry" in sources:
        logger.info("=== Industry fund flow history ===")
        stats = backfill_sector_fund_flow_hist(
            board_type=2, snapshot_key="industry_fund_flow_hist",
            since=args.since,
        )
        logger.info("  Industry: %s", stats)

    if "concept" in sources:
        logger.info("=== Concept fund flow history ===")
        stats = backfill_sector_fund_flow_hist(
            board_type=3, snapshot_key="concept_fund_flow_hist",
            since=args.since,
        )
        logger.info("  Concept: %s", stats)

    logger.info("=== Done ===")


if __name__ == "__main__":
    main()
