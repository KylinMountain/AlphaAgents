"""VPA Walk-Forward Backtest — day-by-day scan, full market.

For each trading day D that has ≥60 days history before + ≥5 days after:
  - Scan ALL eligible stocks
  - Compute VPA using only data up to and including D
  - Record forward returns T+1..T+5

Then aggregate per day:
  - bullish stocks' hit rate and mean return per horizon
  - bearish stocks' hit rate and mean return per horizon
  - n_bullish, n_bearish, n_total samples

Output:
  - CSV: one row per (date, verdict, horizon) for plotting time series
  - Summary table: mean of daily hit rates across dates
  - Regime classification per date

This is the full walk-forward backtest — no random sampling, no survivorship bias.

Usage:
  uv run python scripts/backtest_vpa_walkforward.py
  uv run python scripts/backtest_vpa_walkforward.py --out data/vpa_wf.csv
  uv run python scripts/backtest_vpa_walkforward.py --max-stocks 500  # faster sample
"""

import argparse
import csv
import logging
import statistics
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(REPO_ROOT / ".env")
except ImportError:
    pass

import numpy as np
import pandas as pd

from alpha_agents.data.market_history import _get_conn as _history_conn
from alpha_agents.tools.vpa import (
    _compute_derived, _detect_patterns, _obv_trend, PATTERN_SCORES,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("backtest_wf")

HORIZONS = [1, 2, 3, 4, 5]
WINDOW = 20
MIN_HISTORY = 60  # need ≥60 days of K-lines before the sample date


def compute_vpa_from_slice(df_slice: pd.DataFrame, regime: str = "") -> dict | None:
    """Compute VPA from an already-sliced DataFrame (≤ sample date)."""
    if len(df_slice) < WINDOW + 5:
        return None
    df = _compute_derived(df_slice.reset_index(drop=True), window=WINDOW)
    patterns = _detect_patterns(df, regime=regime)
    obv = _obv_trend(df)
    net_score = sum(p["strength"] for p in patterns)
    if obv == "上升":
        net_score += 1
    elif obv == "下降":
        net_score -= 1

    # Regime-adaptive thresholds — match alpha_agents/tools/vpa.py
    if regime == "falling":
        bt, brt = 2, -3
    elif regime == "rising":
        bt, brt = 4, -3
    else:
        bt, brt = 2, -4

    if net_score >= bt:
        verdict = "bullish"
    elif net_score <= brt:
        verdict = "bearish"
    else:
        verdict = "neutral"

    primary = ""
    if patterns:
        primary = max(patterns, key=lambda p: abs(p["strength"]))["pattern"]

    return {
        "verdict": verdict,
        "net_score": net_score,
        "primary_pattern": primary,
        "n_patterns": len(patterns),
    }


def load_all_histories(conn, max_stocks: int | None = None) -> dict[str, pd.DataFrame]:
    """Load all stock histories into memory. Pre-sorted by date ascending."""
    codes_rows = conn.execute(
        "SELECT code, COUNT(*) n FROM daily_kline GROUP BY code HAVING n >= ? ORDER BY code",
        (MIN_HISTORY + max(HORIZONS) + 5,),
    ).fetchall()
    codes = [r["code"] for r in codes_rows]
    if max_stocks:
        codes = codes[:max_stocks]

    logger.info("Loading histories for %d stocks...", len(codes))
    histories = {}
    for i, code in enumerate(codes):
        if i % 500 == 0 and i > 0:
            logger.info("  loaded %d/%d", i, len(codes))
        rows = conn.execute(
            "SELECT date, open, high, low, close, volume FROM daily_kline WHERE code = ? ORDER BY date",
            (code,),
        ).fetchall()
        if not rows:
            continue
        df = pd.DataFrame([dict(r) for r in rows])
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["open", "high", "low", "close", "volume"]).reset_index(drop=True)
        if len(df) >= MIN_HISTORY + max(HORIZONS) + 5:
            histories[code] = df
    logger.info("Loaded %d histories", len(histories))
    return histories


def build_regime_map(conn) -> dict[str, str]:
    """Tag each date with regime based on PRIOR 10-day market avg change."""
    rows = conn.execute(
        "SELECT date, AVG(change_pct) AS avg_chg FROM daily_kline GROUP BY date ORDER BY date"
    ).fetchall()
    dates = [r["date"] for r in rows]
    avg_chg = {r["date"]: float(r["avg_chg"] or 0) for r in rows}

    regime_map = {}
    window = 10
    for i, date in enumerate(dates):
        if i < window:
            regime_map[date] = "unknown"
            continue
        prior_vals = [avg_chg[d] for d in dates[i - window: i]]
        mean_val = sum(prior_vals) / len(prior_vals)
        if mean_val > 0.3:
            regime_map[date] = "rising"
        elif mean_val < -0.3:
            regime_map[date] = "falling"
        else:
            regime_map[date] = "ranging"
    return regime_map


