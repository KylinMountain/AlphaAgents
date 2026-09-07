"""VPA v4 conf-filtered strategy backtest (2026-01 → 2026-04).

策略 (基于 v4 rich labeler):
- 每股先用 v4 跑一遍标注 (得到所有 confirmed 信号 + confidence)
- 入场: SC (conf>=0.3) 或 TEST (conf>=0.5) 一旦 CONFIRMED
- 出场: BC/UPTHRUST (conf>=0.5) CONFIRMED, OR 10 天硬退, OR -5% 止损
- 同时持仓: 5 只 (按 confidence 排序选)
- 交易费: 单边 5bp
"""
import sqlite3, json, sys
from collections import defaultdict
import numpy as np
import pandas as pd

sys.path.insert(0, '/Users/evilkylin/Projects/AlphaAgents/scripts')
from vpa_label_v4 import add_features, label_bar_v4


# Strategy params
BUY_THRESHOLDS = {
    'SC': 0.3,        # SC 平均 conf 偏低,放宽 threshold
    'TEST': 0.5,      # TEST 较强,正常 threshold
}
SELL_THRESHOLDS = {
    'BC': 0.5,
    'UPTHRUST': 0.5,
}
MAX_POSITIONS = 5
MAX_HOLD_DAYS = 10            # 硬退出 (匹配 +10d 验证窗口)
STOP_LOSS_PCT = -5.0          # 止损 -5%
TX_COST = 0.0005              # 5bp 单边
EVAL_START = '2026-01-01'
EVAL_END = '2026-04-30'
DATA_START = '2025-09-01'


def label_stock_v4(df_stock):
    """Apply v4 labeling, return ALL labels with exec_idx (= idx + confirm_days)."""
    df = add_features(df_stock.reset_index(drop=True))
    out = []
    for i in range(len(df)):
        labels = label_bar_v4(df, i)
        for l in labels:
            if l['status'] != 'CONFIRMED':
                continue
            exec_idx = l['idx'] + l['confirm_days']
            if exec_idx >= len(df):
                continue
            l['exec_idx'] = exec_idx
            l['exec_date'] = df.iloc[exec_idx]['date']
            l['exec_close'] = float(df.iloc[exec_idx]['close'])
            out.append(l)
    return out


