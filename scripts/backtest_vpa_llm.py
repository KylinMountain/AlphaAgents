"""VPA LLM Walk-Forward Backtest — Anna Coulling prompt + cheap LLM verdict.

For each evaluation day, sample N stocks, run code pre-computation +
LLM interpretation with Anna Coulling prompt, then measure T+1..T+5.

Compare:
  - LLM verdict (看多/偏多/中性/偏空/看空) vs actual returns
  - Code-only verdict vs actual returns (baseline)
  - Per-horizon hit rates

Usage:
  uv run python scripts/backtest_vpa_llm.py --stocks-per-day 100
  uv run python scripts/backtest_vpa_llm.py --stocks-per-day 50 --max-days 20
"""

import argparse
import csv
import json
import logging
import random
import statistics
import sys
import time
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
    _compute_derived, _detect_patterns, _obv_trend, _volume_regime,
    _format_text, _call_llm_vpa, PATTERN_SCORES,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("backtest_vpa_llm")

HORIZONS = [1, 2, 3, 4, 5]
WINDOW = 20
MIN_HISTORY = 60

# Map Chinese verdict to direction
VERDICT_MAP = {
    "看多": "bullish", "偏多": "bullish",
    "看空": "bearish", "偏空": "bearish",
    "中性": "neutral",
}


def load_all_histories(conn, max_stocks=None):
    rows = conn.execute(
        "SELECT code, COUNT(*) n FROM daily_kline GROUP BY code HAVING n >= ? ORDER BY code",
        (MIN_HISTORY + max(HORIZONS) + 5,),
    ).fetchall()
    codes = [r["code"] for r in rows]
    if max_stocks:
        codes = codes[:max_stocks]

    histories = {}
    for code in codes:
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
    return histories


def build_regime_map(conn):
    rows = conn.execute(
        "SELECT date, AVG(change_pct) AS avg_chg FROM daily_kline GROUP BY date ORDER BY date"
    ).fetchall()
    dates = [r["date"] for r in rows]
    avg_chg = {r["date"]: float(r["avg_chg"] or 0) for r in rows}
    regime_map = {}
    for i, date in enumerate(dates):
        if i < 10:
            regime_map[date] = "unknown"
            continue
        vals = [avg_chg[d] for d in dates[i - 10: i]]
        m = sum(vals) / len(vals)
        regime_map[date] = "rising" if m > 0.3 else ("falling" if m < -0.3 else "ranging")
    return regime_map


def compute_code_verdict(df_slice):
    """Code-only verdict (baseline)."""
    df = _compute_derived(df_slice.reset_index(drop=True), window=WINDOW)
    patterns = _detect_patterns(df)
    obv = _obv_trend(df)
    net = sum(p["strength"] for p in patterns)
    if obv == "上升":
        net += 1
    elif obv == "下降":
        net -= 1
    if net >= 2:
        return "bullish"
    elif net <= -4:
        return "bearish"
    return "neutral"


