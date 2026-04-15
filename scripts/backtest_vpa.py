"""VPA Backtest — validate VPA signals against forward returns.

Per sample:
  1. Pick (stock, date) where history ≥60 days before and ≥5 days after
  2. Compute VPA using only data up to `date` (no look-ahead)
  3. Record forward returns T+1, T+2, T+3, T+4, T+5
  4. Tag the sample with regime (market-wide context at sample time)
  5. Optionally ask cheap LLM for a verdict

Analysis:
  - Hit rate + mean return per horizon (T+1..T+5) × per VPA verdict
  - Hit rate per pattern across horizons
  - Signal decay curve (does bearish signal still work at T+5?)
  - Stability across regimes (rising / ranging / falling market)
  - Stability across months
  - LLM vs code verdict agreement + alpha comparison

Usage:
  uv run python scripts/backtest_vpa.py --samples 4000 --seed 42
  uv run python scripts/backtest_vpa.py --samples 500 --use-llm
"""

import argparse
import csv
import json
import logging
import random
import statistics
import sys
from collections import defaultdict
from datetime import datetime
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
from alpha_agents.tools.vpa import _compute_derived, _detect_patterns, _obv_trend, _volume_regime, PATTERN_SCORES

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("backtest_vpa")

HORIZONS = [1, 2, 3, 4, 5]


# ── VPA at a historical date (no look-ahead) ──────────────────

def compute_vpa_at_date(df_all: pd.DataFrame, as_of_date: str, window: int = 20) -> dict | None:
    mask = df_all["date"] <= as_of_date
    df = df_all[mask].copy()
    if len(df) < window + 5:
        return None
    df = _compute_derived(df.reset_index(drop=True), window=window)
    patterns = _detect_patterns(df)
    obv = _obv_trend(df)
    vr = _volume_regime(df)

    net_score = sum(p["strength"] for p in patterns)
    if obv == "上升":
        net_score += 1
    elif obv == "下降":
        net_score -= 1

    if net_score >= 2:
        verdict = "bullish"
    elif net_score <= -4:
        verdict = "bearish"
    else:
        verdict = "neutral"

    return {
        "verdict": verdict,
        "net_score": net_score,
        "obv_trend": obv,
        "volume_regime": vr,
        "patterns": patterns,
    }


def compute_forward_returns(df_all: pd.DataFrame, as_of_date: str, horizons: list[int]) -> dict:
    """Return {horizon: pct_change} or {} if not enough forward data."""
    idx = df_all.index[df_all["date"] == as_of_date].tolist()
    if not idx:
        return {}
    i = idx[0]
    entry = df_all.loc[i, "close"]
    if not entry or entry <= 0:
        return {}
    out = {}
    for h in horizons:
        if i + h >= len(df_all):
            continue
        exit_ = df_all.loc[i + h, "close"]
        out[h] = (exit_ - entry) / entry * 100.0
    return out


# ── Regime tagging ─────────────────────────────────────────────

def build_regime_map(conn) -> dict[str, dict]:
    """For each trading date, compute market-wide 10-day average change as regime proxy.

    Returns {date: {regime, market_avg_10d, month}}.
    """
    rows = conn.execute(
        "SELECT date, AVG(change_pct) AS avg_chg FROM daily_kline GROUP BY date ORDER BY date"
    ).fetchall()
    dates = [r["date"] for r in rows]
    avg_chg = {r["date"]: float(r["avg_chg"] or 0) for r in rows}

    # Rolling 10-day mean of daily market avg change
    window = 10
    regime_map = {}
    for i, date in enumerate(dates):
        if i < window:
            continue
        window_vals = [avg_chg[d] for d in dates[i - window: i]]  # previous 10 days, no look-ahead
        rolling_mean = sum(window_vals) / len(window_vals)

        if rolling_mean > 0.3:
            regime = "rising"     # 升温
        elif rolling_mean < -0.3:
            regime = "falling"    # 调整
        else:
            regime = "ranging"    # 震荡

        month = date[:7]  # YYYY-MM
        regime_map[date] = {
            "regime": regime,
            "market_avg_10d": round(rolling_mean, 3),
            "month": month,
        }
    return regime_map


# ── LLM verdict ────────────────────────────────────────────────

