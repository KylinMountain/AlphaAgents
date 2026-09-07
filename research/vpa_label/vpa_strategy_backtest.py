"""VPA CONFIRMED-signal 策略回测 (2026-01 → 2026-04).

策略:
- 每日扫描 1388 创业板股
- BUY 触发条件: 任一 BUY 信号 (SC / STOPPING / NO_SUPPLY / EFFORT_DOWN) 在 d 日 CONFIRMED
  即 d-3 日的 POTENTIAL 被 d-2/d-1/d 三天确认
  实际买入价 = d 日收盘
- SELL 触发条件: 持仓中,任一 SELL 信号 (NO_DEMAND / TOPPING_OUT / BC / UPTHRUST) 在 d 日 CONFIRMED
  实际卖出价 = d 日收盘
- 同时持仓上限 = 5 (等权)
- 强制平仓: 持仓 30 天
- 交易费: 单边 5bp, 双边 10bp
"""
import sqlite3, json, sys
from collections import defaultdict
import numpy as np
import pandas as pd

sys.path.insert(0, '/Users/evilkylin/Projects/AlphaAgents/scripts')
from vpa_label_v2 import add_vpa_features, label_bar, confirm_label, LOOKAHEAD


# Strategy hyperparameters
BUY_SIGNALS = {'EFFORT_NO_RESULT_DOWN', 'POTENTIAL_SC', 'STOPPING_VOLUME', 'NO_SUPPLY'}
SELL_SIGNALS = {'NO_DEMAND', 'TOPPING_OUT_VOL', 'POTENTIAL_BC', 'UPTHRUST'}
SIGNAL_PRIORITY = {  # 用于同日多候选排序,数字越小优先级越高
    'EFFORT_NO_RESULT_DOWN': 1,
    'POTENTIAL_SC': 2,
    'STOPPING_VOLUME': 3,
    'NO_SUPPLY': 4,
}
MAX_POSITIONS = 5
MAX_HOLD_DAYS = 30
TX_COST = 0.0005      # 5bp single side
EVAL_START = '2026-01-01'
EVAL_END = '2026-04-30'
DATA_START = '2025-09-01'  # need ~60 days history


def precompute_signals(by_code):
    """For each stock, compute all CONFIRMED signals + their execution dates (idx+3).

    Returns:
        signals_by_code: dict[code] → list of {idx, date, exec_idx, exec_date, exec_close, signal}
    """
    out = {}
    for code, df in by_code.items():
        df = df.reset_index(drop=True)
        df = add_vpa_features(df)
        events = []
        for i in range(len(df) - LOOKAHEAD):
            sigs = label_bar(df, i)
            for sig in sigs:
                if sig not in BUY_SIGNALS and sig not in SELL_SIGNALS:
                    continue
                status = confirm_label(df, i, sig)
                if status != 'CONFIRMED':
                    continue
                exec_idx = i + LOOKAHEAD
                if exec_idx >= len(df):
                    continue
                events.append({
                    'idx': i,
                    'date': df.iloc[i]['date'],
                    'exec_idx': exec_idx,
                    'exec_date': df.iloc[exec_idx]['date'],
                    'exec_close': float(df.iloc[exec_idx]['close']),
                    'signal': sig,
                })
        out[code] = events
    return out


