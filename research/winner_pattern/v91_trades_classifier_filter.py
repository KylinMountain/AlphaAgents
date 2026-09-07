"""把 classifier 应用到 v9.1 实际 31 笔 trades 上, 测过滤效果.

每笔 trade 在 entry_date 测 classifier prob, 然后:
1. 按 prob 排序展示每笔 trade 的 prob + 实际 P&L
2. 模拟阈值过滤: prob < threshold 的 trade 不做, 看组合复合收益
3. 找最优 threshold (使复合收益最高)
"""
from __future__ import annotations
import pickle, sys
import numpy as np
import pandas as pd

sys.path.insert(0, '/Users/evilkylin/Projects/AlphaAgents/scripts')
from winner_phase2_train import board_label, extract_features, load_stock_history


def main():
    print('Loading v9.1 trades...', flush=True)
    trades = pd.read_csv('/tmp/v91_c2c3_trades.csv')
    print(f'  {len(trades)} trades')

    print('Loading market data...')
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

    print('\nScoring each trade at entry_date ...', flush=True)
    rows = []
    for _, t in trades.iterrows():
        code = str(t['code']).zfill(6)
        entry_date = t['entry_date']
        if code not in by_code: continue
        g = by_code[code]
        # entry_date is the actual buy date (signal_date + 1 trading day usually)
        # The signal was at entry_date - 1 typically. Use the entry_date row for features
        # since classifier needs to predict given what's available at signal time.
        idx = g.index[g['date'] == entry_date]
        if len(idx) == 0:
            continue
        idx_t = int(idx[0])
        # Use the day BEFORE entry (signal day) for feature extraction
        # because at that moment we'd decide whether to buy at next open
        feats = extract_features(g, idx_t - 1) if idx_t > 0 else None
        if feats is None: continue
        feats['board'] = board_label(code)
        x = pd.DataFrame([feats])[feat_cols]
        if hasattr(clf, 'predict_proba'):
            proba = float(clf.predict_proba(x)[0, 1])
        else:
            proba = float(clf.predict(x)[0])
        # 计算 hold days from dates
        try:
            hd = (pd.Timestamp(t['exit_date']) - pd.Timestamp(t['entry_date'])).days
        except Exception:
            hd = 0
        rows.append({
            'code': code,
            'entry_date': entry_date,
            'exit_date': t['exit_date'],
            'gross_pct': float(t['gross_pct']) if pd.notna(t.get('gross_pct')) else 0,
            'net_pct': float(t['net_pct']) if pd.notna(t.get('net_pct')) else 0,
            'hold_days': hd,
            'classifier_prob': proba,
            'entry_phase': t.get('entry_phase', '') or '',
            'exit_phase': '',
            'exit_reason': t.get('exit_reason', '') or '',
        })

    df = pd.DataFrame(rows)
    if len(df) == 0:
        # The trades file may use different columns. Re-check
        print('Columns in trades file:', list(trades.columns))
        print('Sample row:')
        print(trades.iloc[0])
        return

    # Display all 31 trades sorted by classifier prob (descending)
    print(f'\n=== 31 v9.1 Trades sorted by classifier prob (desc) ===')
    print(f'{"code":<8} {"entry_date":<12} {"exit_date":<12} {"prob":>6} {"net_pct":>8} {"hold":>5}  phase')
    for _, r in df.sort_values('classifier_prob', ascending=False).iterrows():
        net = r['net_pct'] if pd.notna(r['net_pct']) else 0
        hd = r['hold_days'] if pd.notna(r['hold_days']) else 0
        print(f'{r["code"]:<8} {r["entry_date"]:<12} {r["exit_date"]:<12} '
              f'{r["classifier_prob"]:>6.3f} {net:>+7.2f}% {int(hd):>4}  {r["entry_phase"]} → {r["exit_phase"]}')

    # Simulate: filter by threshold, compute compound return
    print(f'\n=== Threshold filter simulation ===')
    print(f'{"threshold":>10} {"n_kept":>8} {"keep%":>6} {"compound":>10} {"avg_net":>9} {"win_rate":>9}  {"vs_no_filter":>12}')
    base_compound = 1.0
    for r in df['net_pct']:
        if pd.notna(r):
            base_compound *= (1 + r / 100)
    base_compound_pct = (base_compound - 1) * 100
    print(f'  Baseline (no filter): {len(df)} trades, compound {base_compound_pct:+.2f}%')
    print()

    for thr in [0.0, 0.1, 0.15, 0.2, 0.25, 0.3, 0.35, 0.4, 0.45, 0.5, 0.6, 0.7, 0.8]:
        sub = df[df['classifier_prob'] >= thr].copy()
        if len(sub) == 0:
            continue
        compound = 1.0
        for r in sub['net_pct']:
            if pd.notna(r):
                compound *= (1 + r / 100)
        compound_pct = (compound - 1) * 100
        avg_net = sub['net_pct'].mean()
        wins = (sub['net_pct'] > 0).sum()
        win_rate = wins / len(sub) * 100 if len(sub) > 0 else 0
        delta = compound_pct - base_compound_pct
        keep_pct = len(sub) / len(df) * 100
        print(f'{thr:>10.2f} {len(sub):>8} {keep_pct:>5.0f}% {compound_pct:>+9.2f}% {avg_net:>+8.2f}% {win_rate:>8.0f}%  {delta:>+11.2f}pp')

    # Best/worst by classifier signal
    print(f'\n=== Classifier 高 vs 低 prob 实际 P&L 对比 ===')
    df_sorted = df.sort_values('classifier_prob', ascending=False)
    n_half = len(df_sorted) // 2
    high_half = df_sorted.head(n_half)
    low_half = df_sorted.tail(n_half)
    print(f'Top {n_half} by prob:    avg net P&L = {high_half["net_pct"].mean():+.2f}%, '
          f'win rate = {(high_half["net_pct"]>0).mean()*100:.0f}%, '
          f'compound = {(np.prod(1 + high_half["net_pct"]/100) - 1)*100:+.2f}%')
    print(f'Bottom {n_half} by prob: avg net P&L = {low_half["net_pct"].mean():+.2f}%, '
          f'win rate = {(low_half["net_pct"]>0).mean()*100:.0f}%, '
          f'compound = {(np.prod(1 + low_half["net_pct"]/100) - 1)*100:+.2f}%')

    # Save
    df.to_csv('/tmp/v91_trades_classifier_prob.csv', index=False)
    print(f'\n[saved] /tmp/v91_trades_classifier_prob.csv')


if __name__ == '__main__':
    main()