def llm_verdict(vpa_result: dict, code: str) -> tuple[str, float, str]:
    try:
        from openai import OpenAI
        from alpha_agents.config import DIGEST_API_KEY, DIGEST_BASE_URL, DIGEST_MODEL

        if not DIGEST_API_KEY:
            return ("neutral", 0.0, "no_api_key")

        client = OpenAI(api_key=DIGEST_API_KEY, base_url=DIGEST_BASE_URL)

        patterns_text = "\n".join(
            f"- {p['label']} ({p['pattern']}, strength {p['strength']:+d}): {p['detail']}"
            for p in vpa_result.get("patterns", [])
        ) or "无显著模式"

        prompt = f"""基于以下 VPA 量价分析数据，判断股票 {code} 未来 5 日大概率走势。

OBV 10日趋势: {vpa_result['obv_trend']}
量能状态: {vpa_result['volume_regime']}
净评分: {vpa_result['net_score']}

检测到的模式:
{patterns_text}

严格按以下 JSON 格式输出（不要其他内容）:
{{"verdict": "bullish|bearish|neutral", "confidence": 0.0-1.0, "reason": "一句话"}}
"""
        resp = client.chat.completions.create(
            model=DIGEST_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200,
            timeout=30,
        )
        text = resp.choices[0].message.content.strip()
        if text.startswith("```"):
            first_nl = text.find("\n")
            if first_nl != -1:
                text = text[first_nl + 1:]
            if text.endswith("```"):
                text = text[:-3]
        data = json.loads(text.strip())
        return (
            data.get("verdict", "neutral"),
            float(data.get("confidence", 0.5)),
            data.get("reason", ""),
        )
    except Exception as e:
        return ("neutral", 0.0, f"llm_error:{type(e).__name__}")


# ── Sample generation ──────────────────────────────────────────

def generate_samples(conn, n_samples: int, min_history: int, max_horizon: int) -> list[tuple[str, str]]:
    rows = conn.execute(
        "SELECT code, COUNT(*) as n FROM daily_kline GROUP BY code HAVING n >= ?",
        (min_history + max_horizon + 5,),
    ).fetchall()
    eligible_codes = [r["code"] for r in rows]
    if not eligible_codes:
        raise RuntimeError("No codes with enough history. Run init-history first.")
    logger.info("Eligible codes: %d", len(eligible_codes))

    samples: list[tuple[str, str]] = []
    attempts = 0
    max_attempts = n_samples * 10
    while len(samples) < n_samples and attempts < max_attempts:
        attempts += 1
        code = random.choice(eligible_codes)
        dates = conn.execute(
            "SELECT date FROM daily_kline WHERE code = ? ORDER BY date",
            (code,),
        ).fetchall()
        if len(dates) < min_history + max_horizon + 5:
            continue
        valid_start = min_history
        valid_end = len(dates) - max_horizon - 1
        if valid_start >= valid_end:
            continue
        idx = random.randint(valid_start, valid_end)
        samples.append((code, dates[idx]["date"]))

    logger.info("Generated %d samples in %d attempts", len(samples), attempts)
    return samples


def load_code_history(conn, code: str) -> pd.DataFrame | None:
    rows = conn.execute(
        "SELECT * FROM daily_kline WHERE code = ? ORDER BY date",
        (code,),
    ).fetchall()
    if not rows:
        return None
    df = pd.DataFrame([dict(r) for r in rows])
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close", "volume"]).reset_index(drop=True)
    return df if len(df) > 0 else None


