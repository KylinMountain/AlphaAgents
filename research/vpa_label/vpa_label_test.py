"""规则化 VPA 信号标注 + 可复制性测试.

目的: 在 1 只股票 (300033) 上, 用严格规则标注 6 个 Anna VPA 信号:
  BC (Buying Climax): 上升趋势末高量宽幅阴线
  SC (Selling Climax): 下跌趋势末高量宽幅阳线/下影线长
  SOS (Sign of Strength): 高量宽阳突破
  SOW (Sign of Weakness): 高量宽阴破位
  Spring: 跌破近期低点后收回 (空头陷阱)
  UTAD (Up-Thrust After Distribution): 突破近期高点后收回 (多头陷阱)

输出:
1. data/vpa_label_300033.jsonl - 每个标注 bar 的详细信息
2. data/charts/300033_vpa_labels.png - K 线图 + 标签
3. 控制台: 信号触发频率 + 前瞻收益验证 (BC 应该接跌, SC 应该接涨...)
"""
import sqlite3, json
from collections import defaultdict
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
from pathlib import Path

# CJK font
_avail = {f.name for f in fm.fontManager.ttflist}
for cjk in ["PingFang HK", "PingFang SC", "Heiti SC", "STHeiti", "Arial Unicode MS"]:
    if cjk in _avail:
        plt.rcParams["font.sans-serif"] = [cjk]
        plt.rcParams["font.family"] = "sans-serif"
        break
plt.rcParams["axes.unicode_minus"] = False


CODE = '300033'
LOOKBACK_VOL = 60     # vol percentile window
LOOKBACK_TREND = 20   # trend slope window
LOOKBACK_SR = 30      # support/resistance window
LOOKBACK_ATR = 20     # ATR window
FORWARD_DAYS = [5, 10, 20]  # forward return windows for validation


def compute_features(rows):
    """rows: [(date, open, high, low, close, volume)]; returns list of feature dicts."""
    n = len(rows)
    feats = [{} for _ in range(n)]
    for i in range(n):
        date, op, hi, lo, cl, vol = rows[i]
        feats[i].update(date=date, open=op, high=hi, low=lo, close=cl, volume=vol)
        if i < LOOKBACK_VOL:
            continue
        # Volume window
        vols_60 = np.array([r[5] for r in rows[i - LOOKBACK_VOL:i]])
        feats[i]['vol_p90'] = np.percentile(vols_60, 90)
        feats[i]['vol_p50'] = np.percentile(vols_60, 50)
        feats[i]['vol_p10'] = np.percentile(vols_60, 10)
        # ATR (true range avg over LOOKBACK_ATR)
        ranges = np.array([rows[j][2] - rows[j][3] for j in range(i - LOOKBACK_ATR, i)])
        feats[i]['atr_20'] = np.mean(ranges)
        # Trend (close at i vs LOOKBACK_TREND days ago)
        prev_close = rows[i - LOOKBACK_TREND][4]
        feats[i]['trend_pct'] = (cl - prev_close) / max(prev_close, 1e-6) * 100
        # Bar shape
        rng = max(hi - lo, 1e-6)
        feats[i]['close_pos'] = (cl - lo) / rng
        feats[i]['range_atr'] = rng / max(feats[i]['atr_20'], 1e-6)
        # Support / resistance
        h30 = rows[i - LOOKBACK_SR:i]
        feats[i]['low_30'] = min(r[3] for r in h30)
        feats[i]['high_30'] = max(r[2] for r in h30)
    return feats


