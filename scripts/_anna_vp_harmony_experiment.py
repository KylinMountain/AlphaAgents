"""Anna's vp_harmony confirmation_level correction experiment.

Rules:
  - bullish direction (看多/偏多) + 5d bullish vp_harmony  → lvl 1 → 2
  - bearish direction (看空/偏空) + 5d bearish vp_harmony  → lvl 1 → 2
  - direction conflicts with vp_harmony + lvl 2           → lvl 1
  - vp_harmony from 5d: up-volume vs down-volume ratio.
    bullish if up_vol > 1.2 * down_vol; bearish if down_vol > 1.2 * up_vol; else neutral.

Then run strategy_capture with entry-tier=1, exit-tier=3.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from statistics import mean, median

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from analyze_vpa_strategy_capture import (  # type: ignore
    BULLISH_FAMILY, BEARISH_FAMILY,
    compute_window_metrics, simulate_strategy, phase_family, signal_confirmed,
)

CACHE = REPO / "data/vpa_stateful_guarded_top20_20251017_20260112_cache.reguarded.jsonl"
DB = REPO / "data/market_history.db"
START = "2025-10-17"
END = "2026-01-12"
ENTRY_TIER = 1
EXIT_TIER = 3

BULLISH_DIRS = ("看多", "偏多")
BEARISH_DIRS = ("看空", "偏空")


def vp_harmony(closes: list[float], opens: list[float], vols: list[float]) -> str:
    """5-day window: sum volume on up days (close>=open) vs down days. Return bullish/bearish/neutral."""
    if len(closes) < 3:
        return "neutral"
    up_vol = down_vol = 0.0
    for c, o, v in zip(closes, opens, vols):
        if v <= 0:
            continue
        if c > o:
            up_vol += v
        elif c < o:
            down_vol += v
    if down_vol <= 0 and up_vol > 0:
        return "bullish"
    if up_vol <= 0 and down_vol > 0:
        return "bearish"
    if up_vol > 1.2 * down_vol:
        return "bullish"
    if down_vol > 1.2 * up_vol:
        return "bearish"
    return "neutral"


def load_cache(path: Path) -> dict:
    cache = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            r = json.loads(line)
            cache[(r["date"], r["code"])] = r["result"]
    return cache


def adjust_confirmation(cache: dict, harmony_by: dict) -> dict:
    """Apply Anna's lift/curb rules. Mutate a copy of cache values."""
    new_cache = {}
    stats = {"lifted": 0, "curbed": 0, "untouched": 0, "no_harmony": 0}
    for key, result in cache.items():
        r2 = dict(result)
        lvl = r2.get("llm_confirmation_level")
        direction = r2.get("llm_verdict", "")
        try:
            lvl_i = int(lvl) if lvl is not None else None
        except (ValueError, TypeError):
            lvl_i = None
        h = harmony_by.get(key, "neutral")
        if h == "neutral" or lvl_i is None:
            stats["no_harmony" if h == "neutral" else "untouched"] += 1
            new_cache[key] = r2
            continue
        bull_dir = direction in BULLISH_DIRS
        bear_dir = direction in BEARISH_DIRS
        if bull_dir and h == "bullish" and lvl_i == 1:
            r2["llm_confirmation_level"] = 2
            stats["lifted"] += 1
        elif bear_dir and h == "bearish" and lvl_i == 1:
            r2["llm_confirmation_level"] = 2
            stats["lifted"] += 1
        elif (bull_dir and h == "bearish") or (bear_dir and h == "bullish"):
            if lvl_i == 2:
                r2["llm_confirmation_level"] = 1
                stats["curbed"] += 1
            else:
                stats["untouched"] += 1
        else:
            stats["untouched"] += 1
        new_cache[key] = r2
    return new_cache, stats


