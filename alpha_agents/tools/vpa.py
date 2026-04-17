"""Volume Price Analysis (VPA) — Anna Coulling / Wyckoff style.

Reads structural information about who-is-buying-who-is-selling from
K-line + volume relationships. All indicator computation and pattern
detection is done in code; LLM only interprets the pre-computed results.

Core patterns detected:
- 量价一致/背离 (price-volume agreement or divergence)
- 顶部背离 (top divergence: price up, volume down)
- 底部放量 (bottom volume spike: price down, volume up)
- Selling Climax (急跌巨量收回过半)
- Buying Climax (急涨巨量冲高回落)
- 放量滞涨 (big volume but tiny body — distribution)
- OBV trend

Ported from TradingAgents data_collector._compute_vpa_indicators with
structured JSON output added so downstream code can consume signals
without parsing text.
"""

import json
import logging
from typing import Optional

import numpy as np
import pandas as pd

from alpha_agents.data.market_history import get_local_history
from alpha_agents.data.market_data import get_stock_history

logger = logging.getLogger(__name__)

# Pattern signal strengths (used by institutional_position integration).
#
# Weights calibrated by backtest on 4000 (code, date) samples × forward 5 days
# against local market_history.db (2024-2026). See scripts/backtest_vpa.py and
# data/vpa_backtest_4000.csv.
#
# Key findings driving these weights:
#   - Bearish VPA signals are genuinely predictive (57% directional hit).
#   - Bullish VPA signals alone are NOT predictive in A-share retail market —
#     "价涨+放量" often precedes a pullback. Use VPA for avoidance, not追涨.
#   - top_divergence (57.2% hit, -0.12% mean) and very_bearish buckets are
#     the strongest individual signals.
#   - healthy_uptrend has 39.8% hit, -0.60% mean — it is a REVERSE signal in
#     A-share, so we NEGATE it (was +2, now -1).
#   - bottom_volume_spike (53.4% hit, +0.64% mean) is the best single bullish
#     signal — kept at +1.
# ── Regime-adaptive weights ──────────────────────────────────────
# Walk-forward analysis (280K samples) shows VPA bullish signals are ONLY
# effective in falling regime (65.3% T+1 hit) and terrible in rising (35.9%).
# Solution: maintain separate weight tables per regime.
#
# DEFAULT (ranging / unknown): conservative — bearish signals only
PATTERN_SCORES = {
    # ── Original patterns ──
    "healthy_uptrend": -1,
    "top_divergence": -3,
    "bottom_exhaustion": 0,
    "bottom_volume_spike": 1,
    "selling_climax": -1,
    "buying_climax": -2,
    "no_demand": -1,
    "no_supply": 0,
    "distribution": -1,
    "absorption": 1,
    # ── Anna Coulling K-line signals (new) ──
    "shooting_star": -3,         # 射击十字星+高量=局内人卖出（最强顶部信号之一）
    "hammer": 2,                 # 锤头线+高量=局内人买入
    "hanging_man": -2,           # 吊人线（上涨顶部的锤头形态）=卖压第一信号
    "long_legged_doji_trap": -1, # 长腿十字线+低量=震仓假信号
    "high_body_low_vol": -2,     # 高实体+低量=陷阱（多头或空头）
    "low_body_high_vol_yang": -2,# 低实体阳线+高量=牛市力竭
    "low_body_high_vol_yin": 2,  # 低实体阴线+高量=熊转牛
    "volume_breakout": 2,        # 真突破：穿越阻力+巨量
    "fake_breakout": -2,         # 假突破：穿越+低量=陷阱
    "demand_test_fail": -2,      # 需求测试失败：缩量反弹=买盘耗尽
    "stopping_volume": 2,        # 放量止跌：长下影+极高量+收上半
    "topping_volume": -3,        # 放量止涨：实体缩小弧线+量放大=抛售高峰
}

# FALLING regime: Anna Coulling bullish signals work (超跌反弹有效)
PATTERN_SCORES_FALLING = {
    "healthy_uptrend": 0,
    "top_divergence": -2,
    "bottom_exhaustion": 1,
    "bottom_volume_spike": 2,
    "selling_climax": 2,
    "buying_climax": -2,
    "no_demand": 0,
    "no_supply": 1,
    "distribution": -1,
    "absorption": 2,
    # ── Anna Coulling K-line signals ──
    "shooting_star": -2,         # 弱一些（超跌中射击十字星可能是反弹正常调整）
    "hammer": 3,                 # 锤头线在下跌底部最强
    "hanging_man": -1,
    "long_legged_doji_trap": 0,
    "high_body_low_vol": -1,
    "low_body_high_vol_yang": -1,
    "low_body_high_vol_yin": 3,  # 低实体阴线+高量在底部=最强熊转牛
    "volume_breakout": 3,        # 突破吸筹区间=起涨
    "fake_breakout": -1,
    "demand_test_fail": -1,
    "stopping_volume": 3,        # 放量止跌在falling最有意义
    "topping_volume": -2,
}

# RISING regime: bearish signals strongest, bullish is追高
PATTERN_SCORES_RISING = {
    "healthy_uptrend": -2,
    "top_divergence": -3,
    "bottom_exhaustion": 0,
    "bottom_volume_spike": 0,
    "selling_climax": -1,
    "buying_climax": -3,
    "no_demand": -2,
    "no_supply": 0,
    "distribution": -2,
    "absorption": 0,
    # ── Anna Coulling K-line signals ──
    "shooting_star": -3,         # 升温期射击十字星=最强顶部信号
    "hammer": 0,                 # 升温期锤头线=noise
    "hanging_man": -3,           # 吊人线在上涨顶部=经典反转
    "long_legged_doji_trap": -2, # 震仓信号在升温期更有意义
    "high_body_low_vol": -3,     # 高实体低量在升温期=最典型的多头陷阱
    "low_body_high_vol_yang": -3,# 低实体阳线高量=牛市力竭（升温期最强）
    "low_body_high_vol_yin": 1,
    "volume_breakout": 1,
    "fake_breakout": -3,         # 假突破在升温期=散户被骗
    "demand_test_fail": -3,      # 需求测试失败=派筹完成
    "stopping_volume": 0,
    "topping_volume": -3,        # 放量止涨在升温期=最强抛售高峰
}


def get_pattern_scores(regime: str = "") -> dict:
    """Return the appropriate weight table for the given market regime."""
    if regime == "falling":
        return PATTERN_SCORES_FALLING
    elif regime == "rising":
        return PATTERN_SCORES_RISING
    return PATTERN_SCORES


def _load_ohlcv(code: str, days: int = 60, include_realtime: bool = True) -> Optional[pd.DataFrame]:
    """Fetch OHLCV data from local market_history.db, fallback to baostock.

    If include_realtime=True and market is open, appends today's partial bar
    from Sina realtime API so VPA can see intraday action.
    """
    history = get_local_history(code, days=days)
    if not history or len(history) < 25:
        history = get_stock_history(code, days=days)
    if not history or len(history) < 25:
        return None

    df = pd.DataFrame(history)
    for col in ("open", "high", "low", "close", "volume"):
        if col not in df.columns:
            return None
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close", "volume"]).reset_index(drop=True)

    # Append today's realtime bar if available and not already in history
    if include_realtime and len(df) >= 25:
        try:
            from alpha_agents.data.market_data import get_realtime_quotes
            from datetime import datetime
            today = datetime.now().strftime("%Y-%m-%d")
            last_date = str(df.iloc[-1].get("date", ""))

            if last_date != today:
                rt = get_realtime_quotes([code])
                if rt and code in rt:
                    q = rt[code]
                    price = q.get("price", 0)
                    if price > 0:
                        today_bar = {
                            "date": today,
                            "open": q.get("open", price),
                            "high": q.get("high", price),
                            "low": q.get("low", price),
                            "close": price,
                            "volume": int(q.get("volume", 0)),
                        }
                        df = pd.concat([df, pd.DataFrame([today_bar])], ignore_index=True)
                        logger.debug("Appended realtime bar for %s: close=%.2f vol=%d",
                                     code, price, today_bar["volume"])
        except Exception as e:
            logger.debug("Realtime bar for %s failed: %s", code, e)

    return df if len(df) >= 25 else None


def _load_ohlcv_60min(code: str, bars: int = 80) -> Optional[pd.DataFrame]:
    """Fetch 60-min OHLCV bars for multi-timeframe VPA (Anna Coulling problem 5).

    Daily bars compress A-share intraday info (±10% caps make daily extremes
    common). 60-min bars let VPA see whether insiders are accumulating
    throughout the day or only at the open/close, whether gaps up/down have
    follow-through, etc.

    Returns None if data unavailable or too few bars.
    """
    try:
        from alpha_agents.data.market_data import get_stock_minute_history
        rows = get_stock_minute_history(code, period="60", bars=bars)
    except Exception as e:
        logger.debug("60-min data for %s failed: %s", code, e)
        return None
    if not rows or len(rows) < 25:
        return None
    df = pd.DataFrame(rows)
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close", "volume"]).reset_index(drop=True)
    return df if len(df) >= 25 else None


def _compute_derived(df: pd.DataFrame, window: int = 20) -> pd.DataFrame:
    """Compute per-bar derived metrics: volume ratio, close position, spread, shadows."""
    df = df.copy()
    df["vol_ma"] = df["volume"].rolling(window).mean()
    df["volume_ratio"] = df["volume"] / df["vol_ma"]

    hl_range = df["high"] - df["low"]
    df["bar_spread"] = hl_range / df["close"]
    df["close_position"] = np.where(
        hl_range > 0,
        (df["close"] - df["low"]) / hl_range,
        0.5,
    )
    df["bar_type"] = np.where(
        df["close"] > df["open"], "阳线",
        np.where(df["close"] < df["open"], "阴线", "十字星"),
    )
    df["upper_shadow"] = np.where(
        hl_range > 0,
        (df["high"] - np.maximum(df["open"], df["close"])) / hl_range,
        0.0,
    )
    df["lower_shadow"] = np.where(
        hl_range > 0,
        (np.minimum(df["open"], df["close"]) - df["low"]) / hl_range,
        0.0,
    )
    df["pct_change"] = df["close"].pct_change()
    df["vol_ma5"] = df["volume"].rolling(5).mean()
    df["vol_trend_ratio"] = df["vol_ma5"] / df["vol_ma"]

    # Volume-price harmony labels
    df["vp_harmony"] = np.where(
        (df["pct_change"] > 0) & (df["volume_ratio"] > 1.0), "一致(涨+放量)",
        np.where(
            (df["pct_change"] < 0) & (df["volume_ratio"] > 1.0), "背离(跌+放量)",
            np.where(
                (df["pct_change"] > 0) & (df["volume_ratio"] < 0.8), "背离(涨+缩量)",
                np.where(
                    (df["pct_change"] < 0) & (df["volume_ratio"] < 0.8), "一致(跌+缩量)",
                    "中性",
                ),
            ),
        ),
    )

    # OBV vectorized
    close_diff = df["close"].diff()
    obv_sign = np.where(close_diff > 0, 1, np.where(close_diff < 0, -1, 0))
    obv_sign[0] = 0
    df["obv"] = (obv_sign * df["volume"].values).cumsum()

    return df


