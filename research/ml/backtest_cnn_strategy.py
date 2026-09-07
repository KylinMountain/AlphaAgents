"""CNN signal-driven backtest: 2026-01-01 → 2026-04-30 创业板 Top-2 持仓.

Logic:
- 每日 CNN 推理所有创业板 stocks (~1400)
- buy_score = P(continuation) + P(bottoming) - P(topping) - P(breakdown)
- sell_signal: P(topping) > sell_threshold OR P(breakdown) > sell_threshold
- 持仓: max 2 只 (equal weight 50/50)
- 每日检查:
    1. 已持仓: 检查 sell signal
    2. 空仓位: 取 top 2 (by buy_score, score > buy_threshold)

Output: 总收益 vs 创业板大盘 buy-and-hold
"""
import argparse, pickle, sqlite3
import numpy as np
import torch
from collections import defaultdict


SEQ_LEN = 60


class VPAConv1D(torch.nn.Module):
    def __init__(self, n_channels=5, n_classes=4):
        super().__init__()
        self.conv = torch.nn.Sequential(
            torch.nn.Conv1d(n_channels, 32, kernel_size=5, padding=2),
            torch.nn.BatchNorm1d(32), torch.nn.ReLU(),
            torch.nn.Conv1d(32, 64, kernel_size=5, padding=2),
            torch.nn.BatchNorm1d(64), torch.nn.ReLU(),
            torch.nn.MaxPool1d(2),
            torch.nn.Conv1d(64, 128, kernel_size=3, padding=1),
            torch.nn.BatchNorm1d(128), torch.nn.ReLU(),
            torch.nn.Conv1d(128, 128, kernel_size=3, padding=1),
            torch.nn.BatchNorm1d(128), torch.nn.ReLU(),
            torch.nn.AdaptiveAvgPool1d(1),
        )
        self.head = torch.nn.Sequential(
            torch.nn.Linear(128, 64), torch.nn.ReLU(), torch.nn.Dropout(0.3),
            torch.nn.Linear(64, n_classes),
        )
    def forward(self, x):
        x = x.transpose(1, 2)
        z = self.conv(x).squeeze(-1)
        return self.head(z)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--data', default='/Users/evilkylin/Projects/AlphaAgents/data/vpa_seq_v2.npz')
    p.add_argument('--meta', default='/Users/evilkylin/Projects/AlphaAgents/data/vpa_seq_v2_meta.pkl')
    p.add_argument('--model', default='/Users/evilkylin/Projects/AlphaAgents/data/vpa_seq_cnn_v2.pt')
    p.add_argument('--start', default='2026-01-01')
    p.add_argument('--end', default='2026-04-30')
    p.add_argument('--max-positions', type=int, default=2)
    p.add_argument('--buy-threshold', type=float, default=0.0, help='buy_score 最低值')
    p.add_argument('--sell-topping', type=float, default=0.55)
    p.add_argument('--sell-breakdown', type=float, default=0.55)
    args = p.parse_args()

    device = torch.device('mps' if torch.backends.mps.is_available() else 'cpu')
    print(f'Device: {device}', flush=True)

    print('Loading data...', flush=True)
    data = np.load(args.data)
    X = data['X']
    with open(args.meta, 'rb') as f:
        meta = pickle.load(f)
    print(f'  {len(X)} sequences')

    # Filter to test window
    test_idx = [i for i, m in enumerate(meta) if args.start <= m['as_of'] <= args.end]
    print(f'  Test window {args.start} → {args.end}: {len(test_idx)} sequences')

    X_test = X[test_idx]
    meta_test = [meta[i] for i in test_idx]

    # Load model
    print('Loading model...', flush=True)
    model = VPAConv1D().to(device)
    model.load_state_dict(torch.load(args.model, map_location=device))
    model.eval()

    # Inference in batches
    print('Inferencing...', flush=True)
    preds = []
    batch = 1024
    with torch.no_grad():
        for i in range(0, len(X_test), batch):
            xb = torch.tensor(X_test[i:i+batch]).to(device)
            ps = torch.sigmoid(model(xb)).cpu().numpy()
            preds.append(ps)
    preds = np.concatenate(preds)  # (N, 4): topping, bottoming, continuation, breakdown
    print(f'  {preds.shape} predictions')

    # Group by date
    by_date = defaultdict(list)  # date -> [(code, pred), ...]
    for i, m in enumerate(meta_test):
        by_date[m['as_of']].append((m['code'], preds[i]))

    # Get close prices for return calc
    print('Loading close prices...', flush=True)
    conn = sqlite3.connect('/Users/evilkylin/Projects/AlphaAgents/data/market_history.db')
    closes = defaultdict(dict)  # code -> {date: close}
    for code, date, close in conn.execute('SELECT code, date, close FROM daily_kline ORDER BY code, date'):
        closes[code][date] = close

    # === Strategy simulation ===
    print('\n=== CNN Top-2 strategy simulation ===', flush=True)
    sorted_dates = sorted(by_date.keys())
    print(f'  {len(sorted_dates)} trading days from {sorted_dates[0]} to {sorted_dates[-1]}')

    # Portfolio state: positions[code] = {entry_date, entry_close}
    positions = {}
    cash = 1.0  # 单位资金
    trades = []  # log
    daily_value = []

    for di, date in enumerate(sorted_dates):
        signals = by_date[date]  # [(code, [top, bot, cont, brk])]

        # 1. Check sell signals on existing positions
        to_sell = []
        for code, pos in positions.items():
            sig = next((s for c, s in signals if c == code), None)
            if sig is None: continue
            top_p, bot_p, cont_p, brk_p = sig
            if top_p > args.sell_topping or brk_p > args.sell_breakdown:
                exit_close = closes[code].get(date)
                if exit_close is None: continue
                pct_ret = (exit_close - pos['entry_close']) / pos['entry_close']
                hold_d = di - pos['entry_di']
                trades.append({
                    'code': code, 'entry_date': pos['entry_date'], 'exit_date': date,
                    'entry_close': pos['entry_close'], 'exit_close': exit_close,
                    'realized_pct': pct_ret * 100, 'hold_days': hold_d,
                    'reason': 'topping' if top_p > args.sell_topping else 'breakdown',
                })
                to_sell.append(code)

        for code in to_sell:
            del positions[code]

        # 2. New buys (only if open positions < max)
        slots_open = args.max_positions - len(positions)
        if slots_open > 0:
            held_codes = set(positions.keys())
            buy_score = []
            for code, sig in signals:
                if code in held_codes: continue
                top_p, bot_p, cont_p, brk_p = sig
                # buy_score: high continuation + high bottoming, low topping/breakdown
                score = cont_p + bot_p - top_p - brk_p
                if score > args.buy_threshold:
                    buy_score.append((code, score, sig))
            buy_score.sort(key=lambda x: -x[1])
            new_buys = buy_score[:slots_open]
            for code, score, sig in new_buys:
                entry_close = closes[code].get(date)
                if entry_close is None: continue
                positions[code] = {
                    'entry_date': date, 'entry_di': di,
                    'entry_close': entry_close, 'entry_score': score,
                }

        # 3. Compute current portfolio value (mark-to-market)
        n_pos = len(positions)
        if n_pos == 0:
            value = cash
        else:
            # Equal weight: each position holds 1/max_positions of capital
            # If only 1 position open, half of capital is cash
            position_values = []
            weight_per = 1.0 / args.max_positions
            for code, pos in positions.items():
                cur_close = closes[code].get(date, pos['entry_close'])
                ret = (cur_close - pos['entry_close']) / pos['entry_close']
                position_values.append(weight_per * (1 + ret))
            cash_weight = (args.max_positions - n_pos) / args.max_positions
            value = sum(position_values) + cash_weight
        daily_value.append((date, value, n_pos))

    # 4. Final close at last date
    last_date = sorted_dates[-1]
    for code, pos in list(positions.items()):
        exit_close = closes[code].get(last_date, pos['entry_close'])
        pct_ret = (exit_close - pos['entry_close']) / pos['entry_close']
        trades.append({
            'code': code, 'entry_date': pos['entry_date'], 'exit_date': last_date,
            'entry_close': pos['entry_close'], 'exit_close': exit_close,
            'realized_pct': pct_ret * 100, 'hold_days': len(sorted_dates) - 1 - pos['entry_di'],
            'reason': 'end_of_window',
        })

    # Final value (assume positions liquidated at end)
    final_value = daily_value[-1][1] if daily_value else 1.0
    total_return = (final_value - 1) * 100

    # Buy-and-hold baseline: 创业板大盘平均 (避免 hardcode index)
    # Use only stocks present in data
    cyb_codes = set([m['code'] for m in meta_test])
    bh_returns = []
    for code in cyb_codes:
        first_close = next((closes[code].get(d) for d in sorted_dates if d in closes[code]), None)
        last_close = next((closes[code].get(d) for d in reversed(sorted_dates) if d in closes[code]), None)
        if first_close and last_close and first_close > 0:
            bh_returns.append((last_close - first_close) / first_close * 100)
    bh_avg = sum(bh_returns) / len(bh_returns) if bh_returns else 0

    # Print results
    print(f'\n{"=" * 60}')
    print(f'CNN strategy total return: {total_return:+.2f}%')
    print(f'创业板大盘 buy-hold avg:   {bh_avg:+.2f}%')
    print(f'Strategy alpha:            {total_return - bh_avg:+.2f}pp')
    print(f'Total trades: {len(trades)}')
    if trades:
        wins = sum(1 for t in trades if t['realized_pct'] > 0)
        print(f'Win rate: {wins}/{len(trades)} = {100*wins/len(trades):.0f}%')
        avg_pl = sum(t['realized_pct'] for t in trades) / len(trades)
        avg_hold = sum(t['hold_days'] for t in trades) / len(trades)
        print(f'Avg P&L per trade: {avg_pl:+.2f}%, avg hold: {avg_hold:.1f} days')

        # Best & worst trades
        sorted_trades = sorted(trades, key=lambda x: -x['realized_pct'])
        print(f'\nBest 5 trades:')
        for t in sorted_trades[:5]:
            print(f'  {t["code"]} {t["entry_date"]} → {t["exit_date"]} ({t["hold_days"]}d) {t["realized_pct"]:+.1f}% [{t["reason"]}]')
        print(f'\nWorst 5 trades:')
        for t in sorted_trades[-5:]:
            print(f'  {t["code"]} {t["entry_date"]} → {t["exit_date"]} ({t["hold_days"]}d) {t["realized_pct"]:+.1f}% [{t["reason"]}]')

    # Save trade log
    import json
    with open('/tmp/cnn_strategy_trades.jsonl', 'w') as g:
        for t in trades:
            g.write(json.dumps(t, ensure_ascii=False) + '\n')

    # Save daily portfolio value for plotting
    with open('/tmp/cnn_strategy_daily.jsonl', 'w') as g:
        for d, v, n in daily_value:
            g.write(json.dumps({'date': d, 'value': v, 'positions': n}, ensure_ascii=False) + '\n')

    print(f'\nTrades log: /tmp/cnn_strategy_trades.jsonl')
    print(f'Daily value: /tmp/cnn_strategy_daily.jsonl')


if __name__ == '__main__':
    main()
