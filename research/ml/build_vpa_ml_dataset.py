"""构建 VPA-aware ML 训练集.

策略:
- Features: Anna 概念框架 + raw 时序, 全 continuous (无 hardcode threshold)
- Labels: 纯 future outcome (无主观规则)

Output: data/vpa_ml_dataset.jsonl
- 每行: 一个 (code, as_of) 的 features + multi-task labels

扫描全市场每个 bar (5524 stocks × ~250 days × 1.5 years = 2M bars)
day_stride=5 减小到 ~400K bars
"""
import json, sqlite3, time, sys, argparse
from collections import defaultdict
import numpy as np
import pandas as pd


def compute_features(rows_120d):
    """rows_120d: list of (date, open, high, low, close, volume), 末尾是 as_of."""
    if len(rows_120d) < 60:
        return None
    df = pd.DataFrame(rows_120d, columns=['date','open','high','low','close','volume'])
    today = df.iloc[-1]
    feats = {}

    # 1. K线字符 (continuous, 不阈值化)
    hl = today['high'] - today['low']
    feats['close_position'] = float((today['close'] - today['low']) / max(hl, 0.01))
    feats['upper_shadow_ratio'] = float((today['high'] - max(today['open'], today['close'])) / max(hl, 0.01))
    feats['lower_shadow_ratio'] = float((min(today['open'], today['close']) - today['low']) / max(hl, 0.01))
    feats['body_ratio'] = float(abs(today['close'] - today['open']) / max(hl, 0.01))
    feats['change_pct_today'] = float((today['close'] - df.iloc[-2]['close']) / df.iloc[-2]['close'] * 100) if len(df) >= 2 else 0
    feats['gap_pct_today'] = float((today['open'] - df.iloc[-2]['close']) / df.iloc[-2]['close'] * 100) if len(df) >= 2 else 0

    # 2. 量 (continuous)
    feats['vol_today'] = float(today['volume'])
    feats['vol_log'] = float(np.log1p(today['volume']))
    for w in [5, 20, 60]:
        if len(df) >= w:
            ma = df.tail(w)['volume'].mean()
            feats[f'vol_x_{w}d_ma'] = float(today['volume'] / max(ma, 1))
            # 量分位 (continuous, vs hardcoded p95)
            sorted_vols = sorted(df.tail(w)['volume'].tolist())
            rank = np.searchsorted(sorted_vols, today['volume'])
            feats[f'vol_pct_{w}d'] = float(rank / max(len(sorted_vols), 1))

    # 3. 振幅 (continuous, 不阈值化"宽幅")
    feats['range_pct'] = float(hl / today['close'] * 100) if today['close'] > 0 else 0
    for w in [5, 20]:
        if len(df) >= w:
            avg_range = ((df.tail(w)['high'] - df.tail(w)['low']) / df.tail(w)['close']).mean() * 100
            feats[f'range_x_{w}d'] = float(feats['range_pct'] / max(avg_range, 0.01))

    # 4. 位置 (continuous)
    for w in [20, 60, 120]:
        if len(df) >= w:
            sub = df.tail(w)
            hi = sub['high'].max(); lo = sub['low'].min()
            feats[f'pos_{w}d'] = float((today['close'] - lo) / max(hi - lo, 0.01))
            feats[f'days_since_{w}d_high'] = float(len(sub) - 1 - sub['close'].values.argmax())
            feats[f'days_since_{w}d_low'] = float(len(sub) - 1 - sub['close'].values.argmin())

    # 5. 趋势 (continuous, 多 horizon)
    for w in [3, 5, 10, 20, 40, 60]:
        if len(df) >= w + 1:
            feats[f'trend_{w}d_pct'] = float((today['close'] - df.iloc[-w-1]['close']) / df.iloc[-w-1]['close'] * 100)

    # 6. 波动率 (continuous)
    for w in [5, 20, 60]:
        if len(df) >= w:
            r = df.tail(w)['close'].pct_change().dropna()
            feats[f'vol_std_{w}d'] = float(r.std() * 100) if len(r) > 0 else 0

    # 7. vph (continuous)
    for w in [5, 10, 20]:
        if len(df) >= w:
            sub = df.tail(w).copy()
            sub['ret'] = sub['close'].pct_change()
            up_v = sub[sub['ret'] > 0]['volume'].sum()
            dn_v = sub[sub['ret'] < 0]['volume'].sum()
            feats[f'vph_{w}d'] = float(up_v / max(dn_v, 1))
            feats[f'vph_{w}d_log'] = float(np.log1p(feats[f'vph_{w}d']))

    # 8. raw OHLCV 时序 (lags, normalized to today's close)
    base_close = today['close']
    for lag in [1, 2, 3, 5, 10, 20, 40]:
        if len(df) > lag:
            feats[f'close_lag_{lag}_norm'] = float(df.iloc[-1-lag]['close'] / base_close)
            feats[f'vol_lag_{lag}_log'] = float(np.log1p(df.iloc[-1-lag]['volume']))
            feats[f'high_lag_{lag}_norm'] = float(df.iloc[-1-lag]['high'] / base_close)
            feats[f'low_lag_{lag}_norm'] = float(df.iloc[-1-lag]['low'] / base_close)

    # 9. K-line pattern (last 5 bars)
    last5 = df.tail(5)
    feats['streak_up'] = 0
    feats['streak_down'] = 0
    for c in reversed(last5['close'].pct_change().dropna().tolist()):
        if c > 0.003: feats['streak_up'] += 1; feats['streak_down'] = 0
        elif c < -0.003: feats['streak_down'] += 1; feats['streak_up'] = 0
        else: break

    # 10. 距离 60 日 high/low 的 pct
    if len(df) >= 60:
        last60 = df.tail(60)
        feats['close_to_60d_high_pct'] = float((today['close'] - last60['high'].max()) / last60['high'].max() * 100)
        feats['close_to_60d_low_pct'] = float((today['close'] - last60['low'].min()) / last60['low'].min() * 100)

    return feats


