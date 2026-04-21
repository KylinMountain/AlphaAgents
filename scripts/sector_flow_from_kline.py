"""Reconstruct sector fund flow from baostock 5-min K-line data.

Algorithm:
  For each 5-min bar of each stock:
    net_flow_proxy = amount * (close - open) / (high - low + ε)
    (signed by intra-bar directional move; magnitude scaled by price range)
  Aggregate by concept/industry membership → sector net flow per 5-min.
  Cumulate → end-of-bar cumulative net flow (matches live intraday API shape).

Validation target:
  Our existing `concept_fund_flow_hist` (Eastmoney EOD) gives the "true"
  daily 主力净流入 per concept. We compare reconstructed EOD vs actual.

Usage:
  # Prototype: test one sector (CPO) on one day
  uv run python scripts/sector_flow_from_kline.py --concept "共封装光学(CPO)" --date 2026-04-14

  # Validation: all concepts with at least 5 members, one day
  uv run python scripts/sector_flow_from_kline.py --validate --date 2026-04-14
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime

import baostock as bs
import pandas as pd

from alpha_agents.config import DATA_DIR, no_proxy

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("sector_flow")


@dataclass
class StockBar5m:
    date: str
    time: str
    code: str
    open: float
    high: float
    low: float
    close: float
    volume: int
    amount: float


def _bs_login_once():
    """Idempotent login."""
    if not getattr(_bs_login_once, "_logged_in", False):
        bs.login()
        _bs_login_once._logged_in = True


def _to_bs_code(code: str) -> str:
    """Convert 6-digit code to baostock format: sh.600519 / sz.300475 / sz.688xxx."""
    if code.startswith("6"):
        return f"sh.{code}"
    if code.startswith(("0", "3")):
        return f"sz.{code}"
    if code.startswith(("8", "9")):  # 北交所
        return f"bj.{code}"
    return f"sz.{code}"


def fetch_5min_bars(code: str, date: str) -> list[StockBar5m]:
    """Fetch 5-min K-line for one stock on one trading day.

    baostock 5-min format:
      frequency='5', adjustflag='3' (unadjusted to match sector flow calc)
      fields: date, time, code, open, high, low, close, volume, amount
    """
    _bs_login_once()
    bs_code = _to_bs_code(code)
    with no_proxy():
        rs = bs.query_history_k_data_plus(
            bs_code,
            "date,time,open,high,low,close,volume,amount",
            start_date=date, end_date=date,
            frequency="5", adjustflag="3",
        )
    bars = []
    while rs.error_code == "0" and rs.next():
        r = rs.get_row_data()
        try:
            bars.append(StockBar5m(
                date=r[0],
                time=r[1][8:14],  # "20260414093500" → "093500"
                code=code,
                open=float(r[2]) if r[2] else 0.0,
                high=float(r[3]) if r[3] else 0.0,
                low=float(r[4]) if r[4] else 0.0,
                close=float(r[5]) if r[5] else 0.0,
                volume=int(r[6]) if r[6] else 0,
                amount=float(r[7]) if r[7] else 0.0,
            ))
        except (ValueError, IndexError):
            continue
    return bars


def proxy_net_flow(bar: StockBar5m) -> float:
    """Signed proxy for 主力净流入 within a 5-min bar.

    Heuristic: volume × directional weight. Positive when closes above open.
    Magnitude scaled by how much of the intrabar range was bought up.
    """
    rng = max(bar.high - bar.low, 0.01)
    if bar.close > bar.open:
        weight = (bar.close - bar.open) / rng
        return bar.amount * weight
    elif bar.close < bar.open:
        weight = (bar.open - bar.close) / rng
        return -bar.amount * weight
    return 0.0


def get_concept_members(concept_name: str) -> list[dict]:
    """Read concept's member stocks from stocks.db."""
    conn = sqlite3.connect("data/stocks.db")
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT s.code, s.name FROM concepts c "
        "JOIN concept_stocks cs ON c.id = cs.concept_id "
        "JOIN stocks s ON cs.stock_code = s.code "
        "WHERE c.name = ?",
        (concept_name,),
    ).fetchall()
    conn.close()
    return [{"code": r["code"], "name": r["name"]} for r in rows]


def reconstruct_sector_flow(members: list[dict], date: str) -> dict:
    """Pull 5-min bars for all members, aggregate to sector 5-min time series."""
    # Fetch all members' bars
    all_bars: dict[str, list[StockBar5m]] = {}
    for m in members:
        bars = fetch_5min_bars(m["code"], date)
        if bars:
            all_bars[m["code"]] = bars

    # Bucket bars by time
    by_time: dict[str, list[StockBar5m]] = {}
    for code, bars in all_bars.items():
        for b in bars:
            by_time.setdefault(b.time, []).append(b)

    # Compute per-5min net flow (sum of member proxies)
    times = sorted(by_time.keys())
    series = []
    cumulative = 0.0
    for t in times:
        net = sum(proxy_net_flow(b) for b in by_time[t])
        cumulative += net
        series.append({
            "time": t,
            "net_5min": net,
            "cumulative": cumulative,
        })

    # EOD stats
    total_amount = sum(b.amount for bars in all_bars.values() for b in bars)
    total_net = cumulative  # cumulative over all bars = EOD net flow

    return {
        "members_fetched": len(all_bars),
        "total_amount_yi": round(total_amount / 1e8, 2),
        "eod_net_flow_yi": round(total_net / 1e8, 2),
        "bar_count": len(series),
        "series": series,
    }


