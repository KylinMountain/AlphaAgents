"""VPA v4 - rich output labeler.

每个信号点输出:
  signal             # POTENTIAL_BC / CONFIRMED_BC / FAILED_BC / NO_DEMAND / ...
  confidence         # 0-1 连续分数 (基于各 criterion 满足程度)
  status             # POTENTIAL / CONFIRMED / FAILED / PENDING
  context            # {trend_position, near_resistance, near_support, in_range}
  details            # 触发用到的所有具体数值
  reason             # 人类可读的解释

设计原则 (per Anna 框架 + 你的 spec):
- VPA 是概率不是确定性 → 用 confidence 而不是二值分类
- 信号在不同背景下含义不同 → context 字段单独标出
- 阈值需要校准 → 把 vol_ratio / spread_ratio 等阈值集中在顶部参数
- 三段式 (POTENTIAL → CONFIRMED → FAILED) 通过事件驱动而非固定 LOOKAHEAD
"""
import sqlite3, json, sys
from collections import defaultdict
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
from pathlib import Path

_avail = {f.name for f in fm.fontManager.ttflist}
for cjk in ["PingFang HK", "PingFang SC", "Heiti SC", "STHeiti"]:
    if cjk in _avail:
        plt.rcParams["font.sans-serif"] = [cjk]
        plt.rcParams["font.family"] = "sans-serif"
        break
plt.rcParams["axes.unicode_minus"] = False


# Calibrated thresholds (中国 A 股创业板)
VOL_HIGH = 1.5         # HIGH_VOL 起点
VOL_ULTRA = 2.5        # ULTRA_HIGH_VOL 起点
VOL_LOW = 0.7          # LOW_VOL 上限
SPREAD_WIDE = 1.4
SPREAD_NARROW = 0.7
WICK_BIG = 0.35        # wick 占 spread 比例
N_VOL = 20
N_TREND_LONG = 30
N_SR = 30              # 支撑/压力计算窗口
LOOKAHEAD_MAX = 5      # 最多等 5 根 K 做事件驱动确认


def clamp01(x):
    return max(0.0, min(1.0, x))


def trend_position(trend_30):
    """Categorize trend regime."""
    if trend_30 > 25:
        return 'uptrend_extended'
    elif trend_30 > 8:
        return 'uptrend_normal'
    elif trend_30 > -8:
        return 'sideways'
    elif trend_30 > -25:
        return 'downtrend_normal'
    else:
        return 'downtrend_extended'


def add_features(df):
    df = df.copy()
    df['spread'] = df['high'] - df['low']
    df['body'] = (df['close'] - df['open']).abs()
    df['upper_wick'] = df['high'] - df[['open', 'close']].max(axis=1)
    df['lower_wick'] = df[['open', 'close']].min(axis=1) - df['low']
    df['close_pos'] = np.where(df['spread'] > 0,
                                (df['close'] - df['low']) / df['spread'], 0.5)
    df['upper_wick_pct'] = np.where(df['spread'] > 0, df['upper_wick'] / df['spread'], 0)
    df['lower_wick_pct'] = np.where(df['spread'] > 0, df['lower_wick'] / df['spread'], 0)

    df['vol_ma'] = df['volume'].rolling(N_VOL).mean()
    df['spread_ma'] = df['spread'].rolling(N_VOL).mean()
    df['vol_ratio'] = df['volume'] / df['vol_ma']
    df['spread_ratio'] = df['spread'] / df['spread_ma']

    df['trend_30'] = (df['close'] / df['close'].shift(N_TREND_LONG) - 1) * 100
    df['high_30'] = df['high'].shift(1).rolling(N_SR).max()
    df['low_30'] = df['low'].shift(1).rolling(N_SR).min()

    return df


