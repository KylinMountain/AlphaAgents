"""VPA 规则化标注 v2 - 完整 Anna Coulling 框架.

vs v1 改进:
1. 9 个信号 (v1 只有 6): BC, SC, Stopping Vol, Topping Out, No Demand, No Supply, Test, Upthrust, Effort-vs-Result
2. 量价分层: ULTRA_HIGH (>=2.5×), HIGH (1.5-2.5×), AVG, LOW (<0.8×)
3. 价差分层: WIDE (>=1.4×), NARROW (<=0.7×)
4. 多标签/根 K 线: 一根可同时是 ULTRA_HIGH_VOL + WIDE_SPREAD + POTENTIAL_BC
5. 三段式确认: POTENTIAL → CONFIRMED → FAILED (基于后续 1-3 根 K 验证)

输出:
- data/vpa_label_v2_300033.jsonl
- data/charts/300033_vpa_v2.png
- 控制台: 每个信号 + 每个阶段的预测验证
"""
import sqlite3, json
from collections import defaultdict
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
from pathlib import Path

_avail = {f.name for f in fm.fontManager.ttflist}
for cjk in ["PingFang HK", "PingFang SC", "Heiti SC", "STHeiti", "Arial Unicode MS"]:
    if cjk in _avail:
        plt.rcParams["font.sans-serif"] = [cjk]
        plt.rcParams["font.family"] = "sans-serif"
        break
plt.rcParams["axes.unicode_minus"] = False


CODE = '300033'
N_VOL = 20            # 滚动均量窗口
N_TREND = 10          # 趋势判定窗口
LOOKAHEAD = 3         # 确认信号的前瞻窗口


def load_data(code):
    conn = sqlite3.connect('/Users/evilkylin/Projects/AlphaAgents/data/market_history.db')
    rows = list(conn.execute(
        'SELECT date, open, high, low, close, volume FROM daily_kline WHERE code=? ORDER BY date',
        (code,)
    ))
    df = pd.DataFrame(rows, columns=['date', 'open', 'high', 'low', 'close', 'volume'])
    return df


def add_vpa_features(df, n=N_VOL):
    df = df.copy()
    df['spread'] = df['high'] - df['low']
    df['body'] = (df['close'] - df['open']).abs()
    df['upper_wick'] = df['high'] - df[['open', 'close']].max(axis=1)
    df['lower_wick'] = df[['open', 'close']].min(axis=1) - df['low']
    df['close_pos'] = np.where(df['spread'] > 0,
                                (df['close'] - df['low']) / df['spread'],
                                0.5)
    df['vol_ma'] = df['volume'].rolling(n).mean()
    df['spread_ma'] = df['spread'].rolling(n).mean()
    df['vol_ratio'] = df['volume'] / df['vol_ma']
    df['spread_ratio'] = df['spread'] / df['spread_ma']
    df['trend_10'] = df['close'] - df['close'].shift(N_TREND)
    df['high_10'] = df['high'].shift(1).rolling(N_TREND).max()
    df['low_10'] = df['low'].shift(1).rolling(N_TREND).min()
    return df


