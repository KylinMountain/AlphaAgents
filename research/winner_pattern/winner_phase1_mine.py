"""Phase 1: Winner-pattern mining 数据集构建.

目标:
  在历史数据上找所有"winner instance" — 即 60 天内 peak gain ≥ 50% 的事件,
  作为 Phase 2 特征挖掘 + Phase 3 分类器训练的目标变量.

定义:
  for each (code, date_t) in [TRAIN_START, TRAIN_END]:
      peak_gain_60d = max(close[t+1:t+61]) / close[t] - 1
      if peak_gain_60d >= 0.50:
          → "winner instance" at (code, date_t)
          → record: code, date_t, peak_date, peak_close, peak_gain, days_to_peak

去重:
  对每只股,按时间顺序扫描. 一旦在 t 触发,跳到 t + days_to_peak 之后再继续扫描
  (避免连续多日都被标 winner — 它们其实是同一个 rise event).

Train period: 2024-04-01 → 2025-12-31 (winner trigger 必须落在这个区间)
Holdout    : 2026-01-01 → 2026-04-30 (Phase 5 测试用)
"""
from __future__ import annotations
import argparse, sqlite3, json
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd


DB = '/Users/evilkylin/Projects/AlphaAgents/data/market_history.db'
TRAIN_START = '2024-04-01'
TRAIN_END = '2025-12-31'
HOLDOUT_END = '2026-04-30'

BOARD_PREFIXES = ('000', '001', '002', '003',     # 深圳主板 + 中小板
                   '300', '301',                    # 创业板
                   '600', '601', '603', '605',      # 上海主板
                   '688')                           # 科创板


def board_name(code: str) -> str:
    if code.startswith(('000', '001', '002', '003')): return '深主板'
    if code.startswith(('300', '301', '302')):       return '创业板'
    if code.startswith(('600', '601', '603', '605')): return '沪主板'
    if code.startswith(('688', '689')):              return '科创板'
    if code.startswith('920'):                        return '北交所'
    return '其他'


def load_data(boards):
    print(f'Loading {boards} ...', flush=True)
    conn = sqlite3.connect(DB)
    prefix_clauses = ' OR '.join(f"code LIKE '{p}%'" for p in boards)
    df = pd.read_sql_query(
        f"SELECT code, date, close FROM daily_kline "
        f"WHERE ({prefix_clauses}) AND date >= '{TRAIN_START}' AND date <= '{HOLDOUT_END}' "
        f"ORDER BY code, date",
        conn
    )
    print(f'  {len(df):,} rows, {df.code.nunique()} codes')
    return df


