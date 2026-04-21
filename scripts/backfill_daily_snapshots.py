"""Backfill daily_snapshots from akshare historical APIs.

Tier 1 (fast, days to run):
  - lhb (龙虎榜) — deep history via stock_lhb_detail_em
  - margin (两融) — deep history via stock_margin_detail_sse
  - block_trade — deep history via stock_dzjy_mrtj
  - limit_up_pool / dtgc_pool / zbgc_pool — last 30 days only

Tier 2 (slow, individual stock aggregation):
  - concept_fund_flow / industry_fund_flow — synthesized from
    stock_individual_fund_flow (not yet implemented)

Usage:
    uv run python scripts/backfill_daily_snapshots.py --days 60
    uv run python scripts/backfill_daily_snapshots.py --start 2026-02-01 --end 2026-04-16
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import datetime, timedelta

import akshare as ak

from alpha_agents.data.daily_archive import save_snapshot, get_snapshot
from alpha_agents.config import no_proxy

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("backfill")


def _trading_days(start: datetime, end: datetime) -> list[str]:
    """Weekdays only. Not perfect (doesn't skip holidays) but akshare returns
    empty rows on non-trading days, which is fine."""
    out = []
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            out.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)
    return out


# ── LHB: 龙虎榜 (deep history, single range call) ─────────────────

def backfill_lhb(dates: list[str]) -> int:
    """One range call returns all dates; split back into per-day snapshots."""
    if not dates:
        return 0
    start = dates[0].replace("-", "")
    end = dates[-1].replace("-", "")
    logger.info("LHB: fetching %s → %s (1 call)", start, end)
    try:
        with no_proxy():
            df = ak.stock_lhb_detail_em(start_date=start, end_date=end)
    except Exception as e:
        logger.warning("LHB fetch failed: %s", e)
        return 0

    # 'date' or '上榜日' column
    date_col = None
    for cand in ("上榜日", "交易日期", "date"):
        if cand in df.columns:
            date_col = cand
            break
    if not date_col:
        logger.warning("LHB: no date column found, cols=%s", list(df.columns))
        return 0

    df[date_col] = df[date_col].astype(str)
    count = 0
    for d, group in df.groupby(date_col):
        d_norm = d[:10] if "-" in d else f"{d[:4]}-{d[4:6]}-{d[6:8]}"
        if get_snapshot(d_norm, "lhb"):
            logger.debug("LHB %s already archived, skipping", d_norm)
            continue
        records = group.to_dict(orient="records")
        save_snapshot(d_norm, "lhb", {"records": records, "count": len(records)})
        logger.info("  LHB %s: %d rows saved", d_norm, len(records))
        count += 1
    return count


# ── Margin: 上交所两融 (per-day call) ─────────────────────────────

def backfill_margin(dates: list[str]) -> int:
    count = 0
    for d in dates:
        if get_snapshot(d, "margin"):
            continue
        d_fmt = d.replace("-", "")
        try:
            with no_proxy():
                df = ak.stock_margin_detail_sse(date=d_fmt)
        except Exception as e:
            logger.debug("  Margin %s: %s", d, e)
            continue
        if df is None or df.empty:
            continue
        records = df.to_dict(orient="records")
        save_snapshot(d, "margin", {"records": records, "count": len(records)})
        logger.info("  Margin %s: %d rows", d, len(records))
        count += 1
        time.sleep(0.3)  # be kind to SSE
    return count


# ── Block trade: 大宗交易日统计 (range call) ─────────────────────

def backfill_block_trade(dates: list[str]) -> int:
    if not dates:
        return 0
    start = dates[0].replace("-", "")
    end = dates[-1].replace("-", "")
    logger.info("Block trade: fetching %s → %s (1 call)", start, end)
    try:
        with no_proxy():
            df = ak.stock_dzjy_mrtj(start_date=start, end_date=end)
    except Exception as e:
        logger.warning("Block trade fetch failed: %s", e)
        return 0
    if df is None or df.empty:
        return 0

    date_col = None
    for cand in ("交易日期", "日期", "date"):
        if cand in df.columns:
            date_col = cand
            break
    if not date_col:
        logger.warning("Block trade: no date col; cols=%s", list(df.columns))
        return 0

    df[date_col] = df[date_col].astype(str)
    count = 0
    for d, group in df.groupby(date_col):
        d_norm = d[:10] if "-" in d else f"{d[:4]}-{d[4:6]}-{d[6:8]}"
        if get_snapshot(d_norm, "block_trade"):
            continue
        records = group.to_dict(orient="records")
        save_snapshot(d_norm, "block_trade", {"records": records, "count": len(records)})
        logger.info("  Block trade %s: %d rows", d_norm, len(records))
        count += 1
    return count


# ── Limit-up pool: 涨停/跌停/炸板 (last 30 days only) ────────────

def backfill_limit_up_pool(dates: list[str]) -> int:
    """Only last 30 days are supported by eastmoney."""
    cutoff = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    recent = [d for d in dates if d >= cutoff]
    if len(recent) < len(dates):
        logger.info("Limit-up: trimmed to last 30 days (%d of %d)",
                    len(recent), len(dates))

    count = 0
    for d in recent:
        if get_snapshot(d, "limit_up_pool"):
            continue
        d_fmt = d.replace("-", "")
        record: dict = {}
        try:
            with no_proxy():
                zt = ak.stock_zt_pool_em(date=d_fmt)
            record["limit_up"] = zt.to_dict(orient="records") if zt is not None else []
        except Exception as e:
            logger.debug("  zt %s: %s", d, e)
            record["limit_up"] = []
        try:
            with no_proxy():
                dt = ak.stock_zt_pool_dtgc_em(date=d_fmt)
            record["limit_down"] = dt.to_dict(orient="records") if dt is not None else []
        except Exception as e:
            logger.debug("  dtgc %s: %s", d, e)
            record["limit_down"] = []
        try:
            with no_proxy():
                zb = ak.stock_zt_pool_zbgc_em(date=d_fmt)
            record["broken_board"] = zb.to_dict(orient="records") if zb is not None else []
        except Exception as e:
            logger.debug("  zbgc %s: %s", d, e)
            record["broken_board"] = []

        if any(record.values()):
            save_snapshot(d, "limit_up_pool", record)
            logger.info("  Limit-up %s: %d 涨停, %d 跌停, %d 炸板",
                        d, len(record["limit_up"]),
                        len(record["limit_down"]), len(record["broken_board"]))
            count += 1
        time.sleep(0.3)
    return count


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=60,
                        help="Backfill last N days (default 60)")
    parser.add_argument("--start", type=str, help="YYYY-MM-DD")
    parser.add_argument("--end", type=str, help="YYYY-MM-DD (default: today)")
    parser.add_argument("--sources", type=str,
                        default="lhb,margin,block_trade,limit_up_pool",
                        help="Comma-separated list")
    args = parser.parse_args()

    if args.start:
        start = datetime.strptime(args.start, "%Y-%m-%d")
    else:
        start = datetime.now() - timedelta(days=args.days)
    end = datetime.strptime(args.end, "%Y-%m-%d") if args.end else datetime.now()

    dates = _trading_days(start, end)
    sources = [s.strip() for s in args.sources.split(",")]

    logger.info("=== Backfill plan ===")
    logger.info("  Range: %s → %s  (%d weekdays)", dates[0], dates[-1], len(dates))
    logger.info("  Sources: %s", sources)

    totals = {}
    if "lhb" in sources:
        totals["lhb"] = backfill_lhb(dates)
    if "margin" in sources:
        totals["margin"] = backfill_margin(dates)
    if "block_trade" in sources:
        totals["block_trade"] = backfill_block_trade(dates)
    if "limit_up_pool" in sources:
        totals["limit_up_pool"] = backfill_limit_up_pool(dates)

    logger.info("=== Backfill complete ===")
    for k, v in totals.items():
        logger.info("  %s: %d new days archived", k, v)


if __name__ == "__main__":
    main()