def signal_BC(row, trend_30):
    """Return (matched, confidence, criteria).
    Buying Climax: extended uptrend + ultra/high vol + wide + upper wick + low close_pos.
    """
    if trend_30 < 8:
        return False, 0, {}
    if row['vol_ratio'] < VOL_HIGH:
        return False, 0, {}
    if row['spread_ratio'] < SPREAD_WIDE:
        return False, 0, {}
    if row['upper_wick_pct'] < WICK_BIG:
        return False, 0, {}
    if row['close_pos'] >= 0.6:
        return False, 0, {}
    # Confidence: 越极端越高
    scores = {
        'vol': clamp01((row['vol_ratio'] - VOL_HIGH) / (VOL_ULTRA - VOL_HIGH)),
        'spread': clamp01((row['spread_ratio'] - SPREAD_WIDE) / 0.6),
        'wick': clamp01((row['upper_wick_pct'] - WICK_BIG) / 0.35),
        'close_pos': clamp01((0.6 - row['close_pos']) / 0.4),
        'trend': clamp01((trend_30 - 8) / 25),
    }
    return True, np.mean(list(scores.values())), scores


def signal_SC(row, trend_30):
    """Selling Climax: extended downtrend + high vol + wide + lower wick + high close_pos."""
    if trend_30 > -5:
        return False, 0, {}
    if row['vol_ratio'] < VOL_HIGH:
        return False, 0, {}
    if row['spread_ratio'] < SPREAD_WIDE:
        return False, 0, {}
    if row['lower_wick_pct'] < WICK_BIG:
        return False, 0, {}
    if row['close_pos'] <= 0.4:
        return False, 0, {}
    scores = {
        'vol': clamp01((row['vol_ratio'] - VOL_HIGH) / (VOL_ULTRA - VOL_HIGH)),
        'spread': clamp01((row['spread_ratio'] - SPREAD_WIDE) / 0.6),
        'wick': clamp01((row['lower_wick_pct'] - WICK_BIG) / 0.35),
        'close_pos': clamp01((row['close_pos'] - 0.4) / 0.4),
        'trend': clamp01((-5 - trend_30) / 25),
    }
    return True, np.mean(list(scores.values())), scores


def signal_NO_DEMAND(row, trend_30, prev2_vol, prev1_vol):
    """No Demand: 上涨/盘整中, 小阳线, 窄幅, 低量, 比前两根都低."""
    bullish = row['close'] > row['open']
    if not bullish:
        return False, 0, {}
    if not (-10 < trend_30 < 20):
        return False, 0, {}
    if row['spread_ratio'] > SPREAD_NARROW:
        return False, 0, {}
    if row['vol_ratio'] >= VOL_LOW:
        return False, 0, {}
    if row['volume'] >= prev1_vol or row['volume'] >= prev2_vol:
        return False, 0, {}
    scores = {
        'spread': clamp01((SPREAD_NARROW - row['spread_ratio']) / SPREAD_NARROW),
        'vol': clamp01((VOL_LOW - row['vol_ratio']) / VOL_LOW),
    }
    return True, np.mean(list(scores.values())), scores


def signal_NO_SUPPLY(row, trend_30, prev2_vol, prev1_vol):
    """No Supply: 回调/盘整中, 小阴线, 窄幅, 低量, 比前两根都低."""
    bearish = row['close'] < row['open']
    if not bearish:
        return False, 0, {}
    if not (-15 < trend_30 < 10):
        return False, 0, {}
    if row['spread_ratio'] > SPREAD_NARROW:
        return False, 0, {}
    if row['vol_ratio'] >= VOL_LOW:
        return False, 0, {}
    if row['volume'] >= prev1_vol or row['volume'] >= prev2_vol:
        return False, 0, {}
    scores = {
        'spread': clamp01((SPREAD_NARROW - row['spread_ratio']) / SPREAD_NARROW),
        'vol': clamp01((VOL_LOW - row['vol_ratio']) / VOL_LOW),
    }
    return True, np.mean(list(scores.values())), scores


