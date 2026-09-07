"""单股 RL trading environment v3.

升级 vs v2:
1. 13-channel obs (5 OHLCV + 8 VPA-aware): upper/lower shadow, close_pos, body, vph_5d, vol_pct_60d, range_x, trend_20d
2. 5-dim state (新增 dd_from_peak, alpha_so_far)
3. Reward: relative-to-market + drawdown penalty + terminal alpha bonus
"""
import numpy as np
import gymnasium as gym
from gymnasium import spaces


SEQ_LEN = 60
N_CHANNELS = 13
STATE_DIM = 5


def compute_vpa_features(data):
    """data: (N, 5) float, columns = (open, high, low, close, volume).

    返回 (N, 8): upper_shadow, lower_shadow, close_pos, body_ratio, vph_5d, vol_pct_60d, range_x, trend_20d
    所有特征都是无量纲比例 / 百分比。
    """
    n = len(data)
    op, hi, lo, cl, vol = data[:, 0], data[:, 1], data[:, 2], data[:, 3], data[:, 4]
    rng = np.maximum(hi - lo, 1e-6)

    upper_shadow = (hi - np.maximum(op, cl)) / rng
    lower_shadow = (np.minimum(op, cl) - lo) / rng
    close_pos = (cl - lo) / rng
    body_ratio = np.abs(cl - op) / rng

    vph_5d = np.zeros(n, dtype=np.float32)
    for i in range(5, n):
        prev = max(cl[i - 5], 1e-6)
        vph_5d[i] = (cl[i] - prev) / prev * 100

    vol_pct_60d = np.zeros(n, dtype=np.float32)
    for i in range(20, n):
        avg = vol[max(0, i - 60):i].mean()
        if avg > 1:
            vol_pct_60d[i] = (vol[i] - avg) / avg

    range_x = rng / np.maximum(cl, 1e-6)

    trend_20d = np.zeros(n, dtype=np.float32)
    for i in range(20, n):
        m = cl[max(0, i - 20):i].mean()
        if m > 0:
            trend_20d[i] = (cl[i] - m) / m

    feats = np.column_stack([
        upper_shadow, lower_shadow, close_pos, body_ratio,
        vph_5d / 10, vol_pct_60d, range_x * 10, trend_20d,
    ]).astype(np.float32)
    return np.clip(feats, -10, 10)


