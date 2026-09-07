"""Phase 2 应用: 把 winner classifier 应用到全市场 (5000+ 股) 在 2025-09-23,
看 Top-50 候选实际 forward 60d/全期收益分布。

验证: 如果 Top-50 平均收益显著高于 base rate, 说明分类器有真实选股价值。
"""
from __future__ import annotations
import json, pickle, sqlite3, sys
from pathlib import Path
import numpy as np
import pandas as pd

sys.path.insert(0, '/Users/evilkylin/Projects/AlphaAgents/scripts')
from winner_phase2_train import (
    BOARDS, V91_AS_OF, board_label, extract_features, load_stock_history
)

EVAL_DATE = '2025-09-23'
EVAL_END = '2026-04-24'   # 与 v9.1 backtest 区间一致 (~7 个月)
PEAK_60D_END = '2025-11-22'  # ~60 trading days from EVAL_DATE


def main():
    by_code = load_stock_history()
    feat_cols = ['trend_5d', 'trend_10d', 'trend_30d', 'trend_60d', 'trend_accel',
                  'volatility_30d', 'range_pos_30d', 'range_pos_60d',
                  'dist_from_low_60d', 'dist_from_high_60d',
                  'at_60d_low', 'at_30d_high',
                  'vol_ratio_mean_30d', 'vol_ratio_max_30d', 'vol_ratio_p75_30d',
                  'vol_accel_5_30', 'vol_accel_30_60',
                  'n_up_days_30d', 'up_down_vol_ratio',
                  'body_pct_mean_30d', 'upper_wick_pct_mean_30d',
                  'lower_wick_pct_mean_30d',
                  'max_pullback_30d', 'max_rally_30d', 'board']

    print(f'Loading model...', flush=True)
    with open('/Users/evilkylin/Projects/AlphaAgents/data/winner_model.pkl', 'rb') as f:
        clf = pickle.load(f)

    print(f'\nApplying to all stocks at {EVAL_DATE} ...', flush=True)
    rows = []
    for code, g in by_code.items():
        idx = g.index[g['date'] == EVAL_DATE]
        if len(idx) == 0:
            mask = g['date'] >= EVAL_DATE
            if mask.sum() == 0: continue
            idx_t = int(g.index[mask][0])
        else:
            idx_t = int(idx[0])
        if idx_t < 60 or idx_t + 60 >= len(g):
            continue
        feats = extract_features(g, idx_t)
        if feats is None: continue
        feats['code'] = code
        feats['board'] = board_label(code)
        rows.append(feats)

    print(f'  {len(rows)} stocks scored')

    df = pd.DataFrame(rows)
    if hasattr(clf, 'predict_proba'):
        df['proba'] = clf.predict_proba(df[feat_cols])[:, 1]
    else:
        df['proba'] = clf.predict(df[feat_cols])

    # Compute actual forward returns for each
    print(f'\nComputing actual forward returns (60d peak + terminal at {EVAL_END}) ...')
    forward_data = []
    for _, r in df.iterrows():
        code = r['code']
        g = by_code[code]
        idx = g.index[g['date'] == EVAL_DATE]
        if len(idx) == 0:
            mask = g['date'] >= EVAL_DATE
            if mask.sum() == 0: continue
            idx_t = int(g.index[mask][0])
        else:
            idx_t = int(idx[0])
        c_t = g.iloc[idx_t]['close']
        if c_t <= 0:
            continue
        forward_60 = g.iloc[idx_t+1:idx_t+61]['close']
        forward_full = g.iloc[idx_t+1:]['close']
        forward_full = forward_full[g.iloc[idx_t+1:]['date'] <= EVAL_END]
        peak_60d = (forward_60.max() - c_t) / c_t if len(forward_60) > 30 else float('nan')
        terminal = (forward_full.iloc[-1] - c_t) / c_t if len(forward_full) > 0 else float('nan')
        peak_full = (forward_full.max() - c_t) / c_t if len(forward_full) > 0 else float('nan')
        forward_data.append({
            'code': code, 'peak_60d': peak_60d,
            'peak_full': peak_full, 'terminal': terminal,
        })
    fwd = pd.DataFrame(forward_data)
    df = df.merge(fwd, on='code')

    # Sort by predicted prob descending
    df = df.sort_values('proba', ascending=False).reset_index(drop=True)
    df = df.dropna(subset=['peak_60d'])

    # Base rate (all stocks)
    base_peak60 = df['peak_60d'].mean() * 100
    base_terminal = df['terminal'].mean() * 100
    base_pct_winner = (df['peak_60d'] >= 0.50).mean() * 100

    print(f'\n=== Base rate (all {len(df)} stocks) ===')
    print(f'  Mean 60d peak:    {base_peak60:+.2f}%')
    print(f'  Mean terminal:    {base_terminal:+.2f}%')
    print(f'  Pct winners (≥50% peak in 60d): {base_pct_winner:.1f}%')

    print(f'\n=== Top-K predicted candidates ===')
    print(f'{"K":>5} {"mean_peak_60d":>14} {"mean_terminal":>14} {"pct_winner":>11} {"vs base lift":>13}')
    for k in [20, 50, 100, 200, 500]:
        if len(df) < k: continue
        top = df.head(k)
        peak60 = top['peak_60d'].mean() * 100
        term = top['terminal'].mean() * 100
        pct_win = (top['peak_60d'] >= 0.50).mean() * 100
        lift_peak = peak60 / max(base_peak60, 1e-9)
        print(f'{k:>5} {peak60:>13.2f}% {term:>13.2f}% {pct_win:>10.1f}% {lift_peak:>12.2f}x')

    # Distribution by board in top 50
    print(f'\n=== Top 50 board distribution ===')
    top50 = df.head(50)
    board_names = {0: '深主板', 1: '创业板', 2: '沪主板', 3: '科创板'}
    for b, n in top50['board'].value_counts().items():
        print(f'  {board_names.get(b, str(b))}: {n}')

    # Detail top 20
    print(f'\n=== Top 20 detail ===')
    print(f'{"Rank":>4} {"Code":<8} {"Prob":>6} {"Peak60d":>10} {"Terminal":>10} {"Status":>10}')
    for i, r in df.head(20).iterrows():
        status = '✓ winner' if r['peak_60d'] >= 0.50 else ('partial' if r['peak_60d'] >= 0.20 else '✗ loser')
        print(f'{i+1:>4} {r["code"]:<8} {r["proba"]:>6.3f} {r["peak_60d"]*100:>9.1f}% {r["terminal"]*100:>9.1f}% {status:>10}')

    # Save
    df[['code', 'proba', 'peak_60d', 'peak_full', 'terminal']].to_csv(
        '/tmp/winner_phase2_full_market_at_20250923.csv', index=False
    )
    print(f'\n[saved] /tmp/winner_phase2_full_market_at_20250923.csv')


if __name__ == '__main__':
    main()
