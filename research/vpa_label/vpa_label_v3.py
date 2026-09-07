"""VPA 标注 v3 - 基于 v2 backtest 失败的诊断修复.

修复点 (vs v2):
1. SC / STOPPING_VOLUME 必须 trend_30 < -5% (真长期下跌, 不是牛市中 5 天小回调)
2. SC 添加 wide_spread 要求 (Anna 原 spec 要求, v2 漏了)
3. NO_SUPPLY 限制在 trend_30 ∈ (-15%, +10%) — 暴涨/暴跌不算 pullback
4. BC / TOPPING_OUT 必须 trend_30 > +10% (真 markup)
5. NO_DEMAND 限制在 trend_30 ∈ (-10%, +20%)
"""
import numpy as np
import pandas as pd

N_VOL = 20
N_TREND = 10           # short-term trend (kept for some signals)
N_TREND_LONG = 30      # 新增: long-term trend window
LOOKAHEAD = 3


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
    # NEW: 30-day trend in PERCENT (not absolute price diff)
    df['trend_30_pct'] = (df['close'] / df['close'].shift(N_TREND_LONG) - 1) * 100
    df['high_10'] = df['high'].shift(1).rolling(N_TREND).max()
    df['low_10'] = df['low'].shift(1).rolling(N_TREND).min()
    return df


def label_bar(df, i):
    row = df.iloc[i]
    if pd.isna(row['vol_ratio']) or pd.isna(row['spread_ratio']) or pd.isna(row['trend_30_pct']):
        return []

    s = []
    uptrend_short = row['trend_10'] > 0
    downtrend_short = row['trend_10'] < 0
    trend_30 = row['trend_30_pct']
    ultra_high_vol = row['vol_ratio'] >= 2.5
    high_vol = row['vol_ratio'] >= 1.5
    low_vol = row['vol_ratio'] < 0.8
    wide_spread = row['spread_ratio'] >= 1.4
    narrow_spread = row['spread_ratio'] <= 0.7
    upper_wick_big = row['upper_wick'] > row['spread'] * 0.35
    lower_wick_big = row['lower_wick'] > row['spread'] * 0.35
    bullish = row['close'] > row['open']
    bearish = row['close'] < row['open']

    if ultra_high_vol:
        s.append('ULTRA_HIGH_VOL')
    elif high_vol:
        s.append('HIGH_VOL')
    if wide_spread:
        s.append('WIDE_SPREAD')
    elif narrow_spread:
        s.append('NARROW_SPREAD')

    # BC: trend_30 > +10% AND high_vol AND wide AND upper_wick AND close_pos<0.6
    if (trend_30 > 10 and high_vol and wide_spread and upper_wick_big
            and row['close_pos'] < 0.6):
        s.append('POTENTIAL_BC')

    # SC: trend_30 < -5% AND high_vol AND wide AND lower_wick AND close_pos>0.4
    if (trend_30 < -5 and high_vol and wide_spread and lower_wick_big
            and row['close_pos'] > 0.4):
        s.append('POTENTIAL_SC')

    # STOPPING_VOLUME: trend_30 < -5% AND high_vol AND lower_wick AND close_pos>0.35
    if (trend_30 < -5 and high_vol and lower_wick_big and row['close_pos'] > 0.35):
        s.append('STOPPING_VOLUME')

    # TOPPING_OUT: trend_30 > +10% AND high_vol AND upper_wick AND close_pos<0.65
    if (trend_30 > 10 and high_vol and upper_wick_big and row['close_pos'] < 0.65):
        s.append('TOPPING_OUT_VOL')

    # NO_DEMAND / NO_SUPPLY (with regime constraints)
    if i >= 2:
        prev_v0 = df.iloc[i-1]['volume']
        prev_v1 = df.iloc[i-2]['volume']
        lower_than_two = row['volume'] < prev_v0 and row['volume'] < prev_v1
        # NO_DEMAND: 上涨过程中(但非暴涨) 反弹小阳线无量
        if (bullish and narrow_spread and low_vol and lower_than_two
                and -10 < trend_30 < 20):
            s.append('NO_DEMAND')
        # NO_SUPPLY: 回调过程中(但非暴跌) 小阴线无量
        if (bearish and narrow_spread and low_vol and lower_than_two
                and -15 < trend_30 < 10):
            s.append('NO_SUPPLY')

    # TEST (low vol pullback with long lower wick) - keep as v2
    if low_vol and lower_wick_big and row['close_pos'] > 0.5:
        s.append('TEST')

    # UPTHRUST: 突破前 10 日高失败 + close_pos<0.4
    if not pd.isna(row['high_10']) and row['high'] > row['high_10']:
        if upper_wick_big and row['close_pos'] < 0.4:
            s.append('UPTHRUST')

    # EFFORT vs RESULT
    if high_vol and narrow_spread:
        if uptrend_short:
            s.append('EFFORT_NO_RESULT_UP')
        elif downtrend_short:
            s.append('EFFORT_NO_RESULT_DOWN')

    return s


def confirm_label(df, i, signal):
    """Same as v2."""
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
    return 'N/A'


def forward_return(df, idx, days):
    if idx + days >= len(df):
        return None
    c0 = df.iloc[idx]['close']
    c1 = df.iloc[idx + days]['close']
    return (c1 - c0) / c0 * 100
