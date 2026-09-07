"""多任务 LightGBM 训练 — 用 retrospective labels 让 ML 自由发挥。

Targets (4 个独立模型, 全用同一组 features):
  T1: real_drop_15: max_dd_20 < -15  (binary, 真派发)
  T2: real_gain_15: max_gain_20 > +15 (binary, 真上涨)
  T3: gain_20: regression (continuous, 涨幅 magnitude)
  T4: gain_dd_ratio: regression (risk-adjusted)

Features: 原 v5 features + ~25 个新 ext_ features (从 OHLCV 提取的 LLM-style)

Train/Val/Test split (regime-aware):
  Train: 2024-07 → 2025-12
  Val:   2026-02 → 2026-03 大跌
  Test1: 2026-01 大涨
  Test2: 2026-04 反弹

输出 4 个 model + 综合 ensemble score
"""
import json, sys, argparse
import numpy as np
import pandas as pd
import lightgbm as lgb
from sklearn.preprocessing import LabelEncoder
from scipy.stats import spearmanr


CAT = ['weekly_phase', 'monthly_phase', 'candidate_bar_type']


def load_dataset(path):
    rows = []
    with open(path) as f:
        for line in f:
            r = json.loads(line)
            if r.get('gain_20') is not None:
                rows.append(r)
    return pd.DataFrame(rows)


def get_feature_columns(df):
    """All numeric features except meta, target, and synthetic target_*."""
    META = {'code', 'as_of', 'candidate_date', 'signal_type', 'amount_yi'}
    TARGETS = {'gain_5','gain_10','gain_20','dd_5','dd_10','dd_20'}
    cols = []
    for c in df.columns:
        if c in META or c in TARGETS or c in CAT: continue
        if c.startswith('target_'): continue  # exclude synthetic targets
        if c.startswith('gain_') or c.startswith('dd_'): continue  # extra safety
        if df[c].dtype in (np.float64, np.int64, np.float32, np.int32, float, int):
            cols.append(c)
    return cols


