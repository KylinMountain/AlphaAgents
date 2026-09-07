"""Signal-timing analytics: when does each BUY gate fire relative to
the actual price action, and what are the forward returns?

For each BUY event (strict_c2 or trade_setup_actionable), records:
  - days_since_20d_low — how far past the swing low are we
  - days_since_20d_high — distance from the most recent 20-day high
  - pct_from_20d_low / pct_from_20d_high — chase or fresh
  - fwd_5d / fwd_10d / fwd_20d return from the entry bar's close

Then aggregates per gate:
  - mean timing lag
  - mean forward returns
  - hit rate (fwd_5d > 0, fwd_10d > 0, etc.)

Usage:
    python scripts/analyze_signal_timing.py \\
        --cache /tmp/cyb_top20_cache.jsonl \\
        --codes-file /tmp/pool_cyb_top20.csv \\
        --start 2025-09-23 --end 2026-04-01

The reviewer's claim: strict_c2 fires *after* the move is well underway
because Anna's "wait for confirmation" gate requires a follow-through
bar. trade_setup is supposed to fire *on* the breakout day. This script
turns that claim into a measurable difference.
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
import statistics
from collections import defaultdict
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from alpha_agents.tools.vpa.regime import derive_trade_setup

DB = REPO / "data/market_history.db"


def load_cache(path: Path) -> dict:
    return load_cache_filtered(path, "")


def load_cache_filtered(path: Path, provider_filter: str) -> dict:
    """Load cache; if ``provider_filter`` is set, only keep rows whose
    ``cache_identity.provider`` matches (later rows still overwrite)."""
    cache: dict = {}
    if not path.exists():
        return cache
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if provider_filter:
                ident = r.get("cache_identity") or {}
                if ident.get("provider") != provider_filter:
                    continue
            cache[(r["date"], r["code"])] = r["result"]
    return cache


def load_codes(pool_csv: Path) -> list[str]:
    codes = []
    with open(pool_csv) as f:
        for r in csv.DictReader(f):
            codes.append(r["code"])
    return codes


def load_history(code: str, start_buffer: str, end: str) -> pd.DataFrame:
    """Load wider history so backward-looking 20d windows + forward 20d
    look-ahead are both covered."""
    conn = sqlite3.connect(str(DB))
    rows = conn.execute(
        "SELECT date,open,high,low,close,volume FROM daily_kline "
        "WHERE code = ? AND date >= ? AND date <= ? ORDER BY date",
        (code, start_buffer, end),
    ).fetchall()
    return pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])


def is_strict_c2_buy(cache_row: dict) -> bool:
    if not cache_row or not cache_row.get("ok"):
        return False
    v = cache_row.get("llm_verdict")
    L = cache_row.get("llm_confirmation_level", 0) or 0
    return v in ("看多", "偏多") and L >= 2


def signal_metrics(df: pd.DataFrame, di: int) -> dict | None:
    """Compute timing + forward returns for the signal on bar di."""
    if di < 20 or di + 20 >= len(df):
        return None
    today_close = float(df.iloc[di]["close"])
    prior_20 = df.iloc[di - 20: di]
    low_20 = float(prior_20["low"].min())
    high_20 = float(prior_20["high"].max())
    low_idx = int(prior_20["low"].idxmin())
    high_idx = int(prior_20["high"].idxmax())
    return {
        "days_since_20d_low": di - low_idx,
        "days_since_20d_high": di - high_idx,
        "pct_from_20d_low": (today_close - low_20) / low_20 * 100,
        "pct_from_20d_high": (today_close - high_20) / high_20 * 100,
        "fwd_5d": (float(df.iloc[di + 5]["close"]) - today_close) / today_close * 100,
        "fwd_10d": (float(df.iloc[di + 10]["close"]) - today_close) / today_close * 100,
        "fwd_20d": (float(df.iloc[di + 20]["close"]) - today_close) / today_close * 100,
    }


def summarize(events: list[dict]) -> dict:
    if not events:
        return {"n": 0}
    fields = ("days_since_20d_low", "days_since_20d_high",
              "pct_from_20d_low", "pct_from_20d_high",
              "fwd_5d", "fwd_10d", "fwd_20d")
    out = {"n": len(events)}
    for f in fields:
        vals = [e[f] for e in events]
        out[f"{f}_mean"] = statistics.mean(vals)
        out[f"{f}_median"] = statistics.median(vals)
    out["fwd_5d_win_rate"] = sum(1 for e in events if e["fwd_5d"] > 0) / len(events)
    out["fwd_10d_win_rate"] = sum(1 for e in events if e["fwd_10d"] > 0) / len(events)
    out["fwd_20d_win_rate"] = sum(1 for e in events if e["fwd_20d"] > 0) / len(events)
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cache", type=Path, required=True)
    p.add_argument("--codes-file", type=Path,
                   help="Optional fixed pool. If omitted, scans every code present in the cache.")
    p.add_argument("--provider", type=str, default="",
                   help="If set, only count cache rows whose cache_identity.provider matches "
                        "(e.g. 'env' to filter v91 cache down to mimo entries only).")
    p.add_argument("--start", type=str, required=True)
    p.add_argument("--end", type=str, required=True)
    args = p.parse_args()

    cache = load_cache_filtered(args.cache, args.provider)
    if args.codes_file:
        codes = load_codes(args.codes_file)
    else:
        codes = sorted({c for (_, c) in cache.keys()})
    print(f"Cache rows: {len(cache)}  |  Codes to analyze: {len(codes)}")

    # Buffer to ensure 20-bar backward lookup + 20-bar forward returns
    # both fit. ~60 calendar days each side is plenty.
    start_dt = pd.to_datetime(args.start) - pd.Timedelta(days=90)
    end_dt = pd.to_datetime(args.end) + pd.Timedelta(days=60)

    events_by_gate: dict = defaultdict(list)
    # Dedupe: only count the FIRST firing of a signal after a quiet
    # window. Otherwise actionable state lingers for multiple days and
    # the same uptrend gets credited as 4-5 separate signals.
    COOLDOWN_DAYS = 3

    for code in codes:
        df = load_history(code, start_dt.strftime("%Y-%m-%d"), end_dt.strftime("%Y-%m-%d"))
        if len(df) < 50:
            continue
        last_fired: dict = {}
        for di in range(len(df)):
            d = str(df.iloc[di]["date"])
            if d < args.start or d > args.end:
                continue
            sub = df.iloc[: di + 1]
            cache_row = cache.get((d, code), {})

            # Gate A: strict_c2 (Anna level≥2)
            if is_strict_c2_buy(cache_row):
                if di - last_fired.get("strict_c2", -1000) > COOLDOWN_DAYS:
                    m = signal_metrics(df, di)
                    if m:
                        events_by_gate["strict_c2"].append(m)
                last_fired["strict_c2"] = di

            # Gate B: deterministic actionable
            r = derive_trade_setup(sub)
            if r["setup"] in ("long_actionable", "long_confirmed"):
                if di - last_fired.get("setup_actionable", -1000) > COOLDOWN_DAYS:
                    m = signal_metrics(df, di)
                    if m:
                        events_by_gate["setup_actionable"].append(m)
                last_fired["setup_actionable"] = di
            elif r["setup"] == "long_watch":
                if di - last_fired.get("setup_watch", -1000) > COOLDOWN_DAYS:
                    m = signal_metrics(df, di)
                    if m:
                        events_by_gate["setup_watch"].append(m)
                last_fired["setup_watch"] = di

    # Render comparison
    print(f"Signal timing analysis  ({args.start} → {args.end})")
    src = args.codes_file.name if args.codes_file else f"cache provider={args.provider or 'any'}"
    print(f"Pool: {len(codes)} codes from {src}")
    print()
    cols = ("n", "days_since_20d_low_mean", "pct_from_20d_low_mean",
            "fwd_5d_mean", "fwd_5d_win_rate", "fwd_10d_mean", "fwd_10d_win_rate",
            "fwd_20d_mean", "fwd_20d_win_rate")
    hdr = f"{'gate':24}  {'n':>4}  {'days_lo':>7}  {'%from_lo':>8}  {'fwd_5d':>7}  {'win5%':>6}  {'fwd_10d':>7}  {'win10%':>6}  {'fwd_20d':>7}  {'win20%':>6}"
    print(hdr)
    print("-" * len(hdr))
    for gate, events in events_by_gate.items():
        s = summarize(events)
        print(f"{gate:24}  {s['n']:>4}  "
              f"{s.get('days_since_20d_low_mean', 0):>6.1f}d  "
              f"{s.get('pct_from_20d_low_mean', 0):>+7.1f}%  "
              f"{s.get('fwd_5d_mean', 0):>+6.1f}%  "
              f"{s.get('fwd_5d_win_rate', 0)*100:>5.0f}%  "
              f"{s.get('fwd_10d_mean', 0):>+6.1f}%  "
              f"{s.get('fwd_10d_win_rate', 0)*100:>5.0f}%  "
              f"{s.get('fwd_20d_mean', 0):>+6.1f}%  "
              f"{s.get('fwd_20d_win_rate', 0)*100:>5.0f}%")


if __name__ == "__main__":
    main()
