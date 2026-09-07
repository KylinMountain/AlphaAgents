"""训练 PPO RL trading agent on multiple stocks.

Train on cyb stocks 2024-04 → 2025-12, evaluate on 2026-01 → 2026-04.
"""
import argparse, sqlite3
from collections import defaultdict
import numpy as np
import torch
import torch.nn as nn
from stable_baselines3 import PPO
from stable_baselines3.common.vec_env import DummyVecEnv
from stable_baselines3.common.torch_layers import BaseFeaturesExtractor
from stable_baselines3.common.callbacks import BaseCallback
import gymnasium as gym
from gymnasium import spaces
import sys

sys.path.insert(0, '/Users/evilkylin/Projects/AlphaAgents/scripts')
from rl_trading_env import SingleStockTradingEnv, SEQ_LEN


class CNNFeatureExtractor(BaseFeaturesExtractor):
    """CNN encoder for OHLCV sequence."""

    def __init__(self, observation_space, features_dim=128):
        super().__init__(observation_space, features_dim)
        # observation_space is a Dict with 'seq' (60, 5) and 'state' (3,)
        self.cnn = nn.Sequential(
            nn.Conv1d(5, 32, kernel_size=5, padding=2),
            nn.BatchNorm1d(32), nn.ReLU(),
            nn.Conv1d(32, 64, kernel_size=5, padding=2),
            nn.BatchNorm1d(64), nn.ReLU(),
            nn.MaxPool1d(2),
            nn.Conv1d(64, 128, kernel_size=3, padding=1),
            nn.BatchNorm1d(128), nn.ReLU(),
            nn.AdaptiveAvgPool1d(1),
        )
        # 128 (CNN) + 3 (state) → features_dim
        self.fc = nn.Linear(128 + 3, features_dim)

    def forward(self, obs):
        seq = obs['seq']  # (B, 60, 5)
        # Squeeze batch if needed (sb3 sometimes adds extra dim)
        if seq.dim() == 4 and seq.shape[1] == 1:
            seq = seq.squeeze(1)
        seq = seq.transpose(1, 2)  # (B, 5, 60)
        z = self.cnn(seq).squeeze(-1)  # (B, 128)
        state = obs['state']
        if state.dim() == 3 and state.shape[1] == 1:
            state = state.squeeze(1)
        combined = torch.cat([z, state], dim=-1)
        return self.fc(combined)


class MultiStockTrainEnv(gym.Env):
    """Wraps SingleStockTradingEnv to cycle through multiple stocks per episode."""

    def __init__(self, stocks_data, max_hold_days=30, transaction_cost=0.0):
        """stocks_data: list of arrays (one per stock)."""
        super().__init__()
        self.stocks = stocks_data
        self.max_hold = max_hold_days
        self.tc = transaction_cost
        self.cur_env = SingleStockTradingEnv(self.stocks[0], max_hold_days, transaction_cost)
        self.observation_space = self.cur_env.observation_space
        self.action_space = self.cur_env.action_space

    def reset(self, seed=None, options=None):
        super().reset(seed=seed)
        # Pick random stock
        stock_idx = np.random.randint(len(self.stocks))
        self.cur_env = SingleStockTradingEnv(self.stocks[stock_idx], self.max_hold, self.tc)
        return self.cur_env.reset(seed=seed)

    def step(self, action):
        return self.cur_env.step(action)


def load_stocks(db_path, start, end, boards):
    """Load each cyb stock as np array (open, high, low, close, volume)."""
    conn = sqlite3.connect(db_path)
    hist = defaultdict(list)
    for code, date, op, hi, lo, cl, vol in conn.execute(
        'SELECT code, date, open, high, low, close, volume FROM daily_kline WHERE date>=? AND date<=? ORDER BY code, date',
        (start, end)
    ):
        if not code.startswith(boards): continue
        hist[code].append((op, hi, lo, cl, vol))
    return [np.array(rows, dtype=np.float32) for rows in hist.values() if len(rows) >= SEQ_LEN + 30]


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--train-start', default='2024-04-01')
    p.add_argument('--train-end', default='2025-12-31')
    p.add_argument('--total-timesteps', type=int, default=200000)
    p.add_argument('--out-model', default='/Users/evilkylin/Projects/AlphaAgents/data/rl_trading_agent.zip')
    args = p.parse_args()

    print(f'Loading stocks {args.train_start} → {args.train_end}...', flush=True)
    stocks = load_stocks('/Users/evilkylin/Projects/AlphaAgents/data/market_history.db',
                         args.train_start, args.train_end, ('300', '301', '302'))
    print(f'  {len(stocks)} stocks loaded', flush=True)

    # Build env
    def make_env():
        return MultiStockTrainEnv(stocks, max_hold_days=30, transaction_cost=0.0)  # remove tc to encourage exploration

    env = DummyVecEnv([make_env])

    policy_kwargs = dict(
        features_extractor_class=CNNFeatureExtractor,
        features_extractor_kwargs=dict(features_dim=128),
        net_arch=[64, 64],
    )

    device = 'mps' if torch.backends.mps.is_available() else 'cpu'
    print(f'Device: {device}', flush=True)

    model = PPO('MultiInputPolicy', env, learning_rate=3e-4, n_steps=2048,
                batch_size=64, n_epochs=4, gamma=0.99, gae_lambda=0.95,
                clip_range=0.2, ent_coef=0.1, verbose=1,  # ent_coef 0.01→0.1 强制探索
                policy_kwargs=policy_kwargs, device=device)

    print(f'\nTraining {args.total_timesteps} steps...', flush=True)
    model.learn(total_timesteps=args.total_timesteps, progress_bar=False)

    model.save(args.out_model)
    print(f'\nModel saved to {args.out_model}')


if __name__ == '__main__':
    main()
