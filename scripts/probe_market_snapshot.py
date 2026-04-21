"""Feasibility probe for whole-market quote snapshot.

Measures: response time, row count, byte size, DB growth per capture.
Does NOT wire into the pipeline. Safe to run anytime.

Usage:
  uv run python scripts/probe_market_snapshot.py              # 1 capture
  uv run python scripts/probe_market_snapshot.py --runs 3 --interval 60
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

import sqlite3

from alpha_agents.data.snapshot_store import (
    SNAPSHOTS_DB_PATH, save_all_quotes, fetch_all_quotes_tencent, _get_conn,
)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("probe")


def _all_codes() -> list[str]:
    """All A-share codes from local stocks.db."""
    conn = sqlite3.connect("data/stocks.db")
    rows = conn.execute("SELECT code FROM stocks").fetchall()
    conn.close()
    return [r[0] for r in rows]


def run_one(run_idx: int, codes: list[str]) -> dict:
    """One capture cycle. Returns timing + size metrics."""
    db_size_before = SNAPSHOTS_DB_PATH.stat().st_size if SNAPSHOTS_DB_PATH.exists() else 0

    t_fetch = time.time()
    df = fetch_all_quotes_tencent(codes)
    fetch_dt = time.time() - t_fetch

    if df is None or df.empty:
        return {"run": run_idx, "status": "empty", "fetch_dt": fetch_dt}

    # Rough in-memory bytes (csv-style estimate)
    mem_bytes = df.memory_usage(deep=True).sum()

    t_save = time.time()
    n = save_all_quotes(df)
    save_dt = time.time() - t_save

    db_size_after = SNAPSHOTS_DB_PATH.stat().st_size
    db_growth = db_size_after - db_size_before

    return {
        "run": run_idx,
        "status": "ok",
        "fetch_dt": round(fetch_dt, 2),
        "save_dt": round(save_dt, 2),
        "rows": n,
        "columns": len(df.columns),
        "mem_kb": round(mem_bytes / 1024, 1),
        "db_growth_kb": round(db_growth / 1024, 1),
        "db_total_kb": round(db_size_after / 1024, 1),
        "sample_cols": list(df.columns)[:8],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=1, help="number of capture cycles")
    parser.add_argument("--interval", type=int, default=30, help="seconds between runs")
    args = parser.parse_args()

    codes = _all_codes()
    logger.info("Probe starting: runs=%d, interval=%ds, %d codes",
                args.runs, args.interval, len(codes))

    results = []
    for i in range(1, args.runs + 1):
        try:
            r = run_one(i, codes)
            results.append(r)
            if r["status"] == "ok":
                logger.info(
                    "Run %d: %d rows × %d cols in %.2fs fetch + %.2fs save "
                    "(mem=%.0f KB, DB+%s KB → %.1f KB total)",
                    r["run"], r["rows"], r["columns"],
                    r["fetch_dt"], r["save_dt"],
                    r["mem_kb"], r["db_growth_kb"], r["db_total_kb"],
                )
            else:
                logger.warning("Run %d: %s", r["run"], r["status"])
        except Exception as e:
            logger.error("Run %d failed: %s", i, e)
            results.append({"run": i, "status": "error", "err": str(e)})

        if i < args.runs:
            time.sleep(args.interval)

    # Summary
    ok_runs = [r for r in results if r.get("status") == "ok"]
    print()
    print("=" * 60)
    print(f"SUMMARY: {len(ok_runs)}/{len(results)} runs successful")
    if ok_runs:
        avg_fetch = sum(r["fetch_dt"] for r in ok_runs) / len(ok_runs)
        avg_rows = sum(r["rows"] for r in ok_runs) / len(ok_runs)
        avg_growth = sum(r["db_growth_kb"] for r in ok_runs) / len(ok_runs)
        print(f"  avg fetch time: {avg_fetch:.2f}s")
        print(f"  avg rows: {avg_rows:.0f}")
        print(f"  avg DB growth per capture: {avg_growth:.1f} KB")
        projected_day = avg_growth * 48  # 48 cycles/day
        projected_year = projected_day * 245  # trading days
        print(f"  projected: {projected_day:.0f} KB/day, {projected_year / 1024:.1f} MB/year")
        print(f"  columns in akshare output: {ok_runs[0]['columns']}")
        print(f"  sample cols: {ok_runs[0]['sample_cols']}")

    sys.exit(0 if ok_runs else 1)


if __name__ == "__main__":
    main()
