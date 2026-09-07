"""Entry × Exit phase matrix on a VPA cache.

Reads a reparsed cache jsonl + market_history.db and runs every
(entry_phase, exit_phase) pair as a phase-only strategy, reporting
per-cell PnL / trades / win-rate. Pure local computation, no LLM calls.

Usage:
    python scripts/analyze_vpa_matrix.py CACHE.jsonl POOL_CSV
        [--start-date YYYY-MM-DD] [--end-date YYYY-MM-DD]
        [--initial-capital N] [--position-pct 0.10] [--max-positions 10]

The pool CSV should have a 6-digit code in the second column (matching the
cybetf_159915_top20 format). The first column is treated as a header row.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean

REPO = Path(__file__).resolve().parent.parent

# ── A-share execution costs (mirrors alpha_agents.data.portfolio) ────────
LOT_SIZE = 100
COMMISSION_RATE = 0.0003
MIN_COMMISSION = 5.0
STAMP_DUTY_SELL_RATE = 0.0005
TRANSFER_FEE_RATE = 0.00001
SLIPPAGE_RATE = 0.0005


def net_pnl(open_px: float, close_px: float, shares: int) -> float:
    if open_px <= 0 or close_px <= 0 or shares <= 0:
        return 0.0
    bp = open_px * (1 + SLIPPAGE_RATE)
    sp = close_px * (1 - SLIPPAGE_RATE)
    bv, sv = bp * shares, sp * shares
    bc = max(bv * COMMISSION_RATE, MIN_COMMISSION)
    sc = max(sv * COMMISSION_RATE, MIN_COMMISSION)
    cost_basis = bv + bc + bv * TRANSFER_FEE_RATE
    proceeds = sv - sc - sv * TRANSFER_FEE_RATE - sv * STAMP_DUTY_SELL_RATE
    return proceeds - cost_basis


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
    codes: list[str] = []
    with open(pool_csv, encoding="utf-8") as f:
        next(f)  # header
        for line in f:
            parts = line.split(",")
            if len(parts) < 2:
                continue
            c = parts[1].strip()
            if c.isdigit() and len(c) == 6:
                codes.append(c)
    return codes


def load_ohlc(db_path: Path, codes: list[str]) -> dict:
    conn = sqlite3.connect(str(db_path))
    ohlc: dict[str, dict] = {}
    for c in codes:
        rows = conn.execute(
            "SELECT date,open,high,low,close FROM daily_kline "
            "WHERE code=? ORDER BY date",
            (c,),
        ).fetchall()
        ohlc[c] = {
            r[0]: (r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]))
            for r in rows if r[1] and r[4]
        }
    return ohlc


def simulate(
    cache: dict, codes: list[str], ohlc: dict, eval_dates: list[str],
    didx: dict, *, entry_pred, exit_pred,
    initial_capital: float, position_pct: float, max_positions: int,
):
    pos, pend = {}, {}
    trades = []
    for d in eval_dates:
        for code, act in list(pend.items()):
            bar = ohlc.get(code, {}).get(d)
            if not bar:
                pend.pop(code)
                continue
            op = float(bar[1] or 0)
            if op <= 0:
                pend.pop(code)
                continue
            if act["action"] == "buy" and code not in pos and len(pos) < max_positions:
                shares = int(initial_capital * position_pct / op) // LOT_SIZE * LOT_SIZE
                if shares > 0:
                    pos[code] = {"entry_date": d, "entry_price": op, "shares": shares}
            elif act["action"] == "sell" and code in pos:
                p = pos.pop(code)
                pnl = net_pnl(p["entry_price"], op, p["shares"])
                trades.append({
                    "pnl": pnl,
                    "ret_pct": pnl / (p["entry_price"] * p["shares"]) * 100,
                    "holding": didx[d] - didx[p["entry_date"]],
                })
            pend.pop(code, None)
        for code in codes:
            v = cache.get((d, code))
            if not v or not v.get("ok"):
                continue
            if code in pos:
                if exit_pred(v):
                    pend[code] = {"action": "sell", "vpa": v}
            else:
                if entry_pred(v):
                    pend[code] = {"action": "buy", "vpa": v}
    last = eval_dates[-1]
    for code, p in list(pos.items()):
        bar = ohlc.get(code, {}).get(last)
        if not bar:
            continue
        cp = float(bar[4] or 0)
        if cp <= 0:
            continue
        pnl = net_pnl(p["entry_price"], cp, p["shares"])
        trades.append({
            "pnl": pnl,
            "ret_pct": pnl / (p["entry_price"] * p["shares"]) * 100,
            "holding": didx[last] - didx[p["entry_date"]],
        })
    return trades


def make_pred(target_phase: str, cfm_req=None):
    def pred(v):
        if (v.get("llm_phase", "") or "") != target_phase:
            return False
        if cfm_req is not None and bool(v.get("llm_confirmed")) != cfm_req:
            return False
        return True
    return pred


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("cache", type=Path)
    p.add_argument("pool_csv", type=Path)
    p.add_argument("--db", type=Path, default=REPO / "data/market_history.db")
    p.add_argument("--start-date", type=str, default="2025-10-17")
    p.add_argument("--end-date", type=str, default="2026-04-16")
    p.add_argument("--initial-capital", type=float, default=1_000_000)
    p.add_argument("--position-pct", type=float, default=0.10)
    p.add_argument("--max-positions", type=int, default=10)
    p.add_argument("--exit-cfm", action=argparse.BooleanOptionalAction, default=True,
                   help="exit cells require confirmed=True (default on)")
    args = p.parse_args()

    cache = load_cache(args.cache)
    codes = load_codes(args.pool_csv)
    ohlc = load_ohlc(args.db, codes)

    all_dates = sorted({d for code_d in ohlc.values() for d in code_d.keys()})
    didx = {d: i for i, d in enumerate(all_dates)}
    eval_dates = sorted({
        d for (d, _) in cache.keys() if args.start_date <= d <= args.end_date
    })

    print(f"cache: {len(cache)} obs  codes: {len(codes)}  eval_dates: {len(eval_dates)} "
          f"({eval_dates[0]} → {eval_dates[-1]})  exit_cfm_required={args.exit_cfm}\n")

    cell_n = defaultdict(int)
    for (d, c), v in cache.items():
        if v.get("ok") and args.start_date <= d <= args.end_date:
            cell_n[v.get("llm_phase", "") or ""] += 1

    ENTRIES = ["吸筹", "拉升", "震荡", "吸筹尾声", "卖压衰竭", "派发初期",
               "吸筹初期", "吸筹末期"]
    EXITS = ["派发", "派发初期", "派发中期", "派发尾声", "抛售高峰", "下跌"]
    cfm_req = True if args.exit_cfm else None

    def run(entry_pred, exit_pred):
        return simulate(cache, codes, ohlc, eval_dates, didx,
                        entry_pred=entry_pred, exit_pred=exit_pred,
                        initial_capital=args.initial_capital,
                        position_pct=args.position_pct,
                        max_positions=args.max_positions)

    print("=" * 130)
    print("ENTRY × EXIT MATRIX (PnL / trades / wins)   '*' = entry cell_n < 8")
    print("=" * 130)
    header = f"{'entry':<10}{'cell_n':>7}  " + "".join(f"{e[:8]:>14}" for e in EXITS) + f"{'NO_EXIT':>14}"
    print(header)
    print("-" * len(header))

    matrix = {}
    for ent in ENTRIES:
        n = cell_n.get(ent, 0)
        flag = "*" if n < 8 else " "
        line = f"{ent:<10}{flag}{n:>5}   "
        for exo in EXITS:
            trades = run(make_pred(ent), make_pred(exo, cfm_req=cfm_req))
            matrix[(ent, exo)] = trades
            if trades:
                pnl = sum(t["pnl"] for t in trades)
                w = sum(1 for t in trades if t["pnl"] > 0)
                line += f"{pnl:>+8,.0f}/{len(trades):>2}/{w:<2}"
            else:
                line += f"{'-':>14}"
        ne = run(make_pred(ent), lambda v: False)
        matrix[(ent, "NO_EXIT")] = ne
        if ne:
            pnl = sum(t["pnl"] for t in ne)
            w = sum(1 for t in ne if t["pnl"] > 0)
            line += f"{pnl:>+8,.0f}/{len(ne):>2}/{w:<2}"
        else:
            line += f"{'-':>14}"
        print(line)

    print("\n" + "=" * 90)
    print("BEST exit per entry (vs NO_EXIT baseline)")
    print("=" * 90)
    print(f"{'entry':<12}{'cell_n':>7}{'best_exit':>14}{'best_pnl':>14}{'no_exit_pnl':>14}{'lift':>12}")
    for ent in ENTRIES:
        no_exit = matrix.get((ent, "NO_EXIT"), [])
        ne_pnl = sum(t["pnl"] for t in no_exit) if no_exit else 0
        candidates = []
        for exo in EXITS:
            trs = matrix.get((ent, exo), [])
            if not trs:
                continue
            pnl = sum(t["pnl"] for t in trs)
            candidates.append((exo, pnl, len(trs)))
        if not candidates:
            print(f"{ent:<12}{cell_n.get(ent,0):>7}    no trades")
            continue
        candidates.sort(key=lambda x: -x[1])
        best_exo, best_pnl, _ = candidates[0]
        lift = best_pnl - ne_pnl
        print(f"{ent:<12}{cell_n.get(ent,0):>7}{best_exo:>14}"
              f"{best_pnl:>+13,.0f}{ne_pnl:>+13,.0f}{lift:>+11,.0f}")

    print("\n" + "=" * 90)
    print("TOP 10 (entry, exit) cells by PnL")
    print("=" * 90)
    ranked = []
    for (ent, exo), trades in matrix.items():
        if exo == "NO_EXIT" or not trades:
            continue
        pnl = sum(t["pnl"] for t in trades)
        w = sum(1 for t in trades if t["pnl"] > 0)
        ranked.append((ent, exo, len(trades), w, pnl))
    ranked.sort(key=lambda r: -r[4])
    print(f"{'entry':<12}{'exit':<12}{'trades':>8}{'win':>6}{'pnl':>14}")
    for ent, exo, n, w, pnl in ranked[:10]:
        print(f"{ent:<12}{exo:<12}{n:>8}{w:>4}/{n:<2}{pnl:>+13,.0f}")


if __name__ == "__main__":
    main()