def get_all_trading_dates(conn) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT date FROM daily_kline ORDER BY date"
    ).fetchall()
    return [r["date"] for r in rows]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=str, default="data/vpa_walkforward.csv")
    parser.add_argument("--detail-out", type=str, default="data/vpa_walkforward_detail.csv")
    parser.add_argument("--max-stocks", type=int, default=None,
                        help="Limit stocks for faster testing (default: all)")
    args = parser.parse_args()

    conn = _history_conn()
    regime_map = build_regime_map(conn)
    all_dates = get_all_trading_dates(conn)
    histories = load_all_histories(conn, max_stocks=args.max_stocks)

    max_h = max(HORIZONS)
    # Trading dates we can actually evaluate: need ≥MIN_HISTORY before, ≥max_h after
    eval_dates = all_dates[MIN_HISTORY: len(all_dates) - max_h]
    logger.info("Eval dates: %d (%s → %s)", len(eval_dates),
                eval_dates[0] if eval_dates else "-",
                eval_dates[-1] if eval_dates else "-")

    # Per-day aggregated rows for plotting
    daily_rows = []
    # Per-sample detail rows (big CSV)
    detail_rows = []

    for di, date in enumerate(eval_dates):
        if di % 10 == 0:
            logger.info("Progress: day %d/%d (%s)", di, len(eval_dates), date)

        # Per-day accumulator
        day_data = {
            "bullish": {h: [] for h in HORIZONS},
            "bearish": {h: [] for h in HORIZONS},
            "neutral": {h: [] for h in HORIZONS},
        }
        day_samples = {"bullish": 0, "bearish": 0, "neutral": 0}

        for code, df in histories.items():
            # Find index of `date` in this code's history
            idx_matches = df.index[df["date"] == date]
            if len(idx_matches) == 0:
                continue
            idx = idx_matches[0]
            if idx < MIN_HISTORY:
                continue
            if idx + max_h >= len(df):
                continue

            # Slice history up to and including date
            df_slice = df.iloc[: idx + 1]
            vpa = compute_vpa_from_slice(df_slice, regime=regime_map.get(date, ""))
            if not vpa:
                continue

            entry = df.iloc[idx]["close"]
            if not entry or entry <= 0:
                continue

            # Forward returns
            fwds = {}
            for h in HORIZONS:
                exit_close = df.iloc[idx + h]["close"]
                fwds[h] = (exit_close - entry) / entry * 100.0

            verdict = vpa["verdict"]
            day_samples[verdict] += 1
            for h in HORIZONS:
                day_data[verdict][h].append(fwds[h])

            detail_rows.append({
                "date": date,
                "code": code,
                "regime": regime_map.get(date, "unknown"),
                "verdict": verdict,
                "net_score": vpa["net_score"],
                "primary_pattern": vpa["primary_pattern"],
                **{f"t{h}_return": fwds[h] for h in HORIZONS},
            })

        # Aggregate per verdict per horizon
        for verdict in ("bullish", "bearish", "neutral"):
            for h in HORIZONS:
                vals = day_data[verdict][h]
                if not vals:
                    continue
                mean = statistics.mean(vals)
                if verdict == "bullish":
                    hit = sum(1 for x in vals if x > 0) / len(vals) * 100
                elif verdict == "bearish":
                    hit = sum(1 for x in vals if x < 0) / len(vals) * 100
                else:
                    hit = sum(1 for x in vals if abs(x) < 2) / len(vals) * 100

                daily_rows.append({
                    "date": date,
                    "regime": regime_map.get(date, "unknown"),
                    "verdict": verdict,
                    "horizon": h,
                    "n": len(vals),
                    "mean_return": round(mean, 3),
                    "hit_rate": round(hit, 2),
                })

    # Save CSVs
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if daily_rows:
        with open(out, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(daily_rows[0].keys()))
            w.writeheader()
            w.writerows(daily_rows)
        logger.info("Saved per-day aggregates to %s", out)

    detail_out = Path(args.detail_out)
    if detail_rows:
        with open(detail_out, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(detail_rows[0].keys()))
            w.writeheader()
            w.writerows(detail_rows)
        logger.info("Saved sample details to %s (%d rows)", detail_out, len(detail_rows))

    print_daily_summary(daily_rows)


def _binom_significance(hits: int, n: int) -> str:
    """Rough two-sided test: is hit rate significantly different from 50%?
    Uses normal approximation; fine when n ≥ 30.
    Returns annotation string like ' ***' (p<0.01), ' **' (p<0.05), ' *' (p<0.10), or ''.
    """
    if n < 20:
        return ""
    import math
    p_hat = hits / n
    se = math.sqrt(0.5 * 0.5 / n)
    z = (p_hat - 0.5) / se if se > 0 else 0
    abs_z = abs(z)
    if abs_z >= 2.576:
        return " ***"  # p<0.01
    if abs_z >= 1.96:
        return " **"   # p<0.05
    if abs_z >= 1.645:
        return " *"    # p<0.10
    return ""