class SingleStockTradingEnvV3(gym.Env):
    """v3: 13-channel + drawdown-aware + relative-to-market reward."""

    metadata = {"render_modes": []}

    def __init__(self, stock_data, market_data=None, max_hold_days=30, transaction_cost=0.0):
        """
        stock_data: (N, 5) array (open, high, low, close, volume)
        market_data: (N,) array of market index/avg closes aligned to stock_data dates.
        """
        super().__init__()
        self.data = np.asarray(stock_data, dtype=np.float32)
        self.n_bars = len(self.data)
        self.max_hold = max_hold_days
        self.tc = transaction_cost

        self.vpa_feats = compute_vpa_features(self.data)

        if market_data is None:
            self.market = self.data[:, 3].copy()
        else:
            self.market = np.asarray(market_data, dtype=np.float32)
            assert len(self.market) == self.n_bars, "market_data length must match"

        self.observation_space = spaces.Dict({
            'seq': spaces.Box(low=-10, high=10, shape=(SEQ_LEN, N_CHANNELS), dtype=np.float32),
            'state': spaces.Box(low=-10, high=10, shape=(STATE_DIM,), dtype=np.float32),
        })
        self.action_space = spaces.Discrete(3)  # 0=HOLD, 1=BUY, 2=SELL

    def reset(self, seed=None, options=None):
        super().reset(seed=seed)
        self.t = SEQ_LEN
        self.pos = 0
        self.entry_close = 0.0
        self.entry_t = 0
        self.peak_close = 0.0
        self.peak_dd = 0.0  # max dd seen during current hold (%)
        self.episode_value = 1.0  # cumulative portfolio multiplier
        self.episode_peak_value = 1.0
        self.worst_episode_dd = 0.0
        self.episode_start_market = float(self.market[self.t])
        return self._obs(), {}

    def _obs(self):
        ohlcv = self.data[self.t - SEQ_LEN:self.t]
        vpa = self.vpa_feats[self.t - SEQ_LEN:self.t]
        base_close = max(ohlcv[-1, 3], 1e-6)
        base_vol = max(ohlcv[-20:, 4].mean(), 1)
        ohlcv_norm = np.column_stack([
            ohlcv[:, 0] / base_close,
            ohlcv[:, 1] / base_close,
            ohlcv[:, 2] / base_close,
            ohlcv[:, 3] / base_close,
            ohlcv[:, 4] / base_vol,
        ]).astype(np.float32)
        seq = np.clip(np.concatenate([ohlcv_norm, vpa], axis=1), -10, 10).astype(np.float32)

        if self.pos == 1:
            cur_close = ohlcv[-1, 3]
            days_held = (self.t - self.entry_t) / self.max_hold
            pnl = (cur_close - self.entry_close) / max(self.entry_close, 1e-6)
            dd_from_peak = (self.peak_close - cur_close) / max(self.peak_close, 1e-6)
        else:
            days_held = 0.0
            pnl = 0.0
            dd_from_peak = 0.0

        market_change = (float(self.market[self.t - 1]) - self.episode_start_market) / max(self.episode_start_market, 1e-6)
        portfolio_change = self.episode_value - 1.0
        alpha = portfolio_change - market_change

        state = np.array([float(self.pos), days_held, pnl, dd_from_peak, alpha], dtype=np.float32)
        return {'seq': seq, 'state': np.clip(state, -10, 10)}

    def step(self, action):
        cur_close = float(self.data[self.t, 3])
        prev_close = float(self.data[self.t - 1, 3])
        cur_market = float(self.market[self.t])
        prev_market = float(self.market[self.t - 1])

        daily_ret = (cur_close - prev_close) / max(prev_close, 1e-6) * 100
        market_ret = (cur_market - prev_market) / max(prev_market, 1e-6) * 100

        reward = 0.0
        info = {}

        if action == 1:  # BUY
            if self.pos == 0:
                self.pos = 1
                self.entry_close = cur_close
                self.entry_t = self.t
                self.peak_close = cur_close
                self.peak_dd = 0.0
                reward -= self.tc
                info['action'] = 'BUY'
            else:
                reward -= 0.05
                info['action'] = 'INVALID_BUY'
        elif action == 2:  # SELL
            if self.pos == 1:
                realized = (cur_close - self.entry_close) / self.entry_close * 100
                reward += realized - self.tc * 100
                self.episode_value *= (1 + realized / 100)
                self.episode_peak_value = max(self.episode_peak_value, self.episode_value)
                self.pos = 0
                info['action'] = 'SELL'
                info['realized_pct'] = realized
                info['peak_dd'] = self.peak_dd
            else:
                reward -= 0.05
                info['action'] = 'INVALID_SELL'
        else:  # HOLD
            if self.pos == 1:
                self.peak_close = max(self.peak_close, cur_close)
                cur_dd = (self.peak_close - cur_close) / self.peak_close * 100
                self.peak_dd = max(self.peak_dd, cur_dd)

                reward += daily_ret - market_ret
                if cur_dd > 5:
                    reward -= cur_dd  # ×1: 6% dd → -6 per step

                if self.t - self.entry_t > self.max_hold:
                    realized = (cur_close - self.entry_close) / self.entry_close * 100
                    reward += realized - 1
                    self.episode_value *= (1 + realized / 100)
                    self.episode_peak_value = max(self.episode_peak_value, self.episode_value)
                    self.pos = 0
                    info['action'] = 'FORCED_EXIT'
                    info['realized_pct'] = realized
                else:
                    info['action'] = 'HOLD_LONG'
            else:
                if market_ret > 1.0:
                    reward -= market_ret * 0.5
                info['action'] = 'HOLD_FLAT'

        cur_episode_dd = (self.episode_peak_value - self.episode_value) / max(self.episode_peak_value, 1e-6) * 100
        self.worst_episode_dd = max(self.worst_episode_dd, cur_episode_dd)

        self.t += 1
        terminated = self.t >= self.n_bars - 1
        truncated = False

        if terminated:
            if self.pos == 1:
                cur_close = float(self.data[self.t, 3])
                realized = (cur_close - self.entry_close) / self.entry_close * 100
                reward += realized
                self.episode_value *= (1 + realized / 100)
                info['action'] = 'EOD_CLOSE'
                info['realized_pct'] = realized
                self.pos = 0

            agent_total = (self.episode_value - 1.0) * 100
            market_total = (float(self.market[self.t - 1]) - self.episode_start_market) / max(self.episode_start_market, 1e-6) * 100
            alpha = agent_total - market_total

            if alpha > 10 and self.worst_episode_dd < 5:
                reward += 20
            elif alpha > 10:
                reward += 15
            elif alpha > 5:
                reward += 10
            elif agent_total > 0:
                reward += 5
            elif agent_total > -5:
                reward -= 2
            else:
                reward -= 10

            info['terminal_alpha'] = alpha
            info['terminal_return'] = agent_total
            info['terminal_market'] = market_total
            info['terminal_dd'] = self.worst_episode_dd

        return self._obs(), float(np.clip(reward, -100, 100)), terminated, truncated, info