def compute_outcomes(rows_after, base_close):
    """rows_after: list of (date, close) starting from t+1."""
    if len(rows_after) < 5:
        return None
    closes = [r[1] for r in rows_after]
    rets = [(c - base_close) / base_close * 100 for c in closes]
    out = {}
    for w in [5, 10, 20]:
        if len(rets) >= w:
            sub = rets[:w]
            out[f'gain_{w}'] = float(max(sub))
            out[f'dd_{w}'] = float(min(sub))
            out[f'close_ret_{w}'] = float(rets[w-1]) if w-1 < len(rets) else None
        else:
            out[f'gain_{w}'] = None; out[f'dd_{w}'] = None; out[f'close_ret_{w}'] = None
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--start', default='2024-07-01')
    p.add_argument('--end', default='2026-04-29')
    p.add_argument('--boards', default='300,301,302', help='comma-separated code prefixes')
    p.add_argument('--day-stride', type=int, default=5)
    p.add_argument('--out', default='/Users/evilkylin/Projects/AlphaAgents/data/vpa_ml_dataset.jsonl')
    args = p.parse_args()

    boards = tuple(args.boards.split(','))

    print('Loading market history...', flush=True)
    conn = sqlite3.connect('/Users/evilkylin/Projects/AlphaAgents/data/market_history.db')
    hist = defaultdict(list)
    for code, date, op, hi, lo, cl, vol in conn.execute(
        'SELECT code, date, open, high, low, close, volume FROM daily_kline ORDER BY code, date'
    ):
        hist[code].append((date, op, hi, lo, cl, vol))

    target_codes = sorted([c for c in hist if c.startswith(boards)])
    print(f'  {len(target_codes)} codes', flush=True)

    all_dates = sorted(set(d for rows in hist.values() for d, *_ in rows))
    eval_dates = [d for d in all_dates if args.start <= d <= args.end][::args.day_stride]
    print(f'  {len(eval_dates)} eval dates ({eval_dates[0]} → {eval_dates[-1]})', flush=True)

    n_done = 0
    t0 = time.time()
    with open(args.out, 'w') as g:
        for di, as_of in enumerate(eval_dates):
            day_count = 0
            for code in target_codes:
                rows = hist.get(code, [])
                # Find as_of position
                idx = None
                for i, r in enumerate(rows):
                    if r[0] == as_of: idx = i; break
                if idx is None or idx < 60: continue

                window = rows[max(0, idx-119):idx+1]
                feats = compute_features(window)
                if feats is None: continue

                outcomes = compute_outcomes([(r[0], r[4]) for r in rows[idx+1:idx+1+25]], rows[idx][4])
                if outcomes is None or outcomes.get('gain_20') is None: continue

                g.write(json.dumps({
                    'code': code, 'as_of': as_of,
                    **feats, **outcomes,
                }, ensure_ascii=False) + '\n')
                day_count += 1
                n_done += 1
            if (di+1) % 5 == 0:
                elapsed = time.time() - t0
                eta = elapsed / (di+1) * (len(eval_dates) - di - 1)
                print(f'  {di+1}/{len(eval_dates)} {as_of}: {day_count} rows | total {n_done} | {elapsed:.0f}s ETA {eta:.0f}s', flush=True)

    print(f'\nDONE: {n_done} rows → {args.out}')


if __name__ == '__main__':
    main()
