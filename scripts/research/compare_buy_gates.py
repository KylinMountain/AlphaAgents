"""Gate comparison harness: re-simulate trades on existing cache under
different BUY/SELL gate definitions.

The point is to test the reviewer's core claim — that the
``confirmation_level≥2`` Anna gate is structurally late as a buy
trigger and a deterministic ``trade_setup`` gate would catch more of
the early entries it misses.

Usage:
    python scripts/compare_buy_gates.py \\
        --cache /tmp/cyb_top20_cache.jsonl \\
        --codes-file /tmp/pool_cyb_top20.csv \\
        --start 2025-09-23 --end 2026-03-15 \\
        --max-positions 2

Outputs a comparison table (per-gate: trades, win-rate, total return,
max drawdown, alpha vs chinext index 399006).
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Callable

import pandas as pd

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from alpha_agents.tools.vpa.regime import derive_trade_setup, classify_regime

DB = REPO / "data/market_history.db"
TX_COST_RT = 0.002  # 0.2% round-trip


# ── Gate definitions ─────────────────────────────────────────

def gate_strict_c2_buy(cache_row: dict, df: pd.DataFrame) -> bool:
    """v91 baseline: verdict ∈ {看多, 偏多} AND confirmation_level ≥ 2."""
    if not cache_row or not cache_row.get("ok"):
        return False
    v = cache_row.get("llm_verdict")
    L = cache_row.get("llm_confirmation_level", 0) or 0
    return v in ("看多", "偏多") and L >= 2


def gate_strict_c2_sell(cache_row: dict, df: pd.DataFrame) -> bool:
    """v91 baseline SELL: phase-exit (verdict bearish + pc.to bearish + pc.confirmed)
    OR legacy C3 bearish."""
    if not cache_row or not cache_row.get("ok"):
        return False
    v = cache_row.get("llm_verdict", "")
    L = cache_row.get("llm_confirmation_level", 0) or 0
    if v in ("看空", "偏空") and L >= 3:
        return True
    pc = cache_row.get("llm_phase_change") or {}
    if not pc.get("confirmed"):
        return False
    return v in ("看空", "偏空") and str(pc.get("to", "") or "").startswith(("派发", "下跌", "抛售"))


def gate_setup_actionable_buy(cache_row: dict, df: pd.DataFrame) -> bool:
    """B1: deterministic trade_setup ∈ {long_actionable, long_confirmed}."""
    r = derive_trade_setup(df)
    return r["setup"] in ("long_actionable", "long_confirmed")


def gate_setup_actionable_sell(cache_row: dict, df: pd.DataFrame) -> bool:
    r = derive_trade_setup(df)
    return r["setup"] in ("short_actionable", "short_confirmed")


def gate_setup_loose_buy(cache_row: dict, df: pd.DataFrame) -> bool:
    """B1 + watch level — trade more aggressively on early signals."""
    r = derive_trade_setup(df)
    return r["setup"] in ("long_watch", "long_actionable", "long_confirmed")


def gate_setup_loose_sell(cache_row: dict, df: pd.DataFrame) -> bool:
    r = derive_trade_setup(df)
    return r["setup"] in ("short_watch", "short_actionable", "short_confirmed")


GATES = {
    "strict_c2 (v91 baseline)": (gate_strict_c2_buy, gate_strict_c2_sell),
    "setup_actionable (B1)": (gate_setup_actionable_buy, gate_setup_actionable_sell),
    "setup_loose (incl. watch)": (gate_setup_loose_buy, gate_setup_loose_sell),
}


# ── Data loaders ─────────────────────────────────────────────

def load_cache(path: Path) -> dict:
    """Returns ``{(date, code): result_dict}`` — latest entry wins on
    duplicates so identity-blind reuse of mixed caches still picks the
    most recent verdict."""
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
            cache[(r["date"], r["code"])] = r["result"]
    return cache


def load_codes(pool_csv: Path) -> list[str]:
    codes = []
    with open(pool_csv) as f:
        reader = csv.DictReader(f) if pool_csv.suffix == ".csv" else None
        if reader is None:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    codes.append(line.split(",")[0])
        else:
            for r in reader:
                codes.append(r["code"])
    return codes


def load_prices(codes: list[str], start: str, end: str) -> dict:
    conn = sqlite3.connect(str(DB))
    placeholders = ",".join("?" * len(codes))
    rows = conn.execute(
        f"SELECT code,date,open,high,low,close,volume FROM daily_kline "
        f"WHERE code IN ({placeholders}) AND date >= ? AND date <= ? ORDER BY code, date",
        codes + [start, end],
    ).fetchall()
    out: dict = {}
    df = pd.DataFrame(rows, columns=["code", "date", "open", "high", "low", "close", "volume"])
    for code, g in df.groupby("code"):
        out[code] = g.reset_index(drop=True)
    return out


def load_history_for_setup(code: str, end: str, lookback_days: int = 150) -> pd.DataFrame:
    """Load enough history for derive_trade_setup() to compute features."""
    conn = sqlite3.connect(str(DB))
    rows = conn.execute(
        "SELECT date,open,high,low,close,volume FROM daily_kline "
        "WHERE code = ? AND date <= ? ORDER BY date DESC LIMIT ?",
        (code, end, lookback_days),
    ).fetchall()
    return pd.DataFrame(list(reversed(rows)),
                         columns=["date", "open", "high", "low", "close", "volume"])


# ── Simulator ────────────────────────────────────────────────

def simulate(gate_buy: Callable, gate_sell: Callable, codes: list[str],
             eval_dates: list[str], cache: dict, prices: dict, max_positions: int) -> dict:
    """Walk forward day-by-day. Each day:
      1. Check SELL gate on each held position.
      2. If slots open, scan codes for BUY signals (in code list order).
      3. Execute at next bar's open (next eval_date in this code's history).
    Returns metrics dict.
    """
    positions: dict = {}
    trades: list = []
    daily_value = []  # mark-to-market

    for di, date in enumerate(eval_dates):
        # 1. SELL pass on held positions
        for code in list(positions):
            pos = positions[code]
            df = pos["df"]
            df_until = df[df["date"] <= date]
            if len(df_until) == 0:
                continue
            cache_row = cache.get((date, code), {})
            today_close = float(df_until.iloc[-1]["close"])
            pos["peak"] = max(pos.get("peak", pos["entry_px"]), today_close)
            # Hard risk: -12% abs / -20% from peak
            gross = (today_close - pos["entry_px"]) / pos["entry_px"] * 100
            from_peak = (today_close - pos["peak"]) / pos["peak"] * 100
            sell = False
            reason = ""
            if gross <= -12.0:
                sell, reason = True, "abs_stop"
            elif from_peak <= -20.0:
                sell, reason = True, "trail_stop"
            elif gate_sell(cache_row, df_until):
                sell, reason = True, "gate_sell"
            if sell:
                # Exit at next bar's open
                next_idx = di + 1
                if next_idx < len(eval_dates):
                    exit_date = eval_dates[next_idx]
                    exit_row = df[df["date"] == exit_date]
                    if len(exit_row):
                        exit_px = float(exit_row.iloc[0]["open"])
                        net = (exit_px - pos["entry_px"]) / pos["entry_px"] * 100 - TX_COST_RT * 100
                        trades.append({
                            "code": code, "entry_date": pos["entry_date"], "entry_px": pos["entry_px"],
                            "exit_date": exit_date, "exit_px": exit_px, "net_pct": net, "reason": reason,
                        })
                        del positions[code]

        # 2. BUY pass — fill open slots in code list order
        if len(positions) < max_positions:
            for code in codes:
                if code in positions:
                    continue
                if len(positions) >= max_positions:
                    break
                df = prices.get(code)
                if df is None:
                    continue
                df_until = df[df["date"] <= date]
                if len(df_until) < 25:
                    continue
                cache_row = cache.get((date, code), {})
                if gate_buy(cache_row, df_until):
                    next_idx = di + 1
                    if next_idx < len(eval_dates):
                        entry_date = eval_dates[next_idx]
                        entry_row = df[df["date"] == entry_date]
                        if len(entry_row):
                            entry_px = float(entry_row.iloc[0]["open"])
                            positions[code] = {
                                "entry_date": entry_date, "entry_px": entry_px,
                                "df": df, "peak": entry_px,
                            }

        # 3. Mark-to-market for the day
        slot_pnl = 0.0
        n_held = len(positions)
        for code, pos in positions.items():
            df_until = pos["df"][pos["df"]["date"] <= date]
            if len(df_until):
                slot_pnl += (float(df_until.iloc[-1]["close"]) - pos["entry_px"]) / pos["entry_px"]
        # Equal-weight assumption: each slot is 1/max_positions of capital.
        port_value = (slot_pnl + sum((t["net_pct"] / 100) for t in trades)) / max_positions
        daily_value.append({"date": date, "value": port_value, "n_held": n_held})

    # Compute metrics
    realized_net = sum(t["net_pct"] for t in trades) / max_positions
    unrealized = 0.0
    final_date = eval_dates[-1]
    for code, pos in positions.items():
        df_until = pos["df"][pos["df"]["date"] <= final_date]
        if len(df_until):
            unrealized += (float(df_until.iloc[-1]["close"]) - pos["entry_px"]) / pos["entry_px"] * 100
    unrealized = unrealized / max_positions

    series = pd.Series([d["value"] for d in daily_value])
    peak = series.cummax()
    drawdown = (series - peak)
    max_dd = float(drawdown.min())

    return {
        "n_trades": len(trades),
        "winners": sum(1 for t in trades if t["net_pct"] > 0),
        "losers": sum(1 for t in trades if t["net_pct"] <= 0),
        "win_rate": sum(1 for t in trades if t["net_pct"] > 0) / len(trades) if trades else 0.0,
        "realized_pct": realized_net,
        "unrealized_pct": unrealized,
        "total_pct": realized_net + unrealized,
        "max_dd_pct": max_dd * 100,
        "trades": trades,
        "daily_value": daily_value,
    }


def chinext_return(start: str, end: str) -> float:
    conn = sqlite3.connect(str(DB))
    r1 = conn.execute("SELECT close FROM daily_kline WHERE code='399006' AND date>=? ORDER BY date LIMIT 1", (start,)).fetchone()
    r2 = conn.execute("SELECT close FROM daily_kline WHERE code='399006' AND date<=? ORDER BY date DESC LIMIT 1", (end,)).fetchone()
    if not r1 or not r2:
        return 0.0
    return (r2[0] - r1[0]) / r1[0] * 100


# ── Main ─────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cache", type=Path, required=True)
    p.add_argument("--codes-file", type=Path, required=True)
    p.add_argument("--start", type=str, required=True)
    p.add_argument("--end", type=str, required=True)
    p.add_argument("--max-positions", type=int, default=2)
    args = p.parse_args()

    print(f"Loading cache from {args.cache}")
    cache = load_cache(args.cache)
    print(f"  rows: {len(cache)}")

    codes = load_codes(args.codes_file)
    print(f"Codes: {len(codes)}")

    prices = load_prices(codes, args.start, args.end)
    print(f"Loaded prices for {len(prices)} codes")

    # eval_dates = market dates across all codes
    all_dates = set()
    for df in prices.values():
        all_dates.update(df["date"].astype(str).tolist())
    eval_dates = sorted(d for d in all_dates if args.start <= d <= args.end)
    print(f"Eval dates: {len(eval_dates)} ({eval_dates[0]} → {eval_dates[-1]})")

    cyb = chinext_return(args.start, args.end)
    print(f"\n创业板 399006 {args.start} → {args.end}: {cyb:+.2f}%\n")

    print(f"{'gate':30}  {'trades':>6}  {'win%':>6}  {'realized':>9}  {'unreal':>7}  {'total':>7}  {'max_dd':>7}  {'alpha':>7}")
    print("-" * 100)

    for name, (buy, sell) in GATES.items():
        result = simulate(buy, sell, codes, eval_dates, cache, prices, args.max_positions)
        alpha = result["total_pct"] - cyb
        print(f"{name:30}  {result['n_trades']:>6}  {result['win_rate']*100:>5.0f}%  "
              f"{result['realized_pct']:>+8.2f}%  {result['unrealized_pct']:>+6.2f}%  "
              f"{result['total_pct']:>+6.2f}%  {result['max_dd_pct']:>+6.2f}%  {alpha:>+6.2f}pp")


if __name__ == "__main__":
    main()
