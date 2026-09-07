"""Train LightGBM screener on dataset built by build_ml_dataset.py.

Strategy:
- Target: 20-day max_gain (or risk-adjusted: gain/max(|dd|, 1))
- Features: K-line characteristics + multi-tf + phase context
- Split (regime-aware per user request):
    Train:      2024-01 → 2025-12 (跨多 regime)
    Validation: 2026-02 → 2026-03 (大跌期, 严苛)
    Test 1:     2026-01 (大涨期)
    Test 2:     2026-04 (反弹期)

Output:
- model: data/ml_screener.lgb
- feature importances + per-split metrics
"""
import sys, json, argparse
from pathlib import Path
import numpy as np
import pandas as pd
from sklearn.preprocessing import LabelEncoder

import lightgbm as lgb


FEATURES_NUMERIC = [
    'close', 'amount_yi', 'pos_60d', 'own_60d_return', 'relative_strength',
    'weekly_trend', 'weekly_consist', 'monthly_trend', 'monthly_pos',
    'prior_markup_weeks', 'prior_markdown_weeks', 'prior_base_weeks',
    'candidate_max_pct', 'candidate_close_position',
    'candidate_lower_shadow', 'candidate_upper_shadow', 'candidate_range_x',
]

FEATURES_CATEGORICAL = [
    'weekly_phase', 'monthly_phase', 'candidate_bar_type',
]


def load_dataset(path):
    rows = []
    with open(path) as f:
        for line in f:
            r = json.loads(line)
            if r.get('gain_20') is None:
                continue
            rows.append(r)
    df = pd.DataFrame(rows)
    return df


def build_features(df, label_encoders=None):
    if label_encoders is None:
        label_encoders = {}
    X = df[FEATURES_NUMERIC].fillna(0).copy()
    for cat in FEATURES_CATEGORICAL:
        if cat in df.columns:
            if cat not in label_encoders:
                le = LabelEncoder()
                le.fit(df[cat].fillna('').astype(str))
                label_encoders[cat] = le
            le = label_encoders[cat]
            X[cat] = le.transform(df[cat].fillna('').astype(str))
    return X, label_encoders


def split_by_regime(df):
    """User request: 1月 (大涨) → test, 2-3月 (大跌) → val, 4月 (反弹) → test2."""
    train = df[(df['as_of'] >= '2024-01-01') & (df['as_of'] <= '2025-12-31')].copy()
    val = df[(df['as_of'] >= '2026-02-01') & (df['as_of'] <= '2026-03-31')].copy()
    test1 = df[(df['as_of'] >= '2026-01-01') & (df['as_of'] <= '2026-01-31')].copy()
    test2 = df[(df['as_of'] >= '2026-04-01') & (df['as_of'] <= '2026-04-30')].copy()
    return train, val, test1, test2


def metrics(y_true, y_pred, name):
    """Return dict of useful metrics."""
    from scipy.stats import spearmanr, pearsonr
    sr = spearmanr(y_pred, y_true).correlation
    pr = pearsonr(y_pred, y_true).statistic if hasattr(pearsonr(y_pred, y_true), 'statistic') else pearsonr(y_pred, y_true)[0]
    # Top quintile vs bottom quintile
    n = len(y_true)
    order = np.argsort(-y_pred)
    q = n // 5
    top_q = np.array(y_true)[order[:q]].mean()
    bot_q = np.array(y_true)[order[-q:]].mean()
    # Top 30 / 50 picks
    top30 = np.array(y_true)[order[:30]].mean() if n >= 30 else None
    top50 = np.array(y_true)[order[:50]].mean() if n >= 50 else None
    return {
        'name': name, 'n': n,
        'spearman': sr, 'pearson': pr,
        'top_q_avg': top_q, 'bot_q_avg': bot_q, 'lift_q': top_q - bot_q,
        'top30_avg': top30, 'top50_avg': top50,
        'mean_target': np.mean(y_true),
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--data', default='/Users/evilkylin/Projects/AlphaAgents/data/ml_dataset.jsonl')
    p.add_argument('--target', default='gain_20', choices=['gain_20', 'gain_10', 'gain_5'])
    p.add_argument('--objective', default='regression', choices=['regression', 'lambdarank'])
    p.add_argument('--out-model', default='/Users/evilkylin/Projects/AlphaAgents/data/ml_screener.lgb')
    args = p.parse_args()

    print('Loading dataset...')
    df = load_dataset(args.data)
    print(f'  N = {len(df)}, columns = {len(df.columns)}')
    print(f'  date range: {df["as_of"].min()} → {df["as_of"].max()}')

    train, val, test1, test2 = split_by_regime(df)
    print(f'\nSplit (regime-aware):')
    print(f'  Train (2024-01→2025-12): N={len(train)} ({train["as_of"].min()}→{train["as_of"].max()})')
    print(f'  Val   (2026-02→2026-03 大跌): N={len(val)}')
    print(f'  Test1 (2026-01 大涨): N={len(test1)}')
    print(f'  Test2 (2026-04 反弹): N={len(test2)}')

    X_train, encoders = build_features(train)
    y_train = train[args.target].fillna(0).values
    X_val, _ = build_features(val, encoders)
    y_val = val[args.target].fillna(0).values
    X_test1, _ = build_features(test1, encoders)
    y_test1 = test1[args.target].fillna(0).values
    X_test2, _ = build_features(test2, encoders)
    y_test2 = test2[args.target].fillna(0).values

    print(f'\nFeatures: {list(X_train.columns)}')
    print(f'Target: {args.target} (mean train={y_train.mean():.2f}%)')

    model = lgb.LGBMRegressor(
        n_estimators=500,
        learning_rate=0.03,
        num_leaves=31,
        min_child_samples=30,
        reg_alpha=0.5,
        reg_lambda=0.5,
        objective='regression',
        verbose=-1,
    )
    model.fit(
        X_train, y_train,
        eval_set=[(X_val, y_val)],
        eval_metric='rmse',
        callbacks=[lgb.early_stopping(stopping_rounds=30, verbose=False)],
        categorical_feature=FEATURES_CATEGORICAL,
    )
    print(f'\nBest iteration: {model.best_iteration_}')

    y_train_pred = model.predict(X_train)
    y_val_pred = model.predict(X_val)
    y_test1_pred = model.predict(X_test1)
    y_test2_pred = model.predict(X_test2)

    print('\n=== Metrics ===')
    for ds, ytrue, ypred in [
        ('Train', y_train, y_train_pred),
        ('Val 大跌期', y_val, y_val_pred),
        ('Test1 大涨期 (2026-01)', y_test1, y_test1_pred),
        ('Test2 反弹期 (2026-04)', y_test2, y_test2_pred),
    ]:
        m = metrics(ytrue, ypred, ds)
        print(f'  {ds:<22}  N={m["n"]:>4}  '
              f'r_spearman={m["spearman"]:+.3f}  '
              f'top_quintile_avg={m["top_q_avg"]:+.2f}%  '
              f'bot_quintile_avg={m["bot_q_avg"]:+.2f}%  '
              f'lift={m["lift_q"]:+.2f}pp  '
              f'top30_avg={m["top30_avg"]:+.2f}%' if m["top30_avg"] is not None else '')

    # Feature importance
    print('\n=== Feature Importance (Top 15) ===')
    imp = pd.Series(model.feature_importances_, index=X_train.columns).sort_values(ascending=False)
    for f, v in imp.head(15).items():
        print(f'  {f:<28}  {v:>6}')

    # Save
    model.booster_.save_model(args.out_model)
    print(f'\nModel saved to {args.out_model}')


if __name__ == '__main__':
    main()
