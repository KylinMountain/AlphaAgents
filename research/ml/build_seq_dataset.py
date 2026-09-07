"""为序列模型构建数据集 — 直接保存 60 天 OHLCV 序列 + 4 个 binary labels.

Output: data/vpa_seq_dataset.npz
  - X: (N, 60, 5)  60 天 × OHLCV (normalized)
  - y_topping, y_bottoming, y_continuation, y_breakdown: (N,) binary
  - meta: (N,) dict of {code, as_of}
"""
import json, sqlite3, time, sys, argparse, pickle
from collections import defaultdict
import numpy as np


SEQ_LEN = 60


def normalize_seq(closes, opens, highs, lows, vols):
    """Normalize each channel by today's close."""
    base_close = closes[-1]
    base_vol = max(np.mean(vols[-20:]), 1)
    arr = np.column_stack([
        np.array(opens) / base_close,
        np.array(highs) / base_close,
        np.array(lows) / base_close,
        np.array(closes) / base_close,
        np.array(vols) / base_vol,
    ])
    return arr.astype(np.float32)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--start', default='2024-07-01')
    p.add_argument('--end', default='2026-04-29')
    p.add_argument('--boards', default='300,301,302')
    p.add_argument('--day-stride', type=int, default=1)
    p.add_argument('--out', default='/Users/evilkylin/Projects/AlphaAgents/data/vpa_seq_dataset.npz')
    args = p.parse_args()

    boards = tuple(args.boards.split(','))
    print('Loading market history...', flush=True)
    conn = sqlite3.connect('/Users/evilkylin/Projects/AlphaAgents/data/market_history.db')
    hist = defaultdict(list)
    for code, date, op, hi, lo, cl, vol in conn.execute(
        'SELECT code, date, open, high, low, close, volume FROM daily_kline ORDER BY code, date'
    ):
        hist[code].append((date, op, hi, lo, cl, vol))

    target_codes = sorted([c for c in hist if c.startswith(boards)])
    print(f'  {len(target_codes)} codes', flush=True)

    all_dates = sorted(set(d for rows in hist.values() for d, *_ in rows))
    eval_dates = [d for d in all_dates if args.start <= d <= args.end][::args.day_stride]
    print(f'  {len(eval_dates)} eval dates', flush=True)

    X_list = []
    y_topping = []; y_bottoming = []; y_continuation = []; y_breakdown = []
    meta = []
    t0 = time.time()
    n_done = 0

    for di, as_of in enumerate(eval_dates):
        for code in target_codes:
            rows = hist.get(code, [])
            idx = next((i for i, r in enumerate(rows) if r[0] == as_of), None)
            if idx is None or idx < SEQ_LEN: continue
            window = rows[idx-SEQ_LEN+1:idx+1]
            if len(window) < SEQ_LEN: continue

            closes = [r[4] for r in window]
            opens = [r[1] for r in window]
            highs = [r[2] for r in window]
            lows = [r[3] for r in window]
            vols = [r[5] for r in window]
            if closes[-1] <= 0: continue

            # Future outcomes (need 20 days)
            future = rows[idx+1:idx+21]
            if len(future) < 5: continue
            f_closes = [r[4] for r in future]
            base = closes[-1]
            rets = [(c - base) / base * 100 for c in f_closes]

            gain_20 = max(rets[:20]) if len(rets) >= 20 else max(rets)
            dd_20 = min(rets[:20]) if len(rets) >= 20 else min(rets)
            close_ret_5 = rets[4] if len(rets) >= 5 else 0

            # Need full 20-day forward
            if len(rets) < 20: continue

            arr = normalize_seq(closes, opens, highs, lows, vols)
            X_list.append(arr)
            y_topping.append(int(dd_20 < -15))
            y_bottoming.append(int(gain_20 > 15))
            y_continuation.append(int(close_ret_5 > 3))
            y_breakdown.append(int(close_ret_5 < -3))
            meta.append({'code': code, 'as_of': as_of})
            n_done += 1

        if (di+1) % 10 == 0:
            elapsed = time.time() - t0
            eta = elapsed/(di+1) * (len(eval_dates) - di - 1)
            print(f'  {di+1}/{len(eval_dates)} {as_of}: total {n_done} | {elapsed:.0f}s ETA {eta:.0f}s', flush=True)

    X = np.array(X_list, dtype=np.float32)
    y_topping = np.array(y_topping, dtype=np.int8)
    y_bottoming = np.array(y_bottoming, dtype=np.int8)
    y_continuation = np.array(y_continuation, dtype=np.int8)
    y_breakdown = np.array(y_breakdown, dtype=np.int8)

    np.savez_compressed(args.out, X=X,
        y_topping=y_topping, y_bottoming=y_bottoming,
        y_continuation=y_continuation, y_breakdown=y_breakdown)
    # save meta separately
    with open(args.out.replace('.npz', '_meta.pkl'), 'wb') as g:
        pickle.dump(meta, g)
    print(f'\nDONE: {n_done} sequences ({X.shape}) saved to {args.out}')
    print(f'Label rates: topping={y_topping.mean():.2%}, bottoming={y_bottoming.mean():.2%}, '
          f'continuation={y_continuation.mean():.2%}, breakdown={y_breakdown.mean():.2%}')


if __name__ == '__main__':
    main()