def print_daily_summary(daily_rows: list[dict]) -> None:
    """Print signal stability across dates."""
    if not daily_rows:
        return

    print(f"\n{'=' * 120}")
    print("VPA Walk-Forward Daily Summary — mean of daily stats across all evaluation dates")
    print(f"{'=' * 120}\n")

    # Group by (verdict, horizon) → list of daily means/hits
    grouped = defaultdict(lambda: {"means": [], "hits": [], "ns": [], "regimes": []})
    for r in daily_rows:
        key = (r["verdict"], r["horizon"])
        grouped[key]["means"].append(r["mean_return"])
        grouped[key]["hits"].append(r["hit_rate"])
        grouped[key]["ns"].append(r["n"])
        grouped[key]["regimes"].append(r["regime"])

    print("Signal stability across all evaluation days:\n")
    print(f"  {'verdict':10s} {'horizon':>8s} {'days':>6s} {'avg_n':>7s} "
          f"{'mean_μ':>10s} {'mean_σ':>8s} {'hit%_μ':>8s} {'hit%_σ':>8s} "
          f"{'pct_days_bearish':>18s}")

    for verdict in ("bullish", "bearish", "neutral"):
        for h in HORIZONS:
            g = grouped.get((verdict, h))
            if not g or not g["means"]:
                continue
            mean_of_means = statistics.mean(g["means"])
            std_of_means = statistics.pstdev(g["means"]) if len(g["means"]) > 1 else 0
            mean_of_hits = statistics.mean(g["hits"])
            std_of_hits = statistics.pstdev(g["hits"]) if len(g["hits"]) > 1 else 0
            avg_n = statistics.mean(g["ns"])
            # % of days where mean return was negative (bullish context) or positive (bearish context)
            if verdict == "bullish":
                pct_fail_days = sum(1 for x in g["means"] if x < 0) / len(g["means"]) * 100
                label = f"neg_days%={pct_fail_days:.0f}"
            elif verdict == "bearish":
                pct_fail_days = sum(1 for x in g["means"] if x > 0) / len(g["means"]) * 100
                label = f"pos_days%={pct_fail_days:.0f}"
            else:
                label = "-"

            print(f"  {verdict:10s} T+{h:<6d} {len(g['means']):>6d} {avg_n:>7.0f} "
                  f"{mean_of_means:>+9.3f}% {std_of_means:>7.3f} "
                  f"{mean_of_hits:>7.2f}% {std_of_hits:>7.2f} "
                  f"{label:>18s}")

    # Aggregate significance across ALL samples pooled (not per-day avg)
    # Count total samples and hits by (verdict, horizon) using detail rows
    # We don't have detail here — recompute from daily
    print("\n\nStatistical significance (pooled across all samples, two-sided test vs random 50%):\n")
    pooled_hits = defaultdict(lambda: {"hits": 0, "n": 0})
    for r in daily_rows:
        key = (r["verdict"], r["horizon"])
        n = r["n"]
        hit = int(r["hit_rate"] / 100 * n)
        pooled_hits[key]["hits"] += hit
        pooled_hits[key]["n"] += n

    print(f"  {'verdict':10s} {'horizon':>8s} {'total_n':>8s} {'hit%':>8s} {'sig':>5s}")
    for verdict in ("bullish", "bearish"):
        for h in HORIZONS:
            g = pooled_hits.get((verdict, h))
            if not g or g["n"] == 0:
                continue
            hit_pct = g["hits"] / g["n"] * 100
            sig = _binom_significance(g["hits"], g["n"])
            print(f"  {verdict:10s} T+{h:<6d} {g['n']:>8d} {hit_pct:>7.2f}% {sig:>5s}")
    print("\n  Significance legend: * p<0.10, ** p<0.05, *** p<0.01")

    # Regime breakdown
    print("\n\nBy regime (T+5 only):\n")
    regime_stats = defaultdict(lambda: defaultdict(lambda: {"means": [], "hits": []}))
    for r in daily_rows:
        if r["horizon"] != 5:
            continue
        regime_stats[r["regime"]][r["verdict"]]["means"].append(r["mean_return"])
        regime_stats[r["regime"]][r["verdict"]]["hits"].append(r["hit_rate"])

    for regime in ("rising", "ranging", "falling"):
        rs = regime_stats.get(regime, {})
        if not rs:
            continue
        print(f"\n  regime={regime}:")
        for verdict in ("bullish", "bearish"):
            s = rs.get(verdict)
            if not s or not s["means"]:
                continue
            mean_m = statistics.mean(s["means"])
            mean_h = statistics.mean(s["hits"])
            std_m = statistics.pstdev(s["means"]) if len(s["means"]) > 1 else 0
            print(f"    VPA {verdict:8s} days={len(s['means']):3d}  "
                  f"daily_mean_return={mean_m:+.3f}% (σ={std_m:.2f})  "
                  f"daily_hit%={mean_h:.2f}%")

    print(f"\n{'=' * 120}\n")


if __name__ == "__main__":
    main()
