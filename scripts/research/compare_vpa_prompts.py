"""Side-by-side compare of two VPA caches on the same stocks/window.

Reports per-stock:
  - n_cfm_True bearish signals (派发系列 / 抛售高峰 / 下跌)
  - n_wrong_bearish (cfm=True bearish where fwd5d > 0)
  - wrong rate
  - cfm=True bullish signals same comparison (rare wrong but useful for symmetry)

Usage:
    python scripts/compare_vpa_prompts.py OLD_CACHE.jsonl NEW_CACHE.jsonl \
        CODES (comma-separated) \
        --start-date 2025-10-17 --end-date 2026-01-12
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

BULLISH = ("吸筹", "吸筹初期", "吸筹尾声", "吸筹末期", "拉升", "拉升初期",
           "买入高峰", "卖压衰竭")
BEARISH = ("派发", "派发初期", "派发中期", "派发尾声", "抛售高峰",
           "下跌", "下跌初期", "下跌尾声", "下跌末期")


def load_cache(path: Path) -> dict:
    cache = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            r = json.loads(line)
            cache[(r["date"], r["code"])] = r["result"]
    return cache


def audit(cache: dict, codes: list[str], db: Path,
          start: str, end: str) -> dict:
    conn = sqlite3.connect(str(db))
    out = {}
    for c in codes:
        rows = conn.execute(
            "SELECT date,open,high,low,close FROM daily_kline "
            "WHERE code=? AND date BETWEEN ? AND ? ORDER BY date",
            (c, start, end),
        ).fetchall()
        ohlc = [(d, float(o), float(h), float(l), float(cl))
                for d, o, h, l, cl in rows if o and cl]
        n = len(ohlc)
        cfm_t_bear = 0
        cfm_t_bear_wrong = 0
        cfm_t_bull = 0
        cfm_t_bull_wrong = 0
        wrong_signals = []
        for i, (d, o, _, _, cl) in enumerate(ohlc):
            v = cache.get((d, c), {})
            if not v.get("ok"):
                continue
            if not bool(v.get("llm_confirmed")):
                continue
            ph = v.get("llm_phase", "") or ""
            f5 = (ohlc[i + 5][4] - cl) / cl * 100 if i + 5 < n else None
            if ph in BEARISH:
                cfm_t_bear += 1
                if f5 is not None and f5 > 0:
                    cfm_t_bear_wrong += 1
                    wrong_signals.append((d, ph, "bear", f5))
            elif ph in BULLISH:
                cfm_t_bull += 1
                if f5 is not None and f5 < 0:
                    cfm_t_bull_wrong += 1
                    wrong_signals.append((d, ph, "bull", f5))
        out[c] = {
            "n_cfm_T_bear": cfm_t_bear,
            "n_wrong_bear": cfm_t_bear_wrong,
            "wrong_bear_pct": cfm_t_bear_wrong / max(1, cfm_t_bear) * 100,
            "n_cfm_T_bull": cfm_t_bull,
            "n_wrong_bull": cfm_t_bull_wrong,
            "wrong_bull_pct": cfm_t_bull_wrong / max(1, cfm_t_bull) * 100,
            "wrong_signals": wrong_signals,
        }
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("old_cache", type=Path)
    p.add_argument("new_cache", type=Path)
    p.add_argument("codes", type=str, help="comma-separated 6-digit codes")
    p.add_argument("--db", type=Path, default=REPO / "data/market_history.db")
    p.add_argument("--start-date", type=str, default="2025-10-17")
    p.add_argument("--end-date", type=str, default="2026-01-12")
    args = p.parse_args()

    codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    old = load_cache(args.old_cache)
    new = load_cache(args.new_cache)

    print(f"{'='*98}")
    print(f"COMPARE: window {args.start_date} → {args.end_date}, codes: {codes}")
    print(f"  OLD cache: {args.old_cache.name}")
    print(f"  NEW cache: {args.new_cache.name}")
    print(f"{'='*98}\n")

    old_audit = audit(old, codes, args.db, args.start_date, args.end_date)
    new_audit = audit(new, codes, args.db, args.start_date, args.end_date)

    print(f"{'code':<8}{'cfm_T_bear':>15}{'wrong/total':>15}{'wrong_pct':>12}  "
          f"{'cfm_T_bull':>13}{'wrong/total':>15}{'wrong_pct':>12}")
    print("-" * 95)
    for label, audit_dict in [("OLD", old_audit), ("NEW", new_audit)]:
        for c in codes:
            d = audit_dict[c]
            print(f"  {label} {c:<5}"
                  f"{d['n_cfm_T_bear']:>13}"
                  f"  {d['n_wrong_bear']:>3}/{d['n_cfm_T_bear']:<3}     "
                  f"{d['wrong_bear_pct']:>9.1f}%  "
                  f"{d['n_cfm_T_bull']:>11}"
                  f"  {d['n_wrong_bull']:>3}/{d['n_cfm_T_bull']:<3}     "
                  f"{d['wrong_bull_pct']:>9.1f}%")
        print()

    print(f"\n=== 错误派发信号清单（NEW prompt 下） ===")
    for c in codes:
        d = new_audit[c]
        wrong_bear = [s for s in d["wrong_signals"] if s[2] == "bear"]
        if not wrong_bear:
            print(f"\n{c}: 无错误派发信号 ✓")
            continue
        print(f"\n{c}: {len(wrong_bear)} 个错误派发:")
        for date, phase, _, f5 in wrong_bear:
            print(f"  {date}  {phase}  fwd5d={f5:+.1f}%")


if __name__ == "__main__":
    main()