def build_close_lookup(by_code):
    """code → date → close (float)."""
    return {code: dict(zip(df['date'], df['close'].astype(float)))
             for code, df in by_code.items()}


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

    print('\nPre-computing signals...', flush=True)
    signals = precompute_signals(by_code)
    closes = build_close_lookup(by_code)

    # Index signals by exec_date for fast daily lookup
    buy_events_by_date = defaultdict(list)   # date → [(code, signal, exec_close), ...]
    sell_events_by_date = defaultdict(list)
    for code, events in signals.items():
        for ev in events:
            d = ev['exec_date']
            if d < EVAL_START or d > EVAL_END:
                continue
            entry = (code, ev['signal'], ev['exec_close'])
            if ev['signal'] in BUY_SIGNALS:
                buy_events_by_date[d].append(entry)
            elif ev['signal'] in SELL_SIGNALS:
                sell_events_by_date[d].append(entry)

    n_buy_events = sum(len(v) for v in buy_events_by_date.values())
    n_sell_events = sum(len(v) for v in sell_events_by_date.values())
    print(f'  {n_buy_events} BUY events, {n_sell_events} SELL events in eval window')

    # All trading dates in eval window
    all_dates = sorted(set(d for code_dates in closes.values() for d in code_dates
                            if EVAL_START <= d <= EVAL_END))
    print(f'  {len(all_dates)} trading days {all_dates[0]} → {all_dates[-1]}')

    # Strategy simulation
    print('\n=== Simulation ===', flush=True)
    positions = {}  # code → {entry_date, entry_close, entry_signal, entry_di}
    trades = []
    daily_value = []
    cash_w_per_slot = 1.0 / MAX_POSITIONS

    for di, date in enumerate(all_dates):
        # 1. SELL signals on existing positions
        sell_candidates = sell_events_by_date.get(date, [])
        sell_codes_today = {c for c, _, _ in sell_candidates}
        to_close = []
        for code, pos in list(positions.items()):
            should_close = False
            close_reason = None
            close_price = closes[code].get(date, pos['entry_close'])
            if code in sell_codes_today:
                # Pick the matching sell signal
                signal = next(s for c, s, _ in sell_candidates if c == code)
                close_reason = signal
                should_close = True
            elif di - pos['entry_di'] >= MAX_HOLD_DAYS:
                close_reason = 'MAX_HOLD'
                should_close = True
            if should_close:
                pct = (close_price - pos['entry_close']) / pos['entry_close'] * 100
                pct -= TX_COST * 100 * 2  # round-trip cost (already paid entry; but apply both for symmetry)
                trades.append({
                    'code': code,
                    'entry_date': pos['entry_date'],
                    'exit_date': date,
                    'hold_days': di - pos['entry_di'],
                    'entry_close': pos['entry_close'],
                    'exit_close': close_price,
                    'realized_pct': pct,
                    'entry_signal': pos['entry_signal'],
                    'exit_signal': close_reason,
                })
                to_close.append(code)
        for c in to_close:
            del positions[c]

        # 2. BUY new positions
        slots = MAX_POSITIONS - len(positions)
        if slots > 0:
            buy_candidates = buy_events_by_date.get(date, [])
            held = set(positions.keys())
            cands = [(c, s, p) for c, s, p in buy_candidates if c not in held]
            # 排序: 信号优先级 + 股代码
            cands.sort(key=lambda x: (SIGNAL_PRIORITY.get(x[1], 99), x[0]))
            for code, signal, price in cands[:slots]:
                positions[code] = {
                    'entry_date': date,
                    'entry_di': di,
                    'entry_close': price,
                    'entry_signal': signal,
                }

        # 3. Mark to market
        pv = 0.0
        for code, pos in positions.items():
            cur_close = closes[code].get(date, pos['entry_close'])
            pv += cash_w_per_slot * (1 + (cur_close - pos['entry_close']) / pos['entry_close'])
        cash_w = (MAX_POSITIONS - len(positions)) / MAX_POSITIONS
        value = pv + cash_w
        daily_value.append({'date': date, 'value': value, 'positions': len(positions)})

    # 强制平仓
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
            'exit_signal': 'EOD_FORCED',
        })

    final_value = daily_value[-1]['value'] if daily_value else 1.0
    total_return = (final_value - 1) * 100

    # Buy-hold baseline
    bh_returns = []
    for code, code_closes in closes.items():
        eval_dates = sorted(d for d in code_closes if EVAL_START <= d <= EVAL_END)
        if len(eval_dates) < 2:
            continue
        c0 = code_closes[eval_dates[0]]
        c1 = code_closes[eval_dates[-1]]
        if c0 > 0:
            bh_returns.append((c1 - c0) / c0 * 100)
    bh_avg = np.mean(bh_returns) if bh_returns else 0

    print(f'\n{"=" * 60}')
    print(f'VPA CONFIRMED-signal Top-{MAX_POSITIONS} strategy')
    print(f'  Total return:        {total_return:+.2f}%')
    print(f'  创业板 buy-hold avg: {bh_avg:+.2f}%')
    print(f'  Alpha:               {total_return - bh_avg:+.2f}pp')
    print()
    print(f'  Total trades: {len(trades)}')
    if trades:
        wins = sum(1 for t in trades if t['realized_pct'] > 0)
        print(f'  Win rate: {100 * wins / len(trades):.0f}%')
        print(f'  Avg P&L per trade: {np.mean([t["realized_pct"] for t in trades]):+.2f}%')
        print(f'  Avg hold: {np.mean([t["hold_days"] for t in trades]):.1f} days')

        # Per signal type
        print(f'\n  By BUY signal:')
        sigs_pnl = defaultdict(list)
        for t in trades:
            sigs_pnl[t['entry_signal']].append(t['realized_pct'])
        for sig in sorted(sigs_pnl.keys()):
            ps = sigs_pnl[sig]
            print(f'    {sig:<28} N={len(ps):>4}  mean {np.mean(ps):+6.2f}%  win {100 * sum(1 for p in ps if p > 0) / len(ps):>3.0f}%')

        print(f'\n  By EXIT reason:')
        exit_pnl = defaultdict(list)
        for t in trades:
            exit_pnl[t['exit_signal']].append(t['realized_pct'])
        for sig in sorted(exit_pnl.keys()):
            ps = exit_pnl[sig]
            print(f'    {sig:<28} N={len(ps):>4}  mean {np.mean(ps):+6.2f}%')

        # Best/worst
        s = sorted(trades, key=lambda x: -x['realized_pct'])
        print(f'\n  Best 5 trades:')
        for t in s[:5]:
            print(f'    {t["code"]} {t["entry_date"]}→{t["exit_date"]} ({t["hold_days"]}d) {t["realized_pct"]:+6.2f}% [{t["entry_signal"]}→{t["exit_signal"]}]')
        print(f'  Worst 5 trades:')
        for t in s[-5:]:
            print(f'    {t["code"]} {t["entry_date"]}→{t["exit_date"]} ({t["hold_days"]}d) {t["realized_pct"]:+6.2f}% [{t["entry_signal"]}→{t["exit_signal"]}]')

    # Save
    with open('/tmp/vpa_strategy_trades.jsonl', 'w') as g:
        for t in trades:
            g.write(json.dumps(t, ensure_ascii=False) + '\n')
    with open('/tmp/vpa_strategy_daily.jsonl', 'w') as g:
        for d in daily_value:
            g.write(json.dumps(d, ensure_ascii=False) + '\n')
    print(f'\n[saved] /tmp/vpa_strategy_trades.jsonl ({len(trades)} trades)')


if __name__ == '__main__':
    main()