def signal_UPTHRUST(row, trend_30):
    """Upthrust: 突破前 30 日高失败 + 长上影 + 收盘下方."""
    if pd.isna(row['high_30']):
        return False, 0, {}
    if row['high'] <= row['high_30']:
        return False, 0, {}
    if row['upper_wick_pct'] < WICK_BIG:
        return False, 0, {}
    if row['close_pos'] >= 0.4:
        return False, 0, {}
    breach_pct = (row['high'] - row['high_30']) / row['high_30'] * 100
    scores = {
        'breach': clamp01(breach_pct / 3),       # 突破越多越像 upthrust
        'wick': clamp01((row['upper_wick_pct'] - WICK_BIG) / 0.35),
        'close_pos': clamp01((0.4 - row['close_pos']) / 0.4),
    }
    return True, np.mean(list(scores.values())), scores


def signal_TEST(row, trend_30):
    """Test: 低量, 长下影, 收盘上半."""
    if row['vol_ratio'] >= VOL_LOW:
        return False, 0, {}
    if row['lower_wick_pct'] < WICK_BIG:
        return False, 0, {}
    if row['close_pos'] <= 0.5:
        return False, 0, {}
    scores = {
        'vol': clamp01((VOL_LOW - row['vol_ratio']) / VOL_LOW),
        'wick': clamp01((row['lower_wick_pct'] - WICK_BIG) / 0.35),
        'close_pos': clamp01((row['close_pos'] - 0.5) / 0.5),
    }
    return True, np.mean(list(scores.values())), scores


# Signal type → (function, thesis_direction)
SIGNAL_DEFS = {
    'BC':         (signal_BC,         'bearish'),
    'SC':         (signal_SC,         'bullish'),
    'NO_DEMAND':  (None,              'bearish'),  # special args
    'NO_SUPPLY':  (None,              'bullish'),
    'UPTHRUST':   (signal_UPTHRUST,   'bearish'),
    'TEST':       (signal_TEST,       'bullish'),
}


def event_driven_confirm(df, i, signal_thesis, signal_high, signal_low, max_lookahead=LOOKAHEAD_MAX):
    """Event-driven 确认: 一旦满足条件即停, 不固定等 N 根.

    bearish thesis (BC/UPTHRUST/NO_DEMAND): 信号成立条件是后续不创新高+至少1阴
       - 创新高即立即 FAILED
       - 出现阴线且未创新高 → CONFIRMED
       - max_lookahead 内都没满足 → PENDING
    bullish thesis (SC/NO_SUPPLY/TEST): 后续不创新低+至少1阳
    """
    for k in range(1, min(max_lookahead, len(df) - i - 1) + 1):
        nxt = df.iloc[i + k]
        if signal_thesis == 'bearish':
            if nxt['high'] > signal_high:
                return 'FAILED', k
            if nxt['close'] < nxt['open']:
                return 'CONFIRMED', k
        elif signal_thesis == 'bullish':
            if nxt['low'] < signal_low:
                return 'FAILED', k
            if nxt['close'] > nxt['open']:
                return 'CONFIRMED', k
    return 'PENDING', max_lookahead


