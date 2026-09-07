"""Train RL trading agent v3 with VPA features + pretrain transfer + new reward."""
import argparse, sqlite3
from collections import defaultdict
import numpy as np
import torch
import torch.nn as nn
from stable_baselines3 import PPO
from stable_baselines3.common.vec_env import DummyVecEnv
from stable_baselines3.common.torch_layers import BaseFeaturesExtractor
import gymnasium as gym
import sys

sys.path.insert(0, '/Users/evilkylin/Projects/AlphaAgents/scripts')
from rl_trading_env_v3 import SingleStockTradingEnvV3, SEQ_LEN, N_CHANNELS, STATE_DIM


class CNNFeatureExtractorV3(BaseFeaturesExtractor):
    """13-channel CNN encoder, architecture matches supervised CNN v2 for transfer."""

    def __init__(self, observation_space, features_dim=128, pretrained_path=None):
        super().__init__(observation_space, features_dim)
        self.cnn = nn.Sequential(
            nn.Conv1d(N_CHANNELS, 32, kernel_size=5, padding=2),
            nn.BatchNorm1d(32), nn.ReLU(),
            nn.Conv1d(32, 64, kernel_size=5, padding=2),
            nn.BatchNorm1d(64), nn.ReLU(),
            nn.MaxPool1d(2),
            nn.Conv1d(64, 128, kernel_size=3, padding=1),
            nn.BatchNorm1d(128), nn.ReLU(),
            nn.Conv1d(128, 128, kernel_size=3, padding=1),
            nn.BatchNorm1d(128), nn.ReLU(),
            nn.AdaptiveAvgPool1d(1),
        )
        self.fc = nn.Linear(128 + STATE_DIM, features_dim)

        if pretrained_path:
            self._load_pretrained(pretrained_path)

    def _load_pretrained(self, path):
        """Partial transfer from supervised CNN v2 (5-channel input).

        For first conv: copy weights into first 5 input channels, leave 8 new ones random-init.
        Other layers: full transfer (shapes match).
        """
        src = torch.load(path, map_location='cpu')
        own = self.state_dict()
        loaded, partial = 0, 0
        for src_key, src_w in src.items():
            if not src_key.startswith('conv.'):
                continue
            tgt_key = 'cnn.' + src_key[len('conv.'):]
            if tgt_key not in own:
                continue
            tgt_w = own[tgt_key]
            if src_w.shape == tgt_w.shape:
                own[tgt_key] = src_w
                loaded += 1
            elif tgt_key == 'cnn.0.weight' and src_w.shape[0] == tgt_w.shape[0] and src_w.shape[2] == tgt_w.shape[2]:
                new_w = tgt_w.clone()
                new_w[:, :src_w.shape[1], :] = src_w
                own[tgt_key] = new_w
                partial += 1
        self.load_state_dict(own)
        print(f'  Pretrained: full-load {loaded} tensors, partial-load {partial} (first conv 5/{N_CHANNELS} channels)')

    def forward(self, obs):
        seq = obs['seq']
        if seq.dim() == 4 and seq.shape[1] == 1:
            seq = seq.squeeze(1)
        seq = seq.transpose(1, 2)  # (B, C, T)
        z = self.cnn(seq).squeeze(-1)
        state = obs['state']
        if state.dim() == 3 and state.shape[1] == 1:
            state = state.squeeze(1)
        return self.fc(torch.cat([z, state], dim=-1))


class MultiStockTrainEnvV3(gym.Env):
    """Cycles through (stock, market) tuples per episode."""

    def __init__(self, stock_market_pairs, max_hold_days=30, transaction_cost=0.0):
        super().__init__()
        self.pairs = stock_market_pairs
        self.max_hold = max_hold_days
        self.tc = transaction_cost
        s, m = self.pairs[0]
        self.cur_env = SingleStockTradingEnvV3(s, m, max_hold_days, transaction_cost)
        self.observation_space = self.cur_env.observation_space
        self.action_space = self.cur_env.action_space

    def reset(self, seed=None, options=None):
        super().reset(seed=seed)
        idx = np.random.randint(len(self.pairs))
        s, m = self.pairs[idx]
        self.cur_env = SingleStockTradingEnvV3(s, m, self.max_hold, self.tc)
        return self.cur_env.reset(seed=seed)

    def step(self, action):
        return self.cur_env.step(action)


def load_stocks_with_market(db_path, start, end, boards):
    """Load each cyb stock + per-day cyb-avg market index aligned by date."""
    conn = sqlite3.connect(db_path)
    by_code = defaultdict(dict)
    for code, date, op, hi, lo, cl, vol in conn.execute(
        'SELECT code, date, open, high, low, close, volume FROM daily_kline WHERE date>=? AND date<=? ORDER BY code, date',
        (start, end)
    ):
        if not code.startswith(boards):
            continue
        by_code[code][date] = (op, hi, lo, cl, vol)

    daily_closes = defaultdict(list)
    for code_d in by_code.values():
        for d, v in code_d.items():
            daily_closes[d].append(v[3])
    market_avg = {d: float(np.mean(closes)) for d, closes in daily_closes.items()}

    pairs = []
    for code, code_d in by_code.items():
        dates = sorted(code_d.keys())
        if len(dates) < SEQ_LEN + 30:
            continue
        stock_arr = np.array([code_d[d] for d in dates], dtype=np.float32)
        market_arr = np.array([market_avg[d] for d in dates], dtype=np.float32)
        pairs.append((stock_arr, market_arr))
    return pairs


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--train-start', default='2024-04-01')
    p.add_argument('--train-end', default='2025-12-31')
    p.add_argument('--total-timesteps', type=int, default=200000)
    p.add_argument('--pretrained', default='/Users/evilkylin/Projects/AlphaAgents/data/vpa_seq_cnn_v2.pt')
    p.add_argument('--no-pretrain', action='store_true')
    p.add_argument('--out-model', default='/Users/evilkylin/Projects/AlphaAgents/data/rl_trading_agent_v3.zip')
    args = p.parse_args()

    print(f'Loading {args.train_start} → {args.train_end}...', flush=True)
    pairs = load_stocks_with_market(
        '/Users/evilkylin/Projects/AlphaAgents/data/market_history.db',
        args.train_start, args.train_end, ('300', '301', '302'))
    print(f'  {len(pairs)} (stock, market) pairs', flush=True)

    def make_env():
        return MultiStockTrainEnvV3(pairs, max_hold_days=30, transaction_cost=0.0)

    env = DummyVecEnv([make_env])

    pretrained_path = None if args.no_pretrain else args.pretrained
    policy_kwargs = dict(
        features_extractor_class=CNNFeatureExtractorV3,
        features_extractor_kwargs=dict(features_dim=128, pretrained_path=pretrained_path),
        net_arch=[64, 64],
    )

    device = 'mps' if torch.backends.mps.is_available() else 'cpu'
    print(f'Device: {device}', flush=True)

    model = PPO('MultiInputPolicy', env, learning_rate=3e-4, n_steps=2048,
                batch_size=64, n_epochs=10, gamma=0.99, gae_lambda=0.95,
                clip_range=0.2, ent_coef=0.05, verbose=1,
                policy_kwargs=policy_kwargs, device=device)

    print(f'\nTraining {args.total_timesteps} steps...', flush=True)
    model.learn(total_timesteps=args.total_timesteps, progress_bar=False)

    model.save(args.out_model)
    print(f'\nSaved: {args.out_model}')


if __name__ == '__main__':
    main()