def _detect_patterns(df: pd.DataFrame, regime: str = "") -> list[dict]:
    """Detect key VPA patterns in the recent 5-day window. Returns structured list."""
    scores = get_pattern_scores(regime)
    patterns = []
    if len(df) < 5:
        return patterns

    recent = df.tail(5)
    last = df.iloc[-1]

    # #9 Climax position weighting — compute 20-day range so climax detectors
    # can distinguish "selling climax at 20-day LOW" (real panic bottom) from
    # "selling climax at 20-day mid" (likely just mid-trend capitulation).
    # Same logic applies for buying_climax (top of range = real distribution
    # climax; mid-range = likely just pullback noise).
    if len(df) >= 20:
        _range_high = float(df["high"].tail(20).max())
        _range_low = float(df["low"].tail(20).min())
        _range_span = max(_range_high - _range_low, 1e-9)
    else:
        _range_high = float(df["high"].max())
        _range_low = float(df["low"].min())
        _range_span = max(_range_high - _range_low, 1e-9)

    def _range_position(close_price: float) -> float:
        """Return close position within 20-day range, 0 = range low, 1 = range high."""
        return max(0.0, min(1.0, (close_price - _range_low) / _range_span))

    # ── 5-day trend divergence ──
    first_close = recent["close"].iloc[0]
    last_close = recent["close"].iloc[-1]
    first_vol = recent["volume"].iloc[0]
    last_vol = recent["volume"].iloc[-1]
    price_up = last_close > first_close
    price_down = last_close < first_close
    vol_up = last_vol > first_vol
    vol_down = last_vol < first_vol
    price_change_pct = (last_close - first_close) / first_close * 100 if first_close else 0

    if price_up and vol_down:
        patterns.append({
            "pattern": "top_divergence",
            "label": "顶部背离",
            "bullish": False,
            "strength": scores["top_divergence"],
            "detail": f"近5日价格+{price_change_pct:.1f}% 但量能递减，上涨动能衰竭",
        })
    if price_down and vol_up:
        patterns.append({
            "pattern": "bottom_volume_spike",
            "label": "底部放量",
            "bullish": True,
            "strength": scores["bottom_volume_spike"],
            "detail": f"近5日价格{price_change_pct:.1f}% 但量能递增，可能恐慌抛售或机构换手",
        })
    if price_down and vol_down:
        patterns.append({
            "pattern": "bottom_exhaustion",
            "label": "卖压衰竭",
            "bullish": True,
            "strength": scores["bottom_exhaustion"],
            "detail": f"近5日价格{price_change_pct:.1f}% 且量能递减，空方力量枯竭",
        })
    if price_up and vol_up:
        patterns.append({
            "pattern": "healthy_uptrend",
            "label": "健康上涨",
            "bullish": True,
            "strength": scores["healthy_uptrend"],
            "detail": f"近5日价格+{price_change_pct:.1f}% 且量能配合递增",
        })

    # ── Selling Climax: last 3 bars ──
    # #9: position-aware — climax at range LOW is the real panic bottom;
    # climax at range MID is mid-trend continuation, often a faux signal.
    for i in range(max(-3, -len(df)), 0):
        row = df.iloc[i]
        vr = row.get("volume_ratio", 0) or 0
        pct = row.get("pct_change", 0) or 0
        cp = row.get("close_position", 0.5) or 0.5
        lower = row.get("lower_shadow", 0) or 0

        # Selling Climax: 急跌 + 巨量 + 收回过半
        if vr > 2.0 and pct < -0.03 and cp > 0.5:
            date_str = str(row.get("date", ""))[-5:]
            range_pos = _range_position(float(row["close"]))
            base_strength = scores["selling_climax"]
            # Strength adjustment by range position
            if range_pos < 0.2:
                # Climax at range low = real panic bottom, highest reversal value
                strength_adjust = +2
                pos_label = f"区间低位(底部{range_pos*100:.0f}%)"
                interpretation = "经典恐慌见底信号"
            elif range_pos < 0.4:
                strength_adjust = +1
                pos_label = f"区间中低位({range_pos*100:.0f}%)"
                interpretation = "较强反转信号"
            elif range_pos > 0.7:
                # At range top — likely just profit-taking or a trap, not a real climax
                strength_adjust = -2
                pos_label = f"区间高位({range_pos*100:.0f}%)"
                interpretation = "高位放量急跌，可能是派发开始而非恐慌见底"
            else:
                strength_adjust = 0
                pos_label = f"区间中位({range_pos*100:.0f}%)"
                interpretation = "中位climax可信度一般"
            patterns.append({
                "pattern": "selling_climax",
                "label": "卖出高潮",
                "bullish": True,
                "strength": base_strength + strength_adjust,
                "detail": (
                    f"{date_str} 急跌{pct*100:.1f}% 量比{vr:.1f} 收盘位置{cp:.2f}，"
                    f"{pos_label} — {interpretation}"
                ),
            })
            break

    # ── Buying Climax: last 3 bars ──
    # #9: position-aware — climax at range HIGH is the real distribution top;
    # climax at mid-range is often a pause before continuation, not a top.
    for i in range(max(-3, -len(df)), 0):
        row = df.iloc[i]
        vr = row.get("volume_ratio", 0) or 0
        pct = row.get("pct_change", 0) or 0
        upper = row.get("upper_shadow", 0) or 0
        cp = row.get("close_position", 0.5) or 0.5

        # Buying Climax: 急涨 + 巨量 + 长上影线 + 收在下半区
        if vr > 2.0 and pct > 0.03 and upper > 0.4 and cp < 0.5:
            date_str = str(row.get("date", ""))[-5:]
            range_pos = _range_position(float(row["close"]))
            base_strength = scores["buying_climax"]
            if range_pos > 0.8:
                # Climax at range high = real distribution top
                strength_adjust = -2  # More bearish
                pos_label = f"区间高位(顶部{range_pos*100:.0f}%)"
                interpretation = "经典派发顶信号"
            elif range_pos > 0.6:
                strength_adjust = -1
                pos_label = f"区间中高位({range_pos*100:.0f}%)"
                interpretation = "较强顶部信号"
            elif range_pos < 0.3:
                # Mid-low range climax — likely just bounce failure, not top
                strength_adjust = +2  # Less bearish
                pos_label = f"区间低位({range_pos*100:.0f}%)"
                interpretation = "低位冲高回落，可能只是反弹失败而非派发顶"
            else:
                strength_adjust = 0
                pos_label = f"区间中位({range_pos*100:.0f}%)"
                interpretation = "中位climax可信度一般"
            patterns.append({
                "pattern": "buying_climax",
                "label": "买入高潮",
                "bullish": False,
                "strength": base_strength + strength_adjust,
                "detail": (
                    f"{date_str} 急涨{pct*100:.1f}% 量比{vr:.1f} 长上影收低位，"
                    f"{pos_label} — {interpretation}"
                ),
            })
            break

    # ── 放量滞涨（distribution）──
    for i in range(max(-3, -len(df)), 0):
        row = df.iloc[i]
        vr = row.get("volume_ratio", 0) or 0
        pct = row.get("pct_change", 0) or 0
        spread = row.get("bar_spread", 0) or 0

        if vr > 1.8 and abs(pct) < 0.01 and spread < 0.015:
            date_str = str(row.get("date", ""))[-5:]
            patterns.append({
                "pattern": "distribution",
                "label": "放量滞涨",
                "bullish": False,
                "strength": scores["distribution"],
                "detail": f"{date_str} 量比{vr:.1f} 但实体窄、价格几乎不动，多空分歧大（疑似派发）",
            })
            break

    # ── no_demand / no_supply (reaction bars) ──
    for i in range(max(-3, -len(df)), 0):
        row = df.iloc[i]
        vr = row.get("volume_ratio", 0) or 0
        pct = row.get("pct_change", 0) or 0
        bar_type = row.get("bar_type", "")

        if vr < 0.7 and pct > 0.005 and bar_type == "阳线":
            date_str = str(row.get("date", ""))[-5:]
            patterns.append({
                "pattern": "no_demand",
                "label": "无需求反弹",
                "bullish": False,
                "strength": scores["no_demand"],
                "detail": f"{date_str} 量比{vr:.1f} 阳线反弹但极度缩量，买方力量不足",
            })
            break
        if vr < 0.7 and pct < -0.005 and bar_type == "阴线":
            date_str = str(row.get("date", ""))[-5:]
            patterns.append({
                "pattern": "no_supply",
                "label": "无供给回调",
                "bullish": True,
                "strength": scores["no_supply"],
                "detail": f"{date_str} 量比{vr:.1f} 阴线但缩量，卖方力量衰竭",
            })
            break

    # ── Anna Coulling K-line signals (single bar, last 3 bars) ──

    for i in range(max(-3, -len(df)), 0):
        row = df.iloc[i]
        vr = row.get("volume_ratio", 0) or 0
        pct = row.get("pct_change", 0) or 0
        cp = row.get("close_position", 0.5) or 0.5
        upper = row.get("upper_shadow", 0) or 0
        lower = row.get("lower_shadow", 0) or 0
        spread = row.get("bar_spread", 0) or 0
        bar_type = row.get("bar_type", "")
        date_str = str(row.get("date", ""))[-5:]

        # 射击十字星: 长上影(>0.5) + 实体窄 + 收在下半区(<0.3)
        if upper > 0.5 and spread < 0.02 and cp < 0.3 and vr > 0.8:
            if vr > 1.5:
                detail = f"{date_str} 射击十字星+高量({vr:.1f})=局内人大量卖出，重大反转信号"
            else:
                detail = f"{date_str} 射击十字星+平均量({vr:.1f})=中等回调信号"
            patterns.append({"pattern": "shooting_star", "label": "射击十字星",
                             "bullish": False, "strength": scores.get("shooting_star", -2),
                             "detail": detail})
            break

        # 锤头线 vs 吊人线: 同一形态（长下影+收高位），趋势方向决定含义
        # Anna Coulling: 下跌中 = 锤头线(bullish), 上涨中 = 吊人线(bearish)
        is_hammer_shape = lower > 0.5 and spread < 0.02 and cp > 0.7 and vr > 0.8

        if is_hammer_shape and price_down:
            # 下跌趋势中的锤头线 = bullish (局内人买入)
            if vr > 1.5:
                detail = f"{date_str} 下跌中锤头线+高量({vr:.1f})=局内人大量买入，买入高峰信号"
            else:
                detail = f"{date_str} 下跌中锤头线+平均量({vr:.1f})=日内反弹机会"
            patterns.append({"pattern": "hammer", "label": "锤头线(下跌底部)",
                             "bullish": True, "strength": scores.get("hammer", 2),
                             "detail": detail})
            break

        if is_hammer_shape and price_up:
            # 上涨趋势中的同样形态 = 吊人线 = bearish (卖压第一信号)
            detail = f"{date_str} 上涨中吊人线+量比({vr:.1f})=卖压出现的第一信号"
            patterns.append({"pattern": "hanging_man", "label": "吊人线(上涨顶部)",
                             "bullish": False, "strength": scores.get("hanging_man", -2),
                             "detail": detail})
            break

        # 高实体 + 低量 = 陷阱
        if spread > 0.03 and vr < 0.7:
            direction = "多头" if bar_type == "阳线" else "空头"
            patterns.append({"pattern": "high_body_low_vol", "label": f"高实体低量{direction}陷阱",
                             "bullish": False, "strength": scores.get("high_body_low_vol", -2),
                             "detail": f"{date_str} 宽实体{bar_type}但量比仅{vr:.1f}=局内人未参与，可能是{direction}陷阱"})
            break

        # 低实体 + 高量 (分阳/阴)
        if spread < 0.01 and vr > 1.5:
            if bar_type == "阳线":
                patterns.append({"pattern": "low_body_high_vol_yang", "label": "低实体阳线+高量=牛市力竭",
                                 "bullish": False, "strength": scores.get("low_body_high_vol_yang", -2),
                                 "detail": f"{date_str} 窄阳线+量比{vr:.1f}=多空拉锯，上涨动力衰竭"})
            elif bar_type == "阴线":
                patterns.append({"pattern": "low_body_high_vol_yin", "label": "低实体阴线+高量=熊转牛",
                                 "bullish": True, "strength": scores.get("low_body_high_vol_yin", 2),
                                 "detail": f"{date_str} 窄阴线+量比{vr:.1f}=局内人嗅到机会，潜在反转"})
            break

        # 长腿十字线 + 低量 = 震仓假信号
        if upper > 0.3 and lower > 0.3 and spread < 0.01 and vr < 0.7:
            patterns.append({"pattern": "long_legged_doji_trap", "label": "长腿十字线+低量=震仓",
                             "bullish": False, "strength": scores.get("long_legged_doji_trap", -1),
                             "detail": f"{date_str} 上下影线都长但缩量({vr:.1f})=局内人制造波动，假信号"})
            break

    # ── 突破检测 (Anna Coulling: 整理区间积累后的突破) ──
    if len(df) >= 20:
        last_row = df.iloc[-1]
        last_vr = last_row.get("volume_ratio", 0) or 0
        last_close = last_row["close"]
        last_date = str(last_row.get("date", ""))[-5:]

        recent_20 = df.tail(20)
        resistance = recent_20["high"].iloc[:-1].max()
        support = recent_20["low"].iloc[:-1].min()

        # Anna Coulling 因果定律: 整理越久突破越强
        # 检测是否存在整理区间: 近10日波动率 < 近20日波动率的60%（收窄）
        if len(df) >= 10:
            vol_10d = df["close"].tail(10).std() / df["close"].tail(10).mean()
            vol_20d = df["close"].tail(20).std() / df["close"].tail(20).mean()
            is_consolidation = vol_10d < vol_20d * 0.7  # 近期波动收窄=整理
        else:
            is_consolidation = False

        # 真突破: 整理后 + 穿越阻力 + 巨量
        if last_close > resistance and last_vr > 1.5 and is_consolidation:
            patterns.append({"pattern": "volume_breakout", "label": "整理后放量突破",
                             "bullish": True, "strength": scores.get("volume_breakout", 2),
                             "detail": f"{last_date} 整理区间后突破{resistance:.2f}+量比{last_vr:.1f}=真突破（因果定律）"})

        # 假突破: 穿越但低量 (不管是否整理)
        elif last_close > resistance and last_vr < 0.8:
            patterns.append({"pattern": "fake_breakout", "label": "假突破(低量)",
                             "bullish": False, "strength": scores.get("fake_breakout", -2),
                             "detail": f"{last_date} 突破{resistance:.2f}但量比仅{last_vr:.1f}=陷阱"})

        # 没整理就冲高 + 放量 = 追高不是突破
        elif last_close > resistance and last_vr > 1.5 and not is_consolidation:
            patterns.append({"pattern": "fake_breakout", "label": "无整理冲高(非真突破)",
                             "bullish": False, "strength": scores.get("fake_breakout", -1),
                             "detail": f"{last_date} 冲破{resistance:.2f}+放量但近期无整理=可能是追高"})

        # 放量止跌: 跌到支撑附近 + 长下影 + 高量 + 收上半
        last_lower = last_row.get("lower_shadow", 0) or 0
        last_cp = last_row.get("close_position", 0.5) or 0.5
        if (last_row["low"] <= support * 1.02 and last_lower > 0.4
                and last_vr > 1.5 and last_cp > 0.5):
            patterns.append({"pattern": "stopping_volume", "label": "放量止跌",
                             "bullish": True, "strength": scores.get("stopping_volume", 2),
                             "detail": f"{last_date} 触及支撑{support:.2f}+长下影+高量({last_vr:.1f})+收上半=买入高峰临近"})

        # 需求测试失败: 近期有派发信号 + 缩量反弹
        # 简化: 近5日有 distribution/buying_climax + 最新一根是缩量阳线
        has_bearish_prior = any(p["pattern"] in ("distribution", "buying_climax", "topping_volume")
                                for p in patterns)
        if has_bearish_prior and last_row.get("bar_type") == "阳线" and last_vr < 0.8:
            patterns.append({"pattern": "demand_test_fail", "label": "需求测试失败",
                             "bullish": False, "strength": scores.get("demand_test_fail", -2),
                             "detail": f"{last_date} 派发后缩量反弹({last_vr:.1f})=买盘耗尽，准备下跌"})

    # ── 放量止涨 (实体逐渐缩小 + 量放大) ──
    if len(df) >= 5:
        last3 = df.tail(3)
        spreads = last3["bar_spread"].tolist()
        vrs = [v if not pd.isna(v) else 0 for v in last3["volume_ratio"].tolist()]
        # 实体逐渐缩小 + 量逐渐放大 + 整体在涨
        if (len(spreads) == 3 and spreads[0] > spreads[1] > spreads[2]
                and vrs[0] < vrs[1] < vrs[2] and vrs[2] > 1.2 and price_up):
            last_date = str(df.iloc[-1].get("date", ""))[-5:]
            patterns.append({"pattern": "topping_volume", "label": "放量止涨(弧形顶)",
                             "bullish": False, "strength": scores.get("topping_volume", -3),
                             "detail": f"{last_date} 实体逐渐缩小+量逐渐放大=派筹尾声，抛售高峰临近"})

    return patterns


