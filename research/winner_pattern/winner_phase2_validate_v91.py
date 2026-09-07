"""真正的验证 - 在 v9.1 每个 BUY 信号触发当日, 测分类器能否区分 winner vs loser.

数据流:
  Step 1: 载入 v9.1 cache, 找出所有"BUY 信号"日 (verdict bullish + level >= 2)
  Step 2: 在每个 (code, signal_date) 提取 phase2 特征 + 应用 classifier
  Step 3: 计算实际 forward 收益 (从 signal_date 后 60 天 peak + terminal at 2026-04-24)
  Step 4: 看 classifier prob 跟实际收益的相关性
  Step 5: 模拟"v9.1 + classifier filter" 策略: 只在 prob > threshold 时实际买入
"""
from __future__ import annotations
import json, pickle, sqlite3, sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, '/Users/evilkylin/Projects/AlphaAgents/scripts')
from winner_phase2_train import (
    BOARDS, board_label, extract_features, load_stock_history
)

EVAL_END = '2026-04-24'  # v9.1 backtest 区间末
BULL_VERDICTS = {'bullish', '看多', '偏多'}


def load_v91_buy_signals(jsonl_path):
    """每只股的每个 BUY 信号天 (verdict bullish + level>=2)."""
    signals = []
    with open(jsonl_path) as f:
        for line in f:
            obj = json.loads(line)
            res = obj['result']
            verdict = str(res.get('llm_verdict', ''))
            if verdict not in BULL_VERDICTS:
                continue
            level = int(res.get('llm_confirmation_level', 0) or 0)
            if level < 2:
                continue
            signals.append({
                'date': obj['date'],
                'code': obj['code'],
                'verdict': verdict,
                'level': level,
                'phase': res.get('llm_phase', ''),
                'confidence': float(res.get('llm_confidence', 0)),
            })
    return signals


