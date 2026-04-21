"""Backfill Tushare EOD data (2026-01-01 → today or user-specified range).

Five endpoints, all filled via shared community token:
  - moneyflow_dc       → stock_fund_flow_daily
  - top_list           → lhb_daily
  - top_inst           → lhb_inst_daily
  - hm_detail          → hm_daily
  - moneyflow_hsgt     → north_flow_daily
  - margin             → margin_daily

Usage:
  uv run python scripts/backfill_tushare.py --start 2026-01-01
  uv run python scripts/backfill_tushare.py --start 2026-01-01 --only fund_flow
  uv run python scripts/backfill_tushare.py --start 2026-01-01 --throttle 0.3
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta

from alpha_agents.data.tushare_client import call_with_retry, is_permission_error
from alpha_agents.data.tushare_store import (
    save_stock_fund_flow_daily,
    save_lhb_daily, save_lhb_inst_daily, save_hm_daily,
    save_north_flow_daily, save_margin_daily,
    save_kpl_limit_list_daily, save_kpl_concept_daily,
)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("backfill_tushare")


def _date_range(start: str, end: str) -> list[str]:
    """Return YYYYMMDD strings for each calendar day in [start, end]."""
    s = datetime.strptime(start, "%Y-%m-%d").date()
    e = datetime.strptime(end, "%Y-%m-%d").date()
    days = []
    d = s
    while d <= e:
        days.append(d.strftime("%Y%m%d"))
        d += timedelta(days=1)
    return days


# Each entry: (name, tushare_api_name, save_fn, one_call_per_day, extra_kwargs)
ENDPOINTS = [
    ("fund_flow",   "moneyflow_dc",     save_stock_fund_flow_daily, True, {}),
    ("lhb",         "top_list",         save_lhb_daily,             True, {}),
    ("lhb_inst",    "top_inst",         save_lhb_inst_daily,        True, {}),
    ("hm",          "hm_detail",        save_hm_daily,              True, {}),
    ("north",       "moneyflow_hsgt",   save_north_flow_daily,      True, {}),
    ("margin",      "margin",           save_margin_daily,          True, {}),
    ("kpl_limit",   "kpl_list",         save_kpl_limit_list_daily,  True, {"tag": "涨停"}),
    ("kpl_concept", "limit_cpt_list",   save_kpl_concept_daily,     True, {}),
]


def backfill_one(name: str, api_name: str, save_fn, days: list[str],
                 throttle: float, extra_kwargs: dict | None = None) -> tuple[int, int, int]:
    """Backfill one endpoint across dates. Returns (total_rows, hit_days, err_days)."""
    total = 0
    hit = 0
    err = 0
    extra = extra_kwargs or {}
    for d in days:
        try:
            df = call_with_retry(api_name, trade_date=d, **extra)
            if df is None or df.empty:
                continue
            n = save_fn(df)
            if n:
                total += n
                hit += 1
                logger.info("  %s %s: +%d rows", name, d, n)
        except Exception as e:
            if is_permission_error(e):
                logger.error("  %s %s: PERMISSION DENIED — skipping endpoint", name, d)
                return total, hit, err + (len(days) - days.index(d))
            err += 1
            logger.warning("  %s %s failed: %s", name, d, str(e)[:80])
        if throttle > 0:
            time.sleep(throttle)
    return total, hit, err


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="YYYY-MM-DD (default: today)")
    parser.add_argument("--only", default=None,
                        help="comma-separated endpoint names (fund_flow,lhb,lhb_inst,hm,north,margin)")
    parser.add_argument("--throttle", type=float, default=0.3,
                        help="seconds between API calls")
    args = parser.parse_args()

    end = args.end or datetime.now().strftime("%Y-%m-%d")
    days = _date_range(args.start, end)
    logger.info("Backfilling %d days: %s → %s", len(days), args.start, end)

    only = set(args.only.split(",")) if args.only else None
    endpoints = [e for e in ENDPOINTS if not only or e[0] in only]
    logger.info("Endpoints: %s", [e[0] for e in endpoints])

    summary = []
    for name, api_name, save_fn, _, extra_kwargs in endpoints:
        logger.info("=" * 60)
        logger.info("Starting %s (%s)", name, api_name)
        t0 = time.time()
        total, hit, err = backfill_one(name, api_name, save_fn, days, args.throttle,
                                        extra_kwargs=extra_kwargs)
        dt = time.time() - t0
        summary.append((name, total, hit, err, dt))
        logger.info("Finished %s: %d rows, %d/%d days hit, %d errors, %.1fs",
                    name, total, hit, len(days), err, dt)

    print()
    print("=" * 60)
    print(f"{'Endpoint':<12} {'Rows':>10} {'Days':>8} {'Errs':>6} {'Time':>8}")
    print("-" * 60)
    for n, t, h, e, dt in summary:
        print(f"{n:<12} {t:>10} {h:>4}/{len(days):<4} {e:>6} {dt:>7.1f}s")


if __name__ == "__main__":
    main()