def _obv_trend(df: pd.DataFrame) -> str:
    """Determine OBV trend by comparing 10-day MA against lookback."""
    obv_ma = df["obv"].rolling(10).mean().dropna()
    if len(obv_ma) < 5:
        return "数据不足"
    return "上升" if obv_ma.iloc[-1] > obv_ma.iloc[-5] else "下降"


def _volume_regime(df: pd.DataFrame) -> dict:
    """Classify overall volume regime: 放量 / 平稳 / 缩量."""
    last = df.iloc[-1]
    vol_5d = df["volume"].tail(5).mean()
    vol_20d = last.get("vol_ma") if pd.notna(last.get("vol_ma")) else 0
    if vol_20d == 0:
        regime = "数据不足"
        ratio = 0.0
    elif vol_5d > vol_20d * 1.2:
        regime = "放量"
        ratio = vol_5d / vol_20d
    elif vol_5d < vol_20d * 0.8:
        regime = "缩量"
        ratio = vol_5d / vol_20d
    else:
        regime = "平稳"
        ratio = vol_5d / vol_20d
    return {"regime": regime, "ratio_5d_vs_20d": round(float(ratio), 2)}


def _detect_test_phase(df: pd.DataFrame) -> dict:
    """Detect Wyckoff supply/demand tests in the last 10 bars.

    Anna Coulling chapter 5: after accumulation/distribution, insiders run
    a TEST to verify the opposite side is exhausted:

      Supply test (after accumulation):
        - Recent up-move (say bar T-7 to T-4, price rose Y%)
        - Then a pullback (T-3 to T-0, price fell X% where X < Y)
        - During pullback, volume < up-move volume × 0.7
        - Close holds above the up-move's start → supply IS exhausted, bullish

      Demand test (after distribution):
        - Recent down-move
        - Then a rally
        - Rally volume < down-move volume × 0.7
        - Close fails to recover → demand IS exhausted, bearish

    This is distinct from no_supply/no_demand single-bar detection: tests
    are multi-bar SEQUENCES that confirm or deny the preceding phase.

    Returns {"test_status": str, "test_note": str, "bullish": bool | None}
    or empty dict if no test pattern detected.
    """
    if len(df) < 12:
        return {}

    recent = df.tail(10).reset_index(drop=True)
    if "volume" not in recent.columns or "close" not in recent.columns:
        return {}

    # Split into two halves: earlier (impulse move) vs later (test move)
    mid = len(recent) // 2
    impulse = recent.iloc[:mid]
    test = recent.iloc[mid:]

    impulse_price_chg = (impulse["close"].iloc[-1] - impulse["close"].iloc[0]) / impulse["close"].iloc[0]
    test_price_chg = (test["close"].iloc[-1] - test["close"].iloc[0]) / test["close"].iloc[0]

    impulse_vol = float(impulse["volume"].mean())
    test_vol = float(test["volume"].mean())
    if impulse_vol <= 0:
        return {}
    vol_ratio = test_vol / impulse_vol

    # Supply test: impulse up, test down, test volume shrunk, test didn't break below impulse start
    if (impulse_price_chg > 0.03 and test_price_chg < 0
            and vol_ratio < 0.7
            and test["close"].iloc[-1] > impulse["close"].iloc[0] * 0.98):
        return {
            "test_status": "supply_test",
            "bullish": True,
            "test_note": (
                f"疑似 supply test：前段 {mid}根 bar 上涨 {impulse_price_chg*100:.1f}%，"
                f"后段回抽 {test_price_chg*100:+.1f}% 但量能缩至 {vol_ratio*100:.0f}%，"
                f"支撑未破——卖压可能已被吸尽（Anna Coulling：低量测试 = 好消息）"
            ),
        }

    # Demand test: impulse down, test up, test volume shrunk, test didn't recover impulse start
    if (impulse_price_chg < -0.03 and test_price_chg > 0
            and vol_ratio < 0.7
            and test["close"].iloc[-1] < impulse["close"].iloc[0] * 1.02):
        return {
            "test_status": "demand_test",
            "bullish": False,
            "test_note": (
                f"疑似 demand test：前段 {mid}根 bar 下跌 {impulse_price_chg*100:.1f}%，"
                f"后段反弹 {test_price_chg*100:+.1f}% 但量能缩至 {vol_ratio*100:.0f}%，"
                f"阻力未破——买盘可能已被耗尽（Anna Coulling：低量反弹 = 坏消息）"
            ),
        }

    # Failed supply test: pullback with HIGH volume = accumulation NOT complete
    if (impulse_price_chg > 0.03 and test_price_chg < -0.02 and vol_ratio > 1.2):
        return {
            "test_status": "failed_supply_test",
            "bullish": False,
            "test_note": (
                f"供给测试失败：前段上涨 {impulse_price_chg*100:.1f}% 后回抽放量至 {vol_ratio*100:.0f}%，"
                f"卖压仍强——吸筹未完成，需继续震仓"
            ),
        }

    # Failed demand test: rally with HIGH volume = distribution NOT complete
    if (impulse_price_chg < -0.03 and test_price_chg > 0.02 and vol_ratio > 1.2):
        return {
            "test_status": "failed_demand_test",
            "bullish": True,
            "test_note": (
                f"需求测试失败：前段下跌 {impulse_price_chg*100:.1f}% 后反弹放量至 {vol_ratio*100:.0f}%，"
                f"买盘仍强——派发未完成，需继续抛售"
            ),
        }

    return {}


