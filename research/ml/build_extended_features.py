"""为 ml_dataset_full.jsonl 加 ~10 个新 features,
全部从原始 OHLCV 数据计算 (LLM reasoning 启发).

新 features:
1. vph_today, vph_5d_change             — 量价配合 (涨日量/跌日量比)
2. consolidation_days                    — 近期窄幅整理天数
3. days_since_local_low                  — 距 60 日低点天数
4. days_to_recover_prior_drop            — 从前期跌幅回升天数
5. recent_3bar_pattern                   — 最近 3 根 K 线序列编码
6. gap_count_recent                      — 近 5 日跳空数
7. streak_up_days, streak_down_days      — 连续涨/跌天数
8. prior_5d_volatility, prior_20d_vol    — 短中期波动率
9. range_compression_ratio               — 近 5 日区间压缩度
10. higher_highs_count, higher_lows_count — HH/HL 计数 (近 10 日)
11. weeks_since_low                       — 周线距低点周数 (已有, 重新提取)
"""
import json, sqlite3, sys, time
from collections import defaultdict
import numpy as np
import pandas as pd

DB = '/Users/evilkylin/Projects/AlphaAgents/data/market_history.db'
SRC = '/Users/evilkylin/Projects/AlphaAgents/data/ml_dataset_full.jsonl'
OUT = '/Users/evilkylin/Projects/AlphaAgents/data/ml_dataset_full_extended.jsonl'


def compute_extended(rows_120d):
    """rows_120d: list of (date, open, high, low, close, volume), 末尾是 as_of."""
    if len(rows_120d) < 30:
        return {}

    df = pd.DataFrame(rows_120d, columns=['date','open','high','low','close','volume'])
    df['change_pct'] = df['close'].pct_change() * 100
    df['range_pct'] = (df['high'] - df['low']) / df['close'] * 100
    df['gap_pct'] = (df['open'] - df['close'].shift(1)) / df['close'].shift(1) * 100

    today = df.iloc[-1]

    feats = {}

    # 1. vph (5 日量价配合: 涨日总量 / 跌日总量)
    last5 = df.tail(5)
    up_vol = last5[last5['change_pct'] > 0]['volume'].sum()
    down_vol = last5[last5['change_pct'] < 0]['volume'].sum()
    feats['vph_5d'] = up_vol / max(down_vol, 1)
    feats['vph_5d_log'] = np.log1p(feats['vph_5d'])

    # 5 日前的 vph 对比 → 求 change
    prior5 = df.iloc[-10:-5]
    prior_up = prior5[prior5['change_pct'] > 0]['volume'].sum()
    prior_down = prior5[prior5['change_pct'] < 0]['volume'].sum()
    feats['vph_prior5'] = prior_up / max(prior_down, 1)
    feats['vph_change'] = feats['vph_5d'] - feats['vph_prior5']

    # 2. consolidation_days (近 N 日的窄幅天数: range < 60 日均 range × 0.6)
    range_60d_avg = df.tail(60)['range_pct'].mean()
    narrow = df.tail(20)['range_pct'] < range_60d_avg * 0.6
    feats['consolidation_days'] = int(narrow.sum())

    # 3. days_since_local_low (60 日低点)
    last60 = df.tail(60)
    low_idx = last60['close'].idxmin()
    feats['days_since_low_60d'] = int(len(last60) - 1 - last60.index.get_loc(low_idx))
    high_idx = last60['close'].idxmax()
    feats['days_since_high_60d'] = int(len(last60) - 1 - last60.index.get_loc(high_idx))

    # 4. days_to_recover_prior_drop (距 5%+ 跌幅 完全回升的天数)
    prior_low_in_20d = df.tail(20)['close'].min()
    if prior_low_in_20d > 0:
        recovery_pct = (today['close'] - prior_low_in_20d) / prior_low_in_20d * 100
        feats['recovery_from_low_20d'] = recovery_pct
    else:
        feats['recovery_from_low_20d'] = 0

    # 5. recent_3bar_pattern (最近 3 根: U=阳, D=阴, F=平)
    last3 = df.tail(3)
    pat = ''.join(['U' if c > 0.3 else ('D' if c < -0.3 else 'F') for c in last3['change_pct']])
    feats['pat_UUU'] = int(pat == 'UUU')
    feats['pat_DDD'] = int(pat == 'DDD')
    feats['pat_DUU'] = int(pat == 'DUU')
    feats['pat_UDU'] = int(pat == 'UDU')
    feats['pat_DDU'] = int(pat == 'DDU')
    feats['pat_UUD'] = int(pat == 'UUD')

    # 6. gap_count_recent (近 5 日跳空)
    last5_gaps = df.tail(5)['gap_pct'].abs()
    feats['gap_up_count_5d'] = int((df.tail(5)['gap_pct'] > 1.0).sum())
    feats['gap_down_count_5d'] = int((df.tail(5)['gap_pct'] < -1.0).sum())

    # 7. streak (连续涨/跌)
    streak_up = 0; streak_down = 0
    for c in reversed(df['change_pct'].tolist()):
        if c > 0.3: streak_up += 1; streak_down = 0
        elif c < -0.3: streak_down += 1; streak_up = 0
        else: break
    feats['streak_up_days'] = streak_up
    feats['streak_down_days'] = streak_down

    # 8. volatility
    feats['vol_5d_std'] = float(df.tail(5)['change_pct'].std()) if len(df) >= 5 else 0
    feats['vol_20d_std'] = float(df.tail(20)['change_pct'].std()) if len(df) >= 20 else 0
    feats['vol_60d_std'] = float(df.tail(60)['change_pct'].std()) if len(df) >= 60 else 0

    # 9. range_compression: 5d range mean / 60d range mean
    range_5d = df.tail(5)['range_pct'].mean()
    range_60d = df.tail(60)['range_pct'].mean() if len(df) >= 60 else range_5d
    feats['range_compression'] = range_5d / max(range_60d, 0.01)

    # 10. HH/HL count (last 10 days HH = close > prev_max, HL = low > prev_min)
    last10 = df.tail(10)
    hh = 0; hl = 0
    for i in range(1, len(last10)):
        if last10.iloc[i]['close'] > last10.iloc[i-1]['close']: hh += 1
        if last10.iloc[i]['low'] > last10.iloc[i-1]['low']: hl += 1
    feats['hh_count_10d'] = hh
    feats['hl_count_10d'] = hl

    # 11. position metrics
    feats['close_to_60d_high_pct'] = (today['close'] - last60['high'].max()) / last60['high'].max() * 100
    feats['close_to_60d_low_pct'] = (today['close'] - last60['low'].min()) / last60['low'].min() * 100

    return feats


