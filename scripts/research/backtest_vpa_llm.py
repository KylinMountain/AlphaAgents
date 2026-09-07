"""VPA LLM Walk-Forward Backtest — identical to production compute_vpa_with_llm.

Uses the same code path as production (`compute_vpa_with_llm`) so the measured
LLM signal quality reflects what the live system would produce. Key alignment:
  - Same prompt (Anna Coulling) via `_call_llm_vpa`
  - Same pre-computation (`_compute_derived` + `_detect_patterns` + OBV + vol regime)
  - Same `_format_text` output fed to the LLM
  - Same continuity: previous analysis injected into the prompt

Backtest-specific adjustments (do not change LLM behaviour):
  - `skip_save=True` — avoid polluting production memory_store
  - `previous_analysis_override` — in-memory per-code latest report, reproducing
    production continuity without the shared DB write
  - `as_of=date` — point-in-time data slice (no future leakage)

Usage:
  uv run python scripts/backtest_vpa_llm.py --stocks-per-day 20 --max-days 10
  uv run python scripts/backtest_vpa_llm.py --stocks-per-day 50 --max-days 20 --workers 3
"""

import argparse
import csv
import json
import logging
import random
import statistics
import sys
import time
from collections import Counter, defaultdict
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
from alpha_agents.tools.vpa import compute_vpa_with_llm

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("backtest_vpa_llm")

HORIZONS = [1, 2, 3, 4, 5]
WINDOW = 20
MIN_HISTORY = 60

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