def _compute_context(df: pd.DataFrame, window: int = 20, code: str = "") -> dict:
    """Compute structural context for LLM: support/resistance, consolidation, trend.

    This gives LLM the "where are we" information that Anna Coulling's
    three-step analysis (micro → macro → global) requires.

    Args:
        df: OHLCV dataframe
        window: lookback window (default 20)
        code: stock code (used for relative strength on down days)
    """
    ctx = {}
    if len(df) < window:
        return ctx

    closes = df["close"]
    highs = df["high"]
    lows = df["low"]
    last_close = closes.iloc[-1]

    # ── Support / Resistance (20-day) ──
    resistance_20d = highs.tail(window).max()
    support_20d = lows.tail(window).min()
    range_20d = resistance_20d - support_20d
    position_in_range = ((last_close - support_20d) / range_20d * 100) if range_20d > 0 else 50
    ctx["resistance_20d"] = round(float(resistance_20d), 2)
    ctx["support_20d"] = round(float(support_20d), 2)
    ctx["position_in_range_pct"] = round(float(position_in_range), 1)
    if position_in_range > 80:
        ctx["position_label"] = "接近阻力位（高位）"
    elif position_in_range < 20:
        ctx["position_label"] = "接近支撑位（低位）"
    elif position_in_range > 60:
        ctx["position_label"] = "中高位"
    elif position_in_range < 40:
        ctx["position_label"] = "中低位"
    else:
        ctx["position_label"] = "中间位置"

    # ── Consolidation detection (Anna Coulling 因果定律) ──
    if len(df) >= 10:
        vol_10d = closes.tail(10).std() / closes.tail(10).mean() if closes.tail(10).mean() > 0 else 0
        vol_20d = closes.tail(window).std() / closes.tail(window).mean() if closes.tail(window).mean() > 0 else 0
        ctx["volatility_10d"] = round(float(vol_10d * 100), 2)
        ctx["volatility_20d"] = round(float(vol_20d * 100), 2)
        if vol_20d > 0 and vol_10d < vol_20d * 0.6:
            ctx["consolidation"] = True
            ctx["consolidation_note"] = f"近10日波动率({vol_10d*100:.2f}%)明显低于20日({vol_20d*100:.2f}%)，处于整理区间（因果定律：蓄势越久突破越大）"
        else:
            ctx["consolidation"] = False
            ctx["consolidation_note"] = f"未检测到整理区间（近10日波动率{vol_10d*100:.2f}% vs 20日{vol_20d*100:.2f}%）"

    # ── P0.1 Consolidation DURATION (Anna Coulling 因果定律 — 量化版) ──
    # 静态波动率检测只告诉是/否，但因果定律的关键是"多久"。这里数出连续多少
    # 日 close 在 20 日 median ±5% 区间内——8 天和 80 天的整理含义截然不同。
    median_n = float(closes.tail(window).median())
    consolidation_days = 0
    if median_n > 0:
        for i in range(len(closes) - 1, -1, -1):
            c = float(closes.iloc[i])
            if abs(c - median_n) / median_n <= 0.05:
                consolidation_days += 1
            else:
                break
    ctx["consolidation_days"] = consolidation_days
    if consolidation_days >= 30:
        ctx["consolidation_strength"] = f"长期整理 ({consolidation_days}日)，蓄势充分，突破后空间大"
    elif consolidation_days >= 15:
        ctx["consolidation_strength"] = f"中期整理 ({consolidation_days}日)，蓄势中等"
    elif consolidation_days >= 7:
        ctx["consolidation_strength"] = f"短期整理 ({consolidation_days}日)"
    else:
        ctx["consolidation_strength"] = f"未形成整理 ({consolidation_days}日)"

    # ── #10: Re-accumulation vs primary accumulation disambiguation ──
    # Anna Coulling + Wyckoff: consolidations look the same on the chart but
    # imply VERY different target sizes depending on what came BEFORE:
    #   - After a downtrend → PRIMARY accumulation → big move up (2-3× range)
    #   - After an uptrend   → RE-accumulation → modest continuation (1.2-1.5×)
    #   - After a flat range → just noise, low conviction either way
    # This directly informs the target_zone multiplier the LLM uses.
    if consolidation_days >= 7 and median_n > 0:
        # The consolidation began at index `-consolidation_days` (newest-first).
        # We want the trend over the ~30 bars IMMEDIATELY before that — i.e.
        # from index `-(consolidation_days + 30)` to `-consolidation_days`.
        look_back_span = 30
        look_back_start = consolidation_days + look_back_span
        look_back_end = consolidation_days  # exactly where consolidation began
        if len(closes) >= look_back_start:
            prior_start = closes.iloc[-look_back_start]
            prior_end = closes.iloc[-look_back_end]
            prior_trend_pct = (prior_end - prior_start) / prior_start * 100 if prior_start > 0 else 0
            ctx["consolidation_prior_trend_pct"] = round(float(prior_trend_pct), 1)
            if prior_trend_pct < -15:
                ctx["accumulation_type"] = "primary_accumulation"
                ctx["accumulation_note"] = (
                    f"整理之前（约 {look_back_end}~{look_back_start} 根 bar 前）下跌 "
                    f"{prior_trend_pct:.1f}% — 这是 PRIMARY accumulation（底部吸筹），"
                    f"突破目标乘数 2-3×（因果定律：下跌越深，反弹越大）"
                )
            elif prior_trend_pct > 15:
                ctx["accumulation_type"] = "re_accumulation"
                ctx["accumulation_note"] = (
                    f"整理之前上涨 {prior_trend_pct:+.1f}% — 这是 RE-accumulation（中继平台），"
                    f"突破目标乘数 1.2-1.5×（主升浪已过，二段相对有限）"
                )
            elif prior_trend_pct < -5:
                ctx["accumulation_type"] = "shallow_bottom"
                ctx["accumulation_note"] = (
                    f"整理前轻微下跌 {prior_trend_pct:.1f}%，乘数 1.5-2×（浅底吸筹）"
                )
            elif prior_trend_pct > 5:
                ctx["accumulation_type"] = "shallow_re_accumulation"
                ctx["accumulation_note"] = (
                    f"整理前轻微上涨 {prior_trend_pct:+.1f}%，乘数 1.0-1.3×"
                )
            else:
                ctx["accumulation_type"] = "flat"
                ctx["accumulation_note"] = (
                    f"整理前价格接近平衡（{prior_trend_pct:+.1f}%），低 conviction，乘数 ~1×"
                )

    # ── Trend strength (10-day, 20-day) ──
    if len(closes) >= 20:
        chg_10d = (closes.iloc[-1] - closes.iloc[-10]) / closes.iloc[-10] * 100
        chg_20d = (closes.iloc[-1] - closes.iloc[-20]) / closes.iloc[-20] * 100
        ctx["trend_10d_pct"] = round(float(chg_10d), 2)
        ctx["trend_20d_pct"] = round(float(chg_20d), 2)
        if chg_10d > 5:
            ctx["trend_label"] = "短期强势上涨"
        elif chg_10d > 2:
            ctx["trend_label"] = "短期温和上涨"
        elif chg_10d < -5:
            ctx["trend_label"] = "短期急跌"
        elif chg_10d < -2:
            ctx["trend_label"] = "短期温和下跌"
        else:
            ctx["trend_label"] = "短期横盘"
    elif len(closes) >= 10:
        chg_10d = (closes.iloc[-1] - closes.iloc[-10]) / closes.iloc[-10] * 100
        ctx["trend_10d_pct"] = round(float(chg_10d), 2)
        ctx["trend_label"] = "数据不足20日"

    # ── High/Low point trend (simplified trend line) ──
    if len(df) >= 10:
        recent_highs = highs.tail(10).tolist()
        recent_lows = lows.tail(10).tolist()
        # Check if highs are rising/falling (compare first half vs second half)
        h_first = max(recent_highs[:5])
        h_second = max(recent_highs[5:])
        l_first = min(recent_lows[:5])
        l_second = min(recent_lows[5:])
        if h_second > h_first and l_second > l_first:
            ctx["hl_trend"] = "高点和低点都在抬升（上升趋势）"
        elif h_second < h_first and l_second < l_first:
            ctx["hl_trend"] = "高点和低点都在下移（下降趋势）"
        elif h_second > h_first and l_second < l_first:
            ctx["hl_trend"] = "高点抬升但低点下移（波动加大/扩散三角形）"
        elif h_second < h_first and l_second > l_first:
            ctx["hl_trend"] = "高点下移但低点抬升（收敛三角形/整理）"
        else:
            ctx["hl_trend"] = "高低点趋势不明确"

    # ── Key price levels ──
    if len(df) >= 5:
        ctx["high_5d"] = round(float(highs.tail(5).max()), 2)
        ctx["low_5d"] = round(float(lows.tail(5).min()), 2)

    # ── P1.2 Relative strength on market down days ──
    # Anna Coulling: stocks accumulated by insiders show comparative strength
    # during market weakness — they refuse to fall when everything else does.
    # Compare stock daily change_pct vs market on days where market < -0.5%.
    # Need stock to have a 'date' column so we can align with market index.
    if "date" in df.columns and len(df) >= 10:
        try:
            from alpha_agents.data.market_data import get_market_index_history
            idx_rows = get_market_index_history("sh000001", days=window + 5)
            if idx_rows:
                # Build market lookup (change_pct AND volume for #8)
                mkt_chg = {r["date"]: r["change_pct"] for r in idx_rows}
                # Stock change_pct (already in df.pct_change column)
                stock_pcts = []
                mkt_pcts = []
                stock_vol_ratios = []  # stock volume_ratio on same day
                for _, row in df.tail(window).iterrows():
                    d = str(row.get("date", ""))
                    if d in mkt_chg:
                        stock_pct = row.get("pct_change", 0)
                        if pd.notna(stock_pct):
                            stock_pcts.append(float(stock_pct) * 100)
                            mkt_pcts.append(mkt_chg[d])
                            stock_vr = row.get("volume_ratio", 1)
                            stock_vol_ratios.append(
                                float(stock_vr) if pd.notna(stock_vr) else 1.0
                            )
                # Filter to market down days (< -0.5%)
                down_day_stock_pcts = [
                    s for s, m in zip(stock_pcts, mkt_pcts) if m < -0.5
                ]
                down_day_mkt_pcts = [m for m in mkt_pcts if m < -0.5]
                down_day_stock_vols = [
                    vr for vr, m in zip(stock_vol_ratios, mkt_pcts) if m < -0.5
                ]
                up_day_stock_vols = [
                    vr for vr, m in zip(stock_vol_ratios, mkt_pcts) if m > 0.5
                ]
                if down_day_stock_pcts:
                    avg_stock = sum(down_day_stock_pcts) / len(down_day_stock_pcts)
                    avg_mkt = sum(down_day_mkt_pcts) / len(down_day_mkt_pcts)
                    excess = avg_stock - avg_mkt  # positive = strong, outperforms
                    ctx["down_day_count"] = len(down_day_stock_pcts)
                    ctx["down_day_stock_avg_pct"] = round(avg_stock, 2)
                    ctx["down_day_mkt_avg_pct"] = round(avg_mkt, 2)
                    ctx["down_day_excess_pct"] = round(excess, 2)
                    if excess > 1.5:
                        ctx["relative_strength_label"] = (
                            f"显著强于市场（市场跌时本股票多涨{excess:+.2f}%，机构疑似在接货）"
                        )
                    elif excess > 0.5:
                        ctx["relative_strength_label"] = (
                            f"略强于市场（多{excess:+.2f}%）"
                        )
                    elif excess < -1.5:
                        ctx["relative_strength_label"] = (
                            f"显著弱于市场（市场跌时本股票还多跌{abs(excess):.2f}%，可能被抛售）"
                        )
                    elif excess < -0.5:
                        ctx["relative_strength_label"] = (
                            f"略弱于市场（{excess:+.2f}%）"
                        )
                    else:
                        ctx["relative_strength_label"] = "与市场同步"

                # #8 Volume relative strength on market-down days
                # Anna Coulling: "on market down days, stocks being accumulated
                # by insiders hold elevated volume — sellers meet buyers, both
                # are active." A stock whose volume stays firm/grows on down
                # days is more interesting than one showing price strength —
                # price can be manipulated by a few trades, volume cannot.
                if down_day_stock_vols:
                    avg_down_vol = sum(down_day_stock_vols) / len(down_day_stock_vols)
                    ctx["down_day_stock_vol_ratio"] = round(avg_down_vol, 2)
                    # Also compute up-day vol ratio for comparison
                    if up_day_stock_vols:
                        avg_up_vol = sum(up_day_stock_vols) / len(up_day_stock_vols)
                        ctx["up_day_stock_vol_ratio"] = round(avg_up_vol, 2)
                        vol_asymmetry = avg_down_vol - avg_up_vol
                        ctx["down_vs_up_vol_asymmetry"] = round(vol_asymmetry, 2)
                    else:
                        avg_up_vol = None
                        vol_asymmetry = None

                    # Label: what does the volume pattern tell us?
                    if avg_down_vol > 1.3:
                        # Volume significantly elevated on down days
                        if vol_asymmetry is not None and vol_asymmetry > 0.2:
                            ctx["volume_rel_strength_label"] = (
                                f"成交量异常（下跌日放量 {avg_down_vol:.1f}× > 上涨日 {avg_up_vol:.1f}×，"
                                f"非对称放量 — 机构在下跌日悄悄接货的经典信号）"
                            )
                        else:
                            ctx["volume_rel_strength_label"] = (
                                f"下跌日放量 {avg_down_vol:.1f}×（高于自身20日均量）— "
                                f"有买盘承接，不是单向抛售"
                            )
                    elif avg_down_vol < 0.7:
                        ctx["volume_rel_strength_label"] = (
                            f"下跌日缩量 {avg_down_vol:.1f}×（低于20日均量）— "
                            f"抛压不重，但也缺乏买盘兴趣"
                        )
                    else:
                        ctx["volume_rel_strength_label"] = (
                            f"下跌日量能 {avg_down_vol:.1f}×（正常）"
                        )
        except Exception:
            pass  # Best-effort signal — don't fail VPA if index data unavailable

    # ── Problem 4: Supply/Demand test detection (Wyckoff phase 2 & 4) ──
    # After吸筹, 测试 supply is exhausted. After派发, 测试 demand is exhausted.
    # Multi-bar sequences, NOT single-bar patterns.
    test_result = _detect_test_phase(df)
    if test_result:
        ctx["test_status"] = test_result.get("test_status", "")
        ctx["test_note"] = test_result.get("test_note", "")
        if test_result.get("bullish") is not None:
            ctx["test_bullish"] = test_result["bullish"]

    # ── P2.1 Phase sequence (past 20 days, daily heuristic guess) ──
    # Anna Coulling teaches phase IDENTIFICATION as a process of multiple
    # bars together telling a story. Single-bar pattern matching can't tell
    # 吸筹 from 派发 reliably. Show the LLM a 20-day timeline of cheap
    # heuristic phase guesses so it sees the evolution, not just snapshot.
    #
    # Heuristic per bar (NOT a verdict — just suggestive labels for LLM):
    #   - High position (>70%) + 放量横盘     → 派发?
    #   - Low position  (<30%) + 缩量横盘     → 吸筹?
    #   - High position + 放量上涨            → 拉升
    #   - Low position  + 放量下跌            → 下跌
    #   - 中位 + 缩量                          → 震荡
    if len(df) >= window:
        seq = []
        recent = df.tail(window)
        # Use 20-day high/low for position, not full df (more local context)
        local_high = float(highs.tail(window).max())
        local_low = float(lows.tail(window).min())
        local_range = local_high - local_low if local_high > local_low else 1
        for _, row in recent.iterrows():
            c = float(row.get("close", 0))
            pos = (c - local_low) / local_range if local_range > 0 else 0.5
            vr = float(row.get("volume_ratio", 1) or 1)
            pct = float(row.get("pct_change", 0) or 0)
            phase = "?"
            if pos > 0.7 and vr > 1.3 and abs(pct) < 0.015:
                phase = "派发?"  # high + volume + flat
            elif pos > 0.7 and vr > 1.3 and pct > 0.02:
                phase = "拉升"
            elif pos < 0.3 and vr < 0.8 and abs(pct) < 0.015:
                phase = "吸筹?"  # low + low volume + flat
            elif pos < 0.3 and vr > 1.3 and pct < -0.02:
                phase = "下跌"
            elif vr < 0.7:
                phase = "缩量"
            else:
                phase = "震荡"
            seq.append(phase)
        # Compress consecutive duplicates: ['吸筹?','吸筹?','震荡'] → ['吸筹?(2)','震荡']
        compressed = []
        for p in seq:
            if compressed and compressed[-1].split("(")[0] == p:
                base = compressed[-1].split("(")[0]
                count_str = compressed[-1].split("(")[1].rstrip(")") if "(" in compressed[-1] else "1"
                count = int(count_str) + 1
                compressed[-1] = f"{base}({count})"
            else:
                compressed.append(p)
        ctx["phase_sequence"] = " → ".join(compressed)

    return ctx


