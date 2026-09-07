"""Backtest RL v3 agent on cyb 2026-01 → 2026-04."""
import argparse, sqlite3, json, sys
from collections import defaultdict
import numpy as np
import torch
from stable_baselines3 import PPO

sys.path.insert(0, '/Users/evilkylin/Projects/AlphaAgents/scripts')
from rl_trading_env_v3 import SingleStockTradingEnvV3, SEQ_LEN
from train_rl_agent_v3 import CNNFeatureExtractorV3


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--model', default='/Users/evilkylin/Projects/AlphaAgents/data/rl_trading_agent_v3.zip')
    p.add_argument('--start', default='2026-01-01')
    p.add_argument('--end', default='2026-04-30')
    p.add_argument('--data-start', default='2025-09-01')
    args = p.parse_args()

    conn = sqlite3.connect('/Users/evilkylin/Projects/AlphaAgents/data/market_history.db')

    print('Loading...', flush=True)
    by_code = defaultdict(dict)
    for code, date, op, hi, lo, cl, vol in conn.execute(
        'SELECT code, date, open, high, low, close, volume FROM daily_kline WHERE date>=? AND date<=? ORDER BY code, date',
        (args.data_start, args.end)
    ):
        if not code.startswith(('300', '301', '302')):
            continue
        by_code[code][date] = (op, hi, lo, cl, vol)

    # Per-day cyb avg
    daily_closes = defaultdict(list)
    for code_d in by_code.values():
        for d, v in code_d.items():
            daily_closes[d].append(v[3])
    market_avg = {d: float(np.mean(closes)) for d, closes in daily_closes.items()}

    print('Loading model...', flush=True)
    model = PPO.load(args.model)

    print(f'\nRunning agent on {len(by_code)} stocks...', flush=True)
    per_stock = []
    all_trades = []

    for code, code_d in by_code.items():
        dates = sorted(code_d.keys())
        try:
            start_idx = next(i for i, d in enumerate(dates) if d >= args.start)
        except StopIteration:
            continue
        if start_idx < SEQ_LEN:
            continue

        cut_dates = dates[start_idx - SEQ_LEN:]
        if len(cut_dates) < SEQ_LEN + 5:
            continue
        stock = np.array([code_d[d] for d in cut_dates], dtype=np.float32)
        market = np.array([market_avg[d] for d in cut_dates], dtype=np.float32)

        env = SingleStockTradingEnvV3(stock, market, max_hold_days=30, transaction_cost=0.05)
        obs, _ = env.reset()
        done = False
        actions_log = []
        ep_reward = 0
        stock_trades = []
        while not done:
            action, _ = model.predict(obs, deterministic=True)
            obs, reward, terminated, truncated, info = env.step(action)
            ep_reward += reward
            actions_log.append((cut_dates[env.t - 1] if env.t - 1 < len(cut_dates) else None,
                                info.get('action'), info.get('realized_pct')))
            if 'realized_pct' in info:
                stock_trades.append({
                    'code': code,
                    'date': cut_dates[env.t - 1] if env.t - 1 < len(cut_dates) else None,
                    'realized_pct': float(info['realized_pct']),
                    'peak_dd': float(info.get('peak_dd', 0)),
                })
            done = terminated or truncated
        all_trades.extend(stock_trades)

        stock_pnl = sum(t['realized_pct'] for t in stock_trades)
        per_stock.append({
            'code': code,
            'n_trades': len(stock_trades),
            'total_pnl': float(stock_pnl),
            'reward': float(ep_reward),
            'actions': [a for _, a, _ in actions_log if a],
            'terminal_alpha': float(info.get('terminal_alpha', 0)),
            'terminal_dd': float(info.get('terminal_dd', 0)),
        })

    print('\n=== RL v3 Backtest ===')
    print(f'Total stocks: {len(per_stock)}')
    if per_stock:
        all_pnl = [r['total_pnl'] for r in per_stock]
        traded = [r for r in per_stock if r['n_trades'] > 0]
        print(f'Stocks traded: {len(traded)} / {len(per_stock)}')
        print(f'Total trades: {sum(r["n_trades"] for r in per_stock)}')
        print(f'Avg P&L per stock: {np.mean(all_pnl):.2f}%')
        if traded:
            print(f'Avg P&L per traded stock: {np.mean([r["total_pnl"] for r in traded]):.2f}%')
        winners = sum(1 for p in all_pnl if p > 0)
        print(f'Winners: {winners} / {len(per_stock)} = {100 * winners / len(per_stock):.0f}%')

        avg_alpha = np.mean([r['terminal_alpha'] for r in per_stock])
        avg_dd = np.mean([r['terminal_dd'] for r in per_stock])
        print(f'Avg terminal alpha: {avg_alpha:+.2f}%, avg worst dd: {avg_dd:.2f}%')

    if all_trades:
        pnls = [t['realized_pct'] for t in all_trades]
        peak_dds = [t['peak_dd'] for t in all_trades]
        print(f'\nPer-trade: N={len(all_trades)}')
        print(f'  Mean: {np.mean(pnls):.2f}%, median: {np.median(pnls):.2f}%')
        print(f'  Win rate: {100 * np.mean(np.array(pnls) > 0):.0f}%')
        print(f'  Best: {max(pnls):.2f}%, worst: {min(pnls):.2f}%')
        print(f'  Avg peak_dd during hold: {np.mean(peak_dds):.2f}%')
        print(f'  pct > +5%: {100 * np.mean(np.array(pnls) > 5):.0f}%')
        print(f'  pct > +10%: {100 * np.mean(np.array(pnls) > 10):.0f}%')
        print(f'  pct < -5%: {100 * np.mean(np.array(pnls) < -5):.0f}%')

    all_actions = [a for r in per_stock for a in r['actions']]
    if all_actions:
        from collections import Counter
        c = Counter(all_actions)
        total = sum(c.values())
        print(f'\nAction distribution:')
        for a, n in c.most_common():
            print(f'  {a:<20} {n:>6} ({100 * n / total:.1f}%)')

    bh_returns = []
    for code, code_d in by_code.items():
        dates = sorted(code_d.keys())
        eval_dates = [d for d in dates if d >= args.start]
        if len(eval_dates) < 2:
            continue
        c0 = code_d[eval_dates[0]][3]
        c1 = code_d[eval_dates[-1]][3]
        if c0 > 0:
            bh_returns.append((c1 - c0) / c0 * 100)
    if bh_returns:
        bh = np.mean(bh_returns)
        avg = np.mean(all_pnl) if per_stock else 0
        print(f'\n创业板 buy-hold avg: {bh:+.2f}%')
        print(f'RL v3 avg per stock:  {avg:+.2f}%')
        print(f'Alpha:                {avg - bh:+.2f}pp')

    with open('/tmp/rl_v3_trades.jsonl', 'w') as g:
        for t in all_trades:
            g.write(json.dumps(t, ensure_ascii=False) + '\n')
    print(f'\nTrades log: /tmp/rl_v3_trades.jsonl')


if __name__ == '__main__':
    main()