def load_actual_sector_flow(concept_name: str, date: str) -> dict | None:
    """Read the actual Eastmoney-sourced 板块资金流 for this date from daily_snapshots."""
    from alpha_agents.data.memory_store import _get_conn
    row = _get_conn().execute(
        "SELECT data FROM daily_snapshots WHERE data_type='concept_fund_flow_hist' "
        "AND date = ?",
        (date,),
    ).fetchone()
    if not row:
        return None
    data = json.loads(row["data"])
    # Fuzzy match concept name
    em_name_keywords = concept_name.replace("概念", "").replace("(", "").replace(")", "").replace(" ", "")
    for sec in data.get("sectors", []):
        s_name = sec.get("name", "")
        s_key = s_name.replace("概念", "").replace("(", "").replace(")", "").replace(" ", "")
        if em_name_keywords in s_key or s_key in em_name_keywords:
            return {
                "em_name": s_name,
                "main_net_yi": round(sec.get("main_net", 0) / 1e8, 2),
                "close_pct_change": sec.get("close_pct_change", 0),
            }
    return None


def compare_one_concept(concept_name: str, date: str) -> dict:
    members = get_concept_members(concept_name)
    if not members:
        return {"error": f"No members for concept '{concept_name}' in stocks.db"}

    logger.info("Reconstructing %s (%d members) for %s...",
                concept_name, len(members), date)
    recon = reconstruct_sector_flow(members, date)

    actual = load_actual_sector_flow(concept_name, date)

    comparison = {
        "concept": concept_name,
        "date": date,
        "members_in_db": len(members),
        "members_fetched": recon["members_fetched"],
        "reconstructed_eod_net_yi": recon["eod_net_flow_yi"],
        "reconstructed_total_amount_yi": recon["total_amount_yi"],
    }
    if actual:
        comparison["actual_em_name"] = actual["em_name"]
        comparison["actual_main_net_yi"] = actual["main_net_yi"]
        if actual["main_net_yi"] != 0:
            ratio = recon["eod_net_flow_yi"] / actual["main_net_yi"]
            comparison["ratio"] = round(ratio, 3)
            comparison["direction_match"] = (
                (recon["eod_net_flow_yi"] > 0) == (actual["main_net_yi"] > 0)
            )
    return comparison


def validate_all(date: str, min_members: int = 5) -> None:
    """Compare reconstruction for all concepts with >= min_members."""
    conn = sqlite3.connect("data/stocks.db")
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT c.name, COUNT(cs.stock_code) as n "
        "FROM concepts c LEFT JOIN concept_stocks cs ON c.id = cs.concept_id "
        "GROUP BY c.id HAVING n >= ? ORDER BY n DESC",
        (min_members,),
    ).fetchall()
    conn.close()

    concepts = [dict(r) for r in rows]
    logger.info("Validating %d concepts on %s...", len(concepts), date)

    results = []
    for c in concepts[:30]:  # test top 30 to save time
        try:
            r = compare_one_concept(c["name"], date)
            results.append(r)
            if "ratio" in r:
                logger.info("  %s: recon=%.2f亿 actual=%.2f亿 ratio=%.2f dir_match=%s",
                            r["concept"], r["reconstructed_eod_net_yi"],
                            r["actual_main_net_yi"], r["ratio"], r["direction_match"])
            else:
                logger.info("  %s: recon=%.2f亿 (no actual data to compare)",
                            r["concept"], r.get("reconstructed_eod_net_yi", 0))
        except Exception as e:
            logger.warning("  %s failed: %s", c["name"], e)

    # Summary
    with_actual = [r for r in results if "ratio" in r]
    if with_actual:
        dir_matches = sum(1 for r in with_actual if r["direction_match"])
        ratios = [r["ratio"] for r in with_actual]
        logger.info("=" * 60)
        logger.info("Summary: %d concepts compared", len(with_actual))
        logger.info("  Direction accuracy: %d/%d (%.0f%%)",
                    dir_matches, len(with_actual),
                    100 * dir_matches / len(with_actual))
        logger.info("  Ratio mean: %.2f, median: %.2f",
                    sum(ratios) / len(ratios), sorted(ratios)[len(ratios) // 2])
        logger.info("  Ratio range: [%.2f, %.2f]", min(ratios), max(ratios))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--concept", type=str, help="Single concept to test")
    parser.add_argument("--date", type=str, required=True, help="YYYY-MM-DD")
    parser.add_argument("--validate", action="store_true",
                        help="Validate all concepts on the given date")
    args = parser.parse_args()

    if args.validate:
        validate_all(args.date)
    elif args.concept:
        r = compare_one_concept(args.concept, args.date)
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
