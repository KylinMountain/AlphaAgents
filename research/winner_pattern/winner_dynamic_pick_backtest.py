"""Dynamic 选股回测 — 每天用 classifier 在全市场 5K 股选 top 2,看 buy-hold 表现.

回测期: 2026-01-01 → 2026-04-30 (training 完全没碰过)
基准: 创业板平均 buy-hold + 全市场平均 buy-hold + cyb 指数

每只 pick 评估:
  - terminal_return: 持仓期末收益 (买后 hold 到 2026-04-30)
  - peak_return: 持仓期内最高 return
  - max_drawdown: 持仓期内最大回撤 (从 peak 算)
"""
from __future__ import annotations
import pickle, sys
from datetime import datetime
import numpy as np
import pandas as pd

sys.path.insert(0, '/Users/evilkylin/Projects/AlphaAgents/scripts')
from winner_phase2_train import (
    BOARDS, board_label, extract_features, load_stock_history
)

EVAL_START = '2026-01-01'
EVAL_END = '2026-04-30'
TOP_N = 2


def compute_pick_metrics(g: pd.DataFrame, idx_t: int, end_date: str) -> dict | None:
    """给定 stock g 和 entry idx, 计算 forward metrics 直到 end_date."""
    closes = g['close'].values
    dates = g['date'].values
    if idx_t + 1 >= len(g):
        return None
    entry_close = closes[idx_t + 1] if idx_t + 1 < len(closes) else closes[idx_t]
    if entry_close <= 0:
        return None
    # Forward window: from idx_t+1 to end_date
    end_mask = dates <= end_date
    if not end_mask.any():
        return None
    end_idx = max(np.where(end_mask)[0])
    if end_idx <= idx_t:
        return None
    fwd_closes = closes[idx_t + 1:end_idx + 1]
    if len(fwd_closes) < 1:
        return None
    terminal_pct = (fwd_closes[-1] - entry_close) / entry_close * 100
    peak_pct = (fwd_closes.max() - entry_close) / entry_close * 100
    # Max drawdown during hold
    running_peak = np.maximum.accumulate(fwd_closes)
    drawdowns = (running_peak - fwd_closes) / np.maximum(running_peak, 1e-9) * 100
    max_dd = float(drawdowns.max())
    return {
        'entry_close': float(entry_close),
        'terminal_close': float(fwd_closes[-1]),
        'terminal_pct': float(terminal_pct),
        'peak_pct': float(peak_pct),
        'max_dd': max_dd,
        'hold_days': int(len(fwd_closes)),
    }