def compute_llm_verdict(code, df_slice):
    """LLM verdict using full Anna Coulling prompt — complete report + VERDICT extraction."""
    df = _compute_derived(df_slice.reset_index(drop=True), window=WINDOW)
    patterns = _detect_patterns(df)
    text = _format_text(code, "", df, patterns, window=WINDOW)
    result = _call_llm_vpa(code, text)
    # _call_llm_vpa now returns full report + extracted verdict from <!-- VERDICT: --> tag
    raw_v = result.get("verdict", "中性")
    direction = VERDICT_MAP.get(raw_v, "neutral")
    report = result.get("report", "")
    return direction, result.get("confidence", 0.5), result.get("phase", ""), result.get("reason", ""), raw_v, report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stocks-per-day", type=int, default=100)
    parser.add_argument("--max-days", type=int, default=None)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=str, default="data/vpa_llm_walkforward.csv")
    parser.add_argument("--workers", type=int, default=10, help="Parallel LLM call workers")
    args = parser.parse_args()

    random.seed(args.seed)
    np.random.seed(args.seed)

    conn = _history_conn()
    regime_map = build_regime_map(conn)
    histories = load_all_histories(conn)
    all_codes = list(histories.keys())
    logger.info("Loaded %d stock histories", len(all_codes))

    all_dates = sorted(set(
        d for df in histories.values() for d in df["date"].tolist()
    ))
    max_h = max(HORIZONS)
    eval_dates = all_dates[MIN_HISTORY: len(all_dates) - max_h]
    if args.max_days:
        eval_dates = eval_dates[:args.max_days]
    logger.info("Eval dates: %d (%s → %s)", len(eval_dates),
                eval_dates[0], eval_dates[-1])

    results = []
    total_llm_calls = 0
    start_time = time.time()

    for di, date in enumerate(eval_dates):
        # Sample N stocks for this day
        eligible = [c for c in all_codes
                    if date in histories[c]["date"].values
                    and histories[c].index[histories[c]["date"] == date][0] >= MIN_HISTORY
                    and histories[c].index[histories[c]["date"] == date][0] + max_h < len(histories[c])]

        if not eligible:
            continue

        sample = random.sample(eligible, min(args.stocks_per_day, len(eligible)))

        # Pre-compute all slices and code verdicts (fast, no API)
        tasks = []
        for code in sample:
            df = histories[code]
            idx = df.index[df["date"] == date][0]
            df_slice = df.iloc[: idx + 1]
            code_v = compute_code_verdict(df_slice)
            entry = df.iloc[idx]["close"]
            fwds = {}
            for h in HORIZONS:
                exit_c = df.iloc[idx + h]["close"]
                fwds[h] = (exit_c - entry) / entry * 100.0
            tasks.append((code, df_slice, code_v, fwds))

        # Parallel LLM calls
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _do_llm(item):
            code, df_slice, code_v, fwds = item
            llm_v, llm_conf, llm_phase, llm_reason, llm_raw, report = compute_llm_verdict(code, df_slice)
            return code, code_v, llm_v, llm_conf, llm_phase, llm_reason, llm_raw, report, fwds

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = [pool.submit(_do_llm, t) for t in tasks]
            for fut in as_completed(futures):
                try:
                    code, code_v, llm_v, llm_conf, llm_phase, llm_reason, llm_raw, report, fwds = fut.result()
                except Exception as e:
                    logger.warning("LLM call failed: %s", e)
                    continue
                total_llm_calls += 1

                results.append({
                    "date": date,
                    "code": code,
                    "regime": regime_map.get(date, "unknown"),
                    "code_verdict": code_v,
                    "llm_verdict": llm_v,
                    "llm_raw_verdict": llm_raw,
                    "llm_confidence": llm_conf,
                    "llm_phase": llm_phase,
                    "llm_reason": llm_reason,
                    "llm_report": report[:500],  # Truncate for CSV (full reports too large)
                    **{f"t{h}_return": round(fwds[h], 3) for h in HORIZONS},
                })

        elapsed = time.time() - start_time
        rate = total_llm_calls / elapsed if elapsed > 0 else 0
        logger.info("Day %d/%d (%s) — %d LLM calls total (%.1f calls/sec)",
                     di + 1, len(eval_dates), date, total_llm_calls, rate)

    # Save
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if results:
        with open(out, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(results[0].keys()))
            w.writeheader()
            w.writerows(results)
        logger.info("Saved %d rows to %s", len(results), out)

    print_summary(results)


