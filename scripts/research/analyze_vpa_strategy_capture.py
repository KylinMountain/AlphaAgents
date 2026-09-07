"""Per-stock strategy capture analysis on a stateful VPA cache.

Question this answers:
  For each stock, given the LLM phase signals + confirmation:
    - Did we BUY when 拉升 was confirmed and SELL when 派发/下跌 was confirmed?
    - Did the strategy capture the actual rally and avoid the actual decline?

Outputs:
  - raw_ret              : buy at day-1 open, sell at last close (buy-and-hold)
  - window_max_pct       : peak close from day-1 open (max favorable excursion)
  - window_max_dd        : trough close from running peak (max drawdown)
  - strategy_trades      : list of trades the rule produced
  - strategy_pnl_pct     : strategy realized return
  - captured_pct_of_max  : strategy_pnl / window_max_pct (only if positive)
  - escape_score         : strategy_pnl - raw_ret (positive = avoided some decline)

Usage:
  python scripts/analyze_vpa_strategy_capture.py CACHE.jsonl POOL_CSV \
    [--start-date 2025-10-17] [--end-date 2026-01-12] \
    [--entry-tier 2] [--exit-tier 2]

  --entry-tier / --exit-tier control the confirmation_level threshold
  (3 = only C3 ★ stars, 2 = include C2 ▲▽, 1 = include C1, 0 = include all)
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from statistics import mean, median

REPO = Path(__file__).resolve().parent.parent

BULLISH_FAMILY = ("吸筹", "拉升", "买入高峰", "卖压衰竭")
BEARISH_FAMILY = ("派发", "下跌", "抛售高峰")


def load_cache(path: Path) -> dict:
    cache = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            r = json.loads(line)
            cache[(r["date"], r["code"])] = r["result"]
    return cache


def load_codes(pool_csv: Path) -> list[str]:
    codes = []
    with open(pool_csv, encoding="utf-8") as f:
        for line in f:
            for cell in line.strip().split(","):
                cell = cell.strip()
                if cell.isdigit() and len(cell) == 6:
                    codes.append(cell)
                    break
    return codes


def phase_family(phase: str, families: tuple) -> bool:
    return any(f in phase for f in families)


def signal_confirmed(result: dict, tier_min: int) -> bool:
    """A signal is confirmed if confirmation_level >= tier_min OR llm_confirmed=True
    (the latter for caches without state machine fields)."""
    lvl = result.get("llm_confirmation_level", None)
    if lvl is not None:
        try:
            return int(lvl) >= tier_min
        except (ValueError, TypeError):
            pass
    # Fallback for old caches
    return bool(result.get("llm_confirmed"))


def simulate_strategy(code: str, ohlc: list, cache: dict,
                      entry_tier: int, exit_tier: int) -> tuple[list, dict]:
    """Walk through dates. State machine: flat → buy on confirmed bullish phase next open.
    Holding → sell on confirmed bearish phase next open. End: force close at last close.
    """
    if not ohlc:
        return [], {}
    state = "flat"
    entry_idx = None
    entry_date = None
    entry_price = None
    trades = []

    for i in range(len(ohlc) - 1):  # need next-day open for execution
        d, o, _, _, c = ohlc[i]
        v = cache.get((d, code), {})
        if not v.get("ok"):
            continue
        phase = v.get("llm_phase", "") or ""

        if state == "flat":
            if phase_family(phase, BULLISH_FAMILY) and signal_confirmed(v, entry_tier):
                # buy at next open
                next_open = ohlc[i + 1][1]
                if next_open > 0:
                    state = "holding"
                    entry_idx = i + 1
                    entry_date = ohlc[i + 1][0]
                    entry_price = next_open
        elif state == "holding":
            if phase_family(phase, BEARISH_FAMILY) and signal_confirmed(v, exit_tier):
                # sell at next open
                next_open = ohlc[i + 1][1]
                if next_open > 0:
                    pnl_pct = (next_open - entry_price) / entry_price * 100
                    trades.append({
                        "entry_date": entry_date, "entry_price": entry_price,
                        "exit_date": ohlc[i + 1][0], "exit_price": next_open,
                        "pnl_pct": pnl_pct, "exit_reason": "phase:" + phase,
                        "holding_days": (i + 1) - entry_idx,
                    })
                    state = "flat"
                    entry_idx = entry_date = entry_price = None

    # Force close any open position at last close (end of window)
    if state == "holding" and entry_price is not None:
        last_close = ohlc[-1][4]
        pnl_pct = (last_close - entry_price) / entry_price * 100
        trades.append({
            "entry_date": entry_date, "entry_price": entry_price,
            "exit_date": ohlc[-1][0], "exit_price": last_close,
            "pnl_pct": pnl_pct, "exit_reason": "end_of_window",
            "holding_days": len(ohlc) - 1 - entry_idx,
        })

    # Cumulative strategy P&L (compounded across trades)
    cumret = 1.0
    for t in trades:
        cumret *= (1 + t["pnl_pct"] / 100)
    strategy_pnl = (cumret - 1) * 100

    info = {
        "trades": trades,
        "strategy_pnl_pct": strategy_pnl,
        "n_trades": len(trades),
        "in_market_days": sum(t["holding_days"] for t in trades),
    }
    return trades, info


def compute_window_metrics(ohlc: list) -> dict:
    """Compute raw return + max favorable excursion + max drawdown over the window."""
    if not ohlc:
        return {}
    open0 = ohlc[0][1]
    closes = [r[4] for r in ohlc]
    raw_ret = (closes[-1] - open0) / open0 * 100
    window_max = (max(closes) - open0) / open0 * 100
    window_min = (min(closes) - open0) / open0 * 100

    # Max drawdown from running peak
    running_peak = closes[0]
    max_dd = 0
    for c in closes:
        running_peak = max(running_peak, c)
        dd = (c - running_peak) / running_peak * 100
        max_dd = min(max_dd, dd)

    # Identify peak day and trough day
    peak_idx = closes.index(max(closes))
    trough_idx = closes.index(min(closes))

    return {
        "raw_ret": raw_ret,
        "window_max_pct": window_max,
        "window_min_pct": window_min,
        "max_drawdown": max_dd,
        "peak_date": ohlc[peak_idx][0],
        "trough_date": ohlc[trough_idx][0],
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("cache", type=Path)
    p.add_argument("pool_csv", type=Path)
    p.add_argument("--db", type=Path, default=REPO / "data/market_history.db")
    p.add_argument("--start-date", type=str, default="2025-10-17")
    p.add_argument("--end-date", type=str, default="2026-01-12")
    p.add_argument("--entry-tier", type=int, default=2,
                   help="confirmation_level required for entry (3=only ★, 2=include ▲▽, 1=any, 0=all)")
    p.add_argument("--exit-tier", type=int, default=2,
                   help="confirmation_level required for exit")
    args = p.parse_args()

    cache = load_cache(args.cache)
    codes = load_codes(args.pool_csv)

    conn = sqlite3.connect(str(args.db))

    print(f"\nCache: {args.cache.name}")
    print(f"Window: {args.start_date} → {args.end_date}")
    print(f"Entry rule: 拉升/吸筹 family + confirmation_level >= {args.entry_tier}")
    print(f"Exit rule:  派发/下跌/抛售高峰 family + confirmation_level >= {args.exit_tier}")
    print()

    print(f"{'code':<7}{'raw%':>8}{'peak%':>8}{'trough%':>9}{'max_dd':>8}  "
          f"{'strat%':>8}{'trades':>7}{'in_days':>8}  {'capture%':>10}{'escape':>9}")
    print("-" * 90)

    rows = []
    for code in codes:
        rs = conn.execute(
            "SELECT date,open,high,low,close FROM daily_kline "
            "WHERE code=? AND date BETWEEN ? AND ? ORDER BY date",
            (code, args.start_date, args.end_date),
        ).fetchall()
        ohlc = [(d, float(o), float(h), float(l), float(c))
                for d, o, h, l, c in rs if o and c]
        if not ohlc:
            continue
        win = compute_window_metrics(ohlc)
        trades, info = simulate_strategy(code, ohlc, cache,
                                          args.entry_tier, args.exit_tier)

        # Capture% of max favorable: strategy pnl / window_max_pct
        capture = (info["strategy_pnl_pct"] / win["window_max_pct"] * 100
                   if win["window_max_pct"] > 0 else float("nan"))
        # Escape: strategy beats raw return — relevant for declines
        escape = info["strategy_pnl_pct"] - win["raw_ret"]

        rows.append({
            "code": code, **win, **info,
            "capture_pct": capture, "escape": escape,
        })
        cap_s = f"{capture:>+6.1f}%" if win["window_max_pct"] > 0 else "    -  "
        print(f"{code:<7}{win['raw_ret']:>+7.1f}%{win['window_max_pct']:>+7.1f}%"
              f"{win['window_min_pct']:>+8.1f}%{win['max_drawdown']:>+7.1f}%  "
              f"{info['strategy_pnl_pct']:>+7.1f}%{info['n_trades']:>7}{info['in_market_days']:>8}  "
              f"{cap_s:>10}{escape:>+8.1f}%")

    print("-" * 90)
    # Aggregates
    avg_raw = mean(r["raw_ret"] for r in rows)
    avg_strat = mean(r["strategy_pnl_pct"] for r in rows)
    avg_peak = mean(r["window_max_pct"] for r in rows)
    avg_dd = mean(r["max_drawdown"] for r in rows)
    in_market_total = sum(r["in_market_days"] for r in rows)
    n_total = len(rows)
    n_winners_strat = sum(1 for r in rows if r["strategy_pnl_pct"] > 0)
    n_winners_raw = sum(1 for r in rows if r["raw_ret"] > 0)

    print(f"\nAVERAGES (across {n_total} stocks):")
    print(f"  raw return       : {avg_raw:+.1f}%")
    print(f"  strategy return  : {avg_strat:+.1f}%   (vs raw: {avg_strat - avg_raw:+.1f}%)")
    print(f"  window peak (avg): {avg_peak:+.1f}%   (max favorable excursion)")
    print(f"  max drawdown     : {avg_dd:+.1f}%")
    print(f"  strategy winners : {n_winners_strat}/{n_total}")
    print(f"  buy-hold winners : {n_winners_raw}/{n_total}")
    print(f"  total in-market days: {in_market_total} (out of {n_total * (len(rows[0].get('trades', [])) and 60 or 60)} possible)")

    # Group analysis: winners (raw>0) vs losers (raw<0)
    winners = [r for r in rows if r["raw_ret"] > 5]
    losers = [r for r in rows if r["raw_ret"] < -3]
    flat = [r for r in rows if -3 <= r["raw_ret"] <= 5]

    print(f"\n=== 大涨股 ({len(winners)} 只, raw > +5%): 吃到涨了吗？ ===")
    if winners:
        avg_w_raw = mean(r["raw_ret"] for r in winners)
        avg_w_strat = mean(r["strategy_pnl_pct"] for r in winners)
        avg_w_peak = mean(r["window_max_pct"] for r in winners)
        captures = [r["capture_pct"] for r in winners
                    if r["window_max_pct"] > 0 and r["capture_pct"] == r["capture_pct"]]
        print(f"  平均 raw   : {avg_w_raw:+.1f}%")
        print(f"  平均 peak  : {avg_w_peak:+.1f}%   ← 理论最大可吃")
        print(f"  平均 strat : {avg_w_strat:+.1f}%   ← 实际吃到")
        print(f"  比 raw 多: {avg_w_strat - avg_w_raw:+.1f}%")
        if captures:
            print(f"  capture vs peak 中位: {median(captures):+.1f}%")

    print(f"\n=== 下跌股 ({len(losers)} 只, raw < -3%): 跌时跑了吗？ ===")
    if losers:
        avg_l_raw = mean(r["raw_ret"] for r in losers)
        avg_l_strat = mean(r["strategy_pnl_pct"] for r in losers)
        avg_l_dd = mean(r["max_drawdown"] for r in losers)
        print(f"  平均 raw       : {avg_l_raw:+.1f}%   ← buy-hold 实际亏")
        print(f"  平均 strat     : {avg_l_strat:+.1f}%   ← 策略亏多少")
        print(f"  平均 max_dd    : {avg_l_dd:+.1f}%   ← 期间最深跌幅")
        print(f"  避开了多少     : {avg_l_strat - avg_l_raw:+.1f}%")

    if flat:
        avg_f_raw = mean(r["raw_ret"] for r in flat)
        avg_f_strat = mean(r["strategy_pnl_pct"] for r in flat)
        print(f"\n=== 震荡股 ({len(flat)} 只, raw -3%~+5%) ===")
        print(f"  平均 raw   : {avg_f_raw:+.1f}%")
        print(f"  平均 strat : {avg_f_strat:+.1f}%")


if __name__ == "__main__":
    main()