def label_bar(df, i):
    """Return list of labels (potential signals) for bar i."""
    row = df.iloc[i]
    if pd.isna(row['vol_ratio']) or pd.isna(row['spread_ratio']):
        return []

    s = []
    uptrend = row['trend_10'] > 0
    downtrend = row['trend_10'] < 0
    ultra_high_vol = row['vol_ratio'] >= 2.5
    high_vol = row['vol_ratio'] >= 1.5
    low_vol = row['vol_ratio'] < 0.8
    wide_spread = row['spread_ratio'] >= 1.4
    narrow_spread = row['spread_ratio'] <= 0.7
    upper_wick_big = row['upper_wick'] > row['spread'] * 0.35
    lower_wick_big = row['lower_wick'] > row['spread'] * 0.35
    bullish = row['close'] > row['open']
    bearish = row['close'] < row['open']

    # Volume / spread 分层
    if ultra_high_vol:
        s.append('ULTRA_HIGH_VOL')
    elif high_vol:
        s.append('HIGH_VOL')
    if wide_spread:
        s.append('WIDE_SPREAD')
    elif narrow_spread:
        s.append('NARROW_SPREAD')

    # Buying Climax (BC) - 上涨末高量冲高回落
    if uptrend and high_vol and upper_wick_big and row['close_pos'] < 0.6:
        s.append('POTENTIAL_BC')

    # Selling Climax (SC) - 下跌末高量打底拉回
    if downtrend and high_vol and lower_wick_big and row['close_pos'] > 0.4:
        s.append('POTENTIAL_SC')

    # Stopping Volume - 下跌中突然高量但收得不弱
    if downtrend and high_vol and lower_wick_big and row['close_pos'] > 0.35:
        s.append('STOPPING_VOLUME')

    # Topping Out Volume - 上涨中高量但收得不强
    if uptrend and high_vol and upper_wick_big and row['close_pos'] < 0.65:
        s.append('TOPPING_OUT_VOL')

    # No Demand / No Supply - 需要前 2 根低于均量
    if i >= 2:
        prev_v0 = df.iloc[i-1]['volume']
        prev_v1 = df.iloc[i-2]['volume']
        lower_than_two = row['volume'] < prev_v0 and row['volume'] < prev_v1
        if bullish and narrow_spread and low_vol and lower_than_two:
            s.append('NO_DEMAND')
        if bearish and narrow_spread and low_vol and lower_than_two:
            s.append('NO_SUPPLY')

    # Test - 低量回踩,长下影,收回中上
    if low_vol and lower_wick_big and row['close_pos'] > 0.5:
        s.append('TEST')

    # Upthrust - 突破前高失败
    if not pd.isna(row['high_10']) and row['high'] > row['high_10']:
        if upper_wick_big and row['close_pos'] < 0.4:
            s.append('UPTHRUST')

    # Effort vs Result 异常
    if high_vol and narrow_spread:
        if uptrend:
            s.append('EFFORT_NO_RESULT_UP')
        elif downtrend:
            s.append('EFFORT_NO_RESULT_DOWN')

    return s


def confirm_label(df, i, signal):
    """Return CONFIRMED / FAILED / PENDING based on next LOOKAHEAD bars.

    BC / TOPPING / UPTHRUST / NO_DEMAND / EFFORT_NO_RESULT_UP: bearish thesis
      → CONFIRMED if 后续不创新高 + 至少 1 根阴线
      → FAILED   if 后续创新高
    SC / STOPPING / NO_SUPPLY / TEST / EFFORT_NO_RESULT_DOWN: bullish thesis
      → CONFIRMED if 后续不创新低 + 至少 1 根阳线
      → FAILED   if 后续创新低
    """
    if i + LOOKAHEAD >= len(df):
        return 'PENDING'

    cur_high = df.iloc[i]['high']
    cur_low = df.iloc[i]['low']
    nexts = df.iloc[i+1:i+1+LOOKAHEAD]

    bearish_thesis = signal in ('POTENTIAL_BC', 'TOPPING_OUT_VOL', 'UPTHRUST',
                                  'NO_DEMAND', 'EFFORT_NO_RESULT_UP')
    bullish_thesis = signal in ('POTENTIAL_SC', 'STOPPING_VOLUME', 'NO_SUPPLY',
                                  'TEST', 'EFFORT_NO_RESULT_DOWN')

    if bearish_thesis:
        if (nexts['high'] > cur_high).any():
            return 'FAILED'
        if (nexts['close'] < nexts['open']).any():
            return 'CONFIRMED'
        return 'PENDING'

    if bullish_thesis:
        if (nexts['low'] < cur_low).any():
            return 'FAILED'
        if (nexts['close'] > nexts['open']).any():
            return 'CONFIRMED'
        return 'PENDING'

    return 'N/A'  # for tier labels (ULTRA_HIGH_VOL etc.)