def main():
    conn = sqlite3.connect('/Users/evilkylin/Projects/AlphaAgents/data/market_history.db')
    print('Loading...', flush=True)
    df_all = pd.read_sql_query(
        f"SELECT code, date, open, high, low, close, volume FROM daily_kline "
        f"WHERE (code LIKE '300%' OR code LIKE '301%' OR code LIKE '302%') "
        f"AND date >= '{DATA_START}' AND date <= '{EVAL_END}' "
        f"ORDER BY code, date",
        conn
    )
    print(f'  {len(df_all):,} rows, {df_all.code.nunique()} codes')

    by_code = {c: g for c, g in df_all.groupby('code') if len(g) >= 60}
    print(f'  {len(by_code)} stocks with sufficient history')

    # Pre-compute all v4 confirmed labels per code
    print('\nComputing v4 confirmed signals (event-driven confirmation)...', flush=True)
    labels_per_code = {}
    for ci, (code, df) in enumerate(by_code.items()):
        labels_per_code[code] = label_stock_v4(df)
        if (ci + 1) % 200 == 0:
            print(f'  {ci+1}/{len(by_code)} ...', flush=True)

    # Closes lookup
    closes = {code: dict(zip(df['date'], df['close'].astype(float)))
               for code, df in by_code.items()}

    # Index by exec_date, only signals passing conf threshold
    buy_events = defaultdict(list)   # date → [(code, signal, conf, exec_close)]
    sell_events = defaultdict(list)
    for code, labs in labels_per_code.items():
        for l in labs:
            sig = l['signal']
            d = l['exec_date']
            if d < EVAL_START or d > EVAL_END:
                continue
            if sig in BUY_THRESHOLDS and l['confidence'] >= BUY_THRESHOLDS[sig]:
                buy_events[d].append((code, sig, l['confidence'], l['exec_close']))
            elif sig in SELL_THRESHOLDS and l['confidence'] >= SELL_THRESHOLDS[sig]:
                sell_events[d].append((code, sig, l['confidence'], l['exec_close']))

    n_buys = sum(len(v) for v in buy_events.values())
    n_sells = sum(len(v) for v in sell_events.values())
    print(f'\n  Filtered BUY events: {n_buys}, SELL events: {n_sells} in eval window')

    # Eval dates
    all_dates = sorted({d for code_dates in closes.values() for d in code_dates
                         if EVAL_START <= d <= EVAL_END})
    print(f'  {len(all_dates)} trading days {all_dates[0]} → {all_dates[-1]}')

    # Strategy simulation
    print('\n=== Simulation ===', flush=True)
    positions = {}
    trades = []
    daily_value = []
    cash_w_per_slot = 1.0 / MAX_POSITIONS

    for di, date in enumerate(all_dates):
        # 1. Check exits on existing positions
        sell_codes = {c: (s, conf) for c, s, conf, _ in sell_events.get(date, [])}
        to_close = []
        for code, pos in list(positions.items()):
            close_price = closes[code].get(date, pos['entry_close'])
            cur_pct = (close_price - pos['entry_close']) / pos['entry_close'] * 100
            close_reason = None

            if code in sell_codes:
                sig, conf = sell_codes[code]
                close_reason = f'{sig}_conf{conf:.2f}'
            elif cur_pct <= STOP_LOSS_PCT:
                close_reason = 'STOP_LOSS'
            elif di - pos['entry_di'] >= MAX_HOLD_DAYS:
                close_reason = 'MAX_HOLD'

            if close_reason:
                pct = cur_pct - TX_COST * 100 * 2
                trades.append({
                    'code': code,
                    'entry_date': pos['entry_date'],
                    'exit_date': date,
                    'hold_days': di - pos['entry_di'],
                    'entry_close': pos['entry_close'],
                    'exit_close': close_price,
                    'realized_pct': pct,
                    'entry_signal': pos['entry_signal'],
                    'entry_conf': pos['entry_conf'],
                    'exit_signal': close_reason,
                })
                to_close.append(code)
        for c in to_close:
            del positions[c]

        # 2. New buys
        slots = MAX_POSITIONS - len(positions)
        if slots > 0:
            held = set(positions.keys())
            cands = [(c, s, conf, p) for c, s, conf, p in buy_events.get(date, [])
                       if c not in held]
            # 排序: confidence 降序 (用最强信号优先)
            cands.sort(key=lambda x: -x[2])
            for code, sig, conf, price in cands[:slots]:
                positions[code] = {
                    'entry_date': date,
                    'entry_di': di,
                    'entry_close': price,
                    'entry_signal': sig,
                    'entry_conf': conf,
                }

        # Mark to market
        pv = 0.0
        for code, pos in positions.items():
            cur = closes[code].get(date, pos['entry_close'])
            pv += cash_w_per_slot * (1 + (cur - pos['entry_close']) / pos['entry_close'])
        cash_w = (MAX_POSITIONS - len(positions)) / MAX_POSITIONS
        daily_value.append({'date': date, 'value': pv + cash_w, 'positions': len(positions)})

    # Force close
    last_date = all_dates[-1]
    for code, pos in list(positions.items()):
        ec = closes[code].get(last_date, pos['entry_close'])
        pct = (ec - pos['entry_close']) / pos['entry_close'] * 100 - TX_COST * 100 * 2
        trades.append({
            'code': code,
            'entry_date': pos['entry_date'],
            'exit_date': last_date,
            'hold_days': len(all_dates) - 1 - pos['entry_di'],
            'entry_close': pos['entry_close'],
            'exit_close': ec,
            'realized_pct': pct,
            'entry_signal': pos['entry_signal'],
            'entry_conf': pos['entry_conf'],
            'exit_signal': 'EOD_FORCED',
        })

    final_value = daily_value[-1]['value']
    total_return = (final_value - 1) * 100

    # cyb buy-hold
    bh_returns = []
    for code, code_closes in closes.items():
        ed = sorted(d for d in code_closes if EVAL_START <= d <= EVAL_END)
        if len(ed) < 2: continue
        c0, c1 = code_closes[ed[0]], code_closes[ed[-1]]
        if c0 > 0:
            bh_returns.append((c1 - c0) / c0 * 100)
    bh_avg = np.mean(bh_returns) if bh_returns else 0

    print(f'\n{"=" * 60}')
    print(f'VPA v4 conf-filtered Top-{MAX_POSITIONS} strategy')
    print(f'  Total return:        {total_return:+.2f}%')
    print(f'  cyb buy-hold avg:    {bh_avg:+.2f}%')
    print(f'  Alpha:               {total_return - bh_avg:+.2f}pp')
    print()
    print(f'  Total trades: {len(trades)}')
    if trades:
        wins = sum(1 for t in trades if t['realized_pct'] > 0)
        print(f'  Win rate: {100 * wins / len(trades):.0f}%')
        print(f'  Avg P&L per trade: {np.mean([t["realized_pct"] for t in trades]):+.2f}%')
        print(f'  Avg hold: {np.mean([t["hold_days"] for t in trades]):.1f} days')

        sigs_pnl = defaultdict(list)
        for t in trades:
            sigs_pnl[t['entry_signal']].append(t['realized_pct'])
        print(f'\n  By BUY signal:')
        for sig in sorted(sigs_pnl.keys()):
            ps = sigs_pnl[sig]
            print(f'    {sig:<28} N={len(ps):>4}  mean {np.mean(ps):+6.2f}%  win {100 * sum(1 for p in ps if p > 0) / len(ps):>3.0f}%')

        exit_pnl = defaultdict(list)
        for t in trades:
            exit_pnl[t['exit_signal']].append(t['realized_pct'])
        print(f'\n  By EXIT reason:')
        for sig in sorted(exit_pnl.keys()):
            ps = exit_pnl[sig]
            print(f'    {sig:<28} N={len(ps):>4}  mean {np.mean(ps):+6.2f}%')

        s = sorted(trades, key=lambda x: -x['realized_pct'])
        print(f'\n  Best 5:')
        for t in s[:5]:
            print(f'    {t["code"]} {t["entry_date"]}→{t["exit_date"]} ({t["hold_days"]}d) {t["realized_pct"]:+6.2f}% [{t["entry_signal"]} conf{t["entry_conf"]:.2f}→{t["exit_signal"]}]')
        print(f'  Worst 5:')
        for t in s[-5:]:
            print(f'    {t["code"]} {t["entry_date"]}→{t["exit_date"]} ({t["hold_days"]}d) {t["realized_pct"]:+6.2f}% [{t["entry_signal"]} conf{t["entry_conf"]:.2f}→{t["exit_signal"]}]')

    with open('/tmp/vpa_v4_trades.jsonl', 'w') as g:
        for t in trades:
            g.write(json.dumps(t, ensure_ascii=False) + '\n')
    with open('/tmp/vpa_v4_daily.jsonl', 'w') as g:
        for d in daily_value:
            g.write(json.dumps(d, ensure_ascii=False) + '\n')
    print(f'\n[saved] /tmp/vpa_v4_trades.jsonl ({len(trades)} trades)')


if __name__ == '__main__':
    main()