def main():
    cache = load_cache(CACHE)
    codes = sorted({k[1] for k in cache.keys()})

    conn = sqlite3.connect(str(DB))
    # Build harmony_by[(date,code)] using prior 5 trading days (inclusive of current).
    harmony_by = {}
    ohlc_by_code = {}
    for code in codes:
        rows = conn.execute(
            "SELECT date, open, close, volume FROM daily_kline "
            "WHERE code=? AND date BETWEEN ? AND ? ORDER BY date",
            (code, "2025-09-01", END),  # earlier start to allow rolling
        ).fetchall()
        for i, (d, o, c, v) in enumerate(rows):
            if i < 4:
                continue
            window = rows[i - 4: i + 1]
            opens = [float(r[1] or 0) for r in window]
            closes = [float(r[2] or 0) for r in window]
            vols = [float(r[3] or 0) for r in window]
            harmony_by[(d, code)] = vp_harmony(closes, opens, vols)
        # Also for window-metric computation
        ohlc_by_code[code] = [
            (d, float(o or 0), 0.0, 0.0, float(c or 0))
            for d, o, _, c in [(r[0], r[1], None, r[2]) for r in rows]
            if (d >= START and d <= END)
        ]

    new_cache, stats = adjust_confirmation(cache, harmony_by)
    print(f"Cache adjustments: lifted={stats['lifted']} curbed={stats['curbed']} "
          f"untouched={stats['untouched']} no_harmony={stats['no_harmony']}  "
          f"(total={sum(stats.values())})")

    # Simulate strategy with entry=1 exit=3 on adjusted cache
    rows_out = []
    for code in codes:
        rs = conn.execute(
            "SELECT date,open,high,low,close FROM daily_kline "
            "WHERE code=? AND date BETWEEN ? AND ? ORDER BY date",
            (code, START, END),
        ).fetchall()
        ohlc = [(d, float(o), float(h), float(l), float(c))
                for d, o, h, l, c in rs if o and c]
        if not ohlc:
            continue
        win = compute_window_metrics(ohlc)
        trades, info = simulate_strategy(code, ohlc, new_cache, ENTRY_TIER, EXIT_TIER)
        capture = (info["strategy_pnl_pct"] / win["window_max_pct"] * 100
                   if win["window_max_pct"] > 0 else float("nan"))
        escape = info["strategy_pnl_pct"] - win["raw_ret"]
        rows_out.append({"code": code, **win, **info,
                         "capture_pct": capture, "escape": escape})

    # 3 numbers Anna asked for
    avg_strat = mean(r["strategy_pnl_pct"] for r in rows_out)
    winners = [r for r in rows_out if r["raw_ret"] > 5]
    losers = [r for r in rows_out if r["raw_ret"] < -3]
    capt_winners = [r["capture_pct"] for r in winners
                    if r["window_max_pct"] > 0 and r["capture_pct"] == r["capture_pct"]]
    avg_l_strat = mean(r["strategy_pnl_pct"] for r in losers) if losers else float("nan")
    avg_l_raw = mean(r["raw_ret"] for r in losers) if losers else float("nan")
    escape_pp = avg_l_strat - avg_l_raw if losers else float("nan")

    avg_raw = mean(r["raw_ret"] for r in rows_out)
    n_winners_strat = sum(1 for r in rows_out if r["strategy_pnl_pct"] > 0)
    in_market = sum(r["in_market_days"] for r in rows_out)

    print()
    print("=" * 60)
    print(f"  策略收益 (avg)        : {avg_strat:+.1f}%   (raw {avg_raw:+.1f}%)")
    print(f"  大涨股 capture 中位   : {median(capt_winners):+.1f}%   ({len(winners)} 只)")
    print(f"  下跌股 escape         : {escape_pp:+.1f}pp  ({len(losers)} 只, raw {avg_l_raw:+.1f}% → strat {avg_l_strat:+.1f}%)")
    print("=" * 60)
    print(f"  winners {n_winners_strat}/{len(rows_out)}, in-market days {in_market}")

    # Per-stock detail
    print()
    print(f"{'code':<7}{'raw%':>7}{'peak%':>7}{'strat%':>8}{'cap%':>7}{'esc':>6}  trades")
    for r in sorted(rows_out, key=lambda x: -x["raw_ret"]):
        cap = f"{r['capture_pct']:>+5.0f}" if r["window_max_pct"] > 0 else "  -- "
        print(f"{r['code']:<7}{r['raw_ret']:>+6.1f}%{r['window_max_pct']:>+6.1f}%"
              f"{r['strategy_pnl_pct']:>+7.1f}%{cap:>7}{r['escape']:>+5.1f}  {r['n_trades']}")


if __name__ == "__main__":
    main()