def split_regime(df):
    train = df[(df['as_of'] >= '2024-07-01') & (df['as_of'] <= '2025-12-31')].copy()
    val = df[(df['as_of'] >= '2026-02-01') & (df['as_of'] <= '2026-03-31')].copy()
    test1 = df[(df['as_of'] >= '2026-01-01') & (df['as_of'] <= '2026-01-31')].copy()
    test2 = df[(df['as_of'] >= '2026-04-01') & (df['as_of'] <= '2026-04-30')].copy()
    return train, val, test1, test2


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--data', default='/Users/evilkylin/Projects/AlphaAgents/data/ml_dataset_full_extended.jsonl')
    p.add_argument('--out-dir', default='/Users/evilkylin/Projects/AlphaAgents/data/')
    args = p.parse_args()

    print('Loading dataset...', flush=True)
    df = load_dataset(args.data)
    print(f'  N = {len(df)}, columns = {len(df.columns)}')

    # Define labels (retrospective)
    df['target_drop15'] = (df['dd_20'] < -15).astype(int)
    df['target_gain15'] = (df['gain_20'] > 15).astype(int)
    df['target_gain_dd_ratio'] = df['gain_20'] / df['dd_20'].abs().clip(lower=1.0)

    print(f'\nTarget label distribution (full):')
    print(f'  target_drop15 (派发真): {df["target_drop15"].mean()*100:.1f}%')
    print(f'  target_gain15 (上涨真): {df["target_gain15"].mean()*100:.1f}%')
    print(f'  gain_20 mean: {df["gain_20"].mean():.2f}%, gain_dd_ratio mean: {df["target_gain_dd_ratio"].mean():.2f}')

    feature_cols = get_feature_columns(df)
    print(f'\n=== Numeric features ({len(feature_cols)}) ===')
    ext_count = sum(1 for c in feature_cols if c.startswith('ext_'))
    print(f'  baseline v5 features: {len(feature_cols) - ext_count}')
    print(f'  extended (ext_*): {ext_count}')

    # Build encoders
    encoders = {}
    for c in CAT:
        if c in df.columns:
            le = LabelEncoder()
            le.fit(df[c].fillna('').astype(str).unique().tolist() + ['__unk__'])
            encoders[c] = le

    def feats(d):
        X = d[feature_cols].fillna(0).copy()
        for c in CAT:
            if c not in d.columns: continue
            v = d[c].fillna('').astype(str)
            v = v.where(v.isin(encoders[c].classes_), '__unk__')
            X[c] = encoders[c].transform(v)
        return X

    train, val, test1, test2 = split_regime(df)
    print(f'\nSplit:')
    print(f'  Train: N={len(train)} ({train["as_of"].min()}→{train["as_of"].max()})')
    print(f'  Val (大跌): N={len(val)}')
    print(f'  Test1 (大涨): N={len(test1)}')
    print(f'  Test2 (反弹): N={len(test2)}')

    X_tr, X_val, X_te1, X_te2 = feats(train), feats(val), feats(test1), feats(test2)

    # Train 4 models
    targets = {
        'drop15': ('classification', train['target_drop15'].values, val['target_drop15'].values,
                   test1['target_drop15'].values, test2['target_drop15'].values),
        'gain15': ('classification', train['target_gain15'].values, val['target_gain15'].values,
                   test1['target_gain15'].values, test2['target_gain15'].values),
        'gain20': ('regression', train['gain_20'].values, val['gain_20'].values,
                   test1['gain_20'].values, test2['gain_20'].values),
        'gain_dd_ratio': ('regression', train['target_gain_dd_ratio'].values, val['target_gain_dd_ratio'].values,
                          test1['target_gain_dd_ratio'].values, test2['target_gain_dd_ratio'].values),
    }

    cat_features = [c for c in CAT if c in X_tr.columns]
    models = {}
    preds = {'val': {}, 'test1': {}, 'test2': {}}

    for name, (kind, ytr, yval, yt1, yt2) in targets.items():
        print(f'\n=== Training {name} ({kind}) ===')
        if kind == 'classification':
            m = lgb.LGBMClassifier(n_estimators=500, learning_rate=0.03, num_leaves=31,
                                    min_child_samples=30, reg_alpha=0.5, reg_lambda=0.5,
                                    objective='binary', verbose=-1)
        else:
            m = lgb.LGBMRegressor(n_estimators=500, learning_rate=0.03, num_leaves=31,
                                   min_child_samples=30, reg_alpha=0.5, reg_lambda=0.5,
                                   objective='regression', verbose=-1)
        m.fit(X_tr, ytr, eval_set=[(X_val, yval)],
              callbacks=[lgb.early_stopping(stopping_rounds=30, verbose=False)],
              categorical_feature=cat_features)

        pred_val = m.predict_proba(X_val)[:, 1] if kind == 'classification' else m.predict(X_val)
        pred_t1 = m.predict_proba(X_te1)[:, 1] if kind == 'classification' else m.predict(X_te1)
        pred_t2 = m.predict_proba(X_te2)[:, 1] if kind == 'classification' else m.predict(X_te2)

        preds['val'][name] = pred_val
        preds['test1'][name] = pred_t1
        preds['test2'][name] = pred_t2

        models[name] = m
        m.booster_.save_model(f'{args.out_dir}/ml_mt_{name}.lgb')
        print(f'  best_iteration: {m.best_iteration_}')

    # Ensemble: simple weighted score
    # score = gain_20_pred + 50 × P(gain15) - 50 × P(drop15) + 5 × gain_dd_ratio
    def ensemble_score(p_dict):
        return (p_dict['gain20'] + 50 * p_dict['gain15'] - 50 * p_dict['drop15'] + 5 * p_dict['gain_dd_ratio'])

    # Per-target eval + ensemble eval
    print('\n=== Per-target Test1 大涨期 ===')
    for name, (kind, ytr, yval, yt1, yt2) in targets.items():
        p = preds['test1'][name]
        if kind == 'classification':
            sr = spearmanr(p, yt1).correlation
            print(f'  {name:<20} r={sr:+.3f}, mean_pred={p.mean():.3f}')
        else:
            sr = spearmanr(p, yt1).correlation
            print(f'  {name:<20} r={sr:+.3f}, mean_pred={p.mean():.2f} vs true {yt1.mean():.2f}')

    # Ensemble eval — use ensemble_score to rank, see if Top X% has better gain_20
    print('\n=== Ensemble score ranking ===')
    print(f'{"set":<20}{"all mean":>10}{"top 5":>10}{"top 10":>10}{"top 20":>10}{"top 50":>10}{"bot 20":>10}')
    for set_name, set_data, gain_col in [
        ('Test1 大涨', test1, 'gain_20'),
        ('Val 大跌', val, 'gain_20'),
        ('Test2 反弹', test2, 'gain_20'),
    ]:
        if set_name == 'Test1 大涨': p = preds['test1']
        elif set_name == 'Val 大跌': p = preds['val']
        else: p = preds['test2']
        score = ensemble_score(p)
        y = set_data[gain_col].values
        order = np.argsort(-score)
        all_avg = y.mean()
        top5 = y[order[:5]].mean() if len(y) >= 5 else 0
        top10 = y[order[:10]].mean() if len(y) >= 10 else 0
        top20 = y[order[:20]].mean() if len(y) >= 20 else 0
        top50 = y[order[:50]].mean() if len(y) >= 50 else 0
        bot20 = y[order[-20:]].mean() if len(y) >= 20 else 0
        print(f'{set_name:<20}{all_avg:>+9.2f}%{top5:>+9.2f}%{top10:>+9.2f}%{top20:>+9.2f}%{top50:>+9.2f}%{bot20:>+9.2f}%')

    print('\n=== Top 15 Feature Importance (gain20 regressor) ===')
    imp = pd.Series(models['gain20'].feature_importances_, index=X_tr.columns).sort_values(ascending=False)
    for f, v in imp.head(15).items():
        marker = '*' if f.startswith('ext_') else ' '
        print(f'  {marker} {f:<32} {v:>5}')


if __name__ == '__main__':
    main()
