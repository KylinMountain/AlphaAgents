"""CNN backtest — 不依赖训练 npz, 直接从 DB 现算 60 天序列推理.

Window: 2026-01-01 → 2026-04-30 (含 4 月全部!)
"""
import argparse, sqlite3
from collections import defaultdict
import numpy as np
import torch
import json


SEQ_LEN = 60


class VPAConv1D(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.conv = torch.nn.Sequential(
            torch.nn.Conv1d(5, 32, 5, padding=2), torch.nn.BatchNorm1d(32), torch.nn.ReLU(),
            torch.nn.Conv1d(32, 64, 5, padding=2), torch.nn.BatchNorm1d(64), torch.nn.ReLU(),
            torch.nn.MaxPool1d(2),
            torch.nn.Conv1d(64, 128, 3, padding=1), torch.nn.BatchNorm1d(128), torch.nn.ReLU(),
            torch.nn.Conv1d(128, 128, 3, padding=1), torch.nn.BatchNorm1d(128), torch.nn.ReLU(),
            torch.nn.AdaptiveAvgPool1d(1),
        )
        self.head = torch.nn.Sequential(
            torch.nn.Linear(128, 64), torch.nn.ReLU(), torch.nn.Dropout(0.3),
            torch.nn.Linear(64, 4),
        )
    def forward(self, x):
        return self.head(self.conv(x.transpose(1, 2)).squeeze(-1))


def normalize_seq(window):
    """window: list of (date, open, high, low, close, volume)"""
    base_close = window[-1][4]
    base_vol = max(np.mean([w[5] for w in window[-20:]]), 1)
    arr = np.array([
        [w[1]/base_close, w[2]/base_close, w[3]/base_close, w[4]/base_close, w[5]/base_vol]
        for w in window
    ], dtype=np.float32)
    return arr


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--model', default='/Users/evilkylin/Projects/AlphaAgents/data/vpa_seq_cnn_v2.pt')
    p.add_argument('--start', default='2026-01-01')
    p.add_argument('--end', default='2026-04-30')
    p.add_argument('--max-positions', type=int, default=2)
    p.add_argument('--buy-threshold', type=float, default=0.0)
    p.add_argument('--sell-topping', type=float, default=0.55)
    p.add_argument('--sell-breakdown', type=float, default=0.55)
    args = p.parse_args()

    device = torch.device('mps' if torch.backends.mps.is_available() else 'cpu')
    print(f'Device: {device}')

    print('Loading market history...', flush=True)
    conn = sqlite3.connect('/Users/evilkylin/Projects/AlphaAgents/data/market_history.db')
    hist = defaultdict(list)
    for code, date, op, hi, lo, cl, vol in conn.execute(
        'SELECT code, date, open, high, low, close, volume FROM daily_kline ORDER BY code, date'
    ):
        hist[code].append((date, op, hi, lo, cl, vol))

    cyb_codes = sorted([c for c in hist if c.startswith(('300','301','302'))])
    print(f'  {len(cyb_codes)} cyb codes')

    all_dates = sorted(set(d for rows in hist.values() for d, *_ in rows))
    eval_dates = [d for d in all_dates if args.start <= d <= args.end]
    print(f'  {len(eval_dates)} trading days {eval_dates[0]} → {eval_dates[-1]}')

    print('Loading model...', flush=True)
    model = VPAConv1D().to(device)
    model.load_state_dict(torch.load(args.model, map_location=device))
    model.eval()

    # Build per-code date->close index for fast lookup
    closes = {code: {d: c for d, _, _, _, c, _ in rows} for code, rows in hist.items()}

    # Strategy state
    positions = {}  # code -> {entry_date, entry_close, entry_di}
    trades = []
    daily_value = []

    print('\n=== Strategy simulation ===', flush=True)
    for di, date in enumerate(eval_dates):
        # Build sequences for all codes that have data on this date
        seqs = []
        codes_with_seq = []
        for code in cyb_codes:
            rows = hist[code]
            idx = next((i for i, r in enumerate(rows) if r[0] == date), None)
            if idx is None or idx < SEQ_LEN - 1: continue
            window = rows[idx-SEQ_LEN+1:idx+1]
            if window[-1][4] <= 0: continue
            seqs.append(normalize_seq(window))
            codes_with_seq.append(code)

        # Inference
        if seqs:
            X = torch.tensor(np.stack(seqs)).to(device)
            with torch.no_grad():
                preds = torch.sigmoid(model(X)).cpu().numpy()
        else:
            preds = np.zeros((0, 4))

        sigs = list(zip(codes_with_seq, preds))

        # 1. Sell signals on existing positions
        to_sell = []
        for code, pos in positions.items():
            sig = next((p for c, p in sigs if c == code), None)
            if sig is None: continue
            top, bot, cont, brk = sig
            if top > args.sell_topping or brk > args.sell_breakdown:
                exit_close = closes[code].get(date)
                if exit_close is None: continue
                pct = (exit_close - pos['entry_close']) / pos['entry_close'] * 100
                trades.append({
                    'code': code, 'entry_date': pos['entry_date'], 'exit_date': date,
                    'realized_pct': pct, 'hold_days': di - pos['entry_di'],
                    'reason': 'topping' if top > args.sell_topping else 'breakdown',
                    'entry_close': pos['entry_close'], 'exit_close': exit_close,
                })
                to_sell.append(code)
        for c in to_sell: del positions[c]

        # 2. New buys
        slots = args.max_positions - len(positions)
        if slots > 0:
            held = set(positions.keys())
            cands = []
            for code, sig in sigs:
                if code in held: continue
                top, bot, cont, brk = sig
                score = cont + bot - top - brk
                if score > args.buy_threshold:
                    cands.append((code, score))
            cands.sort(key=lambda x: -x[1])
            for code, score in cands[:slots]:
                ec = closes[code].get(date)
                if ec is None: continue
                positions[code] = {'entry_date': date, 'entry_di': di, 'entry_close': ec, 'entry_score': score}

        # 3. Mark to market
        n_pos = len(positions)
        weight_per = 1.0 / args.max_positions
        pv = 0
        for code, pos in positions.items():
            cur = closes[code].get(date, pos['entry_close'])
            pv += weight_per * (1 + (cur - pos['entry_close']) / pos['entry_close'])
        cash_w = (args.max_positions - n_pos) / args.max_positions
        value = pv + cash_w
        daily_value.append((date, value, n_pos))

    # Final close
    last_date = eval_dates[-1]
    for code, pos in list(positions.items()):
        ec = closes[code].get(last_date, pos['entry_close'])
        pct = (ec - pos['entry_close']) / pos['entry_close'] * 100
        trades.append({
            'code': code, 'entry_date': pos['entry_date'], 'exit_date': last_date,
            'realized_pct': pct, 'hold_days': len(eval_dates) - 1 - pos['entry_di'],
            'reason': 'end_of_window',
            'entry_close': pos['entry_close'], 'exit_close': ec,
        })

    # Final value
    final_value = daily_value[-1][1] if daily_value else 1.0
    total_return = (final_value - 1) * 100

    # Buy-hold baseline
    bh_returns = []
    for code in cyb_codes:
        c0 = next((closes[code].get(d) for d in eval_dates if d in closes[code]), None)
        c1 = next((closes[code].get(d) for d in reversed(eval_dates) if d in closes[code]), None)
        if c0 and c1 and c0 > 0:
            bh_returns.append((c1-c0)/c0*100)
    bh_avg = sum(bh_returns)/len(bh_returns) if bh_returns else 0

    print(f'\n{"="*60}')
    print(f'CNN Top-{args.max_positions} strategy: {total_return:+.2f}%')
    print(f'创业板 buy-hold avg:        {bh_avg:+.2f}%')
    print(f'Strategy alpha:             {total_return - bh_avg:+.2f}pp')
    print(f'Total trades: {len(trades)}, win_rate: {100*sum(1 for t in trades if t["realized_pct"]>0)/max(len(trades),1):.0f}%')
    if trades:
        avg_pl = sum(t['realized_pct'] for t in trades)/len(trades)
        avg_h = sum(t['hold_days'] for t in trades)/len(trades)
        print(f'Avg P&L/trade: {avg_pl:+.2f}%, avg hold: {avg_h:.1f}d')

        s = sorted(trades, key=lambda x: -x['realized_pct'])
        print(f'\nBest 5:')
        for t in s[:5]:
            print(f'  {t["code"]} {t["entry_date"]}→{t["exit_date"]} ({t["hold_days"]}d) {t["realized_pct"]:+6.1f}% [{t["reason"]}]')
        print(f'Worst 5:')
        for t in s[-5:]:
            print(f'  {t["code"]} {t["entry_date"]}→{t["exit_date"]} ({t["hold_days"]}d) {t["realized_pct"]:+6.1f}% [{t["reason"]}]')

    with open('/tmp/cnn_v2_strategy_trades.jsonl', 'w') as g:
        for t in trades:
            g.write(json.dumps(t, ensure_ascii=False) + '\n')
    with open('/tmp/cnn_v2_daily.jsonl', 'w') as g:
        for d, v, n in daily_value:
            g.write(json.dumps({'date': d, 'value': v, 'positions': n}) + '\n')
    print(f'\nLog saved: /tmp/cnn_v2_strategy_trades.jsonl')


if __name__ == '__main__':
    main()