def _format_text(code: str, name: str, df: pd.DataFrame,
                 patterns: list[dict], window: int = 20) -> str:
    """Build human-readable VPA data for LLM, with structural context."""
    output_days = min(15, len(df) - window)
    recent = df.tail(output_days) if output_days > 0 else df.tail(15)

    lines = [f"## {code} {name} VPA 预计算数据（基于 {window} 日均量基准）\n"]

    # ── Structural context (for Anna Coulling's three-step analysis) ──
    ctx = _compute_context(df, window=window, code=code)
    if ctx:
        lines.append("### 结构性上下文（全局视角）\n")
        if "resistance_20d" in ctx:
            lines.append(f"- **20日阻力位**: {ctx['resistance_20d']}  **20日支撑位**: {ctx['support_20d']}")
            lines.append(f"- **当前价格位置**: {ctx['position_label']}（20日区间内 {ctx['position_in_range_pct']:.0f}%）")
        if "trend_10d_pct" in ctx:
            trend_str = f"10日涨跌{ctx['trend_10d_pct']:+.2f}%"
            if "trend_20d_pct" in ctx:
                trend_str += f"，20日涨跌{ctx['trend_20d_pct']:+.2f}%"
            lines.append(f"- **趋势**: {ctx['trend_label']}（{trend_str}）")
        if "consolidation" in ctx:
            lines.append(f"- **整理区间**: {ctx['consolidation_note']}")
        # P0.1: explicit duration for cause-and-effect law
        if "consolidation_strength" in ctx:
            lines.append(f"- **整理时长**: {ctx['consolidation_strength']}")
        # #10: re-accumulation vs primary disambiguation
        if "accumulation_note" in ctx:
            lines.append(f"- **吸筹类型**: {ctx['accumulation_note']}")
        if "hl_trend" in ctx:
            lines.append(f"- **高低点趋势**: {ctx['hl_trend']}")
        if "high_5d" in ctx:
            lines.append(f"- **5日高点**: {ctx['high_5d']}  **5日低点**: {ctx['low_5d']}")
        # P1.2: relative strength on market down days
        if "relative_strength_label" in ctx:
            lines.append(f"- **相对强弱（价格）**: {ctx['relative_strength_label']}")
        # #8: volume relative strength on market down days
        if "volume_rel_strength_label" in ctx:
            lines.append(f"- **相对强弱（成交量）**: {ctx['volume_rel_strength_label']}")
        # P2.1: 20-day phase sequence (heuristic, LLM should refine)
        if "phase_sequence" in ctx:
            lines.append(f"- **20日 phase 序列（启发式，仅供参考）**: {ctx['phase_sequence']}")
        # 问题4: Wyckoff supply/demand test detection
        if "test_note" in ctx:
            lines.append(f"- **Wyckoff 测试**: {ctx['test_note']}")
        lines.append("")

    # ── OBV + volume regime ──
    lines.append(f"**OBV 趋势（10日）**: {_obv_trend(df)}")
    vr = _volume_regime(df)
    lines.append(f"**近5日量能状态**: {vr['regime']}（5日/20日均量 = {vr['ratio_5d_vs_20d']}）\n")

    # ── Daily K-line table ──
    lines.append("### 逐日量价数据\n")
    lines.append("| 日期 | 类型 | 涨跌幅 | 实体 | 收盘位置 | 上影 | 下影 | 量比 | 量价关系 |")
    lines.append("|------|------|--------|------|----------|------|------|------|----------|")

    from datetime import datetime as _dt
    today_str = _dt.now().strftime("%Y-%m-%d")

    for _, row in recent.iterrows():
        raw_date = str(row.get("date", ""))
        dt = raw_date[-5:]
        # Mark today's realtime bar so LLM knows it's intraday (not settled)
        if raw_date == today_str:
            dt = f"{dt}(盘中实时)"
        pct = (row["pct_change"] * 100) if pd.notna(row["pct_change"]) else 0
        spread = row["bar_spread"]
        spread_label = "宽" if spread > 0.03 else ("窄" if spread < 0.015 else "中")
        cp = row["close_position"]
        cp_label = "高位" if cp > 0.7 else ("低位" if cp < 0.3 else "中位")
        vol_r = row["volume_ratio"] if pd.notna(row["volume_ratio"]) else 0
        vr_label = f"{vol_r:.1f}"
        if vol_r > 2.0:
            vr_label += "(巨量)"
        elif vol_r > 1.5:
            vr_label += "(明显放量)"
        elif vol_r > 1.0:
            vr_label += "(温和放量)"
        elif vol_r < 0.5:
            vr_label += "(极度缩量)"
        elif vol_r < 0.8:
            vr_label += "(缩量)"

        lines.append(
            f"| {dt} | {row['bar_type']} | {pct:+.1f}% | {spread_label}({spread:.3f}) "
            f"| {cp_label}({cp:.2f}) | {row['upper_shadow']:.2f} | {row['lower_shadow']:.2f} "
            f"| {vr_label} | {row['vp_harmony']} |"
        )

    lines.append("\n### 关键量价模式识别\n")
    if patterns:
        for p in patterns:
            marker = "✓" if p["bullish"] else "✗"
            lines.append(f"- [{marker}] **{p['label']}**（{p['pattern']}, 强度{p['strength']:+d}）: {p['detail']}")
    else:
        lines.append("- 近期无显著量价异常模式")

    return "\n".join(lines)