def label_bar_v4(df, i):
    """Apply all signals to bar i. Returns list of label dicts."""
    row = df.iloc[i]
    if pd.isna(row['vol_ratio']) or pd.isna(row['trend_30']):
        return []

    trend_30 = row['trend_30']
    trend_pos = trend_position(trend_30)
    near_resist = (not pd.isna(row['high_30'])) and row['close'] > row['high_30'] * 0.97
    near_support = (not pd.isna(row['low_30'])) and row['close'] < row['low_30'] * 1.03

    out = []
    candidates = []

    # 价量分层信号 (always emitted as info, not directional)
    # Directional signals
    matched, conf, scores = signal_BC(row, trend_30)
    if matched: candidates.append(('BC', conf, scores))

    matched, conf, scores = signal_SC(row, trend_30)
    if matched: candidates.append(('SC', conf, scores))

    if i >= 2:
        prev1 = df.iloc[i-1]['volume']
        prev2 = df.iloc[i-2]['volume']
        matched, conf, scores = signal_NO_DEMAND(row, trend_30, prev2, prev1)
        if matched: candidates.append(('NO_DEMAND', conf, scores))
        matched, conf, scores = signal_NO_SUPPLY(row, trend_30, prev2, prev1)
        if matched: candidates.append(('NO_SUPPLY', conf, scores))

    matched, conf, scores = signal_UPTHRUST(row, trend_30)
    if matched: candidates.append(('UPTHRUST', conf, scores))
    matched, conf, scores = signal_TEST(row, trend_30)
    if matched: candidates.append(('TEST', conf, scores))

    # Event-driven confirmation
    for sig, conf, scores in candidates:
        thesis = SIGNAL_DEFS.get(sig, (None, ''))[1] if sig in SIGNAL_DEFS else \
                 ('bearish' if sig in ('BC', 'UPTHRUST', 'NO_DEMAND') else 'bullish')
        status, days = event_driven_confirm(df, i, thesis, row['high'], row['low'])

        # Reason text
        reason = make_reason(sig, row, trend_30, scores)

        out.append({
            'idx': i,
            'date': row['date'],
            'signal': sig,
            'thesis': thesis,
            'confidence': round(float(conf), 3),
            'status': status,
            'confirm_days': days,
            'context': {
                'trend_30_pct': round(float(trend_30), 2),
                'trend_position': trend_pos,
                'near_resistance': bool(near_resist),
                'near_support': bool(near_support),
            },
            'details': {
                'open': round(float(row['open']), 3),
                'high': round(float(row['high']), 3),
                'low': round(float(row['low']), 3),
                'close': round(float(row['close']), 3),
                'volume': int(row['volume']),
                'vol_ratio': round(float(row['vol_ratio']), 2),
                'spread_ratio': round(float(row['spread_ratio']), 2),
                'upper_wick_pct': round(float(row['upper_wick_pct']), 2),
                'lower_wick_pct': round(float(row['lower_wick_pct']), 2),
                'close_pos': round(float(row['close_pos']), 2),
            },
            'reason': reason,
        })
    return out


def make_reason(sig, row, trend_30, scores):
    if sig == 'BC':
        return (f"上涨末端 (trend_30 {trend_30:+.1f}%, {trend_position(trend_30)}), "
                f"{row['vol_ratio']:.2f}× 均量, {row['spread_ratio']:.2f}× 均幅, "
                f"上影线 {row['upper_wick_pct']:.0%} of K 线, "
                f"收盘在 K 下 {row['close_pos']:.0%} 位置 → 高位放量冲高回落,疑似派发")
    if sig == 'SC':
        return (f"下跌末端 (trend_30 {trend_30:+.1f}%, {trend_position(trend_30)}), "
                f"{row['vol_ratio']:.2f}× 均量, {row['spread_ratio']:.2f}× 均幅, "
                f"下影线 {row['lower_wick_pct']:.0%} of K 线, "
                f"收盘在 K 上 {row['close_pos']:.0%} 位置 → 低位放量打底拉回,疑似 capitulation")
    if sig == 'NO_DEMAND':
        return (f"{trend_position(trend_30)} 中 (trend_30 {trend_30:+.1f}%), "
                f"小阳线 + 窄幅 ({row['spread_ratio']:.2f}× 均幅) + 低量 ({row['vol_ratio']:.2f}× 均量) "
                f"+ 量比前两根都低 → 上涨缺少买盘,偏弱")
    if sig == 'NO_SUPPLY':
        return (f"{trend_position(trend_30)} 中 (trend_30 {trend_30:+.1f}%), "
                f"小阴线 + 窄幅 ({row['spread_ratio']:.2f}× 均幅) + 低量 ({row['vol_ratio']:.2f}× 均量) "
                f"+ 量比前两根都低 → 下跌缺少卖压,偏强")
    if sig == 'UPTHRUST':
        return (f"突破前 30 日高 {row['high_30']:.2f} (实际触及 {row['high']:.2f}) 但收盘 {row['close']:.2f} 回落到 K 下 {row['close_pos']:.0%}, "
                f"上影线 {row['upper_wick_pct']:.0%}, "
                f"trend_30 {trend_30:+.1f}% → 假突破,诱多失败")
    if sig == 'TEST':
        return (f"低量 ({row['vol_ratio']:.2f}× 均量) 下探 + 长下影 ({row['lower_wick_pct']:.0%}) "
                f"+ 收盘 K 上 {row['close_pos']:.0%}, trend_30 {trend_30:+.1f}% → 测试低位无卖压,偏强")
    return f"({sig})"


