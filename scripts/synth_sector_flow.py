"""Synthesize concept/industry fund-flow snapshots from per-stock flow.

Fills ``daily_snapshots['concept_fund_flow_hist']`` and
``['industry_fund_flow_hist']`` — the keys ``scripts/replay_evolution.py``
reads to pick each day's directions, and which had no rows at all because
their only writer needs Eastmoney. See
``docs/exec-plans/active/synth-sector-flow-from-stock-flow.md`` and
``alpha_agents/data/sector_flow_synth`` for the calibration.

Usage:
    # Every session the per-stock table covers
    uv run python scripts/synth_sector_flow.py

    # One window, concepts only
    uv run python scripts/synth_sector_flow.py --start 20260105 --end 20260130 \
        --only concept

    # Refresh THS concept creation dates first (rate-limited, minutes)
    uv run python scripts/synth_sector_flow.py --refresh-dates
"""

from __future__ import annotations

# Load .env before alpha_agents.config so a redirected data dir is honoured.
from dotenv import load_dotenv
load_dotenv()

import argparse
import logging
import sqlite3
import sys

from alpha_agents.config import DATA_DIR, DB_PATH
from alpha_agents.data.concept_dates import (
    earliest_known, fetch_creation_dates, store_creation_dates)
from alpha_agents.data.daily_snapshots import save_snapshot
from alpha_agents.data.db import init_db
from alpha_agents.data.sector_flow_synth import (
    synth_concept_flow, synth_industry_flow)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("synth_sector_flow")

_JOBS = {
    "concept": ("concept_fund_flow_hist", synth_concept_flow),
    "industry": ("industry_fund_flow_hist", synth_industry_flow),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=None, help="YYYYMMDD")
    ap.add_argument("--end", default=None, help="YYYYMMDD")
    ap.add_argument("--only", choices=sorted(_JOBS), default=None)
    ap.add_argument("--refresh-dates", action="store_true",
                    help="re-fetch THS concept creation dates first")
    args = ap.parse_args()

    init_db(DB_PATH)                      # ensures concepts.created_date exists
    stocks = sqlite3.connect(DB_PATH)
    snaps = sqlite3.connect(DATA_DIR / "market_snapshots.db")
    hist = sqlite3.connect(DATA_DIR / "market_history.db")

    if args.refresh_dates:
        dates = fetch_creation_dates()
        if not dates:
            logger.error("no creation dates fetched — refusing to continue, "
                         "the look-ahead gate would be vacuous")
            return 1
        store_creation_dates(DB_PATH, dates)

    bound = earliest_known(stocks)
    if bound is None:
        logger.error("concepts.created_date is empty — run with "
                     "--refresh-dates first, or the gate admits everything")
        return 1
    logger.info("look-ahead gate: dates known back to %s; concepts without "
                "a date are older than that and always admitted", bound)

    sql = ("SELECT DISTINCT trade_date FROM stock_fund_flow_daily "
           "WHERE 1=1")
    params: list[str] = []
    if args.start:
        sql += " AND trade_date >= ?"
        params.append(args.start)
    if args.end:
        sql += " AND trade_date <= ?"
        params.append(args.end)
    days = [d for (d,) in snaps.execute(sql + " ORDER BY trade_date", params)]
    if not days:
        logger.error("no sessions in stock_fund_flow_daily for that range")
        return 1

    jobs = {args.only: _JOBS[args.only]} if args.only else _JOBS
    logger.info("synthesizing %s over %d sessions (%s..%s)",
                "+".join(sorted(jobs)), len(days), days[0], days[-1])

    written = {name: 0 for name in jobs}
    for i, day in enumerate(days, 1):
        iso = "%s-%s-%s" % (day[:4], day[4:6], day[6:])
        for name, (key, fn) in jobs.items():
            sectors = fn(stocks, snaps, hist, day)
            if not sectors:
                logger.warning("  %s %s: no sectors, skipped", name, day)
                continue
            save_snapshot(iso, key, {"count": len(sectors),
                                     "sectors": sectors})
            written[name] += 1
        if i % 20 == 0 or i == len(days):
            logger.info("  %d/%d sessions", i, len(days))

    for name, n in written.items():
        logger.info("%s: %d snapshots written", name, n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
