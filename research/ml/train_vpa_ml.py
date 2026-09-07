"""训练 VPA-aware ML — 4 个 binary classifier:
- topping: P(future_dd_20 < -15)         "ML 学的 BC"
- bottoming: P(future_gain_20 > 15)       "ML 学的 SC"
- continuation: P(future_close_5 / current > 1.03)
- breakdown: P(future_close_5 / current < 0.97)

Features: continuous Anna 概念 + raw 时序 (无 threshold)
"""
import json, sys, argparse
import numpy as np
import pandas as pd
import lightgbm as lgb
from sklearn.metrics import roc_auc_score
from scipy.stats import spearmanr


META = {'code', 'as_of'}
TARGETS = {'gain_5','gain_10','gain_20','dd_5','dd_10','dd_20','close_ret_5','close_ret_10','close_ret_20'}


def load_data(path):
    rows = []
    with open(path) as f:
        for line in f:
            r = json.loads(line)
            if r.get('gain_20') is not None and r.get('close_ret_5') is not None:
                rows.append(r)
    return pd.DataFrame(rows)


def get_feature_cols(df):
    cols = []
    for c in df.columns:
        if c in META or c in TARGETS: continue
        if c.startswith('target_') or c.startswith('t_'): continue  # exclude labels
        if c.startswith('gain_') or c.startswith('dd_') or c.startswith('close_ret_'): continue
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
    p.add_argument('--data', default='/Users/evilkylin/Projects/AlphaAgents/data/vpa_ml_dataset.jsonl')
    p.add_argument('--out-dir', default='/Users/evilkylin/Projects/AlphaAgents/data/')
    args = p.parse_args()

    print('Loading...', flush=True)
    df = load_data(args.data)
    print(f'  N = {len(df)}', flush=True)

    # Build labels
    df['t_topping'] = (df['dd_20'] < -15).astype(int)
    df['t_bottoming'] = (df['gain_20'] > 15).astype(int)
    df['t_continuation'] = (df['close_ret_5'] > 3.0).astype(int)
    df['t_breakdown'] = (df['close_ret_5'] < -3.0).astype(int)

    print(f'\nLabel distribution:')
    print(f'  topping (派发真): {df["t_topping"].mean()*100:.1f}%')
    print(f'  bottoming (吸筹真): {df["t_bottoming"].mean()*100:.1f}%')
    print(f'  continuation (5d涨>3%): {df["t_continuation"].mean()*100:.1f}%')
    print(f'  breakdown (5d跌>3%): {df["t_breakdown"].mean()*100:.1f}%')

    feature_cols = get_feature_cols(df)
    print(f'\n# Features = {len(feature_cols)}')

    train, val, test1, test2 = split_regime(df)
    print(f'\nSplit:')
    print(f'  Train: N={len(train)} ({train.as_of.min()}→{train.as_of.max()})')
    print(f'  Val (大跌期): N={len(val)}')
    print(f'  Test1 (大涨期 1月): N={len(test1)}')
    print(f'  Test2 (反弹期 4月): N={len(test2)}')

    X_tr = train[feature_cols].fillna(0).copy()
    X_val = val[feature_cols].fillna(0).copy()
    X_te1 = test1[feature_cols].fillna(0).copy()
    X_te2 = test2[feature_cols].fillna(0).copy()

    label_names = ['t_topping','t_bottoming','t_continuation','t_breakdown']
    label_zh = {'t_topping':'派发(BC)', 't_bottoming':'吸筹(SC)', 't_continuation':'延续(SOS)', 't_breakdown':'破位(SOW)'}
    models = {}
    preds = {'val':{}, 'test1':{}, 'test2':{}}

    for label in label_names:
        ytr = train[label].values; yval = val[label].values
        yt1 = test1[label].values; yt2 = test2[label].values
        # Class imbalance
        pos_rate = ytr.mean()
        scale = (1-pos_rate)/max(pos_rate, 0.01)

        m = lgb.LGBMClassifier(n_estimators=400, learning_rate=0.03, num_leaves=31,
                                min_child_samples=30, reg_alpha=0.5, reg_lambda=0.5,
                                objective='binary', scale_pos_weight=scale,
                                verbose=-1)
        m.fit(X_tr, ytr, eval_set=[(X_val, yval)], eval_metric='auc',
              callbacks=[lgb.early_stopping(stopping_rounds=30, verbose=False)])
        models[label] = m
        m.booster_.save_model(f'{args.out_dir}/vpa_ml_{label}.lgb')

        preds['val'][label] = m.predict_proba(X_val)[:,1]
        preds['test1'][label] = m.predict_proba(X_te1)[:,1]
        preds['test2'][label] = m.predict_proba(X_te2)[:,1]

        print(f'\n=== {label} ({label_zh[label]}) ===')
        print(f'  best_iter: {m.best_iteration_}')
        for set_name, ytrue, ypred in [
            ('Train', ytr, m.predict_proba(X_tr)[:,1]),
            ('Val 大跌', yval, preds['val'][label]),
            ('Test1 大涨', yt1, preds['test1'][label]),
            ('Test2 反弹', yt2, preds['test2'][label]),
        ]:
            try:
                auc = roc_auc_score(ytrue, ypred) if len(set(ytrue)) > 1 else 0.5
            except: auc = 0.5
            print(f'    {set_name:<12}  N={len(ytrue):>5}  pos_rate={ytrue.mean()*100:>5.1f}%  AUC={auc:.3f}')

    # Top quintile lift on each test
    print(f'\n=== Top quintile lift (Test1 大涨, gain_20) ===')
    yt1_gain = test1['gain_20'].values
    for label in label_names:
        p = preds['test1'][label]
        order = np.argsort(-p)
        n = len(p)
        if n < 5: continue
        topq = yt1_gain[order[:n//5]].mean()
        botq = yt1_gain[order[-n//5:]].mean()
        print(f'  {label:<18} top_q gain_20={topq:>+6.2f}%  bot_q={botq:>+6.2f}%  lift={topq-botq:+.2f}pp')

    # Top 10 features per model
    print(f'\n=== Top 10 features (topping classifier — ML 学的 BC) ===')
    imp = pd.Series(models['t_topping'].feature_importances_, index=feature_cols).sort_values(ascending=False)
    for f, v in imp.head(10).items():
        print(f'  {f:<32}  {v:>5}')

    print(f'\n=== Top 10 features (bottoming classifier — ML 学的 SC) ===')
    imp = pd.Series(models['t_bottoming'].feature_importances_, index=feature_cols).sort_values(ascending=False)
    for f, v in imp.head(10).items():
        print(f'  {f:<32}  {v:>5}')

    # Combined trading signal: BUY = high continuation + low topping
    print(f'\n=== Trading signal Top N (Test1 大涨) ===')
    print(f'  BUY score = P(continuation) + P(bottoming) - P(topping) - P(breakdown)')
    score_t1 = (preds['test1']['t_continuation'] + preds['test1']['t_bottoming']
                - preds['test1']['t_topping'] - preds['test1']['t_breakdown'])
    score_val = (preds['val']['t_continuation'] + preds['val']['t_bottoming']
                 - preds['val']['t_topping'] - preds['val']['t_breakdown'])
    score_t2 = (preds['test2']['t_continuation'] + preds['test2']['t_bottoming']
                - preds['test2']['t_topping'] - preds['test2']['t_breakdown'])

    print(f'{"Set":<22}{"all":>10}{"top 5":>10}{"top 10":>10}{"top 20":>10}{"top 50":>10}{"bot 20":>10}')
    for set_name, sc, y in [
        ('Test1 大涨 (gain_20)', score_t1, test1['gain_20'].values),
        ('Val 大跌 (gain_20)', score_val, val['gain_20'].values),
        ('Test2 反弹 (gain_20)', score_t2, test2['gain_20'].values),
    ]:
        order = np.argsort(-sc)
        all_avg = y.mean()
        t5 = y[order[:5]].mean() if len(y)>=5 else 0
        t10 = y[order[:10]].mean() if len(y)>=10 else 0
        t20 = y[order[:20]].mean() if len(y)>=20 else 0
        t50 = y[order[:50]].mean() if len(y)>=50 else 0
        b20 = y[order[-20:]].mean() if len(y)>=20 else 0
        print(f'{set_name:<22}{all_avg:>+9.2f}%{t5:>+9.2f}%{t10:>+9.2f}%{t20:>+9.2f}%{t50:>+9.2f}%{b20:>+9.2f}%')


if __name__ == '__main__':
    main()