def find_winners(df: pd.DataFrame, peak_thr: float, lookforward: int):
    """For each (code, date_t) in TRAIN window, check if next `lookforward`
    days had peak_gain >= peak_thr. Dedupe overlapping events.
    """
    winners = []
    skipped_no_data = 0
    for code, g in df.groupby('code'):
        g = g.reset_index(drop=True)
        n = len(g)
        if n < lookforward + 5:
            skipped_no_data += 1
            continue
        closes = g['close'].astype(float).values
        dates = g['date'].values

        # Iterate chronologically with skip-after-trigger dedupe
        i = 0
        while i < n - lookforward:
            d_t = dates[i]
            if d_t < TRAIN_START or d_t > TRAIN_END:
                i += 1
                continue
            c_t = closes[i]
            if c_t <= 0:
                i += 1
                continue
            window = closes[i+1 : i+1+lookforward]
            if len(window) < lookforward // 2:  # not enough forward data
                i += 1
                continue
            peak_idx_in_window = int(np.argmax(window))
            peak_close = window[peak_idx_in_window]
            peak_gain = (peak_close - c_t) / c_t
            if peak_gain >= peak_thr:
                days_to_peak = peak_idx_in_window + 1
                peak_date = dates[i + days_to_peak]
                winners.append({
                    'code': code,
                    'date_t': d_t,
                    'close_t': float(c_t),
                    'peak_date': peak_date,
                    'peak_close': float(peak_close),
                    'peak_gain': float(peak_gain),
                    'days_to_peak': int(days_to_peak),
                    'board': board_name(code),
                })
                # Dedupe: skip past the peak
                i += max(days_to_peak, 5)
            else:
                i += 1
    return winners, skipped_no_data


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--peak-thr', type=float, default=0.50, help='peak gain threshold (0.50 = 50%)')
    p.add_argument('--lookforward', type=int, default=60)
    p.add_argument('--boards', type=str, default=','.join(BOARD_PREFIXES))
    args = p.parse_args()

    boards = tuple(args.boards.split(','))
    df = load_data(boards)
    winners, skipped = find_winners(df, args.peak_thr, args.lookforward)

    print(f'\n=== Winner pattern mining results ===')
    print(f'  Window: {args.lookforward}d forward, peak gain ≥ {args.peak_thr*100:.0f}%')
    print(f'  Train period: {TRAIN_START} → {TRAIN_END}')
    print(f'  Stocks scanned: {df.code.nunique() - skipped}, skipped (insufficient data): {skipped}')
    print(f'  Total winner instances: {len(winners):,}')

    if not winners:
        print('No winners found.')
        return

    pdf = pd.DataFrame(winners)

    # By board
    print(f'\n--- By board ---')
    for b, g in pdf.groupby('board'):
        n_codes = g['code'].nunique()
        print(f'  {b:<8} N_instances={len(g):>5}  N_codes={n_codes:>4}  '
              f'peak_gain mean {g["peak_gain"].mean()*100:>5.1f}%  '
              f'median {g["peak_gain"].median()*100:>5.1f}%  '
              f'max {g["peak_gain"].max()*100:>5.0f}%')

    # Peak gain distribution
    print(f'\n--- Peak gain distribution ---')
    bins = [0.5, 0.7, 1.0, 1.5, 2.0, 3.0, 5.0, 100.0]
    for lo, hi in zip(bins[:-1], bins[1:]):
        n = ((pdf['peak_gain'] >= lo) & (pdf['peak_gain'] < hi)).sum()
        print(f'  [{lo*100:>3.0f}% - {hi*100:>3.0f}%): {n:>5}')

    # Days-to-peak distribution
    print(f'\n--- Days to peak distribution ---')
    print(f'  mean: {pdf["days_to_peak"].mean():.1f}, median: {pdf["days_to_peak"].median():.1f}, '
          f'min: {pdf["days_to_peak"].min()}, max: {pdf["days_to_peak"].max()}')
    for lo, hi in [(1, 7), (8, 14), (15, 30), (31, 45), (46, 61)]:
        n = ((pdf['days_to_peak'] >= lo) & (pdf['days_to_peak'] <= hi)).sum()
        print(f'  {lo:>2}-{hi:>2} days: {n:>5}')

    # Top stocks by # winners (multiple rises in train period)
    print(f'\n--- Top 10 stocks by # winner instances (multiple rises) ---')
    top = pdf.groupby('code').size().sort_values(ascending=False).head(10)
    for code, n in top.items():
        sub = pdf[pdf.code == code]
        avg_gain = sub['peak_gain'].mean() * 100
        print(f'  {code} ({sub.iloc[0]["board"]}) N={n}, avg peak_gain {avg_gain:.0f}%, '
              f'instances at {", ".join(sub["date_t"].head(3).tolist())} ...')

    # Time distribution
    print(f'\n--- Winner instances by month ---')
    pdf['month'] = pdf['date_t'].str[:7]
    by_month = pdf.groupby('month').size()
    for m, n in by_month.items():
        bar = '█' * (n // 5 + 1)
        print(f'  {m}: {n:>4}  {bar}')

    # Save
    out = Path('/Users/evilkylin/Projects/AlphaAgents/data') / f'winners_p{int(args.peak_thr*100)}_lf{args.lookforward}.jsonl'
    pdf.drop(columns=['month'], inplace=True)
    with open(out, 'w') as f:
        for r in pdf.to_dict('records'):
            f.write(json.dumps(r, ensure_ascii=False) + '\n')
    print(f'\n[saved] {out}')


if __name__ == '__main__':
    main()
