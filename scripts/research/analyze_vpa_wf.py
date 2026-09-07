"""Post-hoc analysis of walk-forward backtest CSV.

Reads data/vpa_wf_full.csv and prints:
  1. Daily time series (sample output for plotting)
  2. Best/worst days for each verdict
  3. Days where bearish hit > 50% counted
  4. Consistency metric: % of days where a signal is correct
  5. Pattern stability across regime

Usage:
  uv run python scripts/analyze_vpa_wf.py [--csv data/vpa_wf_full.csv]
"""

import argparse
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent


def binom_sig(hits: int, n: int) -> str:
    if n < 20:
        return ""
    p = hits / n
    se = math.sqrt(0.5 * 0.5 / n)
    z = (p - 0.5) / se if se > 0 else 0
    az = abs(z)
    if az >= 2.576: return "***"
    if az >= 1.96: return "**"
    if az >= 1.645: return "*"
    return ""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", type=str, default=str(REPO_ROOT / "data/vpa_wf_full.csv"))
    args = parser.parse_args()

    df = pd.read_csv(args.csv)
    print(f"Loaded {len(df)} aggregated rows from {args.csv}\n")
    print(f"Date range: {df['date'].min()} → {df['date'].max()}")
    print(f"Evaluation days: {df['date'].nunique()}")
    print(f"Regimes seen: {df['regime'].value_counts().to_dict()}\n")

    # ── 1. Daily sample size ──
    print(f"{'='*100}")
    print("1. Daily sample size (avg across days)")
    print(f"{'='*100}")
    sample = df.groupby(["verdict", "horizon"])["n"].agg(["mean", "median", "min", "max"])
    print(sample.round(1))

    # ── 2. Pooled hit rates + significance ──
    print(f"\n{'='*100}")
    print("2. Pooled hit rates (all samples across all days, significance vs random 50%)")
    print(f"{'='*100}")
    pooled = defaultdict(lambda: {"hits": 0, "n": 0})
    for _, r in df.iterrows():
        k = (r["verdict"], r["horizon"])
        n = int(r["n"])
        hits = int(r["hit_rate"] / 100 * n)
        pooled[k]["hits"] += hits
        pooled[k]["n"] += n

    print(f"  {'verdict':10s} {'horizon':>8s} {'total_n':>10s} {'hit%':>8s}  sig")
    for verdict in ("bullish", "bearish"):
        for h in sorted(df["horizon"].unique()):
            g = pooled.get((verdict, h))
            if not g: continue
            hit_pct = g["hits"] / g["n"] * 100
            print(f"  {verdict:10s} T+{h:<6d} {g['n']:>10d} {hit_pct:>7.2f}%  {binom_sig(g['hits'], g['n'])}")

    # ── 3. Day-level consistency ──
    print(f"\n{'='*100}")
    print("3. Day-level consistency: how many days does each signal WORK?")
    print(f"{'='*100}")
    for verdict in ("bullish", "bearish"):
        for h in sorted(df["horizon"].unique()):
            sub = df[(df["verdict"] == verdict) & (df["horizon"] == h)]
            if len(sub) < 5: continue
            if verdict == "bullish":
                working_days = (sub["mean_return"] > 0).sum()
                hit_days = (sub["hit_rate"] > 50).sum()
            else:
                working_days = (sub["mean_return"] < 0).sum()
                hit_days = (sub["hit_rate"] > 50).sum()
            total = len(sub)
            print(f"  VPA {verdict:8s} T+{h}: {working_days}/{total} days ({working_days/total*100:.0f}%) profitable, "
                  f"{hit_days}/{total} days ({hit_days/total*100:.0f}%) with hit>50%")

    # ── 4. Best/worst days ──
    print(f"\n{'='*100}")
    print("4. Best/worst days for each verdict (T+5 only)")
    print(f"{'='*100}")
    for verdict in ("bullish", "bearish"):
        sub = df[(df["verdict"] == verdict) & (df["horizon"] == 5)].sort_values("mean_return")
        if sub.empty: continue
        print(f"\n  VPA {verdict} — worst 3 days:")
        for _, r in sub.head(3).iterrows():
            print(f"    {r['date']} (regime={r['regime']}) N={r['n']} mean={r['mean_return']:+.2f}%  hit={r['hit_rate']}%")
        print(f"  VPA {verdict} — best 3 days:")
        for _, r in sub.tail(3).iterrows():
            print(f"    {r['date']} (regime={r['regime']}) N={r['n']} mean={r['mean_return']:+.2f}%  hit={r['hit_rate']}%")

    # ── 5. Regime breakdown ──
    print(f"\n{'='*100}")
    print("5. By regime (T+5 only) — daily-mean statistics with pooled significance")
    print(f"{'='*100}")
    for regime in ("rising", "ranging", "falling"):
        for verdict in ("bullish", "bearish"):
            sub = df[(df["regime"] == regime) & (df["verdict"] == verdict) & (df["horizon"] == 5)]
            if sub.empty: continue
            daily_means = sub["mean_return"].tolist()
            mean_m = statistics.mean(daily_means)
            std_m = statistics.pstdev(daily_means) if len(daily_means) > 1 else 0
            # Pooled hit
            tot_n = int(sub["n"].sum())
            tot_hits = int((sub["hit_rate"] / 100 * sub["n"]).sum())
            hit_pct = tot_hits / tot_n * 100 if tot_n > 0 else 0
            sig = binom_sig(tot_hits, tot_n)
            print(f"  regime={regime:8s} VPA {verdict:8s} days={len(sub):3d}  "
                  f"daily_mean={mean_m:+.3f}% (σ={std_m:.2f})  pooled_hit={hit_pct:.2f}% {sig}  "
                  f"samples={tot_n}")

    # ── 6. Time series export ──
    print(f"\n{'='*100}")
    print("6. T+5 bearish hit rate time series (can be plotted):")
    print(f"{'='*100}")
    ts = df[(df["verdict"] == "bearish") & (df["horizon"] == 5)].sort_values("date")
    for _, r in ts.iterrows():
        bar = "█" * int(r["hit_rate"] / 2)
        print(f"  {r['date']} ({r['regime']:7s}) hit={r['hit_rate']:5.1f}% n={r['n']:4d}  {bar}")

    print()


if __name__ == "__main__":
    main()