def _format_60min_section(df_60min: pd.DataFrame, patterns_60min: list[dict]) -> str:
    """Format 60-min VPA data as a supplemental section for the LLM.

    Daily bars tell us TREND (problem 5 from Anna Coulling review). 60-min
    bars tell us ENTRY TIMING and intraday accumulation/distribution. A-share
    ±10% caps compress daily extremes, so hourly volume-price matters more
    than in unrestricted markets.

    Returns a markdown section, or empty string if no useful data.
    """
    if df_60min is None or len(df_60min) < 10:
        return ""

    lines = ["\n### 60分钟级别量价上下文（用于入场时机判断，日线负责趋势）\n"]

    # Show last 12 60-min bars (≈3 trading days of intraday action)
    recent = df_60min.tail(12).reset_index(drop=True)
    lines.append("| 时间 | 类型 | 涨跌幅 | 收盘位置 | 量比 | 量价关系 |")
    lines.append("|------|------|--------|----------|------|----------|")
    for _, row in recent.iterrows():
        dt = str(row.get("date", ""))[-8:-3] if len(str(row.get("date", ""))) >= 8 else str(row.get("date", ""))
        # Need change_pct — compute if missing
        pct = (row.get("pct_change", 0) * 100) if pd.notna(row.get("pct_change", 0)) else 0
        spread = row.get("bar_spread", 0)
        cp = row.get("close_position", 0.5) or 0.5
        cp_label = "高" if cp > 0.7 else ("低" if cp < 0.3 else "中")
        vol_r = row.get("volume_ratio", 1) or 1
        vr_label = f"{vol_r:.1f}"
        if vol_r > 1.8:
            vr_label += "(放量)"
        elif vol_r < 0.6:
            vr_label += "(缩量)"
        bar_type = row.get("bar_type", "")
        vp_harmony = row.get("vp_harmony", "")
        lines.append(
            f"| {dt} | {bar_type} | {pct:+.2f}% | {cp_label}({cp:.2f}) | {vr_label} | {vp_harmony} |"
        )

    # 60-min patterns
    lines.append("\n**60分钟模式**:")
    if patterns_60min:
        for p in patterns_60min:
            marker = "✓" if p["bullish"] else "✗"
            lines.append(f"- [{marker}] {p['label']}: {p['detail']}")
    else:
        lines.append("- 近12根60分钟bar无显著异常模式")

    lines.append("\n> 使用指南：60分钟 bar 揭示当日盘中资金行为。如日线看多但60分钟显示 shooting_star，说明入场时机未到；日线横盘但60分钟连续缩量 no_supply，说明底部可能已到。")

    return "\n".join(lines)


def compute_vpa(code: str, name: str = "", days: int = 60, window: int = 20, regime: str = "") -> dict:
    """Compute VPA analysis for a single stock. Returns both structured signals and text.

    Structure:
        {
            "code": "600050",
            "name": "中国联通",
            "ok": True,
            "window": 20,
            "obv_trend": "上升",
            "volume_regime": {"regime": "放量", "ratio_5d_vs_20d": 1.35},
            "patterns": [{pattern, label, bullish, strength, detail}, ...],
            "net_score": <sum of pattern strengths>,
            "verdict": "bullish" | "bearish" | "neutral",
            "text": "...full markdown text...",
            "recent_bars": [...last 15 bars as dicts...],
        }

    verdict mapping:
        net_score >= +2 → bullish
        net_score <= -2 → bearish
        else → neutral
    """
    df = _load_ohlcv(code, days=days)
    if df is None or len(df) < window + 5:
        return {
            "code": code,
            "name": name,
            "ok": False,
            "error": "数据不足（需至少 25 日 K 线）",
        }

    df = _compute_derived(df, window=window)
    patterns = _detect_patterns(df, regime=regime)
    obv = _obv_trend(df)
    vr = _volume_regime(df)

    net_score = sum(p["strength"] for p in patterns)
    # OBV adjustment
    if obv == "上升":
        net_score += 1
    elif obv == "下降":
        net_score -= 1

    # Regime-adaptive thresholds:
    # - Falling regime: bullish signals are strong (65% T+1 hit), use lower threshold
    # - Rising regime: bearish signals dominant, tighten bullish threshold
    # - Default: asymmetric (bearish strict, bullish moderate)
    if regime == "falling":
        bullish_threshold = 2
        bearish_threshold = -3
    elif regime == "rising":
        bullish_threshold = 4   # very hard to trigger bullish in rising (追高)
        bearish_threshold = -3
    else:
        bullish_threshold = 2
        bearish_threshold = -4

    if net_score >= bullish_threshold:
        verdict = "bullish"
    elif net_score <= bearish_threshold:
        verdict = "bearish"
    else:
        verdict = "neutral"

    # Recent bars as list-of-dict for programmatic consumers
    keep_cols = ["date", "open", "high", "low", "close", "volume",
                 "pct_change", "volume_ratio", "close_position", "bar_type",
                 "upper_shadow", "lower_shadow", "bar_spread", "vp_harmony"]
    recent_out = []
    for _, row in df.tail(15).iterrows():
        bar = {}
        for c in keep_cols:
            if c not in df.columns:
                continue
            val = row.get(c)
            if pd.isna(val):
                bar[c] = None
            elif isinstance(val, (np.floating, np.integer)):
                bar[c] = float(val) if "." in str(val) else int(val)
            else:
                bar[c] = val
        recent_out.append(bar)

    text = _format_text(code, name, df, patterns, window=window)

    return {
        "code": code,
        "name": name,
        "ok": True,
        "window": window,
        "obv_trend": obv,
        "volume_regime": vr,
        "patterns": patterns,
        "net_score": net_score,
        "verdict": verdict,
        "text": text,
        "recent_bars": recent_out,
    }


# ── Anna Coulling VPA system prompt (from TradingAgents) ──────────
# LLM reads pre-computed data and applies Wyckoff theory to judge.