def main():
    print('Loading market data...', flush=True)
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

    # All trading dates in eval window (use any stock's date list as reference)
    print('Building eval dates...')
    sample_code = next(c for c in by_code if len(by_code[c]) > 100)
    sample_dates = by_code[sample_code]['date'].tolist()
    eval_dates = [d for d in sample_dates if EVAL_START <= d <= EVAL_END]
    print(f'  {len(eval_dates)} trading days {eval_dates[0]} → {eval_dates[-1]}')

    # For each eval date, compute features for all stocks, apply classifier, pick top N
    print(f'\nDaily picking top-{TOP_N} from full market ...', flush=True)
    all_picks = []
    for di, date in enumerate(eval_dates):
        if di % 20 == 0:
            print(f'  {di+1}/{len(eval_dates)}: {date}', flush=True)
        candidates = []
        for code, g in by_code.items():
            idx = g.index[g['date'] == date]
            if len(idx) == 0:
                continue
            idx_t = int(idx[0])
            if idx_t < 60 or idx_t + 1 >= len(g):
                continue
            feats = extract_features(g, idx_t)
            if feats is None:
                continue
            feats['board'] = board_label(code)
            candidates.append((code, idx_t, feats))

        if not candidates:
            continue
        # Batch predict
        X = pd.DataFrame([c[2] for c in candidates])[feat_cols]
        if hasattr(clf, 'predict_proba'):
            probs = clf.predict_proba(X)[:, 1]
        else:
            probs = clf.predict(X)
        # Take top N
        sorted_idx = np.argsort(-probs)[:TOP_N]
        for rank, ci in enumerate(sorted_idx):
            code, idx_t, _ = candidates[ci]
            prob = float(probs[ci])
            metrics = compute_pick_metrics(by_code[code], idx_t, EVAL_END)
            if metrics is None:
                continue
            all_picks.append({
                'date': date, 'rank': rank + 1, 'code': code, 'prob': prob,
                **metrics,
            })

    df = pd.DataFrame(all_picks)
    print(f'\nTotal picks: {len(df)} ({len(df)//TOP_N} pick-days × {TOP_N} stocks)')

    # Aggregate metrics
    print(f'\n=== Daily top-{TOP_N} pick performance (each pick = buy at next-day open, hold to {EVAL_END}) ===')
    print(f'  Total picks: {len(df)}')
    print(f'  Mean terminal_pct: {df["terminal_pct"].mean():+.2f}%')
    print(f'  Mean peak_pct:     {df["peak_pct"].mean():+.2f}%')
    print(f'  Mean max_dd:       {df["max_dd"].mean():.2f}%')
    print(f'  Win rate (terminal > 0): {(df["terminal_pct"]>0).mean()*100:.1f}%')
    print(f'  Pct terminal ≥ +30%: {(df["terminal_pct"]>=30).mean()*100:.1f}%')
    print(f'  Pct peak ≥ +50%:     {(df["peak_pct"]>=50).mean()*100:.1f}%')

    # By month
    print(f'\n=== By month (mean terminal, peak, max_dd) ===')
    df['month'] = df['date'].str[:7]
    by_month = df.groupby('month').agg(
        n=('code', 'size'),
        terminal=('terminal_pct', 'mean'),
        peak=('peak_pct', 'mean'),
        max_dd=('max_dd', 'mean'),
        win_rate=('terminal_pct', lambda x: (x > 0).mean() * 100),
    )
    print(by_month.to_string())

    # Best / worst picks
    print(f'\n=== Top 10 picks (best terminal) ===')
    top = df.nlargest(10, 'terminal_pct')
    print(f'{"date":<12} {"code":<8} {"prob":>6} {"terminal":>10} {"peak":>10} {"max_dd":>8} {"days":>5}')
    for _, r in top.iterrows():
        print(f'{r["date"]:<12} {r["code"]:<8} {r["prob"]:>6.3f} {r["terminal_pct"]:>+9.2f}% {r["peak_pct"]:>+9.2f}% {r["max_dd"]:>7.2f}% {r["hold_days"]:>5}')

    print(f'\n=== Worst 10 picks ===')
    worst = df.nsmallest(10, 'terminal_pct')
    for _, r in worst.iterrows():
        print(f'{r["date"]:<12} {r["code"]:<8} {r["prob"]:>6.3f} {r["terminal_pct"]:>+9.2f}% {r["peak_pct"]:>+9.2f}% {r["max_dd"]:>7.2f}% {r["hold_days"]:>5}')

    # Compare to baselines
    print(f'\n=== Baseline comparisons ===')
    # 创业板 avg buy-hold over eval period
    cyb_returns = []
    main_returns = []
    all_returns = []
    for code, g in by_code.items():
        eligible = g[(g['date'] >= EVAL_START) & (g['date'] <= EVAL_END)]
        if len(eligible) < 2: continue
        c0 = eligible.iloc[0]['close']
        c1 = eligible.iloc[-1]['close']
        if c0 <= 0: continue
        ret = (c1 - c0) / c0 * 100
        all_returns.append(ret)
        if code.startswith(('300', '301')):
            cyb_returns.append(ret)
        elif code.startswith(('600', '601', '603', '605', '000', '001', '002', '003')):
            main_returns.append(ret)
    print(f'  All market buy-hold avg ({len(all_returns)} stocks):  {np.mean(all_returns):+.2f}%')
    print(f'  创业板 buy-hold avg ({len(cyb_returns)} stocks):       {np.mean(cyb_returns):+.2f}%')
    print(f'  主板 buy-hold avg ({len(main_returns)} stocks):        {np.mean(main_returns):+.2f}%')
    print(f'  Classifier daily top-{TOP_N} mean terminal:          {df["terminal_pct"].mean():+.2f}%')
    print(f'  Lift over all market: {df["terminal_pct"].mean() / max(np.mean(all_returns), 1e-9):.2f}x')

    # Save
    df.drop(columns=['month'], inplace=True)
    df.to_csv('/tmp/winner_dynamic_pick_2026.csv', index=False)
    print(f'\n[saved] /tmp/winner_dynamic_pick_2026.csv')


if __name__ == '__main__':
    main()
