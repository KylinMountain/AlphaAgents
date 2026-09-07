"""Backtest trained RL agent on cyb stocks 2026-01 → 2026-04.

For each stock, run agent through full window and compute realized P&L.
Aggregate: avg per-stock + best/worst trades.
"""
import argparse, sqlite3, json, sys
from collections import defaultdict
import numpy as np
import torch
from stable_baselines3 import PPO

sys.path.insert(0, '/Users/evilkylin/Projects/AlphaAgents/scripts')
from rl_trading_env import SingleStockTradingEnv, SEQ_LEN
from train_rl_agent import CNNFeatureExtractor


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--model', default='/Users/evilkylin/Projects/AlphaAgents/data/rl_trading_agent.zip')
    p.add_argument('--start', default='2026-01-01')
    p.add_argument('--end', default='2026-04-30')
    p.add_argument('--data-start', default='2025-09-01', help='need 60 days history before --start')
    args = p.parse_args()

    conn = sqlite3.connect('/Users/evilkylin/Projects/AlphaAgents/data/market_history.db')

    # Load all cyb stocks with enough data
    print('Loading...', flush=True)
    hist = defaultdict(list)
    dates_by_code = defaultdict(list)
    for code, date, op, hi, lo, cl, vol in conn.execute(
        'SELECT code, date, open, high, low, close, volume FROM daily_kline WHERE date>=? AND date<=? ORDER BY code, date',
        (args.data_start, args.end)
    ):
        if not code.startswith(('300', '301', '302')): continue
        hist[code].append((op, hi, lo, cl, vol))
        dates_by_code[code].append(date)

    # Load model
    print('Loading model...', flush=True)
    model = PPO.load(args.model)

    # Run agent on each stock
    print(f'\nRunning agent on {len(hist)} stocks...', flush=True)
    per_stock_results = []
    all_trades = []

    for code, rows in hist.items():
        if len(rows) < SEQ_LEN + 5: continue
        # Find idx of start date
        dates = dates_by_code[code]
        try:
            start_idx = next(i for i, d in enumerate(dates) if d >= args.start)
        except StopIteration:
            continue
        if start_idx < SEQ_LEN: continue

        # Cut to data starting from (start_idx - SEQ_LEN) so env has 60 days history
        # Then env will start at SEQ_LEN within the cut data
        cut = rows[start_idx - SEQ_LEN:]
        cut_dates = dates[start_idx - SEQ_LEN:]
        if len(cut) < SEQ_LEN + 5: continue

        env = SingleStockTradingEnv(np.array(cut, dtype=np.float32), max_hold_days=30, transaction_cost=0.05)
        obs, _ = env.reset()
        done = False
        actions_log = []
        ep_reward = 0
        stock_trades = []
        while not done:
            action, _ = model.predict(obs, deterministic=True)
            obs, reward, terminated, truncated, info = env.step(action)
            ep_reward += reward
            actions_log.append((cut_dates[env.t-1] if env.t-1 < len(cut_dates) else None, info.get('action'), info.get('realized_pct')))
            if 'realized_pct' in info:
                stock_trades.append({
                    'code': code, 'date': cut_dates[env.t-1], 'realized_pct': info['realized_pct']
                })
            done = terminated or truncated
        all_trades.extend(stock_trades)

        # Compute total realized return for this stock
        stock_pnl = sum(t['realized_pct'] for t in stock_trades)
        n_trades = len(stock_trades)
        per_stock_results.append({
            'code': code, 'n_trades': n_trades, 'total_pnl': stock_pnl, 'reward': ep_reward,
            'actions': [a for _, a, _ in actions_log if a],
        })

    # Aggregate
    print('\n=== RL Agent Backtest Results ===')
    print(f'Total stocks: {len(per_stock_results)}')
    all_pnl = []
    if per_stock_results:
        all_pnl = [r['total_pnl'] for r in per_stock_results]
        n_trades_total = sum(r['n_trades'] for r in per_stock_results)
        traded = [r for r in per_stock_results if r['n_trades'] > 0]
        traded_pnl = [r['total_pnl'] for r in traded]
        print(f'Stocks traded: {len(traded)} / {len(per_stock_results)}')
        print(f'Total trades: {n_trades_total}')
        print(f'Avg total P&L per stock: {np.mean(all_pnl):.2f}%')
        if traded_pnl:
            print(f'Avg total P&L per traded stock: {np.mean(traded_pnl):.2f}%')
        winners = sum(1 for p in all_pnl if p > 0)
        print(f'Winners: {winners} / {len(per_stock_results)} = {100*winners/len(per_stock_results):.0f}%')

    # Per-trade stats
    if all_trades:
        trade_pnls = [t['realized_pct'] for t in all_trades]
        print(f'\nPer-trade stats:')
        print(f'  N = {len(all_trades)}')
        print(f'  Mean: {np.mean(trade_pnls):.2f}%, median: {np.median(trade_pnls):.2f}%')
        print(f'  Win rate: {100*np.mean(np.array(trade_pnls) > 0):.0f}%')
        print(f'  Best: {max(trade_pnls):.2f}%, worst: {min(trade_pnls):.2f}%')
        print(f'  pct > +5%: {100*np.mean(np.array(trade_pnls) > 5):.0f}%')
        print(f'  pct > +10%: {100*np.mean(np.array(trade_pnls) > 10):.0f}%')
        print(f'  pct < -5%: {100*np.mean(np.array(trade_pnls) < -5):.0f}%')

    # Action distribution
    all_actions = [a for r in per_stock_results for a in r['actions']]
    if all_actions:
        from collections import Counter
        c = Counter(all_actions)
        total = sum(c.values())
        print(f'\nAction distribution:')
        for a, n in c.most_common():
            print(f'  {a:<20} {n:>6} ({100*n/total:.1f}%)')

    # Buy-hold benchmark
    bh_returns = []
    for code, rows in hist.items():
        dates = dates_by_code[code]
        if not dates: continue
        try:
            start_idx = next(i for i, d in enumerate(dates) if d >= args.start)
        except StopIteration:
            continue
        if start_idx >= len(rows) - 1: continue
        c0 = rows[start_idx][3]  # close at start
        c1 = rows[-1][3]
        if c0 > 0:
            bh_returns.append((c1 - c0) / c0 * 100)
    if bh_returns:
        print(f'\n创业板 buy-hold avg: {np.mean(bh_returns):.2f}%')
        print(f'RL strategy total: {sum(all_pnl) / len(per_stock_results):.2f}% (avg per stock)')

    # Save trades
    with open('/tmp/rl_agent_trades.jsonl', 'w') as g:
        for t in all_trades:
            g.write(json.dumps(t, ensure_ascii=False) + '\n')
    print(f'\nTrades log: /tmp/rl_agent_trades.jsonl')


if __name__ == '__main__':
    main()