# ── Main ──────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Backtest VPA signals against multi-horizon forward returns")
    parser.add_argument("--samples", type=int, default=4000)
    parser.add_argument("--min-history", type=int, default=60)
    parser.add_argument("--use-llm", action="store_true", help="Also evaluate LLM verdict (uses DIGEST_API_KEY)")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=str, default="data/vpa_backtest.csv")
    args = parser.parse_args()

    random.seed(args.seed)
    np.random.seed(args.seed)

    conn = _history_conn()
    regime_map = build_regime_map(conn)
    logger.info("Regime map built for %d dates", len(regime_map))

    max_h = max(HORIZONS)
    samples = generate_samples(conn, args.samples,
                               min_history=args.min_history, max_horizon=max_h)

    history_cache: dict[str, pd.DataFrame] = {}
    results = []

    logger.info("Running backtest on %d samples (horizons=%s)...", len(samples), HORIZONS)
    for i, (code, date) in enumerate(samples):
        if i % 200 == 0:
            logger.info("Progress: %d/%d", i, len(samples))

        df = history_cache.get(code)
        if df is None:
            df = load_code_history(conn, code)
            if df is None:
                continue
            history_cache[code] = df

        vpa = compute_vpa_at_date(df, date)
        if not vpa:
            continue

        fwd = compute_forward_returns(df, date, HORIZONS)
        if not fwd or max(HORIZONS) not in fwd:
            continue

        regime_info = regime_map.get(date, {})

        llm_v, llm_conf, llm_reason = ("", 0.0, "")
        if args.use_llm:
            llm_v, llm_conf, llm_reason = llm_verdict(vpa, code)

        primary_pattern = ""
        if vpa["patterns"]:
            primary_pattern = max(vpa["patterns"], key=lambda p: abs(p["strength"]))["pattern"]

        row = {
            "code": code,
            "date": date,
            "month": regime_info.get("month", ""),
            "regime": regime_info.get("regime", "unknown"),
            "market_avg_10d": regime_info.get("market_avg_10d", 0.0),
            "vpa_verdict": vpa["verdict"],
            "vpa_net_score": vpa["net_score"],
            "obv_trend": vpa["obv_trend"],
            "volume_regime": vpa["volume_regime"]["regime"],
            "primary_pattern": primary_pattern,
            "n_patterns": len(vpa["patterns"]),
            "llm_verdict": llm_v,
            "llm_confidence": llm_conf,
            "llm_reason": llm_reason,
        }
        for h in HORIZONS:
            row[f"t{h}_return"] = fwd.get(h)
        results.append(row)

    logger.info("Collected %d valid samples", len(results))

    # Save CSV
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if results:
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(results[0].keys()))
            w.writeheader()
            w.writerows(results)
        logger.info("Saved results to %s", out_path)

    print_summary(results)


# ── Analysis ───────────────────────────────────────────────────

def _pattern_direction(pattern: str) -> str:
    score = PATTERN_SCORES.get(pattern, 0)
    return "bullish" if score > 0 else ("bearish" if score < 0 else "neutral")


def _horizon_stats(rows: list[dict], horizon: int, direction_source: str) -> dict | None:
    """Compute mean, hit rate, win rate for a given horizon + direction source."""
    fwds = []
    hit = 0
    valid = 0
    for r in rows:
        v = r.get(f"t{horizon}_return")
        if v is None:
            continue
        fwds.append(v)
        if direction_source == "vpa":
            expected = r.get("vpa_verdict", "")
        elif direction_source == "llm":
            expected = r.get("llm_verdict", "")
        elif direction_source == "pattern":
            expected = _pattern_direction(r.get("primary_pattern", ""))
        else:
            expected = ""
        if not expected:
            continue
        valid += 1
        if expected == "bullish" and v > 0:
            hit += 1
        elif expected == "bearish" and v < 0:
            hit += 1
        elif expected == "neutral" and abs(v) < 2:
            hit += 1
    if not fwds:
        return None
    mean = statistics.mean(fwds)
    std = statistics.pstdev(fwds) if len(fwds) > 1 else 0.0
    win = sum(1 for x in fwds if x > 0) / len(fwds) * 100
    hit_rate = hit / valid * 100 if valid > 0 else None
    sharpe = mean / std if std > 0 else 0.0
    return {
        "N": len(fwds),
        "mean": mean,
        "std": std,
        "win%": win,
        "hit%": hit_rate,
        "sharpe": sharpe,
    }


def _print_horizon_row(label: str, rows: list[dict], direction_source: str) -> None:
    cells = [f"{label:28s}"]
    total_n = None
    for h in HORIZONS:
        s = _horizon_stats(rows, h, direction_source)
        if s is None:
            cells.append(f" T+{h}: N/A".ljust(24))
            continue
        if total_n is None:
            total_n = s["N"]
        hit_str = f"hit{s['hit%']:5.1f}%" if s["hit%"] is not None else "hit  -  "
        cells.append(f" T+{h}: mean{s['mean']:+5.2f}% {hit_str}")
    print("  " + " ".join(cells))