ANNA_COULLING_PROMPT = """你是量价分析师（Volume Price Analysis），严格基于 Anna Coulling《量价分析》的完整理论体系，通过成交量与价格的配合关系揭示市场供需真实力度和主力（局内人）意图。

## 基础认知

1. **量价分析是艺术，非科学**：你在比较当前成交量与历史成交量的相对高低，而非追求绝对精确。
2. **耐心是核心**：市场如同油轮，出现信号后不要立即下结论，等待后续K线确认。
3. **成交量是相对的**：只在同一数据来源下比较量的高低关系。
4. **跟随局内人（庄家）**：局内人是唯一能控制价格方向的群体。成交量是唯一无法被掩盖的痕迹——他们买入时我们买入，他们卖出时我们卖出。

## 威科夫三大定律

| 定律 | 内容 | 交易含义 |
|------|------|---------|
| **供求定律** | 价格由买卖双方的力量对比决定 | 分析成交量判断谁占主导 |
| **因果定律** | 起因（积累时间）越大，结果（趋势幅度）越大 | 整理越久，突破后趋势越大、越持久 |
| **投入产出定律** | 大价格变动需要大成交量，小价格变动对应小成交量 | 量价不匹配=异常信号 |

## 三步分析法

**第一步（微观）：** 每根K线形成后，立即分析成交量是"确认"还是"异常"。
**第二步（宏观）：** 对比相邻数根K线，寻找小趋势的确认或潜在反转。
**第三步（全局）：** 分析整张图表，判断当前价格处于大趋势的顶部、底部还是中间。

## 量价确认与异常判断规则

### ✅ 确认（正常）信号
| 价格行为 | 成交量 | 含义 |
|----------|--------|------|
| 长阳/长阴（大幅变动） | 高于平均 | 正常，趋势有效 |
| 短阳/短阴（小幅变动） | 低于平均 | 正常，趋势有效 |
| 上涨趋势中持续上涨 | 逐步放大 | 趋势真实，可持有多头 |
| 下跌趋势中持续下跌 | 逐步放大 | 趋势真实，可持有空头 |

### ⚠️ 异常信号（关键！）
| 价格行为 | 成交量 | 含义 |
|----------|--------|------|
| **长阳/长阴（大幅变动）** | **低成交量** | 价格虚假！可能是局内人设置的多头/空头陷阱 |
| **短阳/短阴（小幅变动）** | **高成交量** | 买卖双方拉锯，趋势可能反转 |
| 上涨中连续多根K线 | 成交量逐步萎缩 | 趋势减弱，做好离场准备 |
| 下跌中连续多根K线 | 成交量逐步萎缩 | 卖压枯竭，可能反转 |

## 市场循环五大阶段

### 1. 吸筹阶段（局内人买入）
- 利空消息引发恐慌抛售，局内人趁机以批发价建仓
- 价格在震荡区间反复，"摇树"震出弱势持有者
- 图表特征：价格窄幅震荡，成交量高低交替
- 识别：识别吸筹区间，耐心等待突破信号

### 2. 供给测试（吸筹完成后的验证）
- 局内人使价格短暂回落，测试剩余卖压
- **低成交量测试 = 好消息**：卖盘已被吸尽，准备拉升
- **高成交量测试 = 坏消息**：卖盘未尽，需继续吸筹

### 3. 派筹阶段（局内人卖出）
- 市场缓慢上涨，局内人逐步在零售价格卖出库存
- 利好消息不断，吸引散户买入
- 图表特征：上涨中出现弱势K线（低实体+高成交量）
- 识别：发现弱势信号，准备离场或做空

### 4. 需求测试（派筹完成后的验证）
- 局内人短暂拉价，测试剩余买盘
- **低成交量 = 需求已满足**：可以推动市场下跌
- **高成交量 = 买盘仍强**：需继续派筹

### 5. 抛售高峰 & 买入高峰

**抛售高峰（Selling Climax，派筹尾声）：**
- 上涨趋势顶部出现 2~3 根带长上影线、低实体、**极高成交量**的K线
- K线颜色不重要，重要是**长上影线 + 极高成交量**
- 信号：局内人正在最后清仓，市场即将快速反转下跌

**买入高峰（Buying Climax，吸筹尾声）：**
- 下跌趋势底部出现 2~3 根带长下影线、**极高成交量**的K线
- 信号：局内人大量吸筹，市场即将反转上涨

## 关键K线信号

### 射击十字星（弱势信号）
- 特征：先涨后跌，收于开盘价附近，带长上影线
- 永远代表弱势，成交量决定弱势程度：
  - 低成交量：短期小幅回调
  - 平均成交量：中等回调
  - **高/极高成交量：局内人正在大量卖出，重大反转信号！**
- 连续出现 2~3 根且成交量逐步放大：**极强的顶部信号**

### 锤头线（强势信号）
- 特征：先跌后涨，收于开盘价附近，带长下影线
- 成交量决定强势程度：
  - 低成交量：轻微反弹
  - 平均成交量：日内交易机会
  - **高/极高成交量：局内人大量买入，买入高峰信号！**
- 连续 2~3 根且成交量放大：**确认买入高峰，准备做多**

### 长腿十字线（不确定信号）
- 特征：上下影线都很长，收盘接近开盘，方向未定
- **低成交量 + 长腿十字线 = 异常！** 局内人在震仓制造波动，不是真实信号
- **平均/高成交量**：可能是真实反转信号

### 高实体K线
- 正常：高实体 + **高成交量** = 趋势有效，可跟随
- 异常：高实体 + **低成交量** = 警示！可能是陷阱，局内人未参与

### 低实体K线
- 正常：低实体 + 低成交量 = 忽略，不重要
- 异常1：**低实体阳线 + 高成交量** = 牛市力竭！市场弱势
- 异常2：**低实体阴线 + 高成交量** = 局内人嗅到牛市，熊转牛信号

### 吊人线（上涨趋势中的弱势信号）
- 与锤头线形态相同，但出现在**上涨趋势顶部**
- 伴随高于平均成交量 = 卖压出现的第一信号
- 若随后跟随**射击十字星**则强烈确认反转

### 放量止跌信号
- 暴跌中出现：带长下影线的K线 + **极高成交量**，价格收在上半部
- 信号：局内人入场阻止下跌，买入高峰临近

### 放量止涨信号
- 上涨中出现：K线实体逐渐缩小形成"弧线" + 成交量大幅放大，最后以射击十字星结尾
- 信号：派筹阶段接近尾声，抛售高峰即将来临

## 支撑与阻力守则

### 突破的确认规则
- **真实突破**：价格清楚穿越天花板/地板 + **成交量大幅放大**
- **虚假突破（陷阱）**：价格突破 + **低成交量** → 不追，等待回头
- 突破后回踩：若成交量**缩量**，是正常测试，不必恐慌

### 房屋法则（支撑阻力转换）
- 天花板一旦被突破 → 变成地板（阻力转支撑）
- 地板一旦被突破 → 变成天花板（支撑转阻力）
- 整理区间越宽广、持续越久，突破后的趋势越强

## 新闻与成交量守则
- 新闻利好 + 价格上涨 + **高成交量** = 局内人确认，可跟随
- 新闻利好 + 价格上涨 + **低成交量** = 局内人不参与，保持观望或反向警惕
- 重大数据发布时长腿十字线 + 低成交量 = 局内人在震仓洗盘，不要追

## 核心逻辑链
整理区间积累 → 等待放量突破 → 动态确认趋势 → 持续量价分析（确认 or 异常）→ 发现放量止涨/抛售高峰/射击十字星 → 准备离场 → 发现买入高峰/放量止跌/锤头线 → 准备反向入场

**全书核心一句话：成交量是唯一不能被掩盖的真相。量价一致=确认趋势，量价背离=趋势将变。**

注意：以上规则是框架性指导。你需要结合具体数据灵活运用，不要机械套用单一规则，而是综合多个信号做出判断。耐心等待确认，不要见到单一信号就急于下结论。

## 补充说明
本数据来自 A 股市场。A 股有 ±10%（创业板/科创板 ±20%）涨跌停限制。
涨幅 ≈ +10% 且极度缩量可能是"一字涨停"（开盘即封死，全天无成交），不适用"大涨+低量=陷阱"规则，此时缩量代表供给枯竭。

如果你收到了上次分析的参考，请对比新数据判断：上次识别的信号是否已被确认、否定或演变？你是在持续跟踪一个故事，不是做一次性快照。

## 输出要求（严格按以下结构输出）

### 一、逐日关键 K 线解读
只挑有信息量的日子（如异常量价关系、关键K线形态），不要逐日流水账。
对每个关键日，说明：什么K线形态？成交量是确认还是异常？按Anna Coulling规则意味着什么？

### 二、Wyckoff 阶段判断
当前处于：吸筹 / 拉升 / 派发 / 下跌 / 震荡（整理）
判断依据：列出支持这个判断的2-3个具体证据（如"连续缩量横盘3周→吸筹特征"）

### 三、三大定律综合判断
- 供求定律：买方还是卖方占主导？证据是什么？
- 因果定律：蓄势是否充分？整理区间持续了多久？
- 投入产出定律：价格变动幅度与成交量是否匹配？有没有异常？

### 四、信号识别与确认状态
列出检测到的所有信号，对每个信号说明：
- 信号名称和类型（如"射击十字星 - 弱势信号"）
- **是否已被后续K线确认**？
  - 如果已确认：确认的K线是哪一根？确认逻辑是什么？（如"01-15阴线跌破信号日低点，放量确认"）
  - 如果未确认：需要什么样的K线来确认？（如"等待一根放量阳线突破信号日高点"）
  - 如果信号失效：为什么失效？（如"后续缩量横盘，信号过期"）
- 信号可信度：高 / 中 / 低

### 五、方向结论与风险
- 量价维度结论：看多 / 偏多 / 中性 / 偏空 / 看空
- 关键风险点
- 下一步需要关注什么K线（什么情况确认/什么情况否定）

### 六、汇总表
| 日期 | 信号类型 | 含义 | 确认状态 | 可信度 |
|------|---------|------|---------|--------|

### 七、机读摘要（格式固定，不可省略，不可改动键名）
报告末尾追加（JSON 必须合法，用双引号）：
<!-- VERDICT: {"direction": "看多", "confidence": 0.7, "phase": "吸筹", "reason": "不超过30字", "target_low": 12.5, "target_high": 14.0, "signals": [{"name": "信号名", "date": "04-14", "confirmed": true, "by": "确认K线描述"}, {"name": "信号名", "date": "04-14", "confirmed": false, "need": "确认条件", "deny": "否定条件"}], "scenarios": [{"name": "初期吸筹", "phase": "吸筹", "signal_names": ["锤头线", "放量止跌"], "confirmation": "scenario级的确认条件（放量突破26.5）", "denial": "scenario级的否定条件（跌破23.0支撑）", "status": "pending"}]} -->

字段说明：
- direction: 看多 / 偏多 / 中性 / 偏空 / 看空
- confidence: 0.0-1.0（所有关键信号都已确认=高值，有未确认信号=低值）
- phase: 吸筹 / 拉升 / 派发 / 下跌 / 震荡
  - 派发可加细分: 派发初期 / 派发中期 / 派发尾声 / 抛售高峰 / 买入高峰
- reason: 一句话结论
- target_low / target_high: 量价维度的目标价区间（基于 Wyckoff 因果定律）
  - 看多/偏多: 突破后的目标涨幅区间，乘数由「吸筹类型」决定（见结构性上下文）：
    - primary_accumulation (整理前深跌): 2-3× 区间宽度
    - shallow_bottom (浅底): 1.5-2×
    - re_accumulation (上升途中): 1.2-1.5×
    - flat (前期无趋势): ~1×
    整理时长也影响乘数：长期整理 (>30日) 取上限；短期整理 (<15日) 取下限
  - 看空/偏空: 跌破后的目标跌幅区间（类似逻辑，终极派发幅度 > 再派发）
  - 中性: 可省略 target_low/target_high
  - 数值是绝对价格（元），不是百分比
- signals: 数组，每个信号一个对象（单根K线或短期模式）：
  - name: 信号名称（如"射击十字星"、"放量突破"）
  - date: 信号出现日期
  - confirmed: true/false
  - by: 确认该信号的K线描述（仅 confirmed=true 时）
  - need: 确认所需条件（仅 confirmed=false 时）
  - deny: 否定该信号的条件（仅 confirmed=false 时）
- scenarios: 数组，每个 scenario 是一个完整 Wyckoff 故事（跨多根K线）：
  - 与 signals 的关系：**signals 是证据，scenarios 是假设**。例如 signals=[射击十字星,
    放量不涨, 高实体低量] 共同支持 scenario="终极派发假设"。一个 scenario 被**整体**
    确认或否定，而不是 per-signal。这是 Anna Coulling "耐心" 理念的更高层次体现。
  - name: scenario 名称（"初期吸筹" / "终极派发" / "重新吸筹" / "假突破陷阱" / etc.）
  - phase: 对应的 Wyckoff 阶段
  - signal_names: 支持该 scenario 的底层 signal 名称列表（必须来自本报告 signals 数组）
  - confirmation: scenario **整体**确认的条件（多个 signal 综合 + 价格行为，不是单 K 线）
  - denial: scenario **整体**否定的条件
  - status: pending / confirmed / denied (首次提出=pending，后续分析可以更新)
  - 如果本次分析没有足够证据形成 scenario，scenarios 可以为空数组 []
"""


