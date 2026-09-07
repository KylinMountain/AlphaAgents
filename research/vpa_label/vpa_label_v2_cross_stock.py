"""跨股泛化测试 - 在 1388 创业板股上跑 VPA v2 规则,看 CONFIRMED 命中率是否稳定."""
import sqlite3, json, sys
from collections import defaultdict
import numpy as np
import pandas as pd

sys.path.insert(0, '/Users/evilkylin/Projects/AlphaAgents/scripts')
from vpa_label_v2 import add_vpa_features, label_bar, confirm_label, forward_return, LOOKAHEAD


DIRECTIONAL = {'POTENTIAL_BC', 'POTENTIAL_SC', 'TOPPING_OUT_VOL', 'STOPPING_VOLUME',
                'NO_DEMAND', 'NO_SUPPLY', 'TEST', 'UPTHRUST',
                'EFFORT_NO_RESULT_UP', 'EFFORT_NO_RESULT_DOWN'}
BEARISH = {'POTENTIAL_BC', 'TOPPING_OUT_VOL', 'UPTHRUST', 'NO_DEMAND', 'EFFORT_NO_RESULT_UP'}


def label_stock(df_stock):
    """Apply v2 labeling to single-stock dataframe."""
    df = add_vpa_features(df_stock)
    out = []
    for i in range(len(df)):
        sigs = label_bar(df, i)
        if not sigs:
            continue
        for sig in sigs:
            if sig not in DIRECTIONAL:
                continue
            entry = {'idx': i, 'date': df.iloc[i]['date'], 'signal': sig}
            entry['status'] = confirm_label(df, i, sig)
            entry['ret_5d'] = forward_return(df, i, 5)
            entry['ret_10d'] = forward_return(df, i, 10)
            entry['ret_20d'] = forward_return(df, i, 20)
            entry['close'] = float(df.iloc[i]['close'])
            out.append(entry)
    return out


def main():
    conn = sqlite3.connect('/Users/evilkylin/Projects/AlphaAgents/data/market_history.db')
    print('Loading all cyb...', flush=True)
    df_all = pd.read_sql_query(
        "SELECT code, date, open, high, low, close, volume FROM daily_kline "
        "WHERE code LIKE '300%' OR code LIKE '301%' OR code LIKE '302%' "
        "ORDER BY code, date",
        conn
    )
    print(f'  {len(df_all):,} rows, {df_all.code.nunique()} codes')

    all_labels = []
    per_stock_counts = defaultdict(lambda: defaultdict(int))  # code → signal → count

    by_code = df_all.groupby('code')
    n_codes = len(by_code)
    for ci, (code, group) in enumerate(by_code):
        if len(group) < 60:
            continue
        df_stock = group.reset_index(drop=True)
        labels = label_stock(df_stock)
        for l in labels:
            l['code'] = code
            per_stock_counts[code][f'{l["signal"]}_{l["status"]}'] += 1
        all_labels.extend(labels)
        if (ci + 1) % 200 == 0:
            print(f'  {ci+1}/{n_codes} ...', flush=True)

    print(f'\nTotal labels: {len(all_labels)}')

    # Aggregate stats per (signal, status)
    by_pair = defaultdict(list)
    for l in all_labels:
        if l['ret_10d'] is None:
            continue
        by_pair[(l['signal'], l['status'])].append(l)

    print(f'\n=== 跨股泛化: signal × status ===')
    print(f'{"Signal":<24} {"Status":<10} {"+5d":>8} {"+10d":>9} {"+20d":>9} {"hit%":>6} {"N":>7} {"% confirmed":>12}')
    rows = []
    for sig in sorted(DIRECTIONAL):
        bear = sig in BEARISH
        # Count totals for "% confirmed"
        all_for_sig = [l for l in all_labels if l['signal'] == sig and l['status'] in ('CONFIRMED', 'FAILED')]
        n_all = len(all_for_sig)
        for status in ['CONFIRMED', 'FAILED']:
            sub = by_pair.get((sig, status), [])
            if not sub:
                continue
            r5 = np.mean([l['ret_5d'] for l in sub if l['ret_5d'] is not None])
            r10 = np.mean([l['ret_10d'] for l in sub])
            r20 = np.mean([l['ret_20d'] for l in sub if l['ret_20d'] is not None])
            hit = sum(1 for l in sub if (l['ret_10d'] < 0) == bear) / len(sub) * 100
            pct_conf = (len(sub) / n_all * 100) if n_all > 0 else 0
            print(f'{sig:<24} {status:<10} {r5:>+7.2f}% {r10:>+8.2f}% {r20:>+8.2f}% {hit:>5.1f}% {len(sub):>7,} {pct_conf:>11.1f}%')
            rows.append({'signal': sig, 'status': status, 'r5': r5, 'r10': r10, 'r20': r20,
                          'hit_pct': hit, 'n': len(sub), 'pct_confirmed': pct_conf})

    # Per-stock distribution: how concentrated are signals?
    print(f'\n=== Per-stock 信号分布检查 (是否集中在少数股) ===')
    for sig in ['NO_DEMAND', 'NO_SUPPLY', 'POTENTIAL_BC', 'UPTHRUST', 'TOPPING_OUT_VOL']:
        key = f'{sig}_CONFIRMED'
        counts = [c[key] for c in per_stock_counts.values() if c[key] > 0]
        if not counts:
            continue
        print(f'  {sig}_CONFIRMED: {len(counts):>4} stocks have it, mean {np.mean(counts):.1f}/stock, max {max(counts)}/stock, total {sum(counts):,}')

    # Save
    out = '/Users/evilkylin/Projects/AlphaAgents/data/vpa_label_v2_cyb.jsonl'
    with open(out, 'w') as f:
        for l in all_labels:
            f.write(json.dumps(l, ensure_ascii=False) + '\n')
    print(f'\n[saved] {out}')

    # Save aggregated stats
    stats_out = '/Users/evilkylin/Projects/AlphaAgents/data/vpa_v2_cross_stock_stats.json'
    with open(stats_out, 'w') as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    print(f'[saved] {stats_out}')


if __name__ == '__main__':
    main()
