"""单股 RL trading environment.

每个 episode:
  - 从一只股的 day 60 开始 (有 60 天历史)
  - 到 day end-1 结束
  - 每天 agent 选 BUY/HOLD/SELL
"""
import numpy as np
import gymnasium as gym
from gymnasium import spaces


SEQ_LEN = 60


class SingleStockTradingEnv(gym.Env):
    """单股 trading env."""

    metadata = {"render_modes": []}

    def __init__(self, stock_data, max_hold_days=30, transaction_cost=0.0):
        """
        stock_data: list of (open, high, low, close, volume), at least SEQ_LEN+5 rows.
        """
        super().__init__()
        self.data = np.array(stock_data, dtype=np.float32)
        self.n_bars = len(self.data)
        self.max_hold = max_hold_days
        self.tc = transaction_cost

        # State: 60-day OHLCV (5x60=300) + 3 portfolio state (pos, days_held, pnl)
        # We flatten OHLCV to (5, 60) for CNN, expose as Dict for clarity
        self.observation_space = spaces.Dict({
            'seq': spaces.Box(low=-10, high=10, shape=(SEQ_LEN, 5), dtype=np.float32),
            'state': spaces.Box(low=-10, high=10, shape=(3,), dtype=np.float32),
        })
        self.action_space = spaces.Discrete(3)  # 0=HOLD, 1=BUY, 2=SELL

    def reset(self, seed=None, options=None):
        super().reset(seed=seed)
        self.t = SEQ_LEN  # start at day 60 (have full history)
        self.pos = 0  # 0 = no position, 1 = long
        self.entry_close = 0.0
        self.entry_t = 0
        return self._obs(), {}

    def _obs(self):
        # Last 60 days OHLCV, normalize by today's close
        window = self.data[self.t - SEQ_LEN:self.t]
        base_close = window[-1, 3]  # close index = 3
        base_vol = max(window[-20:, 4].mean(), 1)
        seq = np.column_stack([
            window[:, 0] / base_close,  # open
            window[:, 1] / base_close,  # high
            window[:, 2] / base_close,  # low
            window[:, 3] / base_close,  # close
            window[:, 4] / base_vol,    # volume
        ]).astype(np.float32)

        if self.pos == 1:
            days_held = (self.t - self.entry_t) / self.max_hold  # normalized
            pnl = (window[-1, 3] - self.entry_close) / self.entry_close
        else:
            days_held = 0.0
            pnl = 0.0
        state = np.array([self.pos, days_held, pnl], dtype=np.float32)
        return {'seq': seq, 'state': state}

    def step(self, action):
        cur_close = self.data[self.t, 3]
        prev_close = self.data[self.t - 1, 3]

        reward = 0.0
        info = {}

        if action == 1:  # BUY
            if self.pos == 0:
                self.pos = 1
                self.entry_close = cur_close
                self.entry_t = self.t
                reward -= self.tc  # transaction cost
                info['action'] = 'BUY'
            else:
                reward -= 0.05  # invalid action
                info['action'] = 'INVALID_BUY'
        elif action == 2:  # SELL
            if self.pos == 1:
                realized = (cur_close - self.entry_close) / self.entry_close * 100
                reward += realized - self.tc * 100  # realized return as reward, in %
                self.pos = 0
                info['action'] = 'SELL'
                info['realized_pct'] = realized
            else:
                reward -= 0.05  # invalid action
                info['action'] = 'INVALID_SELL'
        else:  # HOLD
            if self.pos == 1:
                # Daily return reward (encourages staying in profitable trades)
                daily_ret = (cur_close - prev_close) / prev_close * 100
                reward += daily_ret
                # Force exit if held too long
                if self.t - self.entry_t > self.max_hold:
                    realized = (cur_close - self.entry_close) / self.entry_close * 100
                    reward += realized - 1  # small penalty for forced exit
                    self.pos = 0
                    info['action'] = 'FORCED_EXIT'
                    info['realized_pct'] = realized
                else:
                    info['action'] = 'HOLD_LONG'
            else:
                # Opportunity cost: penalize HOLD_FLAT when next day rises significantly
                daily_ret = (cur_close - prev_close) / prev_close * 100
                if daily_ret > 1.0:
                    reward -= daily_ret * 0.5  # 错过涨 → 罚一半
                info['action'] = 'HOLD_FLAT'

        self.t += 1

        # Episode end
        terminated = self.t >= self.n_bars - 1
        truncated = False

        # If still holding at end, force close
        if terminated and self.pos == 1:
            cur_close = self.data[self.t, 3]
            realized = (cur_close - self.entry_close) / self.entry_close * 100
            reward += realized
            info['action'] = 'EOD_CLOSE'
            info['realized_pct'] = realized
            self.pos = 0

        return self._obs(), reward, terminated, truncated, info