def label_all(df):
    """Apply rules to all bars. Returns list of (i, signal, status, ...)."""
    out = []
    for i in range(len(df)):
        sigs = label_bar(df, i)
        if not sigs:
            continue
        for sig in sigs:
            entry = {
                'idx': i,
                'date': df.iloc[i]['date'],
                'signal': sig,
                'open': float(df.iloc[i]['open']),
                'high': float(df.iloc[i]['high']),
                'low': float(df.iloc[i]['low']),
                'close': float(df.iloc[i]['close']),
                'volume': int(df.iloc[i]['volume']),
                'vol_ratio': float(df.iloc[i]['vol_ratio']),
                'spread_ratio': float(df.iloc[i]['spread_ratio']),
                'close_pos': float(df.iloc[i]['close_pos']),
            }
            entry['status'] = confirm_label(df, i, sig)
            out.append(entry)
    return out


def forward_return(df, idx, days):
    if idx + days >= len(df):
        return None
    c0 = df.iloc[idx]['close']
    c1 = df.iloc[idx + days]['close']
    return (c1 - c0) / c0 * 100


def plot_chart(df, labels, out_path):
    n = len(df)
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(22, 10), sharex=True,
                                    gridspec_kw={'height_ratios': [3, 1]})

    for i in range(n):
        op, hi, lo, cl = df.iloc[i][['open', 'high', 'low', 'close']]
        color = '#d62728' if cl >= op else '#2ca02c'
        ax1.plot([i, i], [lo, hi], color=color, linewidth=0.4, alpha=0.7)
        ax1.bar(i, abs(cl - op), bottom=min(op, cl), color=color, width=0.7,
                alpha=0.7, edgecolor='black', linewidth=0.15)
        ax2.bar(i, df.iloc[i]['volume'], color=color, width=0.7, alpha=0.7)

    # 只画 6 个核心 directional signal (skip volume tier + spread tier)
    sig_styles = {
        'POTENTIAL_BC':       ('v', '#8B0000', 13, 'BC顶'),
        'POTENTIAL_SC':       ('^', '#006400', 13, 'SC底'),
        'TOPPING_OUT_VOL':    ('v', '#FF1493', 9,  'Topping Out'),
        'STOPPING_VOLUME':    ('^', '#1E90FF', 9,  'Stopping Vol'),
        'UPTHRUST':           ('*', '#000080', 14, 'Upthrust'),
        'NO_DEMAND':          ('o', '#9467bd', 8,  'No Demand'),
        'NO_SUPPLY':          ('o', '#FFA500', 8,  'No Supply'),
        'TEST':               ('D', '#20B2AA', 8,  'Test'),
        'EFFORT_NO_RESULT_UP':   ('s', '#A52A2A', 7, 'Effort↑无果'),
        'EFFORT_NO_RESULT_DOWN': ('s', '#4682B4', 7, 'Effort↓无果'),
    }
    seen = set()
    for lab in labels:
        sig = lab['signal']
        if sig not in sig_styles:
            continue
        m, c, sz, leg = sig_styles[sig]
        # CONFIRMED 用满色 + 大,FAILED 用浅色 + 小
        is_confirmed = lab['status'] == 'CONFIRMED'
        is_failed = lab['status'] == 'FAILED'
        size = sz * (12 if is_confirmed else 8)
        alpha = 1.0 if is_confirmed else (0.4 if is_failed else 0.7)
        edge = 'black' if is_confirmed else 'gray'
        i = lab['idx']
        # 上方还是下方
        bullish = sig in ('POTENTIAL_SC', 'STOPPING_VOLUME', 'NO_SUPPLY', 'TEST', 'EFFORT_NO_RESULT_DOWN')
        y = lab['low'] * 0.97 if bullish else lab['high'] * 1.03
        legend_arg = leg if sig not in seen else None
        ax1.scatter(i, y, marker=m, s=size, color=c, alpha=alpha,
                     edgecolors=edge, linewidths=0.6, zorder=10, label=legend_arg)
        seen.add(sig)

    ax1.set_title(f'{CODE} VPA v2 (实心=CONFIRMED, 透明=FAILED, {df.iloc[0]["date"]} → {df.iloc[-1]["date"]})', fontsize=12)
    ax1.set_ylabel('Price')
    ax1.legend(loc='upper left', fontsize=9, ncol=4)
    ax1.grid(True, alpha=0.3)
    ax2.set_ylabel('Volume')
    ax2.grid(True, alpha=0.3)
    tick_idx = list(range(0, n, 20))
    ax2.set_xticks(tick_idx)
    ax2.set_xticklabels([df.iloc[i]['date'][2:] for i in tick_idx], rotation=45, fontsize=7)
    ax2.set_xlabel('Date')
    plt.tight_layout()
    plt.savefig(out_path, dpi=110, bbox_inches='tight')
    plt.close()


