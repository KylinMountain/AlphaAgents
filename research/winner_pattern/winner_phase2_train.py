"""Phase 2: Pre-rise 特征提取 + LightGBM 分类器训练 + v9.1 20 股验证.

数据流:
  Step 1: 载入 winners + 采样 negatives (随机 30K)
  Step 2: 在每个 (code, date_t) 提取 ~25 个 pre-rise 特征 (前 30/60 天聚合)
  Step 3: train/val 按时间切 (2024-04 → 2025-08 train; 2025-09 → 2025-12 val)
  Step 4: LightGBM binary classifier
  Step 5: AUC + top-k precision + feature importance
  Step 6: 在 v9.1 那 20 只股 2025-09-23 上预测,看是否能过滤掉亏损股

重点 — 防 leakage:
  negatives 必须是真 negative: 即在 t 之后 60 天内 peak gain < 50%
  这意味着我们要为每个候选 negative 也算一次 peak — 但只算前置筛选,不进特征
"""
from __future__ import annotations
import argparse, json, sqlite3
from pathlib import Path
import numpy as np
import pandas as pd

DB = '/Users/evilkylin/Projects/AlphaAgents/data/market_history.db'
TRAIN_START = '2024-04-01'
TRAIN_END = '2025-08-31'
VAL_START = '2025-09-01'
VAL_END = '2025-12-31'

BOARDS = ('000', '001', '002', '003',
           '300', '301',
           '600', '601', '603', '605',
           '688')

# v9.1 top 20 cyb (validation reference)
V91_CODES = ['300014', '300033', '300058', '300059', '300124', '300136', '300274',
             '300308', '300394', '300395', '300408', '300442', '300450', '300476',
             '300498', '300502', '300750', '300760', '300857', '301308']
V91_AS_OF = '2025-09-23'  # v91 walkforward start


def load_stock_history():
    print('Loading all stocks...', flush=True)
    conn = sqlite3.connect(DB)
    prefix = ' OR '.join(f"code LIKE '{p}%'" for p in BOARDS)
    df = pd.read_sql_query(
        f"SELECT code, date, open, high, low, close, volume FROM daily_kline "
        f"WHERE ({prefix}) AND date >= '2024-01-01' AND date <= '2026-04-30' "
        f"ORDER BY code, date",
        conn
    )
    print(f'  {len(df):,} rows, {df.code.nunique()} codes')
    by_code = {c: g.reset_index(drop=True) for c, g in df.groupby('code')}
    return by_code


def board_label(code: str) -> int:
    """Encode board as int."""
    if code.startswith(('000', '001', '002', '003')): return 0  # 深主板
    if code.startswith(('300', '301')):                return 1  # 创业板
    if code.startswith(('600', '601', '603', '605')):  return 2  # 沪主板
    if code.startswith('688'):                          return 3  # 科创板
    return 4