def print_summary(results: list[dict]) -> None:
    if not results:
        print("No results.")
        return

    total = len(results)
    print(f"\n{'=' * 120}")
    print(f"VPA Multi-Horizon Backtest — {total} samples, horizons={HORIZONS}")
    date_range = f"{min(r['date'] for r in results)} → {max(r['date'] for r in results)}"
    print(f"Date range: {date_range}")
    print(f"{'=' * 120}")

    # ── Baseline ──
    print("\n── Baseline (all samples, market-wide drift per horizon) ──")
    _print_horizon_row("ALL", results, "none")

    # ── By VPA verdict ──
    print("\n── By VPA (code) verdict: signal decay across horizons ──")
    for verdict in ("bullish", "neutral", "bearish"):
        rows = [r for r in results if r["vpa_verdict"] == verdict]
        _print_horizon_row(f"VPA {verdict} (N={len(rows)})", rows, "vpa")

    # ── By primary pattern ──
    print("\n── By primary VPA pattern (directional hit%) ──")
    by_pattern = defaultdict(list)
    for r in results:
        if r["primary_pattern"]:
            by_pattern[r["primary_pattern"]].append(r)
    for pattern, rows in sorted(by_pattern.items(), key=lambda kv: -len(kv[1])):
        if len(rows) < 10:
            continue  # skip low-sample patterns
        _print_horizon_row(f"{pattern} (N={len(rows)})", rows, "pattern")

    # ── By net_score bucket ──
    print("\n── By net_score bucket: is a stronger signal more predictive? ──")
    buckets = [
        (float("-inf"), -3, "score<-3 very_bearish"),
        (-3, -1, "score[-3,-1) bearish"),
        (-1, 2, "score[-1,2) neutral"),
        (2, 4, "score[2,4) bullish"),
        (4, float("inf"), "score>=4 very_bullish"),
    ]
    for lo, hi, label in buckets:
        rows = [r for r in results if lo <= r["vpa_net_score"] < hi]
        if not rows:
            continue
        _print_horizon_row(f"{label} (N={len(rows)})", rows, "none")

    # ── By regime ──
    print("\n── By market regime (10d rolling avg change at sample time) ──")
    for regime in ("rising", "ranging", "falling", "unknown"):
        rows = [r for r in results if r["regime"] == regime]
        if not rows:
            continue
        print(f"\n  regime={regime} (N={len(rows)}):")
        for verdict in ("bullish", "bearish"):
            sub = [r for r in rows if r["vpa_verdict"] == verdict]
            if len(sub) < 10:
                continue
            _print_horizon_row(f"    VPA {verdict} (N={len(sub)})", sub, "vpa")

    # ── By month ──
    print("\n── By month: signal stability over time ──")
    months = sorted(set(r["month"] for r in results if r["month"]))
    for month in months:
        rows = [r for r in results if r["month"] == month]
        if len(rows) < 30:
            continue
        print(f"\n  month={month} (N={len(rows)}):")
        for verdict in ("bullish", "bearish"):
            sub = [r for r in rows if r["vpa_verdict"] == verdict]
            if len(sub) < 10:
                continue
            _print_horizon_row(f"    VPA {verdict} (N={len(sub)})", sub, "vpa")

    # ── LLM comparison ──
    llm_used = any(r["llm_verdict"] for r in results)
    if llm_used:
        print("\n── LLM verdict performance ──")
        for verdict in ("bullish", "neutral", "bearish"):
            rows = [r for r in results if r["llm_verdict"] == verdict]
            if len(rows) < 10:
                continue
            _print_horizon_row(f"LLM {verdict} (N={len(rows)})", rows, "llm")

        # Agreement analysis
        agree = sum(1 for r in results if r["vpa_verdict"] == r["llm_verdict"])
        print(f"\n  VPA-LLM verdict agreement: {agree}/{total} ({agree/total*100:.1f}%)")

        # When VPA and LLM both say bullish → how does it perform vs VPA-only?
        print("\n  When VPA+LLM both agree, T+5 performance:")
        for verdict in ("bullish", "bearish"):
            both = [r for r in results if r["vpa_verdict"] == verdict and r["llm_verdict"] == verdict]
            if len(both) < 10:
                continue
            t5 = [r["t5_return"] for r in both if r.get("t5_return") is not None]
            if t5:
                hit = sum(1 for x in t5 if (x > 0 if verdict == "bullish" else x < 0))
                print(f"    BOTH {verdict:7s}: N={len(t5)}  mean={statistics.mean(t5):+.2f}%  "
                      f"hit%={hit/len(t5)*100:.1f}%")

    print(f"\n{'=' * 120}\n")


if __name__ == "__main__":
    main()