def _forward_returns(df, idx):
    """Compute T+1..T+5 returns from close at idx."""
    entry = df.iloc[idx]["close"]
    return {
        h: (df.iloc[idx + h]["close"] - entry) / entry * 100.0
        for h in HORIZONS
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stocks-per-day", type=int, default=20)
    parser.add_argument("--max-days", type=int, default=None)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=str, default="data/vpa_llm_walkforward.csv")
    parser.add_argument("--workers", type=int, default=3, help="Parallel LLM call workers")
    parser.add_argument("--codes", type=str, default=None, help="Comma-separated stock codes (overrides random sample)")
    parser.add_argument("--start-date", type=str, default=None, help="Eval window start (YYYY-MM-DD). Filters eval_dates >= this.")
    args = parser.parse_args()

    random.seed(args.seed)
    np.random.seed(args.seed)

    # Eager-init V8 on main thread. py_mini_racer (used by akshare internals)
    # crashes if the V8 address pool is initialized concurrently from multiple
    # threads — pre-warming here guarantees main-thread init before any
    # ThreadPoolExecutor workers try to touch it via transitive imports.
    try:
        import py_mini_racer
        _v8_warmup = py_mini_racer.MiniRacer()
        _v8_warmup.eval("1+1")
        logger.info("py_mini_racer V8 pre-warmed on main thread")
    except Exception as e:
        logger.warning("py_mini_racer pre-warm failed (non-fatal): %s", e)

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
    # Drop dates with <80% market coverage (data fetch failures, not holidays).
    coverage = {d: sum(1 for df in histories.values() if d in df["date"].values) for d in eval_dates}
    threshold = int(0.8 * len(histories))
    bad_dates = [d for d, n in coverage.items() if n < threshold]
    if bad_dates:
        logger.warning("Dropping %d low-coverage dates: %s", len(bad_dates), bad_dates)
        eval_dates = [d for d in eval_dates if coverage[d] >= threshold]
    if args.start_date:
        eval_dates = [d for d in eval_dates if d >= args.start_date]
    if args.max_days:
        eval_dates = eval_dates[:args.max_days]
    logger.info("Eval dates: %d (%s → %s)", len(eval_dates),
                eval_dates[0], eval_dates[-1])

    # JSONL sidecar — preserves the FULL result dict (selected_climax, all v7
    # confirmation fields, etc.) that the CSV truncates away. Plot scripts and
    # downstream analyses read JSONL when they need richer than CSV provides.
    jsonl_path = Path(str(args.out).replace(".csv", ".jsonl")) if str(args.out).endswith(".csv") else Path(str(args.out) + ".jsonl")
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    jsonl_fh = open(jsonl_path, "w", encoding="utf-8")
    logger.info("JSONL sidecar → %s", jsonl_path)

    # Per-code last-report cache — replaces memory_store writes in backtest,
    # preserving the continuity that production's `previous_analysis` gives.
    # For continuity to actually matter, we test the SAME stock pool across
    # every eval date (rather than re-sampling per day), so each stock
    # accumulates a report history that flows into the next day's prompt.
    prev_report_by_code: dict[str, str] = {}

    pool_eligible = [
        c for c in all_codes
        if all(
            date in histories[c]["date"].values
            and histories[c].index[histories[c]["date"] == date][0] >= MIN_HISTORY
            and histories[c].index[histories[c]["date"] == date][0] + max_h < len(histories[c])
            for date in eval_dates
        )
    ]
    if not pool_eligible:
        logger.error("No stock has full coverage across all %d eval dates", len(eval_dates))
        return
    if args.codes:
        requested = [c.strip() for c in args.codes.split(",") if c.strip()]
        missing = [c for c in requested if c not in pool_eligible]
        if missing:
            logger.error("Codes missing full coverage: %s", missing)
            return
        stock_pool = requested
    else:
        stock_pool = random.sample(pool_eligible, min(args.stocks_per_day, len(pool_eligible)))
    logger.info("Fixed stock pool: %d stocks across %d dates = %d total calls",
                len(stock_pool), len(eval_dates), len(stock_pool) * len(eval_dates))

    results = []
    total_llm_calls = 0
    start_time = time.time()

    for di, date in enumerate(eval_dates):
        sample = stock_pool

        tasks = []
        for code in sample:
            df = histories[code]
            idx = df.index[df["date"] == date][0]
            fwds = _forward_returns(df, idx)
            tasks.append((code, fwds))

        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _do_llm(item):
            code, fwds = item
            prev = prev_report_by_code.get(code, "")
            result = compute_vpa_with_llm(
                code=code, name="",
                as_of=date,
                skip_save=True,
                previous_analysis_override=prev,
            )
            return code, result, fwds

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = [pool.submit(_do_llm, t) for t in tasks]
            for fut in as_completed(futures):
                try:
                    code, result, fwds = fut.result()
                except Exception as e:
                    logger.warning("LLM call failed: %s", e)
                    continue
                if not result.get("ok"):
                    logger.debug("skipping %s: %s", code, result.get("error"))
                    continue
                total_llm_calls += 1

                raw = result.get("llm_verdict", "中性")
                direction = VERDICT_MAP.get(raw, "neutral")
                report = result.get("llm_report", "")
                # Update continuity cache for this code
                if report:
                    prev_report_by_code[code] = report

                # JSONL: full result dict preserves v7 fields (action_confirmed,
                # confirmation_level/tier, selected_climax, signal counts, etc.)
                jsonl_fh.write(json.dumps({"date": date, "code": code, "result": result}, ensure_ascii=False) + "\n")
                jsonl_fh.flush()

                results.append({
                    "date": date,
                    "code": code,
                    "regime": regime_map.get(date, "unknown"),
                    "llm_verdict": direction,
                    "llm_raw_verdict": raw,
                    "llm_confidence": result.get("llm_confidence", 0.5),
                    "llm_phase": result.get("llm_phase", ""),
                    "llm_raw_phase": result.get("llm_raw_phase", result.get("llm_phase", "")),
                    "llm_warning_phase": result.get("llm_warning_phase", ""),
                    "llm_phase_guard_reason": result.get("llm_phase_guard_reason", ""),
                    "llm_confirmed": result.get("llm_confirmed", False),
                    "llm_reason": result.get("llm_reason", ""),
                    "llm_report": report[:500],
                    **{f"t{h}_return": round(fwds[h], 3) for h in HORIZONS},
                })

        elapsed = time.time() - start_time
        rate = total_llm_calls / elapsed if elapsed > 0 else 0
        logger.info("Day %d/%d (%s) — %d LLM calls total (%.1f calls/sec)",
                     di + 1, len(eval_dates), date, total_llm_calls, rate)

    jsonl_fh.close()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if results:
        with open(out, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(results[0].keys()))
            w.writeheader()
            w.writerows(results)
        logger.info("Saved %d rows to %s + %s", len(results), out, jsonl_path)

    print_summary(results)