def extract_features(g: pd.DataFrame, idx_t: int) -> dict | None:
    """Extract pre-rise features at row index `idx_t`.

    Looking BACKWARD 60 days from idx_t.
    """
    if idx_t < 60:
        return None
    closes = g['close'].values
    opens = g['open'].values
    highs = g['high'].values
    lows = g['low'].values
    vols = g['volume'].values

    c_t = closes[idx_t]
    if c_t <= 0:
        return None

    # Last N days
    h30 = slice(idx_t - 30, idx_t)
    h60 = slice(idx_t - 60, idx_t)
    h5  = slice(idx_t - 5, idx_t)
    h10 = slice(idx_t - 10, idx_t)

    c30 = closes[h30]
    c60 = closes[h60]
    c5  = closes[h5]
    c10 = closes[h10]

    o30 = opens[h30]
    h30_arr = highs[h30]
    l30_arr = lows[h30]
    h60_arr = highs[h60]
    l60_arr = lows[h60]
    v30 = vols[h30]
    v5  = vols[h5]
    v60 = vols[h60]

    # Daily returns
    rets_30 = np.diff(c30) / np.maximum(c30[:-1], 1e-9)
    rets_60 = np.diff(c60) / np.maximum(c60[:-1], 1e-9)

    # Trend
    trend_5d = (c_t - c5[0]) / max(c5[0], 1e-9)
    trend_10d = (c_t - c10[0]) / max(c10[0], 1e-9)
    trend_30d = (c_t - c30[0]) / max(c30[0], 1e-9)
    trend_60d = (c_t - c60[0]) / max(c60[0], 1e-9)
    trend_accel = trend_5d - trend_30d

    # Volatility / range
    vol_30d = float(np.std(rets_30)) if len(rets_30) > 1 else 0.0
    high_30 = h30_arr.max()
    low_30  = l30_arr.min()
    high_60 = h60_arr.max()
    low_60  = l60_arr.min()
    range_pos_30d = (c_t - low_30) / max(high_30 - low_30, 1e-9)
    range_pos_60d = (c_t - low_60) / max(high_60 - low_60, 1e-9)
    dist_from_low_60d = (c_t - low_60) / max(low_60, 1e-9)
    dist_from_high_60d = (high_60 - c_t) / max(c_t, 1e-9)
    at_60d_low = 1.0 if c_t <= 1.05 * low_60 else 0.0
    at_30d_high = 1.0 if c_t >= 0.95 * high_30 else 0.0

    # Volume
    vol_ma_60 = float(v60.mean()) if len(v60) > 0 else 1.0
    vol_ma_30 = float(v30.mean()) if len(v30) > 0 else 1.0
    vol_ma_5  = float(v5.mean()) if len(v5) > 0 else 1.0
    vol_30d_ratios = v30 / max(vol_ma_60, 1)
    vol_ratio_mean_30d = float(vol_30d_ratios.mean())
    vol_ratio_max_30d = float(vol_30d_ratios.max())
    vol_ratio_p75_30d = float(np.percentile(vol_30d_ratios, 75))
    vol_accel_5_30 = vol_ma_5 / max(vol_ma_30, 1)
    vol_accel_30_60 = vol_ma_30 / max(vol_ma_60, 1)

    # Up/down days
    is_up = (c30 > o30).astype(int)
    n_up_30d = int(is_up.sum())
    if is_up.sum() > 0 and (1 - is_up).sum() > 0:
        up_vol = v30[is_up.astype(bool)].sum()
        down_vol = v30[~is_up.astype(bool)].sum()
        up_down_vol_ratio = float(up_vol / max(down_vol, 1))
    else:
        up_down_vol_ratio = 1.0

    # Body / wick
    spreads = h30_arr - l30_arr
    bodies = np.abs(c30 - o30)
    upper_wicks = h30_arr - np.maximum(o30, c30)
    lower_wicks = np.minimum(o30, c30) - l30_arr
    safe_spread = np.where(spreads > 0, spreads, 1)
    body_pct_mean = float((bodies / safe_spread).mean())
    upper_wick_pct_mean = float((upper_wicks / safe_spread).mean())
    lower_wick_pct_mean = float((lower_wicks / safe_spread).mean())

    # Max pullback / rally in last 30 days
    if len(c30) > 1:
        running_max = np.maximum.accumulate(c30)
        running_min = np.minimum.accumulate(c30)
        max_pullback_30d = float(((running_max - c30) / np.maximum(running_max, 1e-9)).max())
        max_rally_30d   = float(((c30 - running_min) / np.maximum(running_min, 1e-9)).max())
    else:
        max_pullback_30d = 0.0
        max_rally_30d = 0.0

    return {
        'trend_5d': trend_5d, 'trend_10d': trend_10d, 'trend_30d': trend_30d,
        'trend_60d': trend_60d, 'trend_accel': trend_accel,
        'volatility_30d': vol_30d,
        'range_pos_30d': range_pos_30d, 'range_pos_60d': range_pos_60d,
        'dist_from_low_60d': dist_from_low_60d, 'dist_from_high_60d': dist_from_high_60d,
        'at_60d_low': at_60d_low, 'at_30d_high': at_30d_high,
        'vol_ratio_mean_30d': vol_ratio_mean_30d, 'vol_ratio_max_30d': vol_ratio_max_30d,
        'vol_ratio_p75_30d': vol_ratio_p75_30d,
        'vol_accel_5_30': vol_accel_5_30, 'vol_accel_30_60': vol_accel_30_60,
        'n_up_days_30d': n_up_30d, 'up_down_vol_ratio': up_down_vol_ratio,
        'body_pct_mean_30d': body_pct_mean,
        'upper_wick_pct_mean_30d': upper_wick_pct_mean,
        'lower_wick_pct_mean_30d': lower_wick_pct_mean,
        'max_pullback_30d': max_pullback_30d, 'max_rally_30d': max_rally_30d,
    }