def print_summary(results):
    if not results:
        print("No results.")
        return

    total = len(results)
    print(f"\n{'=' * 120}")
    print(f"VPA LLM Walk-Forward — {total} samples, Anna Coulling prompt")
    print(f"Date range: {min(r['date'] for r in results)} → {max(r['date'] for r in results)}")
    print(f"{'=' * 120}")

    def _hit(rows, verdict_key, h):
        vals = [(r[f"t{h}_return"], r[verdict_key]) for r in rows if r.get(f"t{h}_return") is not None]
        if not vals:
            return None, None, 0
        hit = 0
        for ret, v in vals:
            if v == "bullish" and ret > 0:
                hit += 1
            elif v == "bearish" and ret < 0:
                hit += 1
            elif v == "neutral" and abs(ret) < 2:
                hit += 1
        n = len(vals)
        mean = statistics.mean([r for r, _ in vals])
        return hit / n * 100, mean, n

    # ── Code verdict vs LLM verdict ──
    print("\n── Code verdict (baseline) vs LLM verdict (Anna Coulling) ──\n")
    print(f"  {'source':12s} {'verdict':10s} {'N':>6s}   T+1        T+2        T+3        T+4        T+5")

    for source, key in [("CODE", "code_verdict"), ("LLM", "llm_verdict")]:
        for verdict in ("bullish", "bearish", "neutral"):
            rows = [r for r in results if r[key] == verdict]
            if not rows:
                continue
            cells = [f"  {source:12s} {verdict:10s} {len(rows):>6d}"]
            for h in HORIZONS:
                hit_pct, mean, n = _hit(rows, key, h)
                if hit_pct is None:
                    cells.append("   N/A   ")
                else:
                    cells.append(f"  {hit_pct:5.1f}%/{mean:+5.2f}%")
            print("".join(cells))

    # ── Agreement ──
    agree = sum(1 for r in results if r["code_verdict"] == r["llm_verdict"])
    print(f"\n  Code-LLM agreement: {agree}/{total} ({agree/total*100:.1f}%)")

    # ── LLM verdict distribution ──
    print("\n── LLM raw verdict distribution ──")
    from collections import Counter
    dist = Counter(r["llm_raw_verdict"] for r in results)
    for v, n in dist.most_common():
        print(f"  {v}: {n} ({n/total*100:.1f}%)")

    # ── LLM phase distribution ──
    print("\n── LLM Wyckoff phase distribution ──")
    dist2 = Counter(r["llm_phase"] for r in results)
    for v, n in dist2.most_common():
        print(f"  {v}: {n} ({n/total*100:.1f}%)")

    # ── By regime ──
    print("\n── By regime (T+1 and T+5, LLM verdict) ──")
    for regime in ("rising", "ranging", "falling"):
        sub = [r for r in results if r["regime"] == regime]
        if not sub:
            continue
        print(f"\n  regime={regime} (N={len(sub)}):")
        for verdict in ("bullish", "bearish"):
            rows = [r for r in sub if r["llm_verdict"] == verdict]
            if len(rows) < 5:
                continue
            h1, m1, n1 = _hit(rows, "llm_verdict", 1)
            h5, m5, n5 = _hit(rows, "llm_verdict", 5)
            print(f"    LLM {verdict:8s} N={len(rows):4d}  "
                  f"T+1: hit={h1:.1f}% mean={m1:+.2f}%  "
                  f"T+5: hit={h5:.1f}% mean={m5:+.2f}%")

    # ── High confidence subset ──
    print("\n── High confidence LLM verdicts (confidence >= 0.7) ──")
    high_conf = [r for r in results if r["llm_confidence"] >= 0.7]
    print(f"  Total high-confidence: {len(high_conf)}/{total} ({len(high_conf)/total*100:.1f}%)")
    for verdict in ("bullish", "bearish"):
        rows = [r for r in high_conf if r["llm_verdict"] == verdict]
        if len(rows) < 5:
            continue
        cells = [f"  LLM {verdict:8s} (conf≥0.7) N={len(rows):4d}"]
        for h in HORIZONS:
            hit_pct, mean, _ = _hit(rows, "llm_verdict", h)
            if hit_pct is not None:
                cells.append(f"  T+{h}: {hit_pct:.1f}%/{mean:+.2f}%")
        print("".join(cells))

    print(f"\n{'=' * 120}\n")


if __name__ == "__main__":
    main()