def main():
    print('Loading market history...', flush=True)
    conn = sqlite3.connect(DB)
    hist = defaultdict(list)
    for code, date, op, hi, lo, cl, vol in conn.execute(
        'SELECT code, date, open, high, low, close, volume FROM daily_kline ORDER BY code, date'
    ):
        hist[code].append((date, op, hi, lo, cl, vol))
    print(f'  {len(hist)} codes', flush=True)

    # Process each row in ml_dataset_full
    print('Reading source ml_dataset_full...', flush=True)
    rows = []
    with open(SRC) as f:
        for line in f:
            rows.append(json.loads(line))
    print(f'  {len(rows)} rows to extend', flush=True)

    # For each row, compute extended features
    n_done = 0; t0 = time.time()
    with open(OUT, 'w') as g:
        for r in rows:
            code = r['code']; as_of = r['as_of']
            full_rows = hist.get(code, [])
            # Take rows up to and including as_of, last 120
            cut = [x for x in full_rows if x[0] <= as_of][-120:]
            if len(cut) < 30:
                # Skip rows with too little history
                ext = {}
            else:
                try:
                    ext = compute_extended(cut)
                except Exception as e:
                    ext = {}
            r2 = dict(r)
            r2.update({f'ext_{k}': v for k, v in ext.items()})
            g.write(json.dumps(r2, ensure_ascii=False) + '\n')
            n_done += 1
            if n_done % 5000 == 0:
                elapsed = time.time() - t0
                eta = elapsed / n_done * (len(rows) - n_done)
                print(f'  {n_done}/{len(rows)} ({elapsed:.0f}s, ETA {eta:.0f}s)', flush=True)

    print(f'\nDONE: {n_done} rows extended → {OUT}')


if __name__ == '__main__':
    main()