def label_signals(feats):
    """Apply rules; return list of labels {idx, date, signal, ...details}."""
    labels = []
    for i, f in enumerate(feats):
        if 'vol_p90' not in f:
            continue
        vol = f['volume']
        cl = f['close']
        op = f['open']
        is_high_vol = vol > f['vol_p90']
        is_wide = f['range_atr'] > 1.5
        is_med_vol = vol > f['vol_p50'] * 1.3

        # BC: 上升趋势 (>10% in 20d) + 高量 + 宽幅 + 收盘下半部
        if (f['trend_pct'] > 10 and is_high_vol and is_wide and f['close_pos'] < 0.4):
            labels.append({**f, 'idx': i, 'signal': 'BC'})

        # SC: 下跌趋势 (<-10% in 20d) + 高量 + 宽幅 + 收盘上半部 (长下影)
        if (f['trend_pct'] < -10 and is_high_vol and is_wide and f['close_pos'] > 0.6):
            labels.append({**f, 'idx': i, 'signal': 'SC'})

        # SOS: 高量 + 宽阳 + 收盘上方 + 突破 30 日高点
        if (is_high_vol and is_wide and cl > op and f['close_pos'] > 0.7
                and cl > f['high_30'] * 0.99):
            labels.append({**f, 'idx': i, 'signal': 'SOS'})

        # SOW: 高量 + 宽阴 + 收盘下方 + 跌破 30 日低点
        if (is_high_vol and is_wide and cl < op and f['close_pos'] < 0.3
                and cl < f['low_30'] * 1.01):
            labels.append({**f, 'idx': i, 'signal': 'SOW'})

        # Spring: 跌破 30 日低点 + 收回 (空头陷阱)
        if (f['low'] < f['low_30'] * 0.99 and cl > f['low_30'] * 0.995
                and is_med_vol):
            labels.append({**f, 'idx': i, 'signal': 'Spring'})

        # UTAD: 突破 30 日高点 + 收回 (多头陷阱)
        if (f['high'] > f['high_30'] * 1.01 and cl < f['high_30'] * 1.005
                and is_med_vol):
            labels.append({**f, 'idx': i, 'signal': 'UTAD'})
    return labels


def forward_returns(rows, idx, days):
    """Return percent change from rows[idx].close to rows[idx+days].close."""
    if idx + days >= len(rows):
        return None
    c0 = rows[idx][4]
    c1 = rows[idx + days][4]
    return (c1 - c0) / c0 * 100


def plot_chart(rows, labels, out_path):
    """K-line chart with VPA labels overlaid."""
    n = len(rows)
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(20, 9), sharex=True,
                                    gridspec_kw={'height_ratios': [3, 1]})

    # K-line
    for i, r in enumerate(rows):
        d, op, hi, lo, cl, vol = r
        color = '#d62728' if cl >= op else '#2ca02c'
        ax1.plot([i, i], [lo, hi], color=color, linewidth=0.5, alpha=0.7)
        body_lo, body_hi = min(op, cl), max(op, cl)
        ax1.bar(i, body_hi - body_lo, bottom=body_lo, color=color, width=0.7,
                alpha=0.75, edgecolor='black', linewidth=0.2)

    # Volume bars
    for i, r in enumerate(rows):
        cl, op, vol = r[4], r[1], r[5]
        color = '#d62728' if cl >= op else '#2ca02c'
        ax2.bar(i, vol, color=color, width=0.7, alpha=0.7)

    # Labels (different markers + colors per signal)
    sig_styles = {
        'BC':     ('v', '#8B0000', 'BC (顶)'),
        'SC':     ('^', '#006400', 'SC (底)'),
        'SOS':    ('^', '#1f77b4', 'SOS (强)'),
        'SOW':    ('v', '#9467bd', 'SOW (弱)'),
        'Spring': ('*', '#FF8C00', 'Spring (空头陷阱)'),
        'UTAD':   ('*', '#000080', 'UTAD (多头陷阱)'),
    }
    seen = set()
    for lab in labels:
        sig = lab['signal']
        marker, color, leg = sig_styles[sig]
        i = lab['idx']
        y_offset = lab['high'] * 1.03 if sig in ('BC', 'SOW', 'UTAD') else lab['low'] * 0.97
        label_arg = leg if sig not in seen else None
        ax1.scatter(i, y_offset, marker=marker, s=120, color=color,
                    edgecolors='black', linewidths=0.8, zorder=10, label=label_arg)
        seen.add(sig)

    ax1.set_title(f'{CODE} VPA Rule-based Labels  ({rows[0][0]} → {rows[-1][0]}, {n} bars)', fontsize=13)
    ax1.set_ylabel('Price')
    ax1.legend(loc='upper left', fontsize=10, ncol=3)
    ax1.grid(True, alpha=0.3)

    ax2.set_ylabel('Volume')
    ax2.grid(True, alpha=0.3)
    # x-tick every 20 days
    tick_idx = list(range(0, n, 20))
    ax2.set_xticks(tick_idx)
    ax2.set_xticklabels([rows[i][0][2:] for i in tick_idx], rotation=45, fontsize=8)
    ax2.set_xlabel('Date')

    plt.tight_layout()
    plt.savefig(out_path, dpi=110, bbox_inches='tight')
    plt.close()