def main():
    df = load_data(CODE)
    print(f'{CODE}: {len(df)} bars  {df.iloc[0]["date"]} → {df.iloc[-1]["date"]}')

    df = add_vpa_features(df)
    labels = label_all(df)

    # Filter: only directional signals (skip vol/spread tier labels for stats)
    DIRECTIONAL = {'POTENTIAL_BC', 'POTENTIAL_SC', 'TOPPING_OUT_VOL', 'STOPPING_VOLUME',
                    'NO_DEMAND', 'NO_SUPPLY', 'TEST', 'UPTHRUST',
                    'EFFORT_NO_RESULT_UP', 'EFFORT_NO_RESULT_DOWN'}
    dir_labels = [l for l in labels if l['signal'] in DIRECTIONAL]

    # Frequency
    print(f'\nTotal labels: {len(labels)} (含分层标签); directional: {len(dir_labels)}')
    print(f'\n=== 信号频次 + 三段式状态分布 ===')
    print(f'{"Signal":<24} {"Total":>6} {"CONFIRMED":>10} {"FAILED":>8} {"PENDING":>8}')
    by_sig = defaultdict(list)
    for l in dir_labels:
        by_sig[l['signal']].append(l)
    for sig in sorted(DIRECTIONAL):
        labs = by_sig.get(sig, [])
        if not labs: continue
        c = sum(1 for l in labs if l['status'] == 'CONFIRMED')
        f = sum(1 for l in labs if l['status'] == 'FAILED')
        p = sum(1 for l in labs if l['status'] == 'PENDING')
        print(f'{sig:<24} {len(labs):>6} {c:>10} {f:>8} {p:>8}')

    # Predictive validation - separate by status
    print('\n=== 前瞻收益验证 (按 CONFIRMED / FAILED 分别统计) ===')
    print(f'{"Signal":<24} {"Status":<10} {"+5d mean":>10} {"+10d mean":>11} {"hit%":>6} {"N":>4}')

    BEARISH = {'POTENTIAL_BC', 'TOPPING_OUT_VOL', 'UPTHRUST', 'NO_DEMAND', 'EFFORT_NO_RESULT_UP'}
    for sig in sorted(DIRECTIONAL):
        labs = by_sig.get(sig, [])
        if not labs: continue
        bearish_thesis = sig in BEARISH
        for status in ['CONFIRMED', 'FAILED']:
            sub = [l for l in labs if l['status'] == status]
            if not sub: continue
            r5 = [forward_return(df, l['idx'], 5) for l in sub]
            r10 = [forward_return(df, l['idx'], 10) for l in sub]
            r5 = [r for r in r5 if r is not None]
            r10 = [r for r in r10 if r is not None]
            if not r10: continue
            hit = sum(1 for r in r10 if (r < 0) == bearish_thesis) / len(r10) * 100
            print(f'{sig:<24} {status:<10} {np.mean(r5):>+9.2f}% {np.mean(r10):>+10.2f}% {hit:>5.0f}% {len(sub):>4}')

    # Save
    out_jsonl = Path('/Users/evilkylin/Projects/AlphaAgents/data') / f'vpa_label_v2_{CODE}.jsonl'
    with open(out_jsonl, 'w') as f:
        for l in labels:
            f.write(json.dumps(l, ensure_ascii=False) + '\n')

    chart_dir = Path('/Users/evilkylin/Projects/AlphaAgents/data/charts')
    chart_dir.mkdir(parents=True, exist_ok=True)
    out_png = chart_dir / f'{CODE}_vpa_v2.png'
    plot_chart(df, labels, out_png)

    print(f'\n[saved] {out_jsonl}')
    print(f'[saved] {out_png}')


if __name__ == '__main__':
    main()