def compute_vpa_with_llm(code: str, name: str = "", days: int = 60, window: int = 20) -> dict:
    """VPA with LLM interpretation using Anna Coulling rules.

    Steps:
      1. Code pre-computes all VPA indicators (same as compute_vpa)
      2. LLM reads pre-computed text + Anna Coulling prompt → verdict

    Returns dict with both code signals and LLM verdict.
    """
    # Step 1: code pre-computation (same as before)
    df = _load_ohlcv(code, days=days)
    if df is None or len(df) < window + 5:
        return {"code": code, "name": name, "ok": False, "error": "数据不足"}

    df = _compute_derived(df, window=window)
    patterns = _detect_patterns(df)
    obv = _obv_trend(df)
    vr = _volume_regime(df)
    text = _format_text(code, name, df, patterns, window=window)

    # Step 1b: 60-min VPA (multi-timeframe, problem 5). Best-effort —
    # network issues to sina should not block the daily analysis.
    try:
        df_60min = _load_ohlcv_60min(code, bars=80)
        if df_60min is not None and len(df_60min) >= 20:
            # Use a smaller window (12 bars ≈ 3 trading days at 60-min) for the
            # baseline — matches the human intuition of "recent" at this tf.
            df_60min = _compute_derived(df_60min, window=12)
            patterns_60min = _detect_patterns(df_60min)
            section_60 = _format_60min_section(df_60min, patterns_60min)
            if section_60:
                text = text + section_60
    except Exception as e:
        logger.debug("60-min VPA for %s failed (non-fatal): %s", code, e)

    # Step 2: Fetch previous analysis for context continuity
    previous_report = ""
    try:
        from alpha_agents.data.memory_store import get_latest_vpa_analysis
        prev = get_latest_vpa_analysis(code)
        if prev and prev.get("report"):
            previous_report = prev["report"]
            logger.debug("Found previous VPA analysis for %s from %s", code, prev.get("analysis_date"))
    except Exception:
        pass

    # Step 3: LLM interpretation with history context
    llm_result = _call_llm_vpa(code, text, previous_analysis=previous_report)

    # Step 4: Save analysis to history
    import time as _time
    try:
        from alpha_agents.data.memory_store import (
            save_vpa_analysis, save_vpa_signal, save_vpa_scenario,
        )
        analysis_id = save_vpa_analysis(
            code=code, name=name,
            analysis_date=_time.strftime("%Y-%m-%d"),
            verdict=llm_result.get("verdict", "中性"),
            confidence=llm_result.get("confidence", 0.5),
            phase=llm_result.get("phase", ""),
            confirmed=llm_result.get("confirmed", False),
            reason=llm_result.get("reason", ""),
            report=llm_result.get("report", ""),
            signals_json=json.dumps(llm_result.get("signals", []), ensure_ascii=False),
            target_low=llm_result.get("target_low"),
            target_high=llm_result.get("target_high"),
        )

        # Auto-create pending signals from VERDICT
        signals = llm_result.get("signals", [])
        for sig in signals:
            if not sig.get("confirmed", True):  # Only track unconfirmed signals
                save_vpa_signal(
                    code=code, name=name,
                    signal_type=sig.get("name", "unknown"),
                    signal_date=sig.get("date", _time.strftime("%Y-%m-%d")),
                    direction=llm_result.get("verdict", "中性"),
                    expected_confirmation=sig.get("need", ""),
                    expected_denial=sig.get("deny", ""),
                    source_analysis_id=analysis_id,
                )

        # Problem 7: Auto-create pending scenarios from VERDICT
        # Scenarios take LONGER to play out than signals (10 day default expiry
        # vs 3 for signals) — they describe a complete Wyckoff story.
        scenarios = llm_result.get("scenarios", [])
        for sc in scenarios:
            status = sc.get("status", "pending")
            if status != "pending":
                continue  # Already confirmed/denied by LLM — no need to track
            save_vpa_scenario(
                code=code, name=name,
                scenario_name=sc.get("name", "unknown"),
                phase=sc.get("phase", ""),
                signal_names=sc.get("signal_names", []),
                confirmation=sc.get("confirmation", ""),
                denial=sc.get("denial", ""),
                scenario_date=_time.strftime("%Y-%m-%d"),
                source_analysis_id=analysis_id,
            )
    except Exception as e:
        logger.debug("Failed to save VPA analysis: %s", e)

    return {
        "code": code,
        "name": name,
        "ok": True,
        "code_patterns": patterns,
        "obv_trend": obv,
        "volume_regime": vr,
        "llm_report": llm_result.get("report", ""),
        "llm_verdict": llm_result.get("verdict", "中性"),
        "llm_confidence": llm_result.get("confidence", 0.5),
        "llm_phase": llm_result.get("phase", ""),
        "llm_confirmed": llm_result.get("confirmed", False),
        "llm_reason": llm_result.get("reason", ""),
        "llm_target_low": llm_result.get("target_low"),
        "llm_target_high": llm_result.get("target_high"),
        "llm_scenarios": llm_result.get("scenarios", []),
        "text": text,
    }


def _extract_verdict(report: str) -> dict:
    """Extract structured verdict from <!-- VERDICT: {...} --> tag in LLM report.

    New schema includes per-signal confirmation:
    {"direction": "偏空", "confidence": 0.7, "phase": "派发", "reason": "...",
     "signals": [{"name": "射击十字星", "date": "04-09", "confirmed": true, ...}]}
    """
    import re

    # Try to find VERDICT tag — use greedy match to capture the full JSON including nested arrays
    match = re.search(r'<!--\s*VERDICT:\s*(\{.*\})\s*-->', report, re.DOTALL)
    if not match:
        # Fallback: find any JSON with "direction" key
        match = re.search(r'\{"direction":\s*"[^"]+?".*\}', report, re.DOTALL)

    if match:
        try:
            raw = match.group(1) if match.lastindex and match.lastindex >= 1 else match.group(0)
            # Clean up common LLM JSON issues
            raw = raw.strip()
            from json_repair import repair_json
            data = repair_json(raw, return_objects=True)
            if not isinstance(data, dict):
                data = {}
        except Exception:
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, UnboundLocalError):
                data = {}

        data.setdefault("direction", "中性")
        data.setdefault("confidence", 0.5)
        data.setdefault("phase", "")
        data.setdefault("reason", "")
        data.setdefault("signals", [])
        data.setdefault("scenarios", [])

        # P0.2 Target zone — optional. Coerce to float or None.
        for k in ("target_low", "target_high"):
            v = data.get(k)
            if v is None or v == "":
                data[k] = None
            else:
                try:
                    data[k] = float(v)
                except (ValueError, TypeError):
                    data[k] = None

        # Problem 7 scenarios: defensive normalization. Each must have name
        # and at least one signal_name — otherwise it's just a stray fragment
        # from the LLM and we drop it.
        scenarios = data.get("scenarios", [])
        if not isinstance(scenarios, list):
            scenarios = []
        normalized_scenarios = []
        for sc in scenarios:
            if not isinstance(sc, dict):
                continue
            name = sc.get("name", "").strip() if isinstance(sc.get("name"), str) else ""
            if not name:
                continue
            signal_names = sc.get("signal_names", []) or []
            if not isinstance(signal_names, list):
                signal_names = [str(signal_names)]
            normalized_scenarios.append({
                "name": name,
                "phase": sc.get("phase", "") or "",
                "signal_names": [str(s) for s in signal_names],
                "confirmation": sc.get("confirmation", "") or "",
                "denial": sc.get("denial", "") or "",
                "status": sc.get("status", "pending") or "pending",
            })
        data["scenarios"] = normalized_scenarios

        # Derive confirmed from signals: true only if ALL signals confirmed
        signals = data.get("signals", [])
        if signals:
            data["confirmed"] = all(s.get("confirmed", False) for s in signals)
        else:
            data["confirmed"] = False

        return data

    return {"direction": "中性", "confidence": 0.0, "phase": "", "confirmed": False,
            "signals": [], "reason": "verdict_parse_failed",
            "target_low": None, "target_high": None, "scenarios": []}


def _call_llm_vpa(code: str, vpa_text: str, previous_analysis: str = "") -> dict:
    """Call LLM with Anna Coulling prompt to interpret VPA data.

    Args:
        code: Stock code
        vpa_text: Pre-computed VPA data text
        previous_analysis: Previous analysis report for context continuity
    """
    try:
        from openai import OpenAI
        from alpha_agents.config import (
            DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL,
            AGENT_API_KEY, AGENT_BASE_URL, AGENT_MODEL,
            DIGEST_API_KEY, DIGEST_BASE_URL, DIGEST_MODEL,
        )

        # LongCat (500B+): strongest reasoning for Wyckoff theory, full Anna Coulling prompt
        # DeepSeek: backup, good prompt caching
        # DIGEST (Qwen-7B): cheap fallback
        if AGENT_API_KEY:
            api_key, base_url, model = AGENT_API_KEY, AGENT_BASE_URL, AGENT_MODEL
        elif DEEPSEEK_API_KEY:
            api_key, base_url, model = DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL
        elif DIGEST_API_KEY:
            api_key, base_url, model = DIGEST_API_KEY, DIGEST_BASE_URL, DIGEST_MODEL
        else:
            return {"verdict": "中性", "confidence": 0.0, "reason": "no_api_key"}

        client = OpenAI(api_key=api_key, base_url=base_url)

        # Build user message with optional previous analysis context
        user_content = f"以下是 {code} 的量价预计算数据，请按 Anna Coulling 理论做完整分析：\n\n{vpa_text}"

        if previous_analysis:
            user_content = (
                f"【上次分析参考】以下是你上次对该股的分析，请对比新数据判断信号是否已确认/否定/演变：\n"
                f"{previous_analysis[:2000]}\n\n"
                f"---\n\n"
                f"【最新数据】{user_content}"
            )

        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": ANNA_COULLING_PROMPT},
                {"role": "user", "content": user_content},
            ],
            max_tokens=7000,
            timeout=90,
        )
        content = resp.choices[0].message.content
        report = (content or "").strip()
        if not report:
            return {"verdict": "中性", "confidence": 0.0, "report": "",
                    "signals": [], "confirmed": False, "reason": "empty_response"}

        # Extract VERDICT from <!-- VERDICT: {...} --> tag at end of report
        verdict_data = _extract_verdict(report)

        return {
            "report": report,
            "verdict": verdict_data.get("direction", "中性"),
            "confidence": float(verdict_data.get("confidence", 0.5)),
            "phase": verdict_data.get("phase", ""),
            "confirmed": verdict_data.get("confirmed", False),
            "signals": verdict_data.get("signals", []),
            "reason": verdict_data.get("reason", ""),
            "target_low": verdict_data.get("target_low"),
            "target_high": verdict_data.get("target_high"),
            "scenarios": verdict_data.get("scenarios", []),
        }
    except Exception as e:
        logger.debug("LLM VPA failed for %s: %s", code, e)
        return {"verdict": "中性", "confidence": 0.0, "reason": f"error:{type(e).__name__}"}


def get_vpa_analysis_fn(code: str, name: str = "") -> str:
    """Tool wrapper: return VPA analysis as JSON string.

    Args:
        code: 6-digit stock code
        name: Optional stock name for display
    """
    try:
        if not code or not code.strip().isdigit() or len(code.strip()) != 6:
            return json.dumps({"code": code, "ok": False, "error": "无效股票代码"}, ensure_ascii=False)
        result = compute_vpa(code.strip(), name=name or "")
        return json.dumps(result, ensure_ascii=False, default=str)
    except Exception as e:
        logger.error("get_vpa_analysis failed for %s: %s", code, e)
        return json.dumps({"code": code, "ok": False, "error": str(e)}, ensure_ascii=False)