def main():
    print('Loading v9.1 cache + finding BUY signals (verdict bullish + level>=2)...', flush=True)
    signals = load_v91_buy_signals('/Users/evilkylin/Projects/AlphaAgents/data/vpa_llm_walkforward_hy3_top20cyb_v91.jsonl')
    print(f'  {len(signals)} BUY signal events across {len(set(s["code"] for s in signals))} codes')

    print('\nLoading market data...')
    by_code = load_stock_history()

    print('Loading classifier...')
    with open('/Users/evilkylin/Projects/AlphaAgents/data/winner_model.pkl', 'rb') as f:
        clf = pickle.load(f)

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

    # For each BUY signal: compute features at signal_date, apply classifier, compute forward returns
    print('\nScoring each BUY signal...', flush=True)
    rows = []
    miss = 0
    for sig in signals:
        code = sig['code']
        if code not in by_code:
            miss += 1; continue
        g = by_code[code]
        idx = g.index[g['date'] == sig['date']]
        if len(idx) == 0:
            miss += 1; continue
        idx_t = int(idx[0])
        feats = extract_features(g, idx_t)
        if feats is None:
            miss += 1; continue
        feats['board'] = board_label(code)

        # Apply classifier
        x = pd.DataFrame([feats])[feat_cols]
        if hasattr(clf, 'predict_proba'):
            proba = float(clf.predict_proba(x)[0, 1])
        else:
            proba = float(clf.predict(x)[0])

        # Forward returns
        c_t = g.iloc[idx_t]['close']
        forward_60 = g.iloc[idx_t+1:idx_t+61]['close'].values
        forward_full = g.iloc[idx_t+1:][g.iloc[idx_t+1:]['date'] <= EVAL_END]['close'].values
        peak_60d = (forward_60.max() - c_t) / c_t if len(forward_60) > 30 else float('nan')
        peak_full = (forward_full.max() - c_t) / c_t if len(forward_full) > 0 else float('nan')
        terminal = (forward_full[-1] - c_t) / c_t if len(forward_full) > 0 else float('nan')

        rows.append({
            'date': sig['date'], 'code': code,
            'phase': sig['phase'], 'level': sig['level'],
            'proba': proba,
            'peak_60d': peak_60d, 'peak_full': peak_full, 'terminal': terminal,
        })

    print(f'  scored {len(rows)} signals (skipped {miss})')
    df = pd.DataFrame(rows).dropna(subset=['peak_60d'])

    # Overall stats
    print(f'\n=== ALL v9.1 BUY signals (n={len(df)}) ===')
    print(f'  Mean peak_60d: {df["peak_60d"].mean()*100:+.2f}%')
    print(f'  Mean terminal: {df["terminal"].mean()*100:+.2f}%')
    print(f'  Pct winners (peak ≥50%): {(df["peak_60d"]>=0.5).mean()*100:.1f}%')

    # Bin by classifier probability
    print(f'\n=== By classifier probability quintiles ===')
    df['proba_quintile'] = pd.qcut(df['proba'], 5, labels=['Q1 lowest', 'Q2', 'Q3', 'Q4', 'Q5 highest'])
    quintile_stats = df.groupby('proba_quintile', observed=True).agg(
        n=('proba', 'size'),
        avg_proba=('proba', 'mean'),
        avg_peak60=('peak_60d', 'mean'),
        avg_terminal=('terminal', 'mean'),
        pct_winner=('peak_60d', lambda x: (x >= 0.5).mean()),
    )
    print(quintile_stats.to_string())

    # Correlation
    print(f'\n=== Correlation between classifier prob & forward returns ===')
    pearson_peak = df[['proba', 'peak_60d']].corr().iloc[0, 1]
    pearson_terminal = df[['proba', 'terminal']].corr().iloc[0, 1]
    spearman_peak = df[['proba', 'peak_60d']].corr(method='spearman').iloc[0, 1]
    spearman_terminal = df[['proba', 'terminal']].corr(method='spearman').iloc[0, 1]
    print(f'  Pearson (proba, peak_60d):  {pearson_peak:+.3f}')
    print(f'  Pearson (proba, terminal):  {pearson_terminal:+.3f}')
    print(f'  Spearman (proba, peak_60d): {spearman_peak:+.3f}')
    print(f'  Spearman (proba, terminal): {spearman_terminal:+.3f}')

    # Threshold simulation: if we filter to prob >= threshold, what does P&L look like?
    print(f'\n=== Simulated: v9.1 + classifier filter ===')
    print(f'{"threshold":>10} {"n_signals":>10} {"keep_pct":>8} {"avg_peak60":>11} {"avg_term":>11} {"pct_win":>8}')
    for thr in [0.0, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7]:
        sub = df[df['proba'] >= thr]
        if len(sub) == 0:
            continue
        avg_p60 = sub['peak_60d'].mean() * 100
        avg_t = sub['terminal'].mean() * 100
        pct_win = (sub['peak_60d'] >= 0.5).mean() * 100
        keep_pct = len(sub) / len(df) * 100
        print(f'{thr:>10.2f} {len(sub):>10} {keep_pct:>7.1f}% {avg_p60:>10.2f}% {avg_t:>10.2f}% {pct_win:>7.1f}%')

    # Per-stock analysis - look at each of 20 stocks aggregated
    print(f'\n=== Per-stock analysis (avg classifier prob across all BUY signals for that stock) ===')
    per_stock = df.groupby('code').agg(
        n_signals=('proba', 'size'),
        avg_proba=('proba', 'mean'),
        max_proba=('proba', 'max'),
        avg_peak60=('peak_60d', 'mean'),
        avg_terminal=('terminal', 'mean'),
    ).sort_values('avg_proba', ascending=False)
    print(per_stock.to_string())

    # Save
    df.to_csv('/tmp/v91_buy_signals_with_classifier.csv', index=False)
    print(f'\n[saved] /tmp/v91_buy_signals_with_classifier.csv ({len(df)} signal-days)')


if __name__ == '__main__':
    main()