def _hit_stats(rows, h):
    """Return (hit_rate_pct, mean_return_pct, n)."""
    vals = [(r[f"t{h}_return"], r["llm_verdict"]) for r in rows if r.get(f"t{h}_return") is not None]
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


def print_summary(results):
    if not results:
        print("No results.")
        return

    total = len(results)
    print(f"\n{'=' * 120}")
    print(f"VPA LLM Walk-Forward (production-aligned) — {total} samples")
    print(f"Date range: {min(r['date'] for r in results)} → {max(r['date'] for r in results)}")
    print(f"{'=' * 120}")

    # ── Per-verdict × horizon ──
    print("\n── LLM verdict × T+1..T+5 (hit% / mean%) ──\n")
    print(f"  {'verdict':10s} {'N':>6s}   T+1              T+2              T+3              T+4              T+5")
    for verdict in ("bullish", "bearish", "neutral"):
        rows = [r for r in results if r["llm_verdict"] == verdict]
        if not rows:
            continue
        cells = [f"  {verdict:10s} {len(rows):>6d}"]
        for h in HORIZONS:
            hit_pct, mean, _ = _hit_stats(rows, h)
            if hit_pct is None:
                cells.append("   N/A          ")
            else:
                cells.append(f"  {hit_pct:5.1f}%/{mean:+5.2f}%  ")
        print("".join(cells))

    # ── Raw verdict distribution ──
    print("\n── LLM raw verdict distribution ──")
    dist = Counter(r["llm_raw_verdict"] for r in results)
    for v, n in dist.most_common():
        print(f"  {v}: {n} ({n / total * 100:.1f}%)")

    # ── Phase distribution ──
    print("\n── LLM Wyckoff phase distribution ──")
    dist2 = Counter(r["llm_phase"] or "(empty)" for r in results)
    for v, n in dist2.most_common():
        print(f"  {v}: {n} ({n / total * 100:.1f}%)")

    # ── By regime ──
    print("\n── By regime × verdict (T+1 and T+5) ──")
    for regime in ("rising", "ranging", "falling"):
        sub = [r for r in results if r["regime"] == regime]
        if not sub:
            continue
        print(f"\n  regime={regime} (N={len(sub)}):")
        for verdict in ("bullish", "bearish", "neutral"):
            rows = [r for r in sub if r["llm_verdict"] == verdict]
            if len(rows) < 5:
                continue
            h1, m1, _ = _hit_stats(rows, 1)
            h5, m5, _ = _hit_stats(rows, 5)
            print(f"    {verdict:8s} N={len(rows):4d}  "
                  f"T+1: hit={h1:.1f}% mean={m1:+.2f}%  "
                  f"T+5: hit={h5:.1f}% mean={m5:+.2f}%")

    # ── Confirmed subset ──
    print("\n── Confirmed subset (LLM said 'confirmed': true) ──")
    confirmed = [r for r in results if r["llm_confirmed"]]
    print(f"  Total confirmed: {len(confirmed)}/{total} ({len(confirmed) / total * 100:.1f}%)")
    for verdict in ("bullish", "bearish"):
        rows = [r for r in confirmed if r["llm_verdict"] == verdict]
        if len(rows) < 3:
            continue
        cells = [f"  {verdict:8s} confirmed N={len(rows):4d}"]
        for h in HORIZONS:
            hit_pct, mean, _ = _hit_stats(rows, h)
            if hit_pct is not None:
                cells.append(f"  T+{h}: {hit_pct:.1f}%/{mean:+.2f}%")
        print("".join(cells))

    print(f"\n{'=' * 120}\n")


if __name__ == "__main__":
    main()