def main():
    conn = sqlite3.connect('/Users/evilkylin/Projects/AlphaAgents/data/market_history.db')
    rows = list(conn.execute(
        'SELECT date, open, high, low, close, volume FROM daily_kline WHERE code=? ORDER BY date',
        (CODE,)
    ))
    print(f'{CODE}: {len(rows)} bars  {rows[0][0]} → {rows[-1][0]}')

    feats = compute_features(rows)
    labels = label_signals(feats)
    print(f'\nTotal labels: {len(labels)}')

    by_sig = defaultdict(list)
    for lab in labels:
        by_sig[lab['signal']].append(lab)

    # Frequency table
    print('\n=== Signal frequency ===')
    print(f'{"Signal":<10} {"Count":>6}  {"Per 100 bars":>15}')
    for sig in ['BC', 'SC', 'SOS', 'SOW', 'Spring', 'UTAD']:
        n = len(by_sig.get(sig, []))
        rate = n / len(rows) * 100
        print(f'{sig:<10} {n:>6}  {rate:>14.2f}')

    # Forward return validation
    print('\n=== Predictive validation (forward returns at each label) ===')
    print(f'  Expected directions:')
    print(f'    BC, SOW, UTAD → next return should be NEGATIVE')
    print(f'    SC, SOS, Spring → next return should be POSITIVE')
    print()
    print(f'{"Signal":<10}', '  '.join(f'{f"+{d}d mean":>12}' for d in FORWARD_DAYS),
          f'  {"hit %":>8}  {"N":>4}')
    for sig in ['BC', 'SC', 'SOS', 'SOW', 'Spring', 'UTAD']:
        sig_labels = by_sig.get(sig, [])
        if not sig_labels: continue
        # Compute forward returns
        means_per_d = []
        for d in FORWARD_DAYS:
            rets = [forward_returns(rows, lab['idx'], d) for lab in sig_labels]
            rets = [r for r in rets if r is not None]
            if rets:
                means_per_d.append(np.mean(rets))
            else:
                means_per_d.append(np.nan)

        # Hit rate: fraction matching expected direction (using +10d window)
        expected_dir = -1 if sig in ('BC', 'SOW', 'UTAD') else 1
        rets_10d = [forward_returns(rows, lab['idx'], 10) for lab in sig_labels]
        rets_10d = [r for r in rets_10d if r is not None]
        if rets_10d:
            hit_rate = sum(1 for r in rets_10d if (r > 0) == (expected_dir > 0)) / len(rets_10d) * 100
        else:
            hit_rate = float('nan')

        ret_str = '  '.join(f'{m:+12.2f}%' for m in means_per_d)
        print(f'{sig:<10} {ret_str}  {hit_rate:>7.1f}%  {len(sig_labels):>4}')

    # Save
    out_jsonl = Path('/Users/evilkylin/Projects/AlphaAgents/data') / f'vpa_label_{CODE}.jsonl'
    with open(out_jsonl, 'w') as f:
        for lab in labels:
            # Strip non-serializable / large fields
            slim = {k: v for k, v in lab.items() if k in ('idx', 'date', 'signal', 'open', 'high', 'low', 'close', 'volume', 'trend_pct', 'close_pos', 'range_atr')}
            slim = {k: float(v) if isinstance(v, np.floating) else v for k, v in slim.items()}
            f.write(json.dumps(slim, ensure_ascii=False) + '\n')

    chart_dir = Path('/Users/evilkylin/Projects/AlphaAgents/data/charts')
    chart_dir.mkdir(parents=True, exist_ok=True)
    out_png = chart_dir / f'{CODE}_vpa_labels.png'
    plot_chart(rows, labels, out_png)

    print(f'\n[saved] {out_jsonl}')
    print(f'[saved] {out_png}')


if __name__ == '__main__':
    main()
