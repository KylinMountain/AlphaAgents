"""OHLCV loading + derived metrics + indicator helpers.

Foundational data layer for the VPA pipeline. Pure data computation —
no LLM, no validator, no narrative formatting.
"""

import logging
from datetime import datetime as _dt
from typing import Optional

import numpy as np
import pandas as pd

from alpha_agents.data.market_history import (
    get_latest_trading_day_at_or_before,
    get_local_history,
)
from alpha_agents.data.market_data import get_stock_history

from alpha_agents.tools.vpa.bars import (  # noqa: F401 — re-exported
    _daily_limit_pct,
    _load_ohlcv,
    _fetch_realtime_bar,
    _append_realtime_with_derived,
    _load_ohlcv_weekly,
    _load_ohlcv_monthly,
    _summarize_higher_timeframe,
    _load_ohlcv_60min,
    _prev_trading_day,
    _compute_derived,
    _compute_5d_vph,
    _obv_trend,
    _volume_regime,
)

logger = logging.getLogger(__name__)




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
            ctx["consolidation_note"] = (
                f"近10日波动率{vol_10d*100:.2f}% < 20日波动率{vol_20d*100:.2f}% × 0.6"
            )
        else:
            ctx["consolidation"] = False
            ctx["consolidation_note"] = (
                f"近10日波动率{vol_10d*100:.2f}% vs 20日波动率{vol_20d*100:.2f}%"
            )

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
    ctx["consolidation_strength"] = (
        f"连续 {consolidation_days} 日 close 在 20日 median ±5% 区间内"
    )

    # Re-accumulation vs primary accumulation disambiguation. Anna's
    # cause-and-effect law is qualitative: deeper prior downtrend → stronger
    # potential rebound after accumulation. We surface ONLY the categorical
    # context (prior trend %, accumulation type) and let the LLM judge target
    # magnitude. Numeric multipliers (2-3×, 1.2-1.5× …) used to live here but
    # had no per-stock backtest validation, so they were removed.
    if consolidation_days >= 7 and median_n > 0:
        look_back_span = 30
        look_back_start = consolidation_days + look_back_span
        look_back_end = consolidation_days
        if len(closes) >= look_back_start:
            prior_start = closes.iloc[-look_back_start]
            prior_end = closes.iloc[-look_back_end]
            prior_trend_pct = (prior_end - prior_start) / prior_start * 100 if prior_start > 0 else 0
            ctx["consolidation_prior_trend_pct"] = round(float(prior_trend_pct), 1)
            if prior_trend_pct < -15:
                ctx["accumulation_type"] = "primary_accumulation"
            elif prior_trend_pct > 15:
                ctx["accumulation_type"] = "re_accumulation"
            elif prior_trend_pct < -5:
                ctx["accumulation_type"] = "shallow_bottom"
            elif prior_trend_pct > 5:
                ctx["accumulation_type"] = "shallow_re_accumulation"
            else:
                ctx["accumulation_type"] = "flat"
            ctx["accumulation_note"] = (
                f"整理前 {look_back_end}~{look_back_start} bar 期间价格变动 "
                f"{prior_trend_pct:+.1f}% (类型: {ctx['accumulation_type']})"
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

                    # Neutral physical descriptor — the LLM judges what the
                    # asymmetry means in the current Wyckoff context.
                    if avg_up_vol is not None:
                        ctx["volume_rel_strength_label"] = (
                            f"下跌日均量比 {avg_down_vol:.2f}, 上涨日均量比 {avg_up_vol:.2f}, "
                            f"差值 {vol_asymmetry:+.2f}"
                        )
                    else:
                        ctx["volume_rel_strength_label"] = f"下跌日均量比 {avg_down_vol:.2f}"
        except Exception as e:
            logger.debug("VPA context: relative-volume label unavailable: %s", e)

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


def _detect_patterns(df: pd.DataFrame) -> list[dict]:
    """Detect VPA patterns from the recent window.

    Each match is a *neutral physical observation* (volume rank, body width
    rank, shadow ratios, close position, range position). Whether the
    observation is bullish/bearish/inconclusive is decided by the LLM
    against the Wyckoff framework — the code does not pre-label.

    Threshold philosophy (Anna Coulling: "qualitative comparison relative
    to recent norms"). Volume / abs-pct-change / body-spread are compared
    to their own rolling-window percentile rank instead of fixed numbers.
    Intra-bar geometry (close_position, upper_shadow, lower_shadow) stays
    as absolute fractions because they are already normalized within the
    bar.
    """
    patterns = []
    if len(df) < 5:
        return patterns

    recent = df.tail(5)
    last = df.iloc[-1]

    def _rank(row: pd.Series, col: str, default: float = 0.5) -> float:
        """Return rolling percentile rank of `col` for this row, with default
        when the rolling window has not yet built up."""
        v = row.get(col, default)
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return default
        return float(v)

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
            "pattern": "price_up_volume_down",
            "label": "价升量减",
            "bullish": None,
            "detail": f"近5日价格+{price_change_pct:.1f}%，末日量/首日量={float(last_vol)/float(first_vol):.2f}",
        })
    if price_down and vol_up:
        patterns.append({
            "pattern": "price_down_volume_up",
            "label": "价跌量增",
            "bullish": None,
            "detail": f"近5日价格{price_change_pct:.1f}%，末日量/首日量={float(last_vol)/float(first_vol):.2f}",
        })
    if price_down and vol_down:
        patterns.append({
            "pattern": "price_down_volume_down",
            "label": "价跌量减",
            "bullish": None,
            "detail": f"近5日价格{price_change_pct:.1f}%，末日量/首日量={float(last_vol)/float(first_vol):.2f}",
        })
    if price_up and vol_up:
        patterns.append({
            "pattern": "price_up_volume_up",
            "label": "价升量增",
            "bullish": None,
            "detail": f"近5日价格+{price_change_pct:.1f}%，末日量/首日量={float(last_vol)/float(first_vol):.2f}",
        })

    # ── Wide down bar w/ high volume closing in upper half (last 3 bars) ──
    for i in range(max(-3, -len(df)), 0):
        row = df.iloc[i]
        vr = row.get("volume_ratio", 0) or 0
        pct = row.get("pct_change", 0) or 0
        cp = row.get("close_position", 0.5) or 0.5
        vr_pct = _rank(row, "vr_pct60")
        ap_pct = _rank(row, "abspct_pct20")

        if vr_pct >= 0.85 and pct < 0 and ap_pct >= 0.85 and cp > 0.5:
            date_str = str(row.get("date", ""))[-5:]
            range_pos = _range_position(float(row["close"]))
            patterns.append({
                "pattern": "wide_down_high_vol_close_up",
                "label": "宽幅下跌巨量收上半",
                "bullish": None,
                "detail": (
                    f"{date_str} 跌{pct*100:.1f}%(20日{ap_pct*100:.0f}分位) "
                    f"量比{vr:.1f}(60日{vr_pct*100:.0f}分位) "
                    f"收盘位置{cp:.2f} 20日区间位置{range_pos*100:.0f}%"
                ),
            })
            break

    # ── Wide up bar w/ high volume, long upper shadow, close in lower half ──
    for i in range(max(-3, -len(df)), 0):
        row = df.iloc[i]
        vr = row.get("volume_ratio", 0) or 0
        pct = row.get("pct_change", 0) or 0
        upper = row.get("upper_shadow", 0) or 0
        cp = row.get("close_position", 0.5) or 0.5
        vr_pct = _rank(row, "vr_pct60")
        ap_pct = _rank(row, "abspct_pct20")

        if vr_pct >= 0.85 and pct > 0 and ap_pct >= 0.85 and upper > 0.4 and cp < 0.5:
            date_str = str(row.get("date", ""))[-5:]
            range_pos = _range_position(float(row["close"]))
            patterns.append({
                "pattern": "wide_up_high_vol_upper_shadow",
                "label": "宽幅上涨巨量长上影",
                "bullish": None,
                "detail": (
                    f"{date_str} 涨{pct*100:.1f}%(20日{ap_pct*100:.0f}分位) "
                    f"量比{vr:.1f}(60日{vr_pct*100:.0f}分位) "
                    f"上影{upper:.2f} 收盘位置{cp:.2f} 20日区间位置{range_pos*100:.0f}%"
                ),
            })
            break

    # ── High volume + narrow body (no-progress on heavy turnover) ──
    for i in range(max(-3, -len(df)), 0):
        row = df.iloc[i]
        vr = row.get("volume_ratio", 0) or 0
        pct = row.get("pct_change", 0) or 0
        spread = row.get("bar_spread", 0) or 0
        vr_pct = _rank(row, "vr_pct60")
        ap_pct = _rank(row, "abspct_pct20")
        sp_pct = _rank(row, "spread_pct20")

        if vr_pct >= 0.80 and ap_pct <= 0.30 and sp_pct <= 0.25:
            date_str = str(row.get("date", ""))[-5:]
            patterns.append({
                "pattern": "high_vol_narrow_body",
                "label": "高量窄实体",
                "bullish": None,
                "detail": (
                    f"{date_str} 量比{vr:.1f}(60日{vr_pct*100:.0f}分位) "
                    f"涨跌{pct*100:+.1f}%(20日{ap_pct*100:.0f}分位) "
                    f"实体{spread:.3f}(20日{sp_pct*100:.0f}分位)"
                ),
            })
            break

    # ── Low volume up bar / down bar (reaction bars) ──
    for i in range(max(-3, -len(df)), 0):
        row = df.iloc[i]
        vr = row.get("volume_ratio", 0) or 0
        pct = row.get("pct_change", 0) or 0
        bar_type = row.get("bar_type", "")
        vr_pct = _rank(row, "vr_pct60")
        ap_pct = _rank(row, "abspct_pct20")

        if vr_pct <= 0.20 and ap_pct >= 0.20 and bar_type == "阳线":
            date_str = str(row.get("date", ""))[-5:]
            patterns.append({
                "pattern": "low_vol_up_bar",
                "label": "缩量阳线",
                "bullish": None,
                "detail": (
                    f"{date_str} 阳线 涨{pct*100:.1f}%(20日{ap_pct*100:.0f}分位) "
                    f"量比{vr:.1f}(60日{vr_pct*100:.0f}分位)"
                ),
            })
            break
        if vr_pct <= 0.20 and ap_pct >= 0.20 and bar_type == "阴线":
            date_str = str(row.get("date", ""))[-5:]
            patterns.append({
                "pattern": "low_vol_down_bar",
                "label": "缩量阴线",
                "bullish": None,
                "detail": (
                    f"{date_str} 阴线 跌{pct*100:.1f}%(20日{ap_pct*100:.0f}分位) "
                    f"量比{vr:.1f}(60日{vr_pct*100:.0f}分位)"
                ),
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
        vr_pct = _rank(row, "vr_pct60")
        sp_pct = _rank(row, "spread_pct20")

        # Long upper shadow + narrow body + close in lower third (volume not in low tail)
        if upper > 0.5 and sp_pct <= 0.40 and cp < 0.3 and vr_pct >= 0.40:
            patterns.append({"pattern": "long_upper_shadow_narrow_body",
                             "label": "长上影窄实体收低位",
                             "bullish": None,
                             "detail": (
                                 f"{date_str} 上影{upper:.2f} 实体{spread:.3f}(20日{sp_pct*100:.0f}分位) "
                                 f"收盘位置{cp:.2f} 量比{vr:.1f}(60日{vr_pct*100:.0f}分位)"
                             )})
            break

        # Long lower shadow + narrow body + close in upper third
        is_hammer_shape = lower > 0.5 and sp_pct <= 0.40 and cp > 0.7 and vr_pct >= 0.40

        if is_hammer_shape and price_down:
            patterns.append({"pattern": "long_lower_shadow_in_downtrend",
                             "label": "下跌中长下影窄实体",
                             "bullish": None,
                             "detail": (
                                 f"{date_str} 下影{lower:.2f} 实体{spread:.3f}(20日{sp_pct*100:.0f}分位) "
                                 f"收盘位置{cp:.2f} 量比{vr:.1f}(60日{vr_pct*100:.0f}分位) "
                                 f"5日趋势-{abs(price_change_pct):.1f}%"
                             )})
            break

        if is_hammer_shape and price_up:
            patterns.append({"pattern": "long_lower_shadow_in_uptrend",
                             "label": "上涨中长下影窄实体",
                             "bullish": None,
                             "detail": (
                                 f"{date_str} 下影{lower:.2f} 实体{spread:.3f}(20日{sp_pct*100:.0f}分位) "
                                 f"收盘位置{cp:.2f} 量比{vr:.1f}(60日{vr_pct*100:.0f}分位) "
                                 f"5日趋势+{price_change_pct:.1f}%"
                             )})
            break

        # Wide body + low volume
        if sp_pct >= 0.85 and vr_pct <= 0.30:
            patterns.append({"pattern": "wide_body_low_vol", "label": f"宽实体缩量{bar_type}",
                             "bullish": None,
                             "detail": (
                                 f"{date_str} {bar_type} 实体{spread:.3f}(20日{sp_pct*100:.0f}分位) "
                                 f"量比{vr:.1f}(60日{vr_pct*100:.0f}分位)"
                             )})
            break

        # Narrow body + high volume
        if sp_pct <= 0.20 and vr_pct >= 0.75:
            patterns.append({"pattern": "narrow_body_high_vol",
                             "label": f"窄实体高量{bar_type}",
                             "bullish": None,
                             "detail": (
                                 f"{date_str} {bar_type} 实体{spread:.3f}(20日{sp_pct*100:.0f}分位) "
                                 f"量比{vr:.1f}(60日{vr_pct*100:.0f}分位)"
                             )})
            break

        # Long-legged doji + low volume
        if upper > 0.3 and lower > 0.3 and sp_pct <= 0.20 and vr_pct <= 0.30:
            patterns.append({"pattern": "long_legged_doji_low_vol",
                             "label": "长腿十字缩量",
                             "bullish": None,
                             "detail": (
                                 f"{date_str} 上影{upper:.2f} 下影{lower:.2f} "
                                 f"实体{spread:.3f}(20日{sp_pct*100:.0f}分位) "
                                 f"量比{vr:.1f}(60日{vr_pct*100:.0f}分位)"
                             )})
            break

    # ── Last-bar interactions with 20-day range ──
    if len(df) >= 20:
        last_row = df.iloc[-1]
        last_vr = last_row.get("volume_ratio", 0) or 0
        last_close = last_row["close"]
        last_date = str(last_row.get("date", ""))[-5:]
        last_vr_pct = _rank(last_row, "vr_pct60")

        recent_20 = df.tail(20)
        resistance = recent_20["high"].iloc[:-1].max()
        support = recent_20["low"].iloc[:-1].min()

        # Compression of recent volatility (close-stddev / mean) — relative
        # ratio, no absolute thresholds. The 0.7 cutoff is a within-stock
        # comparison: 10d coefficient of variation must be at least 30%
        # tighter than 20d to count as consolidation.
        if len(df) >= 10:
            vol_10d = df["close"].tail(10).std() / df["close"].tail(10).mean()
            vol_20d = df["close"].tail(20).std() / df["close"].tail(20).mean()
            is_consolidation = vol_10d < vol_20d * 0.7
        else:
            is_consolidation = False
            vol_10d = vol_20d = 0.0

        # Close above 20-day resistance + high volume + prior consolidation
        if last_close > resistance and last_vr_pct >= 0.75 and is_consolidation:
            patterns.append({"pattern": "resistance_break_high_vol_after_consolidation",
                             "label": "整理后破阻力高量",
                             "bullish": None,
                             "detail": (
                                 f"{last_date} 收{last_close:.2f}>20日阻力{resistance:.2f} "
                                 f"量比{last_vr:.1f}(60日{last_vr_pct*100:.0f}分位) "
                                 f"10日/20日波动率={vol_10d/max(vol_20d,1e-9):.2f}"
                             )})

        # Close above 20-day resistance + low volume
        elif last_close > resistance and last_vr_pct <= 0.25:
            patterns.append({"pattern": "resistance_break_low_vol",
                             "label": "破阻力缩量",
                             "bullish": None,
                             "detail": (
                                 f"{last_date} 收{last_close:.2f}>20日阻力{resistance:.2f} "
                                 f"量比{last_vr:.1f}(60日{last_vr_pct*100:.0f}分位)"
                             )})

        # Close above 20-day resistance + high volume + no prior consolidation
        elif last_close > resistance and last_vr_pct >= 0.75 and not is_consolidation:
            patterns.append({"pattern": "resistance_break_high_vol_no_consolidation",
                             "label": "无整理破阻力高量",
                             "bullish": None,
                             "detail": (
                                 f"{last_date} 收{last_close:.2f}>20日阻力{resistance:.2f} "
                                 f"量比{last_vr:.1f}(60日{last_vr_pct*100:.0f}分位) "
                                 f"10日/20日波动率={vol_10d/max(vol_20d,1e-9):.2f}"
                             )})

        # Touch of support + long lower shadow + high volume + close upper half
        last_lower = last_row.get("lower_shadow", 0) or 0
        last_cp = last_row.get("close_position", 0.5) or 0.5
        if (last_row["low"] <= support * 1.02 and last_lower > 0.4
                and last_vr_pct >= 0.75 and last_cp > 0.5):
            patterns.append({"pattern": "support_test_long_lower_shadow_high_vol",
                             "label": "测试支撑长下影高量",
                             "bullish": None,
                             "detail": (
                                 f"{last_date} 低{float(last_row['low']):.2f}≈支撑{support:.2f} "
                                 f"下影{last_lower:.2f} 量比{last_vr:.1f}(60日{last_vr_pct*100:.0f}分位) "
                                 f"收盘位置{last_cp:.2f}"
                             )})

        # Low-volume up-bar following a recent narrow-body-high-vol or wide-up-with-upper-shadow
        has_recent_topping = any(p["pattern"] in (
            "high_vol_narrow_body",
            "wide_up_high_vol_upper_shadow",
            "topping_progression") for p in patterns)
        if has_recent_topping and last_row.get("bar_type") == "阳线" and last_vr_pct <= 0.30:
            patterns.append({"pattern": "low_vol_up_bar_after_topping_pattern",
                             "label": "高量小实体后缩量阳线",
                             "bullish": None,
                             "detail": (
                                 f"{last_date} 阳线 量比{last_vr:.1f}(60日{last_vr_pct*100:.0f}分位)"
                             )})

    # ── 3-bar progression: body shrinks, volume rises, price up ──
    if len(df) >= 5:
        last3 = df.tail(3)
        spreads = last3["bar_spread"].tolist()
        vrs = [v if not pd.isna(v) else 0 for v in last3["volume_ratio"].tolist()]
        last_vr_pct = _rank(df.iloc[-1], "vr_pct60")
        if (len(spreads) == 3 and spreads[0] > spreads[1] > spreads[2]
                and vrs[0] < vrs[1] < vrs[2] and last_vr_pct >= 0.60 and price_up):
            last_date = str(df.iloc[-1].get("date", ""))[-5:]
            patterns.append({"pattern": "topping_progression",
                             "label": "实体递缩量递增",
                             "bullish": None,
                             "detail": (
                                 f"{last_date} 实体{spreads[0]:.3f}→{spreads[1]:.3f}→{spreads[2]:.3f} "
                                 f"量比{vrs[0]:.1f}→{vrs[1]:.1f}→{vrs[2]:.1f} "
                                 f"末根60日{last_vr_pct*100:.0f}分位"
                             )})

    return patterns