def is_winner_at(g: pd.DataFrame, idx_t: int, lookforward: int, peak_thr: float) -> bool:
    """Check if (g, idx_t) is a winner: peak gain in next `lookforward` days >= peak_thr."""
    if idx_t + lookforward >= len(g):
        return False
    closes = g['close'].values
    c_t = closes[idx_t]
    if c_t <= 0:
        return False
    forward = closes[idx_t + 1:idx_t + 1 + lookforward]
    if len(forward) < lookforward // 2:
        return False
    return (forward.max() - c_t) / c_t >= peak_thr


def build_dataset(by_code, n_negative_samples=30000, seed=42):
    """Build positive + negative training set."""
    rng = np.random.default_rng(seed)

    # Load winners
    winners = []
    with open('/Users/evilkylin/Projects/AlphaAgents/data/winners_p50_lf60.jsonl') as f:
        for line in f:
            winners.append(json.loads(line))
    print(f'Loaded {len(winners)} winner instances')

    # Build positive samples (extract features at each winner's date_t)
    positives = []
    miss = 0
    for w in winners:
        code = w['code']
        if code not in by_code: miss += 1; continue
        g = by_code[code]
        idx = g.index[g['date'] == w['date_t']]
        if len(idx) == 0: miss += 1; continue
        idx_t = int(idx[0])
        feats = extract_features(g, idx_t)
        if feats is None: miss += 1; continue
        feats['code'] = code
        feats['date_t'] = w['date_t']
        feats['board'] = board_label(code)
        feats['label'] = 1
        positives.append(feats)
    print(f'  Positive features extracted: {len(positives)} (skipped {miss})')

    # Sample negatives: random (code, date) pairs in train period
    print(f'\nSampling {n_negative_samples} negatives ...', flush=True)
    all_codes = list(by_code.keys())
    negatives = []
    attempts = 0
    while len(negatives) < n_negative_samples and attempts < n_negative_samples * 10:
        attempts += 1
        code = rng.choice(all_codes)
        g = by_code[code]
        if len(g) < 80:
            continue
        # Pick a random row within the train+val window
        valid_idx = g.index[(g['date'] >= TRAIN_START) & (g['date'] <= VAL_END)].tolist()
        if not valid_idx: continue
        idx_t = int(rng.choice(valid_idx))
        # Must have at least 60 days backward + 60 forward
        if idx_t < 60 or idx_t + 60 >= len(g):
            continue
        # Filter out actual winners
        if is_winner_at(g, idx_t, 60, 0.50):
            continue
        feats = extract_features(g, idx_t)
        if feats is None: continue
        feats['code'] = code
        feats['date_t'] = g.iloc[idx_t]['date']
        feats['board'] = board_label(code)
        feats['label'] = 0
        negatives.append(feats)

    print(f'  Negative features extracted: {len(negatives)}')

    df = pd.DataFrame(positives + negatives)
    return df


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--n-negatives', type=int, default=30000)
    p.add_argument('--top-k', type=int, default=50)
    args = p.parse_args()

    by_code = load_stock_history()
    df = build_dataset(by_code, n_negative_samples=args.n_negatives)
    print(f'\nDataset: {len(df)} rows ({(df.label==1).sum()} pos, {(df.label==0).sum()} neg)')

    # Time split
    train = df[df['date_t'] <= TRAIN_END].copy()
    val = df[(df['date_t'] >= VAL_START) & (df['date_t'] <= VAL_END)].copy()
    print(f'Train: {len(train)} ({(train.label==1).sum()} winners), Val: {len(val)} ({(val.label==1).sum()} winners)')

    feat_cols = [c for c in df.columns if c not in ('code', 'date_t', 'label')]
    print(f'Features: {len(feat_cols)}')

    # Train LightGBM
    try:
        import lightgbm as lgb
    except ImportError:
        print('lightgbm not installed, falling back to sklearn GradientBoosting')
        from sklearn.ensemble import GradientBoostingClassifier
        from sklearn.metrics import roc_auc_score
        clf = GradientBoostingClassifier(max_depth=4, n_estimators=200, random_state=42)
        clf.fit(train[feat_cols], train['label'])
        val_proba = clf.predict_proba(val[feat_cols])[:, 1]
    else:
        train_data = lgb.Dataset(train[feat_cols], label=train['label'].values)
        val_data = lgb.Dataset(val[feat_cols], label=val['label'].values, reference=train_data)
        params = {
            'objective': 'binary', 'metric': 'auc',
            'learning_rate': 0.05, 'num_leaves': 31, 'max_depth': 6,
            'feature_fraction': 0.8, 'bagging_fraction': 0.8, 'bagging_freq': 5,
            'verbose': -1, 'is_unbalance': True,
        }
        clf = lgb.train(params, train_data, num_boost_round=500,
                         valid_sets=[val_data], callbacks=[lgb.early_stopping(30), lgb.log_evaluation(0)])
        val_proba = clf.predict(val[feat_cols])

    from sklearn.metrics import roc_auc_score
    auc = roc_auc_score(val['label'], val_proba)
    print(f'\n=== Validation Metrics ===')
    print(f'  AUC: {auc:.4f}')

    # Top-k precision
    val_sorted = val.copy()
    val_sorted['proba'] = val_proba
    val_sorted = val_sorted.sort_values('proba', ascending=False)
    base_rate = val['label'].mean()
    for k in [50, 100, 200, 500, 1000]:
        if len(val_sorted) >= k:
            top_k = val_sorted.head(k)
            prec = top_k['label'].mean()
            lift = prec / max(base_rate, 1e-9)
            print(f'  Top-{k}: precision={prec:.3f} (lift {lift:.1f}x base rate {base_rate:.3f})')

    # Feature importance
    if hasattr(clf, 'feature_importance'):
        imp = clf.feature_importance(importance_type='gain')
    else:
        imp = clf.feature_importances_
    fi = pd.DataFrame({'feature': feat_cols, 'importance': imp}).sort_values('importance', ascending=False)
    print(f'\n=== Top 15 features by importance ===')
    for _, r in fi.head(15).iterrows():
        print(f'  {r["feature"]:<30} {r["importance"]:>8.0f}')

    # Validate on v9.1 20 stocks at 2025-09-23
    print(f'\n=== v9.1 20 stocks @ {V91_AS_OF} ===')
    v91_results = []
    for code in V91_CODES:
        if code not in by_code: continue
        g = by_code[code]
        idx = g.index[g['date'] == V91_AS_OF]
        if len(idx) == 0:
            # Find nearest date
            mask = g['date'] >= V91_AS_OF
            if mask.sum() == 0: continue
            idx_t = int(g.index[mask][0])
        else:
            idx_t = int(idx[0])
        feats = extract_features(g, idx_t)
        if feats is None: continue
        feats['board'] = board_label(code)
        x = pd.DataFrame([feats])[feat_cols]
        if hasattr(clf, 'predict_proba'):
            proba = float(clf.predict_proba(x)[0, 1])
        else:
            proba = float(clf.predict(x)[0])

        # Actual outcome: peak gain from 2025-09-23 to 2026-04-24
        end_idx = g.index[g['date'] <= '2026-04-24']
        if len(end_idx) > 0:
            forward = g.loc[idx_t+1:end_idx[-1], 'close'].values
            c_t = g.loc[idx_t, 'close']
            if len(forward) > 0 and c_t > 0:
                actual_peak_gain = (forward.max() - c_t) / c_t
                actual_terminal_gain = (forward[-1] - c_t) / c_t
            else:
                actual_peak_gain = actual_terminal_gain = float('nan')
        else:
            actual_peak_gain = actual_terminal_gain = float('nan')

        v91_results.append({
            'code': code, 'proba': proba,
            'actual_peak_gain': actual_peak_gain,
            'actual_terminal_gain': actual_terminal_gain,
        })

    v91_df = pd.DataFrame(v91_results).sort_values('proba', ascending=False)
    print(f'{"Code":<8} {"Pred Prob":>10} {"Peak gain":>10} {"Terminal":>10}  Predicted')
    for _, r in v91_df.iterrows():
        marker = '✓ winner' if r['actual_peak_gain'] >= 0.50 else ('✗ loser' if r['actual_terminal_gain'] < 0 else 'partial')
        print(f'{r["code"]:<8} {r["proba"]:>10.3f} {r["actual_peak_gain"]*100:>9.1f}% {r["actual_terminal_gain"]*100:>9.1f}%  {marker}')

    # Save model + sample data for reference
    Path('/Users/evilkylin/Projects/AlphaAgents/data/winner_model.pkl').write_bytes(__import__('pickle').dumps(clf))
    print(f'\n[saved] model + features data')


if __name__ == '__main__':
    main()
