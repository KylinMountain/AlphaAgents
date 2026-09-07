"""Build ML training dataset:
For each (code, as_of) over the historical window, run anna_screener_v5 to extract features
+ compute realized P&L (max_gain / max_dd over 5/10/20 day windows).

Output: data/ml_dataset.jsonl — each line is one training example.

Usage:
    python scripts/build_ml_dataset.py --start 2024-01-01 --end 2026-04-29
    --boards 创业板 --top-only false
"""
import sys, json, sqlite3, time, argparse
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, '/Users/evilkylin/Projects/AlphaAgents')
sys.path.insert(0, '/Users/evilkylin/Projects/AlphaAgents/scripts')
from dotenv import load_dotenv
load_dotenv('/Users/evilkylin/Projects/AlphaAgents/.env', override=True)

import anna_screener_v5 as v5


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--start', default='2024-01-01')
    p.add_argument('--end', default='2026-04-29')
    p.add_argument('--boards', default='300,301,302', help='comma-separated code prefixes')
    p.add_argument('--out', default='/Users/evilkylin/Projects/AlphaAgents/data/ml_dataset.jsonl')
    p.add_argument('--horizons', default='5,10,20')
    p.add_argument('--min-amount', type=float, default=1.0, help='liquidity threshold (亿)')
    p.add_argument('--day-stride', type=int, default=1, help='take every N-th day to reduce volume')
    args = p.parse_args()

    boards = tuple(args.boards.split(','))
    horizons = [int(h) for h in args.horizons.split(',')]

    conn = sqlite3.connect('/Users/evilkylin/Projects/AlphaAgents/data/market_history.db')
    print('loading market history...', flush=True)
    hist = defaultdict(list)
    for code, date, close in conn.execute('SELECT code, date, close FROM daily_kline ORDER BY code, date'):
        hist[code].append((date, close))

    target_codes = sorted([c for c in hist if c.startswith(boards)])
    print(f'  {len(target_codes)} target codes (boards: {boards})')

    all_dates = sorted(set(d for rows in hist.values() for d, _ in rows))
    eval_dates = [d for d in all_dates if args.start <= d <= args.end]
    if args.day_stride > 1:
        eval_dates = eval_dates[::args.day_stride]
    print(f'  {len(eval_dates)} eval dates ({eval_dates[0]} → {eval_dates[-1]})')

    def excursion(code, as_of, w):
        rows = hist.get(code, [])
        idx = next((i for i, (d, _) in enumerate(rows) if d == as_of), None)
        if idx is None: return None, None
        base = rows[idx][1]
        if base <= 0: return None, None
        end = min(idx + w + 1, len(rows))
        if end - idx < 2: return None, None
        rets = [(rows[i][1] - base) / base * 100 for i in range(idx + 1, end)]
        return max(rets), min(rets)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    n_total = n_passed = 0
    t0 = time.time()
    with open(out_path, 'w') as f:
        for di, as_of in enumerate(eval_dates):
            try:
                baseline = v5._compute_market_baseline_60d(target_codes, as_of)
            except Exception:
                baseline = 0.0
            day_passed = 0
            for code in target_codes:
                n_total += 1
                try:
                    r = v5.screen_one(code, as_of=as_of, min_amount=args.min_amount, market_baseline_60d=baseline)
                except Exception:
                    r = None
                if r is None:
                    continue
                day_passed += 1
                # Compute forward excursions
                fwds = {}
                for h in horizons:
                    g, d = excursion(code, as_of, h)
                    fwds[f'gain_{h}'] = g
                    fwds[f'dd_{h}'] = d
                row = {
                    'as_of': as_of,
                    **r,  # includes all features + score + signal_type
                    **fwds,
                }
                f.write(json.dumps(row, ensure_ascii=False) + '\n')
                n_passed += 1
            if (di + 1) % 5 == 0 or di == len(eval_dates) - 1:
                elapsed = time.time() - t0
                eta = elapsed / (di + 1) * (len(eval_dates) - di - 1)
                print(f'  {di+1}/{len(eval_dates)} {as_of} | day_passed={day_passed} total_passed={n_passed} | {elapsed:.0f}s ETA={eta:.0f}s', flush=True)

    print(f'\nDONE: {n_passed}/{n_total} rows saved to {out_path}')


if __name__ == '__main__':
    main()