def main():
    code = '300033'
    conn = sqlite3.connect('/Users/evilkylin/Projects/AlphaAgents/data/market_history.db')
    rows = list(conn.execute(
        'SELECT date, open, high, low, close, volume FROM daily_kline WHERE code=? ORDER BY date',
        (code,)
    ))
    df = pd.DataFrame(rows, columns=['date', 'open', 'high', 'low', 'close', 'volume'])
    df = add_features(df)
    print(f'{code}: {len(df)} bars  {df.iloc[0]["date"]} → {df.iloc[-1]["date"]}')

    all_labels = []
    for i in range(len(df)):
        all_labels.extend(label_bar_v4(df, i))

    # Stats
    print(f'\nTotal directional labels: {len(all_labels)}')
    by_sig_status = defaultdict(int)
    for l in all_labels:
        by_sig_status[(l['signal'], l['status'])] += 1
    print(f'\n=== 信号 × 状态 ===')
    print(f'{"Signal":<12} {"CONFIRMED":>10} {"FAILED":>8} {"PENDING":>8} {"avg conf":>10}')
    for sig in ('BC', 'SC', 'NO_DEMAND', 'NO_SUPPLY', 'UPTHRUST', 'TEST'):
        c = by_sig_status[(sig, 'CONFIRMED')]
        f_ = by_sig_status[(sig, 'FAILED')]
        p = by_sig_status[(sig, 'PENDING')]
        confs = [l['confidence'] for l in all_labels if l['signal'] == sig]
        avg_conf = np.mean(confs) if confs else 0
        print(f'{sig:<12} {c:>10} {f_:>8} {p:>8} {avg_conf:>10.3f}')

    # Average confirm_days
    cd_by_sig = defaultdict(list)
    for l in all_labels:
        if l['status'] == 'CONFIRMED':
            cd_by_sig[l['signal']].append(l['confirm_days'])
    print(f'\n=== CONFIRMED 平均确认天数 (event-driven, max {LOOKAHEAD_MAX}) ===')
    for sig, days in cd_by_sig.items():
        print(f'  {sig}: avg {np.mean(days):.2f} days, n={len(days)}')

    # Show 2 examples per signal type with full reason
    print(f'\n=== 每个信号 2 个 CONFIRMED 高 confidence 例子 ===')
    for sig in ('BC', 'SC', 'NO_DEMAND', 'NO_SUPPLY', 'UPTHRUST', 'TEST'):
        examples = [l for l in all_labels if l['signal'] == sig and l['status'] == 'CONFIRMED']
        examples.sort(key=lambda x: -x['confidence'])
        if not examples: continue
        print(f'\n  --- {sig} ---')
        for ex in examples[:2]:
            print(f'  {ex["date"]} conf={ex["confidence"]:.2f} {ex["status"]} ({ex["confirm_days"]}d)')
            print(f'    context: trend={ex["context"]["trend_30_pct"]:+.1f}% [{ex["context"]["trend_position"]}], '
                  f'near_resist={ex["context"]["near_resistance"]}, near_support={ex["context"]["near_support"]}')
            print(f'    reason: {ex["reason"]}')

    # Save
    out_jsonl = Path('/Users/evilkylin/Projects/AlphaAgents/data') / f'vpa_v4_{code}.jsonl'
    with open(out_jsonl, 'w') as f:
        for l in all_labels:
            f.write(json.dumps(l, ensure_ascii=False) + '\n')
    print(f'\n[saved] {out_jsonl}')


if __name__ == '__main__':
    main()
